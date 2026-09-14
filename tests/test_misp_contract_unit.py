"""Offline quality gate: the envelopes MISP (feature/ai_tools) builds, replayed against the
module, and the answers checked exactly the way MISP's models read them.

Sources: app/Model/Module.php (buildAiRequest / buildAiParams / AI_PARAM_SETTINGS),
app/Model/Event.php (aiSummarize, aiRecommendTags), app/Model/EventReport.php
(AI_SUMMARY_BLOCK_REGEX, stripAiSummary, mergeAiSummary), tests/ai_fake_module_server.py.
The LLM and the tag-suggest service are faked; nothing here needs the network.
"""

# pylint: disable=redefined-outer-name,unused-argument

import json
import logging
import re

import pytest

from expansion import ai_connector
from genai import llm, prompts, suggest

AI_TAGS = set(prompts.AI_TAGS)
# What MISP sends as `params` (Module::AI_PARAM_SETTINGS, unset settings omitted, 0 sent).
MISP_PARAMS = {
    "openai_api_base": "http://ollama.example:11434/v1",
    "api_key": "misp-configured-key",
    "model_id": "gemma4:12b",
    "temperature": 0,
    "request_timeout": 120,
    "suggest_limit": 5,
    "suggest_min_score": 0,
}
# EventReport::AI_SUMMARY_BLOCK_REGEX, ported from PCRE (\R -> \r?\n, \z -> \Z, named group).
AI_SUMMARY_BLOCK = re.compile(
    r"\A\s*#[ \t]*AI summary[ \t]*(?:\r?\n|\Z)(?:(?P<body>.*?)\r?\n)?"
    r"[ \t]*={10,}[ \t]*(?:\r?\n|\Z)(?:\r?\n)*",
    re.S | re.I,
)
SUMMARY = (
    "## Threat\nPhishing.\n## Targets\nCustomers.\n## Indicators\n203.0.113.42\n"
    "## Recommended actions\nBlock."
)
EVENT_SUMMARY = (
    "## What happened\nPhishing.\n## Key indicators\n203.0.113.42\n"
    "## Context and attribution\nLow.\n## Related events\nnone"
)


def envelope(use_case: str, data: dict, params: dict | None = None) -> dict:
    """Module::buildAiRequest(): the exact envelope MISP posts to /query."""
    return {
        "module": "ai_connector",
        "data": data,
        "use_case": use_case,
        "params": MISP_PARAMS if params is None else params,
        "timeout": 300,
    }


@pytest.fixture
def fake_llm(monkeypatch):
    calls: list[dict] = []

    def install(answer: str):
        def fake(messages, params, settings, json_mode=False):
            calls.append({"messages": messages, "params": params, "settings": settings})
            return answer

        monkeypatch.setattr(llm, "llm_chat", fake)
        monkeypatch.setattr(llm, "model_info", lambda s: {"name": s.model})
        return calls

    return install


@pytest.fixture
def fake_suggest(monkeypatch):
    calls: list[dict] = []

    def install(suggestions: list[tuple[str, float]], abstained: bool = False):
        def fake(url, payload=None, *, headers=None, timeout=0):
            calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
            return {
                "suggestions": [{"tag": t, "score": s} for t, s in suggestions],
                "abstained": abstained,
                "model_version": "m@1",
                "dataset_manifest_sha256": "x",
            }

        monkeypatch.setenv("MISP_TAG_SUGGEST_URL", "http://suggest.example:8000")
        monkeypatch.setattr(llm, "http_json", fake)
        return calls

    return install


def _report_data(dummy_event: dict) -> dict:
    """EventReport::aiSummarize(): the report (summary block stripped) plus its event."""
    event = dummy_event["Event"]
    report = event["EventReport"][0]
    return {
        "EventReport": report,
        "Event": {k: event[k] for k in ("id", "uuid", "info") if k in event},
    }


def test_module_is_listed_as_the_ai_family_expects() -> None:
    meta = ai_connector.version()
    assert meta["name"] == "ai_connector" and meta["module-type"] == ["ai"]
    assert set(MISP_PARAMS) <= set(ai_connector.PARAMS)  # every MISP setting is a known param


def test_summarization_on_event_answers_a_new_report(dummy_event, fake_llm) -> None:
    calls = fake_llm(EVENT_SUMMARY)
    answer = ai_connector.dict_handler(envelope("summarization_on_event", dummy_event))
    assert "error" not in answer, answer
    report = answer["results"]["EventReport"]  # Event::aiSummarize reads name + content
    assert isinstance(report["content"], str) and report["content"].strip()
    assert 0 < len(report["name"]) <= 255
    assert report["content"] == EVENT_SUMMARY
    assert {t["name"] for t in answer["results"]["Tag"]} == AI_TAGS
    assert set(answer["results"]) == {"EventReport", "Tag"}  # only what was produced
    settings = calls[0]["settings"]  # params override .env (invariant 6, MISP edition)
    assert settings.base_url == MISP_PARAMS["openai_api_base"]
    assert settings.api_key == MISP_PARAMS["api_key"]
    assert settings.model == MISP_PARAMS["model_id"]
    assert settings.timeout == MISP_PARAMS["request_timeout"]
    assert calls[0]["params"]["temperature"] == 0


def test_summarization_on_event_report_answers_the_full_revised_report(
    dummy_event, fake_llm
) -> None:
    fake_llm(SUMMARY)
    data = _report_data(dummy_event)
    answer = ai_connector.dict_handler(envelope("summarization_on_eventReport", data))
    assert "error" not in answer, answer
    report = answer["results"]["EventReport"]
    assert report["name"] == data["EventReport"]["name"]
    match = AI_SUMMARY_BLOCK.match(report["content"])  # EventReport::mergeAiSummary keys on this
    assert match, report["content"][:120]
    assert match.group("body").strip() == SUMMARY
    assert report["content"][match.end() :] == data["EventReport"]["content"]  # analyst text kept
    assert {t["name"] for t in answer["results"]["Tag"]} == AI_TAGS


def test_summarization_on_event_report_accepts_a_bare_report(dummy_event, fake_llm) -> None:
    fake_llm(SUMMARY)
    report = dummy_event["Event"]["EventReport"][0]
    answer = ai_connector.dict_handler(envelope("summarization_on_eventReport", report))
    assert answer["results"]["EventReport"]["content"].endswith(report["content"])


def test_summary_may_not_contain_the_delineator(dummy_event, fake_llm) -> None:
    fake_llm(SUMMARY + "\n\n====================\n")
    answer = ai_connector.dict_handler(envelope("summarization_on_event", dummy_event))
    assert "equals signs" in answer["error"]


def test_tag_suggest_answers_names_with_the_ai_tags(dummy_event, fake_suggest) -> None:
    calls = fake_suggest([('rsit:fraud="phishing"', 0.9), ("tlp:amber", 0.6), ("x:y", 0.2)])
    params = {**MISP_PARAMS, "suggest_limit": 3, "suggest_min_score": 0.5}
    answer = ai_connector.dict_handler(envelope("tag_suggest", dummy_event, params))
    assert "error" not in answer, answer
    names = [t["name"] for t in answer["results"]["Tag"]]  # Event::aiRecommendTags reads names
    assert names == ['rsit:fraud="phishing"', *prompts.AI_TAGS]  # tlp:amber already on the event
    assert all(isinstance(n, str) for n in names)
    assert calls[0]["payload"]["limit"] == 3
    assert answer["metadata"]["below_min_score"] == ["x:y"]


def test_tag_suggest_abstention_is_an_empty_list(dummy_event, fake_suggest) -> None:
    fake_suggest([], abstained=True)
    answer = ai_connector.dict_handler(envelope("tag_suggest", dummy_event))
    assert answer["results"] == {"Tag": []} and answer["metadata"]["abstained"] is True


def test_infoextraction_answers_the_added_elements(dummy_event, fake_llm, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="misp-modules.ai_connector")  # misp-modules -d
    fake_llm(
        json.dumps(
            {
                "indicators": [
                    {
                        "type": "domain",
                        "value": "acme-bank-secure.example",
                        "quote": "alerts@acme-bank-secure.example",
                        "confidence": 0.99,
                    }
                ]
            }
        )
    )
    answer = ai_connector.dict_handler(envelope("infoextraction", dummy_event))
    assert "error" not in answer, answer
    (attribute,) = answer["results"]["Attribute"]
    assert attribute["value"] == "acme-bank-secure.example"
    assert {t["name"] for t in attribute["Tag"]} == AI_TAGS
    assert answer["results"]["Object"] == []
    assert set(answer["results"]) == {
        "Attribute",
        "Object",
    }  # what Event::aiExtractIndicators reads
    report_uuid = dummy_event["Event"]["EventReport"][0]["uuid"]  # Event::aiExtractedReportUuids
    assert attribute["comment"] == f"extracted by ai_connector from EventReport {report_uuid}"
    (logged,) = [r for r in caplog.records if "processed event" in r.message]  # only in -d mode
    assert dummy_event["Event"]["uuid"] in logged.message


def test_ping_answers_endpoint_and_model(monkeypatch) -> None:
    def fake(url, payload=None, *, headers=None, timeout=0):
        assert headers == {"Authorization": "Bearer misp-configured-key"}
        if url.endswith("/models"):
            return {"data": [{"id": "gemma4:12b"}, {"id": "other"}]}
        raise llm.LLMError("not ollama")

    monkeypatch.setattr(llm, "http_json", fake)
    monkeypatch.setenv("MISP_TAG_SUGGEST_URL", "http://suggest.example:8000")  # CI has no .env
    monkeypatch.setattr(suggest, "is_reachable", lambda s: True)
    answer = ai_connector.dict_handler(
        {"module": "ai_connector", "use_case": "ping", "params": MISP_PARAMS}
    )
    results = answer["results"]
    assert results["ok"] is True and results["model"]["name"] == "gemma4:12b"
    assert results["endpoint"] == MISP_PARAMS["openai_api_base"] and results["models_listed"] == 2
    assert isinstance(results["latency_ms"], int) and results["tag_suggest"]["reachable"] is True
    bad = ai_connector.dict_handler(
        {"use_case": "ping", "params": {**MISP_PARAMS, "model_id": "nope"}}
    )
    assert "not served" in bad["error"]


@pytest.mark.parametrize(
    "body, fragment",
    [
        (envelope("translate", {"Event": {"info": "x"}}), "unknown use_case"),
        (envelope("tag_suggest", {"Event": {"info": "x"}}, {"nope": 1}), "unknown params"),
        (
            envelope("tag_suggest", {"Event": {"info": "x"}}, {"suggest_limit": "many"}),
            "suggest_limit",
        ),
        (envelope("tag_suggest", []), '"data" must hold'),
        (envelope("summarization_on_eventReport", {"Event": {"info": "x"}}), "one EventReport"),
        ({"module": "ai_connector", "use_case": "infoextraction"}, '"data" must hold'),
    ],
)
def test_contract_violations_are_named(body, fragment) -> None:
    answer = ai_connector.dict_handler(body)
    assert answer["error"].startswith("Invalid request: ") and fragment in answer["error"]


def test_errors_are_the_one_key_misp_reads(dummy_event, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise llm.LLMError("LLM endpoint unreachable or timed out (120s)")

    monkeypatch.setattr(llm, "llm_chat", boom)
    answer = ai_connector.dict_handler(envelope("summarization_on_event", dummy_event))
    assert answer == {"error": "LLM endpoint unreachable or timed out (120s)"}
