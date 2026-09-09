import copy
import json

import pytest
from conftest import FIXTURE_FILES, NO_REPORT, TWO_REPORTS, load_fixture
from pymisp import MISPEvent

from expansion import ai_connector

UNREACHABLE = {"openai_api_base": "http://127.0.0.1:1/v1", "model_id": "m", "request_timeout": 1}


def test_introspection_and_version() -> None:
    assert ai_connector.introspection() == {
        "input": [],
        "output": ["EventReport", "Tag", "Attribute", "Object"],
        "format": "misp_standard",
    }
    info = ai_connector.version()
    assert info["name"] == "ai_connector"
    assert info["module-type"] == ["ai"]
    assert info["config"] == list(ai_connector.PARAMS)
    assert "config" not in ai_connector.moduleinfo  # version() must not mutate the global


def test_handler_without_query_returns_false() -> None:
    assert ai_connector.handler() is False


def test_handler_rejects_invalid_json() -> None:
    assert ai_connector.handler("{not json")["error"].startswith("Invalid JSON request")


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_every_fixture_event_validates_and_round_trips(path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    event = ai_connector.validate_event(raw["Event"])
    out = ai_connector.process_event(event)
    assert out is event
    dumped = json.loads(event.to_json())
    assert dumped["uuid"] == raw["Event"]["uuid"]
    assert dumped["info"] == raw["Event"]["info"]
    assert len(dumped.get("Attribute", [])) == len(raw["Event"].get("Attribute", []))
    assert len(dumped.get("Object", [])) == len(raw["Event"].get("Object", []))


def test_wrapped_and_bare_events_are_equivalent(event_with_report) -> None:
    for body in ({"Event": event_with_report["Event"]}, event_with_report["Event"]):
        raw = ai_connector._extract_data({"data": body}, "infoextraction")  # noqa: SLF001  # pylint: disable=protected-access
        assert raw["uuid"] == event_with_report["Event"]["uuid"]


def test_report_use_case_builds_an_event_around_the_one_report(event_with_report) -> None:
    report = event_with_report["Event"]["EventReport"][0]
    stub = {"uuid": event_with_report["Event"]["uuid"], "info": "the event", "id": "12"}
    raw = ai_connector._extract_data(  # pylint: disable=protected-access
        {"data": {"EventReport": report, "Event": stub}}, "summarization_on_eventReport"
    )
    assert raw == {"uuid": stub["uuid"], "info": "the event", "EventReport": [report]}
    event = ai_connector.validate_event(raw)
    assert [r.name for r in event.event_reports] == [report["name"]]
    bare = ai_connector._extract_data(  # pylint: disable=protected-access
        {"data": report}, "summarization_on_eventReport"
    )
    assert bare["info"] == report["name"]


def test_input_dict_is_not_mutated(event_with_report) -> None:
    before = copy.deepcopy(event_with_report)
    ai_connector.dict_handler(
        {"use_case": "infoextraction", "data": event_with_report, "params": UNREACHABLE}
    )
    assert event_with_report == before


@pytest.mark.parametrize("uuid", [TWO_REPORTS, NO_REPORT])
def test_event_report_joins_non_deleted_reports(uuid) -> None:
    raw = load_fixture(uuid)
    event = ai_connector.validate_event(raw["Event"])
    contents = [r["content"] for r in raw["Event"].get("EventReport", []) if not r.get("deleted")]
    assert ai_connector.get_event_report(event) == "\n\n".join(contents)


def test_deleted_reports_are_ignored(event_with_report) -> None:
    for report in event_with_report["Event"]["EventReport"]:
        report["deleted"] = True
    event = ai_connector.validate_event(event_with_report["Event"])
    assert ai_connector.get_event_report(event) == ""


@pytest.mark.parametrize(
    ("request_body", "fragment"),
    [
        ({"use_case": "infoextraction"}, '"data" must hold'),
        ({"use_case": "infoextraction", "data": "not a dict"}, '"data" must hold'),
        ({"use_case": "infoextraction", "data": []}, '"data" must hold'),
    ],
)
def test_requests_without_an_event_are_rejected(request_body, fragment) -> None:
    result = ai_connector.dict_handler(request_body)
    assert result["error"].startswith("Invalid request: ") and fragment in result["error"]


@pytest.mark.parametrize(
    ("event", "fragment"),
    [
        ({"date": "2024-01-01"}, '"info" is required'),
        ({"info": "x", "date": "not-a-date"}, "date"),
        ({"info": "x", "distribution": 9}, "distribution"),
        ({"info": "x", "EventReport": [{"content": "c"}]}, "name"),
        ({"info": "x", "Attribute": [{"type": "nope", "value": "v"}]}, "nope"),
    ],
)
def test_invalid_events_are_rejected(event, fragment) -> None:
    body = {"use_case": "infoextraction", "data": {"Event": event}, "params": UNREACHABLE}
    result = ai_connector.dict_handler(body)
    assert result["error"].startswith("Invalid MISP Event: ")
    assert fragment in result["error"]


def test_default_galaxy_cluster_with_distribution_is_accepted() -> None:
    # MISP's /events/view output looks like this; PyMISP alone would reject it.
    cluster = {
        "uuid": "b7a2c8a4-0a17-4c0a-9d5d-3d5f7a5f1c11",
        "value": "x",
        "default": True,
        "distribution": "3",
        "sharing_group_id": "0",
    }
    galaxy = {
        "uuid": "c5f6f4c6-9d68-4b6f-9d3a-0c4b3d5a2f22",
        "name": "g",
        "type": "g",
        "GalaxyCluster": [copy.deepcopy(cluster)],
    }
    event = {
        "info": "x",
        "Galaxy": [copy.deepcopy(galaxy)],
        "Attribute": [{"type": "text", "value": "v", "Galaxy": [copy.deepcopy(galaxy)]}],
    }
    validated = ai_connector.validate_event(event)
    assert len(json.loads(validated.to_json())["Galaxy"]) == 1


def test_process_event_is_identity(event_with_report) -> None:
    event = ai_connector.validate_event(event_with_report["Event"])
    assert ai_connector.process_event(event) is event


def test_ai_summary_block_matches_misps_layout() -> None:
    block = ai_connector.ai_summary_block("## Threat\nx\n", "# Notes\nbody")
    assert block == "# AI summary\n## Threat\nx\n\n==================\n\n# Notes\nbody"


def test_process_event_e2etest_writes_event_file(event_with_report, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ai_connector, "E2E_DIR", tmp_path / "e2etests")
    event = ai_connector.validate_event(event_with_report["Event"])
    assert ai_connector.process_event(event) is event
    assert not (tmp_path / "e2etests").exists()  # default: nothing written
    ai_connector.process_event(event, e2etest=True)
    saved = json.loads((tmp_path / "e2etests" / f"{event.uuid}.json").read_text(encoding="utf-8"))
    assert saved["Event"]["uuid"] == event_with_report["Event"]["uuid"]
    assert saved["Event"]["timestamp"] == event_with_report["Event"]["timestamp"]


def test_validate_event_returns_a_misp_event(event_with_report) -> None:
    assert isinstance(ai_connector.validate_event(event_with_report["Event"]), MISPEvent)
