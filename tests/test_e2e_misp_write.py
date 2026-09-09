"""Live write-path e2e (the AGENTS.md loop): create an event on the dev MISP with an orkl.eu
report as EventReport, run the module, push the result back with PyMISP, verify on the
instance, delete the event.

Every event is created with distribution 0 ("your organisation only") and is never published;
set E2E_KEEP=1 to leave the events on the instance for inspection.
"""

# llm_settings is used as a gate only; pylint: disable=redefined-outer-name,unused-argument

import copy
import os
import time
import warnings
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, load_env
from pymisp import MISPEvent, MISPEventReport, PyMISP

from expansion import ai_connector
from genai import prompts

ORKL_DIR = PROJECT_ROOT / "tests" / "fixtures" / "orkl"
REPORTS = sorted(ORKL_DIR.glob("*.txt"))
AI_TAGS = set(prompts.AI_TAGS)
YOUR_ORG_ONLY = 0
SEED_ATTRIBUTES = (
    ("ip-dst", "203.0.113.42"),
    ("domain", "login-acme-bank.example"),
    ("md5", "d41d8cd98f00b204e9800998ecf8427e"),
)


@pytest.fixture(scope="session")
def misp_write(misp_api) -> PyMISP:  # misp_api: the read-only probe gates on reachability/auth
    env = load_env()
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    return PyMISP(misp_api.base_url, env["MISP_API_KEY"], ssl=misp_api.context.verify_mode != 0)


@pytest.fixture
def created_event(misp_write: PyMISP, request):
    """A fresh unpublished org-only event carrying one orkl report; deleted after the test."""
    report: Path = request.param
    event = MISPEvent()
    event.info = f"ai_connector e2e {report.stem[:8]} {int(time.time())}"
    event.distribution = YOUR_ORG_ONLY
    event.published = False
    event.analysis = 0
    event.threat_level_id = 4
    event.add_event_report(name=report.stem, content=report.read_text(encoding="utf-8"))
    for kind, value in SEED_ATTRIBUTES:  # so the event kind has attributes to tell a story from
        event.add_attribute(kind, value, comment="e2e seed attribute")
    created = misp_write.add_event(event, pythonify=True)
    assert created.distribution == YOUR_ORG_ONLY and not created.published
    try:
        yield created.uuid
    finally:
        if not os.environ.get("E2E_KEEP"):
            misp_write.delete_event(created.uuid)


def _call(misp_write: PyMISP, uuid: str, use_case: str, metadata: dict | None = None) -> dict:
    """The event as MISP serves it, through the module, as MISP's Module::queryAI() would."""
    live = misp_write.get_event(uuid, pythonify=False)
    response = ai_connector.dict_handler(
        {"module": "ai_connector", "use_case": use_case, "data": live}
    )
    assert "error" not in response, response.get("error")
    if metadata is not None:
        metadata.update(response["metadata"])
    return response["results"]


def _stored(misp_write: PyMISP, uuid: str) -> dict:
    return misp_write.get_event(uuid, pythonify=False)["Event"]


def _push_event(misp_write: PyMISP, results: dict) -> None:
    """What a script does with results.Event (MISP itself has no path for infoextraction yet)."""
    processed = MISPEvent()
    processed.load(copy.deepcopy(results["Event"]))  # load() mutates its input
    processed.distribution = YOUR_ORG_ONLY
    processed.published = False
    result = misp_write.update_event(processed, pythonify=False)
    assert not result.get("errors"), result


def _push_tags(misp_write: PyMISP, uuid: str, results: dict) -> None:
    """What Event::aiSummarize / aiAttachTags would do with results.Tag (issue on MISP/MISP)."""
    for tag in results.get("Tag", []):
        misp_write.tag(uuid, tag["name"])


def _tags(element: dict) -> set[str]:
    return {t["name"] for t in element.get("Tag", [])}


@pytest.mark.parametrize("created_event", REPORTS, indirect=True, ids=[p.stem[:8] for p in REPORTS])
def test_extraction_lands_in_misp(misp_write, llm_settings, created_event) -> None:
    results = _call(misp_write, created_event, "infoextraction")
    _push_event(misp_write, results)
    after = _stored(misp_write, created_event)
    attributes = list(after["Attribute"]) + [a for o in after["Object"] for a in o["Attribute"]]
    added = [a for a in attributes if a.get("comment", "").startswith("extracted by ai_connector")]
    assert added, "the module added no attribute to the event"
    for attribute in added:
        assert AI_TAGS <= _tags(attribute), (attribute["type"], attribute["value"])
    sent = {
        (a["type"], str(a["value"])): a
        for a in results["Attribute"]
        + [x for o in results["Object"] for x in o.get("Attribute", [])]
    }
    assert len(sent) == len(added)  # results.Attribute/Object are exactly what was added
    for attribute in added:  # to_ids and dates arrive in MISP exactly as the module set them
        expected = sent[(attribute["type"], str(attribute["value"]))]
        assert attribute["to_ids"] == expected["to_ids"], attribute["value"]
        for field in ("first_seen", "last_seen"):
            assert (attribute.get(field) or "")[:10] == (expected.get(field) or "")[:10], field
    assert not _tags(after) & AI_TAGS  # attributes were suggested, the event stays untagged
    assert after["distribution"] == str(YOUR_ORG_ONLY) and after["published"] is False
    (report,) = [r for r in after["EventReport"] if not r["deleted"]]  # source report untouched
    assert report["content"] == (ORKL_DIR / f"{report['name']}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("created_event", REPORTS[:1], indirect=True, ids=[REPORTS[0].stem[:8]])
def test_report_summary_lands_in_misp(misp_write, llm_settings, created_event) -> None:
    """EventReport::aiSummarize(): one report + event stub in, the revised report written back."""
    live = misp_write.get_event(created_event, pythonify=False)["Event"]
    (report,) = [r for r in live["EventReport"] if not r["deleted"]]
    data = {"EventReport": report, "Event": {k: live[k] for k in ("id", "uuid", "info")}}
    response = ai_connector.dict_handler(
        {"module": "ai_connector", "use_case": "summarization_on_eventReport", "data": data}
    )
    assert "error" not in response, response.get("error")
    revised = response["results"]["EventReport"]
    updated = MISPEventReport()
    updated.from_dict(**report)
    updated.content = revised["content"]
    result = misp_write.update_event_report(updated, pythonify=False)
    assert not result.get("errors"), result
    _push_tags(misp_write, created_event, response["results"])
    after = _stored(misp_write, created_event)
    (stored,) = [r for r in after["EventReport"] if not r["deleted"]]
    original = REPORTS[0].read_text(encoding="utf-8")
    assert stored["name"] == report["name"] and stored["content"].startswith(
        "# AI summary\n## Threat"
    )
    assert stored["content"].endswith(original)  # the analyst's text is kept after the block
    assert AI_TAGS <= _tags(after)
    assert after["distribution"] == str(YOUR_ORG_ONLY) and after["published"] is False


@pytest.mark.parametrize("created_event", REPORTS[1:2], indirect=True, ids=[REPORTS[1].stem[:8]])
def test_event_summary_lands_in_misp(misp_write, llm_settings, created_event) -> None:
    """Event::aiSummarize(): results.EventReport becomes a new report on the event."""
    results = _call(misp_write, created_event, "summarization_on_event")
    new_report = MISPEventReport()
    new_report.from_dict(
        name=results["EventReport"]["name"], content=results["EventReport"]["content"]
    )
    added = misp_write.add_event_report(created_event, new_report, pythonify=False)
    assert not added.get("errors"), added
    _push_tags(misp_write, created_event, results)
    after = _stored(misp_write, created_event)
    reports = {r["name"]: r["content"] for r in after["EventReport"] if not r["deleted"]}
    summaries = [n for n in reports if n == f"AI summary of event {after['uuid']}"]
    assert len(summaries) == 1 and reports[summaries[0]].startswith("## What happened")
    assert reports[REPORTS[1].stem] == REPORTS[1].read_text(encoding="utf-8")  # source untouched
    assert AI_TAGS <= _tags(after)
    seeded = {a["value"] for a in after["Attribute"] if a.get("comment") == "e2e seed attribute"}
    assert seeded == {v for _, v in SEED_ATTRIBUTES}  # nothing added or lost on the event
    assert after["distribution"] == str(YOUR_ORG_ONLY) and after["published"] is False


@pytest.mark.parametrize("created_event", REPORTS[:1], indirect=True, ids=[REPORTS[0].stem[:8]])
def test_tag_suggestion_lands_in_misp(misp_write, suggest_settings, created_event) -> None:
    """Event::aiAttachTags(): every results.Tag name accepted, as the UI would after a click."""
    metadata: dict = {}
    results = _call(misp_write, created_event, "tag_suggest", metadata)
    _push_tags(misp_write, created_event, results)
    after = _stored(misp_write, created_event)
    names = [t["name"] for t in results["Tag"]]
    if metadata["added"]:
        assert names == [s["tag"] for s in metadata["added"]] + list(prompts.AI_TAGS)
        assert set(names) <= _tags(after)
    else:  # the service abstained or only returned tags the event already had
        assert names == [] and (metadata["abstained"] or metadata["skipped_existing"])
        assert not _tags(after) & AI_TAGS
    assert after["distribution"] == str(YOUR_ORG_ONLY) and after["published"] is False
