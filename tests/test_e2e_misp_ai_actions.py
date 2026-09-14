"""Live quality gate through MISP's own AI actions (MISP feature/ai_tools, 7de7581d or later).

The dev MISP in .env must run feature/ai_tools with Plugin.AI_services_enable on and
Plugin.AI_services_url/port pointing at a running ai_connector (docs/OPERATOR_GUIDE.md 2.2);
the test checks that first and skips otherwise. It then drives the actions exactly as MISP's
UI does — POST /servers/aiDryRun (ping), POST /events/aiSummarize/<id>,
POST /eventReports/aiSummarize/<id>, GET + POST /events/aiRecommendTags/<id>,
POST /events/aiExtractIndicators/<id> — on a fresh org-only event carrying one orkl report,
and verifies what landed on the instance: the content, the ai-computer-assisted provenance
tags MISP attaches from results.Tag, the source report untouched. The events are deleted
afterwards (E2E_KEEP=1 keeps them).
"""

# pylint: disable=redefined-outer-name,unused-argument

import json
import time
import urllib.error
import urllib.request

import pytest
from conftest import live_gate
from test_e2e_misp_write import (  # noqa: F401  # pylint: disable=unused-import
    REPORTS,
    YOUR_ORG_ONLY,
    created_event,
    misp_write,
)

from expansion import ai_connector
from genai import extract, prompts

AI_TAGS = set(prompts.AI_TAGS)
JOB_TIMEOUT = 600


def _http(misp_api, path: str, body: dict | None = None) -> tuple[int, dict]:
    """(status, json) of a MISP call; MISP answers a failed action with HTTP 403 and a JSON
    body, which conftest.MispApi would read as a rejected key."""
    req = urllib.request.Request(
        f"{misp_api.base_url}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=misp_api.headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=330, context=misp_api.context) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


@pytest.fixture(scope="module")
def misp_ai(misp_api, request):
    """The read-only client, gated on the instance having the AI family switched on."""
    status, setting = _http(misp_api, "/servers/getSetting/Plugin.AI_services_enable")
    if status != 200 or not setting.get("value"):
        reason = f"Plugin.AI_services_enable is not on at {misp_api.base_url} (HTTP {status})"
        live_gate(request.config, "misp-ai", reason)
        pytest.skip(reason)
    return misp_api


def _run(misp_ai, path: str, done) -> dict:
    """POST an AI action, then wait until `done()` is true (MISP queues a background job)."""
    status, answer = _http(misp_ai, path, {})
    assert status == 200 and answer.get("saved"), (status, answer)
    deadline = time.time() + JOB_TIMEOUT
    while time.time() < deadline:
        if done():
            return answer
        time.sleep(3)
    raise AssertionError(f"{path}: no result within {JOB_TIMEOUT}s ({answer})")


def _event(misp_write, uuid: str) -> dict:
    return misp_write.get_event(uuid, pythonify=False)["Event"]


def _tags(element: dict) -> set[str]:
    return {t["name"] for t in element.get("Tag", [])}


def _extracted(event: dict) -> list[dict]:
    attributes = list(event["Attribute"]) + [a for o in event["Object"] for a in o["Attribute"]]
    return [a for a in attributes if a.get("comment", "").startswith(extract.COMMENT)]


def test_ping_through_misp(misp_ai) -> None:
    """The Test LLM button: ServersController::aiDryRun with use_case ping, no event."""
    status, answer = _http(misp_ai, "/servers/aiDryRun", {"use_case": "ping"})
    assert status == 200 and answer.get("success"), (status, answer)
    result = answer["result"]
    assert result["ok"] is True and result["model"]["name"] and result["models_listed"] >= 1
    assert result["tag_suggest"]["reachable"] is True, result["tag_suggest"]


@pytest.mark.parametrize("created_event", REPORTS[:1], indirect=True, ids=[REPORTS[0].stem[:8]])
def test_summarise_event_through_misp(misp_ai, misp_write, created_event) -> None:
    before = _event(misp_write, created_event)

    def reports_of(event: dict) -> dict:
        return {r["name"]: r["content"] for r in event["EventReport"] if not r["deleted"]}

    _run(
        misp_ai,
        f"/events/aiSummarize/{before['id']}",
        lambda: len(reports_of(_event(misp_write, created_event))) > 1,
    )
    after = _event(misp_write, created_event)
    reports = reports_of(after)
    summaries = [n for n in reports if n.startswith("AI summary of event")]
    assert len(summaries) == 1 and reports[summaries[0]].startswith("## What happened")
    assert reports[REPORTS[0].stem] == REPORTS[0].read_text(encoding="utf-8")  # source untouched
    assert after["published"] is False and after["distribution"] == str(YOUR_ORG_ONLY)
    assert AI_TAGS <= _tags(after)  # Event::aiAttachResultTags() honours results.Tag


@pytest.mark.parametrize("created_event", REPORTS[1:2], indirect=True, ids=[REPORTS[1].stem[:8]])
def test_summarise_report_through_misp(misp_ai, misp_write, created_event) -> None:
    before = _event(misp_write, created_event)
    (report,) = [r for r in before["EventReport"] if not r["deleted"]]

    def stored_report() -> dict:
        (stored,) = [
            r for r in _event(misp_write, created_event)["EventReport"] if not r["deleted"]
        ]
        return stored

    def summarised() -> bool:
        return stored_report()["content"].startswith(ai_connector.AI_SUMMARY_HEADING)

    _run(misp_ai, f"/eventReports/aiSummarize/{report['id']}", summarised)
    stored = stored_report()
    original = REPORTS[1].read_text(encoding="utf-8")
    assert stored["content"].startswith(f"{ai_connector.AI_SUMMARY_HEADING}\n## Threat")
    assert ai_connector.AI_SUMMARY_DELINEATOR in stored["content"]
    assert stored["content"].rstrip().endswith(original.rstrip())  # analyst's text kept
    assert AI_TAGS <= _tags(_event(misp_write, created_event))
    # a second run replaces the block instead of stacking one (MISP strips before sending)
    first_timestamp = stored["timestamp"]
    _run(
        misp_ai,
        f"/eventReports/aiSummarize/{report['id']}",
        lambda: stored_report()["timestamp"] != first_timestamp,
    )
    assert stored_report()["content"].count(ai_connector.AI_SUMMARY_DELINEATOR) == 1


@pytest.mark.parametrize("created_event", REPORTS[:1], indirect=True, ids=[REPORTS[0].stem[:8]])
def test_recommend_tags_through_misp(misp_ai, misp_write, created_event) -> None:
    before = _event(misp_write, created_event)
    status, listed = _http(misp_ai, f"/events/aiRecommendTags/{before['id']}")
    assert status == 200, (status, listed)  # 403 + JSON = the module or the service failed
    rows = listed["Tag"]
    names = [r["name"] for r in rows]
    if not names:
        pytest.skip("the tag-suggest service abstained on this event")
    # the two AI names the module appends are listed apart, never offered as rows
    assert set(listed["provenance"]) == AI_TAGS and not set(names) & AI_TAGS, listed
    accepted = [r["name"] for r in rows if r["status"] == "ok" and r["selectable"]]
    assert accepted, rows
    status, answer = _http(misp_ai, f"/events/aiRecommendTags/{before['id']}", {"tags": accepted})
    assert status == 200 and answer.get("saved") and answer.get("attached", 0) >= 1, answer
    assert set(answer["provenance"]["attached"]) == AI_TAGS, answer["provenance"]
    after = _event(misp_write, created_event)
    assert set(accepted) | AI_TAGS <= _tags(after)


@pytest.mark.parametrize("created_event", REPORTS[1:2], indirect=True, ids=[REPORTS[1].stem[:8]])
def test_extract_indicators_through_misp(misp_ai, misp_write, created_event) -> None:
    """A4 over REST (direct apply): results.Attribute / results.Object land through MISP's
    module-result saver, each element AI-tagged and citing its source report."""
    before = _event(misp_write, created_event)
    (report,) = [r for r in before["EventReport"] if not r["deleted"]]
    _run(
        misp_ai,
        f"/events/aiExtractIndicators/{before['id']}",
        lambda: bool(_extracted(_event(misp_write, created_event))),
    )
    after = _event(misp_write, created_event)
    added = _extracted(after)
    for attribute in added:
        assert AI_TAGS <= _tags(attribute), (attribute["type"], attribute["value"])
        assert attribute["comment"].startswith(f"{extract.COMMENT} {report['uuid']}")
    (stored,) = [r for r in after["EventReport"] if not r["deleted"]]  # source untouched
    assert stored["content"] == REPORTS[1].read_text(encoding="utf-8")
    assert after["published"] is False and after["distribution"] == str(YOUR_ORG_ONLY)
    # a re-run adds nothing: the module deduplicates against the event it was sent
    status, answer = _http(misp_ai, f"/events/aiExtractIndicators/{before['id']}", {})
    assert status == 200 and answer.get("saved"), (status, answer)
    if "job_id" in answer:
        time.sleep(30)
    assert len(_extracted(_event(misp_write, created_event))) == len(added)
