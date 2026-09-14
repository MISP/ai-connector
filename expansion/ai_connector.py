"""ai_connector: the MISP AI module (module family "ai", MISP feature/ai_tools).

One request shape, the one MISP's Module::queryAI() sends to POST /query:

    {"module": "ai_connector", "use_case": ..., "data": ..., "params": {...}, "timeout": 300}

    use_case   summarization_on_event | summarization_on_eventReport | tag_suggest
               | infoextraction | ping
    data       {"Event": {...}}                                   (event use-cases)
               {"EventReport": {...}, "Event": {...}}             (summarization_on_eventReport)
    params     openai_api_base, api_key, model_id, temperature, request_timeout,
               suggest_limit, suggest_min_score, min_confidence (what MISP sends) plus prompt,
               seed, top_p, max_tokens, think. Unknown keys are an error.

Data flow (docs/DEVELOPER_GUIDE.md §3):

    request -> resolve_settings(params) -> _extract_data -> validate_event (PyMISP)
        -> process_event(event, settings, metadata) -> results per use-case
        -> {"results": {"EventReport"|"Tag"|"Attribute"|"Object"|ping}, "metadata"}
        or {"error": "..."}
"""

import copy
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pymisp import MISPEvent, PyMISPError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # misp-modules imports this file by path, not as a package
    sys.path.insert(0, str(REPO_ROOT))

from genai import (  # noqa: E402  # pylint: disable=wrong-import-position
    extract,
    llm,
    prompts,
    suggest,
    summarize,
)

misperrors = {"error": "Error"}
mispattributes = {
    "input": [],
    "output": ["EventReport", "Tag", "Attribute", "Object"],
    "format": "misp_standard",
}
moduleinfo = {
    "version": "0.6",
    "author": "Aaron Kaplan / ctitools",
    "description": "MISP AI connector: summaries, tag suggestions and CTI extraction for an event.",
    "module-type": ["ai"],
    "name": "ai_connector",
    "logo": "",
    "requirements": ["pymisp"],
    "features": (
        "Summarises an event or one EventReport, suggests taxonomy tags through the "
        "misp-tag-suggest service, or extracts high-confidence indicators from the EventReports. "
        "Prompts and sampling parameters come from the generic-ai-prompts galaxy; every element "
        "the AI produced carries the two ai-computer-assisted tags (returned as results.Tag)."
    ),
    "references": ["https://github.com/MISP/MISP/blob/feature/ai_tools/docs/dev/ai_integration.md"],
    "input": 'use_case + data ({"Event": ...} or {"EventReport": ..., "Event": ...}) + params',
    "output": "results.EventReport, results.Tag, results.Attribute / results.Object",
}

USE_CASES = (
    "summarization_on_event",
    "summarization_on_eventReport",
    "tag_suggest",
    "infoextraction",
    "ping",
)
# params key -> default. Precedence: params > .env > default. None = "not set, use the cluster".
PARAMS: dict[str, Any] = {
    "openai_api_base": "",  # .env OPENAI_BASE_URL
    "api_key": "",  # .env OPENAI_API_KEY
    "model_id": "",  # .env OPENAI_MODEL
    "temperature": None,
    "seed": None,
    "top_p": None,
    "max_tokens": None,
    "think": None,
    "prompt": "",  # galaxy cluster uuid or value, or inline prompt text
    "request_timeout": llm.DEFAULT_TIMEOUT,
    "min_confidence": 0.9,
    "suggest_limit": 5,
    "suggest_min_score": 0.0,
}
TYPES = {
    "temperature": float,
    "seed": int,
    "top_p": float,
    "max_tokens": int,
    "think": bool,
    "request_timeout": int,
    "min_confidence": float,
    "suggest_limit": int,
    "suggest_min_score": float,
}
ENV_KEYS = {
    "openai_api_base": "OPENAI_BASE_URL",
    "api_key": "OPENAI_API_KEY",
    "model_id": "OPENAI_MODEL",
}
SAMPLING = ("temperature", "seed", "top_p", "max_tokens", "think")
AI_SUMMARY_HEADING = "# AI summary"  # MISP's EventReport::AI_SUMMARY_HEADING / _DELINEATOR
AI_SUMMARY_DELINEATOR = "=================="

# process_event(..., e2etest=True) writes the processed event here as <uuid>.json
E2E_DIR = REPO_ROOT / "tests" / "e2etests"
LOG = logging.getLogger("misp-modules.ai_connector")  # DEBUG when misp-modules runs with -d


def _coerce(key: str, value: Any) -> Any:
    kind = TYPES.get(key)
    if kind is bool and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    try:
        return kind(value) if kind else value
    except (TypeError, ValueError) as error:
        raise ValueError(f"params.{key} must be a {kind.__name__}, got {value!r}") from error


def resolve_settings(params: dict[str, Any] | None) -> dict[str, Any]:
    """params > .env (OPENAI_* for the endpoint, AI_CONNECTOR_<KEY> for the rest) > default."""
    params = params or {}
    if not isinstance(params, dict):
        raise ValueError('"params" must be an object')
    if unknown := sorted(set(params) - set(PARAMS)):
        raise ValueError(f"unknown params {unknown}, expected a subset of {sorted(PARAMS)}")
    env = llm.env()
    settings = {}
    for key, default in PARAMS.items():
        env_value = env.get(ENV_KEYS.get(key, f"AI_CONNECTOR_{key.upper()}"))
        value = next((v for v in (params.get(key), env_value) if v not in (None, "")), default)
        settings[key] = value if value is None else _coerce(key, value)
    settings["sampling"] = {k: settings[k] for k in SAMPLING if settings[k] is not None}
    return settings


def _llm_settings(settings: dict[str, Any]) -> llm.LLMSettings:
    return llm.LLMSettings(
        base_url=(settings["openai_api_base"] or llm.DEFAULT_BASE_URL).rstrip("/"),
        api_key=settings["api_key"],
        model=settings["model_id"],
        timeout=settings["request_timeout"],
    )


def _suggest_status() -> dict[str, Any]:
    """Part of "ping": the misp-tag-suggest service (URL from .env only), best effort."""
    try:
        settings = suggest.SuggestSettings.from_env(10)
    except suggest.SuggestError as error:
        return {"url": "", "reachable": False, "error": str(error)}
    return {"url": settings.base_url, "reachable": suggest.is_reachable(settings)}


def _extract_data(request: dict[str, Any], use_case: str) -> dict[str, Any]:
    """The raw event dict PyMISP will validate. For summarization_on_eventReport the one report
    (plus the event stub MISP sends for context) becomes an event holding only that report."""
    data = request.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError(
            '"data" must hold {"Event": {...}} or {"EventReport": {...}, "Event": {...}}'
        )
    if use_case != "summarization_on_eventReport":
        return data["Event"] if isinstance(data.get("Event"), dict) else data
    report = data["EventReport"] if isinstance(data.get("EventReport"), dict) else data
    if not report.get("content") or not report.get("name"):
        raise ValueError(
            'summarization_on_eventReport needs one EventReport with "name" and "content"'
        )
    stub = data.get("Event") if isinstance(data.get("Event"), dict) else {}
    event = {k: stub[k] for k in ("uuid", "info", "date", "distribution") if stub.get(k)}
    event.setdefault("info", report["name"])
    return event | {"EventReport": [report]}


def _normalise_for_pymisp(data: dict[str, Any]) -> dict[str, Any]:
    """Work around a PyMISP-vs-MISP mismatch.

    MISP's /events/view output sets "distribution" / "sharing_group_id" on default galaxy
    clusters, but PyMISP (MISPGalaxyCluster.from_dict) refuses default clusters carrying them.
    """
    data = copy.deepcopy(data)  # PyMISP also pops keys from nested dicts while loading

    def strip(node: Any) -> None:  # galaxies sit on the event, its attributes and its objects
        if isinstance(node, list):
            for item in node:
                strip(item)
        elif isinstance(node, dict):
            for cluster in node.get("GalaxyCluster", []):
                if cluster.get("default"):
                    cluster.pop("distribution", None)
                    cluster.pop("sharing_group_id", None)
            for value in node.values():
                strip(value)

    strip(data)
    return data


def validate_event(data: dict[str, Any]) -> MISPEvent:
    """Validate the raw event dict with PyMISP; raises PyMISPError on invalid input."""
    if not data.get("info"):
        raise PyMISPError('"info" is required.')
    # force_timestamps: PyMISP otherwise drops "timestamp" from events it considers edited
    event = MISPEvent(force_timestamps=True)
    event.load(_normalise_for_pymisp(data))
    return event


def get_event_report(event: MISPEvent) -> str:
    """Markdown of all non-deleted EventReports of the event, joined; "" if there are none."""
    contents = [
        report.content
        for report in event.event_reports
        if report.content and not getattr(report, "deleted", False)
    ]
    return "\n\n".join(contents)


def _run_use_case(event: MISPEvent, settings: dict[str, Any]) -> dict[str, Any]:
    use_case = settings["use_case"]
    if use_case == "tag_suggest":  # no LLM: the misp-tag-suggest service
        suggest_settings = suggest.SuggestSettings.from_env(settings["request_timeout"])
        result = suggest.suggest_tags(
            event, suggest_settings, settings["suggest_limit"], settings["suggest_min_score"]
        )
        result["model"] = {"name": result["model_version"], "server": suggest_settings.base_url}
        return result
    llm_settings = _llm_settings(settings)
    report = get_event_report(event)
    sampling = settings.get("sampling") or {}  # per-request override of the cluster's values
    if use_case == "infoextraction":
        prompt = prompts.resolve_prompt("cti-info-extraction", settings["prompt"])
        prompt = dataclasses.replace(prompt, params={**prompt.params, **sampling})
        result = extract.extract_iocs(
            event, report, llm_settings, prompt, settings["min_confidence"]
        )
    else:
        kind = "event" if use_case == "summarization_on_event" else "report"
        prompt = prompts.resolve_prompt(f"summary-{kind}", settings["prompt"])
        prompt = dataclasses.replace(prompt, params={**prompt.params, **sampling})
        result = summarize.summarize(event, report, kind, llm_settings, prompt)
    result["model"] = llm.model_info(llm_settings)
    if sampling:
        result["sampling"] = sampling
    return result


def process_event(
    event: MISPEvent,
    e2etest: bool = False,
    settings: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> MISPEvent:
    """Run the use-case in settings["use_case"] on the event ("none" or absent: pass-through).

    `settings` are resolve_settings() values; `metadata`, if given, is filled with what the
    use-case did. With e2etest=True the processed event is also written to E2E_DIR/<uuid>.json
    for the round-trip quality gate.
    """
    settings = {**resolve_settings({}), "use_case": "none", **(settings or {})}
    if settings["use_case"] not in (*USE_CASES, "none") or settings["use_case"] == "ping":
        raise ValueError(f"unknown use_case {settings['use_case']!r}, expected one of {USE_CASES}")
    if settings["use_case"] != "none":
        result = _run_use_case(event, settings)
        if metadata is not None:
            metadata.update(result)
    if e2etest:
        E2E_DIR.mkdir(parents=True, exist_ok=True)
        (E2E_DIR / f"{event.uuid}.json").write_text(
            json.dumps(_to_dict(event), indent=2), encoding="utf-8"
        )
    return event


def _to_dict(event: MISPEvent) -> dict[str, Any]:
    return {"Event": json.loads(event.to_json())}


def ai_summary_block(summary: str, original: str) -> str:
    """The full revised report MISP expects: heading, summary, blank line, delineator, report."""
    return f"{AI_SUMMARY_HEADING}\n{summary.strip()}\n\n{AI_SUMMARY_DELINEATOR}\n\n{original}"


def _results(use_case: str, event: MISPEvent, result: dict, before: set[str]) -> dict[str, Any]:
    """The results block per use-case: only what was produced, in the shape MISP reads."""
    ai_tags = [{"name": name} for name in prompts.AI_TAGS]
    if use_case == "summarization_on_event":
        report = event.event_reports[-1]
        return {"EventReport": {"name": report.name, "content": report.content}, "Tag": ai_tags}
    if use_case == "summarization_on_eventReport":
        original, summary = event.event_reports[0], event.event_reports[-1]
        content = ai_summary_block(summary.content, original.content)
        return {"EventReport": {"name": original.name, "content": content}, "Tag": ai_tags}
    if use_case == "tag_suggest":
        suggested = [{"name": s["tag"], "score": s["score"]} for s in result["added"]]
        return {"Tag": suggested + (ai_tags if suggested else [])}
    added = [i for i in (*event.attributes, *event.objects) if i.uuid not in before]
    if LOG.isEnabledFor(logging.DEBUG):  # MISP ignores the processed event; log it in -d mode
        LOG.debug("infoextraction processed event %s: %s", event.uuid, event.to_json())
    return {
        "Attribute": [json.loads(a.to_json()) for a in added if a in event.attributes],
        "Object": [json.loads(o.to_json()) for o in added if o in event.objects],
    }


def dict_handler(request: dict[str, Any]) -> dict[str, Any]:
    try:
        use_case = request.get("use_case")
        if use_case not in USE_CASES:
            raise ValueError(f"unknown use_case {use_case!r}, expected one of {USE_CASES}")
        settings = resolve_settings(request.get("params")) | {"use_case": use_case}
        if use_case == "ping":
            return {
                "results": llm.ping(_llm_settings(settings)) | {"tag_suggest": _suggest_status()}
            }
        data = _extract_data(request, use_case)
    except ValueError as error:
        return {"error": f"Invalid request: {error}"}
    except (llm.LLMError, suggest.SuggestError) as error:
        return {"error": str(error)}
    try:
        event = validate_event(data)
    except (ValueError, TypeError, KeyError, PyMISPError) as error:
        # PyMISP raises TypeError/KeyError (not PyMISPError) for some missing fields
        return {"error": f"Invalid MISP Event: {error}"}

    before = {a.uuid for a in event.attributes} | {o.uuid for o in event.objects}
    metadata: dict[str, Any] = {}
    try:
        process_event(event, settings=settings, metadata=metadata)
    except (ValueError, llm.LLMError, suggest.SuggestError, PyMISPError) as error:
        # PyMISPError: an accepted candidate PyMISP still refuses (e.g. an unparsable datetime)
        return {"error": str(error)}
    return {"results": _results(use_case, event, metadata, before), "metadata": metadata}


def handler(q: str | bool = False) -> dict[str, Any] | bool:  # pylint: disable=invalid-name
    if q is False:
        return False
    try:
        request = json.loads(q)
    except json.JSONDecodeError as error:
        return {"error": f"Invalid JSON request: {error}"}
    return dict_handler(request)


def introspection() -> dict[str, Any]:
    return mispattributes


def version() -> dict[str, Any]:
    return {**moduleinfo, "config": list(PARAMS)}
