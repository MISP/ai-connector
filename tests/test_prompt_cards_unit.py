"""The shareable prompt-card format (schemas/): the example validates and matches the module."""

import hashlib
import json
from pathlib import Path

import pytest

from expansion.ai_connector import USE_CASES
from genai import prompts

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
SCHEMA = json.loads((SCHEMAS / "prompt-cards.schema.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((SCHEMAS / "prompt-cards.example.json").read_text(encoding="utf-8"))
CLUSTERS = {cluster["uuid"]: cluster for cluster in prompts.load_clusters()}
# MISP use_case (cards) -> generic-ai-prompts galaxy meta.use_case (clusters)
GALAXY_USE_CASE = {
    "infoextraction": "cti-info-extraction",
    "summarization_on_eventReport": "summary-report",
    "summarization_on_event": "summary-event",
}


def test_example_validates_against_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft7Validator.check_schema(SCHEMA)
    validator = jsonschema.Draft7Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
    validator.validate(EXAMPLE)


def test_use_case_enum_is_the_misp_contract():
    enum = SCHEMA["definitions"]["prompt_card"]["properties"]["use_case"]["enum"]
    assert sorted(enum) == sorted(set(USE_CASES) - {"ping"})


def test_example_names_this_schema():
    version = SCHEMA["definitions"]["metadata"]["properties"]["schema_version"]["const"]
    assert EXAMPLE["metadata"]["schema_url"] == SCHEMA["$id"]
    assert EXAMPLE["metadata"]["schema_version"] == version


@pytest.mark.parametrize("card", EXAMPLE["prompts"], ids=lambda card: card["name"])
def test_example_card_is_the_shipped_cluster(card):
    assert hashlib.sha256(card["prompt"].encode()).hexdigest() == card["prompt_sha256"]
    cluster = CLUSTERS[card["uuid"]]
    meta = cluster["meta"]
    assert GALAXY_USE_CASE[card["use_case"]] == meta["use_case"]
    assert (card["name"], card["version"], card["prompt"], card["model_parameters"]) == (
        cluster["value"],
        int(meta["version"]),
        meta["prompt"],
        meta["model_parameters"],
    )
