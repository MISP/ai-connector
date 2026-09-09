"""Live quality gate through MISP's own AI actions (MISP feature/ai_tools).

The dev MISP in .env must run feature/ai_tools with Plugin.AI_services_enable on and
Plugin.AI_services_url/port pointing at a running ai_connector (docs/OPERATOR_GUIDE.md 2.3);
the test checks that first and skips otherwise. It then drives the three actions exactly as
MISP's UI does — POST /events/aiSummarize/<id>, POST /eventReports/aiSummarize/<id>,
GET + POST /events/aiRecommendTags/<id> — on a fresh org-only event carrying one orkl report,
and verifies what landed on the instance. The events are deleted afterwards (E2E_KEEP=1 keeps
them).

Known gap, tracked on MISP/MISP: MISP drops results.Tag for the summaries, so the
ai-computer-assisted tags are asserted as xfail until that lands.
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
from genai import prompts

AI_TAGS = set(prompts.AI_TAGS)
JOB_TIMEOUT = 600
MISP_TAGS_ISSUE = "docs/misp-issues/ai-computer-assisted-tags-dropped.md (to be filed on MISP/MISP)"


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
        with urllib.request.urlopen(req, timeout=120, context=misp_api.context) as response:
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
    if not AI_TAGS <= _tags(after):
        pytest.xfail(f"MISP dropped results.Tag: {MISP_TAGS_ISSUE}")


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
    after = _event(misp_write, created_event)
    stored = stored_report()
    original = REPORTS[1].read_text(encoding="utf-8")
    assert stored["content"].startswith(f"{ai_connector.AI_SUMMARY_HEADING}\n## Threat")
    assert ai_connector.AI_SUMMARY_DELINEATOR in stored["content"]
    assert stored["content"].rstrip().endswith(original.rstrip())  # analyst's text kept
    # a second run replaces the block instead of stacking one (MISP strips before sending)
    first_timestamp = stored["timestamp"]
    _run(
        misp_ai,
        f"/eventReports/aiSummarize/{report['id']}",
        lambda: stored_report()["timestamp"] != first_timestamp,
    )
    assert stored_report()["content"].count(ai_connector.AI_SUMMARY_DELINEATOR) == 1
    if not AI_TAGS <= _tags(after):
        pytest.xfail(f"MISP dropped results.Tag: {MISP_TAGS_ISSUE}")


@pytest.mark.parametrize("created_event", REPORTS[:1], indirect=True, ids=[REPORTS[0].stem[:8]])
def test_recommend_tags_through_misp(misp_ai, misp_write, created_event) -> None:
    before = _event(misp_write, created_event)
    status, listed = _http(misp_ai, f"/events/aiRecommendTags/{before['id']}")
    assert status == 200, (status, listed)  # 403 + JSON = the module or the service failed
    rows = listed["Tag"]
    names = [r["name"] for r in rows]
    if not names:
        pytest.skip("the tag-suggest service abstained on this event")
    assert set(names) >= AI_TAGS, names  # the module appends them to every non-empty answer
    accepted = [r["name"] for r in rows if r["status"] == "ok" and r["selectable"]]
    assert accepted, rows
    status, answer = _http(misp_ai, f"/events/aiRecommendTags/{before['id']}", {"tags": accepted})
    assert status == 200 and answer.get("saved") and answer.get("attached", 0) >= 1, answer
    after = _event(misp_write, created_event)
    assert set(accepted) <= _tags(after)
    if not AI_TAGS <= _tags(after):
        ai_rows = [r for r in rows if r["name"] in AI_TAGS]
        pytest.xfail(f"AI tags not attachable through MISP ({ai_rows}): {MISP_TAGS_ISSUE}")
