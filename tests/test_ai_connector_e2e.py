"""End-to-end tests.

1. Local: start the real `misp-modules` server with this repository as custom module directory
   and POST every fixture event from `fixtures/output/` to `/query`.
2. Live: fetch the same events (uuids from `fixtures/output/hashes.csv`) from the MISP instance
   configured in `.env` and run them through the module. Skipped when `.env` is missing, the
   host is unreachable, or the API key is rejected (see conftest.MispApi). Set
   MISP_VERIFY_SSL=false (in .env or the environment) for self-signed certificates.
"""

# pytest fixtures are injected by name, which pylint reads as shadowing:
# pylint: disable=redefined-outer-name

import csv
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request

import pytest
from conftest import FIXTURE_DIR, FIXTURE_FILES, load_fixture

from expansion import ai_connector

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / "logs" / "test_e2e_server.log"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_service(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except error.URLError:
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}; see {LOG_PATH}")


def _post_json(url: str, body: dict, timeout: float = 30.0) -> dict:
    req = request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.fixture(scope="module")
def misp_modules_url():
    pytest.importorskip("misp_modules")
    port = _free_port()
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as log_handle:
        with subprocess.Popen(
            [
                sys.executable,
                "-m",
                "misp_modules",
                "-c",
                str(PROJECT_ROOT),
                "-l",
                "127.0.0.1",
                "-p",
                str(port),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,
        ) as process:
            try:
                _wait_for_service(f"http://127.0.0.1:{port}/healthcheck")
                yield f"http://127.0.0.1:{port}"
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


UNREACHABLE_LLM = {
    "openai_api_base": "http://127.0.0.1:1/v1",
    "model_id": "m",
    "request_timeout": 1,
}


def _envelope(use_case: str, data: dict, params: dict | None = None) -> dict:
    """The envelope MISP's Module::queryAI() posts."""
    return {
        "module": "ai_connector",
        "use_case": use_case,
        "data": data,
        "params": params or {},
        "timeout": 300,
    }


def test_service_lists_the_ai_connector_module(misp_modules_url) -> None:
    with request.urlopen(f"{misp_modules_url}/modules", timeout=5) as response:
        modules = json.loads(response.read().decode("utf-8"))
    module = next(m for m in modules if m["name"] == "ai_connector")  # MISP's AI_MODULE_NAME
    assert module["mispattributes"] == ai_connector.mispattributes
    assert module["meta"]["version"] == ai_connector.moduleinfo["version"]
    assert module["meta"]["module-type"] == ["ai"]  # what Module::getEnabledModules keys on


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_service_validates_every_fixture_event(misp_modules_url, path) -> None:
    """Without an LLM the furthest a valid event gets is the endpoint call: that error, and not
    an "Invalid MISP Event" one, proves the fixture passed validation through the real server."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    body = _envelope("summarization_on_event", raw, UNREACHABLE_LLM)
    payload = _post_json(f"{misp_modules_url}/query", body)
    assert payload["error"].startswith("LLM endpoint unreachable"), payload


def test_service_rejects_invalid_event(misp_modules_url) -> None:
    payload = _post_json(
        f"{misp_modules_url}/query", _envelope("infoextraction", {"Event": {"date": "x"}})
    )
    assert payload["error"].startswith("Invalid MISP Event")


def test_service_rejects_unknown_params_by_name(misp_modules_url, dummy_event) -> None:
    body = _envelope("infoextraction", dummy_event, {"summary_kind": "event", "min_confidence": 2})
    payload = _post_json(f"{misp_modules_url}/query", body)
    assert payload["error"].startswith("Invalid request: ") and "summary_kind" in payload["error"]


# --- live MISP instance -------------------------------------------------------------------------


def _fixture_uuids() -> list[str]:
    with (FIXTURE_DIR / "hashes.csv").open(encoding="utf-8") as handle:
        return sorted({row[1] for row in csv.reader(handle) if len(row) == 2})


@pytest.mark.parametrize("uuid", _fixture_uuids())
def test_live_misp_events_validate(misp_api, uuid) -> None:
    live = misp_api.fetch(uuid)
    event = ai_connector.validate_event(live["Event"])  # what dict_handler does before any use-case
    assert event.uuid == uuid
    fixture = load_fixture(uuid)
    expected_report = "\n\n".join(
        r["content"] for r in fixture["Event"].get("EventReport", []) if not r.get("deleted")
    )
    assert ai_connector.get_event_report(event) == expected_report


# --- use-cases through the real misp-modules server (live LLM; skips when it is down) ---


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("summarization_on_event", "EventReport"),
        ("infoextraction", "Attribute"),
    ],
)
def test_service_runs_use_cases(
    misp_modules_url, llm_settings, dummy_event, body, expected
) -> None:
    payload = _post_json(f"{misp_modules_url}/query", _envelope(body, dummy_event), timeout=300)
    assert "error" not in payload, payload
    assert expected in payload["results"]
    assert payload["metadata"]["model"]["name"] == llm_settings.model
    tagged = {t["name"] for t in payload["results"].get("Tag", [])} | {
        t["name"] for a in payload["results"].get("Attribute", []) for t in a.get("Tag", [])
    }
    assert 'ai-computer-assisted:assistance-level="ai-generated"' in tagged or not payload[
        "results"
    ].get("Attribute")
