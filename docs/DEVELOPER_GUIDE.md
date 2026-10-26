# Developer guide

**Who this is for:** anyone changing the code — a new use-case, a new filter rule, a new
benchmark, a bug fix. It maps the code, states the invariants you must not break, and shows the
loop from an idea to a merged change.

**The other two guides:** [USER_GUIDE.md](USER_GUIDE.md) explains what it does ·
[OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) installs and runs it.

**Read [AGENTS.md](../AGENTS.md) first.** It is the binding contributor guide: short closed
loops, "less code is more", tests first, a CHANGELOG entry per change, docs updated in the same
pass, `pymisp` as the only third-party runtime dependency.

## 1. Set up

A fresh host or a fresh clone: [OPERATOR_GUIDE.md §1.2](OPERATOR_GUIDE.md#12-install) (uv,
Python 3.14, clone, `.venv`, install). In a working checkout the `.venv` at the repository
root is that install — update it, never recreate it:

```bash
uv pip install --python .venv/bin/python -e ".[dev,e2e]"   # deps + pytest/ruff/pylint + misp-modules
.venv/bin/pytest -q                                        # offline layers must be green before you start
.venv/bin/python -m misp_modules -c . -l 127.0.0.1 -p 6667  # the module in a real misp-modules server
```

`.env` at the repository root configures the LLM, MISP and the tag-suggestion service — see
[OPERATOR_GUIDE.md §1.3](OPERATOR_GUIDE.md#13-configure). Without it the live test layers skip.

## 2. Code map

```text
expansion/ai_connector.py   the module: misp-modules contract, request shapes, validation, settings, dispatch
genai/                    everything else, because misp-modules imports EVERY .py in expansion/
  llm.py                    the only network code: http_json(), llm_chat(), model_info(), LLMSettings
  prompts.py                prompt galaxy clusters, the pinned ai-computer-assisted tags, tag_ai_generated()
  extract.py                UC1 — candidates from the model, filters, objects, provenance comments
  summarize.py              UC2 — render_event(), the structural gate check_summary(), summarize()
  suggest.py                UC3 — the misp-tag-suggest HTTP client (no LLM)
                            llm.ping() is the "ping" use-case (endpoint up, model listed, no generation)
  refang.py, dates.py       the two normalisations extraction is allowed to perform
  classic.py                regex baseline over iocextract — benchmarks and tests only, never imported by the module
galaxies/ · clusters/     the generic-ai-prompts MISP galaxy (prompt text + model + sampling parameters)
schemas/                  prompt cards: the shareable prompt + model + parameters + scores format (PROMPTS.md)
fixtures/                 output/*.json (8 real events, 5 with reports), summary/dummy-event.json, gold/*.iocs.json
tests/                    see §6; conftest.py holds the .env loader, the read-only MispApi and the live gates
benchmarks/               see §7
docs/                     this guide and the reference documents listed in §10
logs/                     benchmark and test-run logs (gitignored), never production logs
```

Two structural facts worth knowing before you move a file:

- **misp-modules imports every `.py` in `expansion/`** as a module, so helper files cannot live
  next to `ai_connector.py`; that is why `genai/` exists. `ai_connector.py` puts the repository root
  on `sys.path` on import.
- `genai/classic.py` is deliberately unreachable from the request path, and
  `tests/test_classic_unit.py` keeps it that way. It exists so benchmarks have a deterministic
  reference extractor.

## 3. The contract and the data flow

The contract is the one MISP's `Module::queryAI()` speaks (MISP `feature/ai_tools`,
[docs/dev/ai_integration.md](https://github.com/MISP/MISP/blob/feature/ai_tools/docs/dev/ai_integration.md)):

```json
{"module": "ai_connector", "use_case": "summarization_on_eventReport",
 "data": {"EventReport": {"name": "...", "content": "..."}, "Event": {"uuid": "...", "info": "..."}},
 "params": {"openai_api_base": "http://127.0.0.1:11434/v1", "api_key": "", "model_id": "gemma4:12b",
            "temperature": 0, "request_timeout": 120, "suggest_limit": 5, "suggest_min_score": 0},
 "timeout": 300}
```

| `use_case` | `data` | `results` |
|---|---|---|
| `summarization_on_event` | `{"Event": {...}}` | `EventReport {name, content}` (a new report) + `Tag` (the two AI tags) |
| `summarization_on_eventReport` | `{"EventReport": {...}, "Event": {...}}` (one report; a bare report dict works too) | `EventReport {name, content}` = the full revised report: `# AI summary`, the summary, a blank line, `==================`, the original text — MISP merges it in place + `Tag` |
| `tag_suggest` | `{"Event": {...}}` | `Tag [{name, score}]` ≥ `suggest_min_score`, at most `suggest_limit`, plus the two AI tags when anything was suggested; `[]` on abstention |
| `infoextraction` | `{"Event": {...}}` (MISP sends only the non-deleted reports it wants extracted) | `Attribute` and `Object` (what this run added, MISP core format, each AI-tagged, `comment` citing the source report uuid) — MISP saves them through its import-module path; the processed event is only logged, at DEBUG (`misp-modules -d`) |
| `ping` | none | `{ok, endpoint, model {name, server, digest}, latency_ms, models_listed, tag_suggest {url, reachable}}` |
| any | | `{"error": "..."}` — one key, every validation problem named; never a partial result |

`params` are MISP's `Plugin.AI_*` settings (unset ones omitted) and override `.env`; the module
also accepts `prompt` (cluster uuid, value or inline text), `min_confidence`, `seed`, `top_p`,
`max_tokens`, `think`. An unknown key or an out-of-range value is an error, never a silent
no-op. `timeout` is misp-modules' own per-request bound.

```text
POST /query {"module": "ai_connector", "use_case": …, "data": …, "params": {…}}
   │
   ├─ resolve_settings()    params > .env (OPENAI_* / AI_CONNECTOR_<KEY>) > default; unknown key → error
   ├─ _extract_data()       unwrap {"Event": …}; for the report use-case build an event around the one report
   ├─ validate_event()      PyMISP MISPEvent.load(force_timestamps=True) → "Invalid MISP Event: …"
   ├─ process_event()       dispatch on use_case; fills the metadata dict
   │     summarization_on_event        → genai/summarize.py  summarize(event, report, "event", …)
   │     summarization_on_eventReport  → genai/summarize.py  summarize(event, report, "report", …)
   │     tag_suggest                   → genai/suggest.py    suggest_tags(event, …)
   │     infoextraction                → genai/extract.py    extract_iocs(event, report, …)
   │     (ping never reaches here: llm.ping() answers before validation)
   ├─ _results()            only what was produced, in the shape MISP reads (table above)
   └─ {"results": {…}, "metadata": {…}}  or  {"error": "…"}
```

`dict_handler(request)` is the in-process entry point used by tests, benchmarks and (preferably)
misp-modules itself; `handler(q)` is the JSON-string entry the module contract requires.
`process_event(event, settings=…)` is the engine entry (settings from `resolve_settings()` plus
`use_case`; absent or `"none"` = pass-through, which the round-trip gate relies on).
Background and the diagrams: [ARCHITECTURE.md](ARCHITECTURE.md).

## 4. Invariants — do not break these

Each one has a test; if your change makes a test fail, change the change, not the test.

1. **MISP data in → only what was produced out.** The request and answer shapes are MISP's
   (`Module::queryAI()`); `results` holds the new report, the tag names or the added elements,
   never a partial result.
2. **Everything the model produced is tagged** with both pinned `ai-computer-assisted` strings —
   per attribute for extraction, on the event for event-level output. Nothing LLM-made may
   leave untagged.
3. **Existing content is never modified or re-tagged.** Additions only; summaries are new
   reports.
4. **No fallback.** Failure is `{"error": …}`; never a synthesised result. A silent fallback
   tagged as AI-generated is exactly the bug this module was rewritten to remove
   ([IMPROVEMENTS.md](IMPROVEMENTS.md) item 7).
5. **The in-source rule.** An extracted value must appear literally in the (refanged) report.
   Refang and hash re-typing are the only normalisations; both are recorded in the comment.
6. **Endpoint, key and model come from MISP's `Plugin.AI_*` settings (`params`) when sent,
   else from `.env`.** A request can therefore point the module at another endpoint — that is
   MISP's design, so the misp-modules port must be reachable from MISP only (operator guide 4.6).
   Every other knob is named and bounded in `PARAMS`; an unknown one is an error.
7. **PyMISP is the validator; no JSON schema.** None of the upstream schemas defines
   `EventReport` ([ARCHITECTURE.md](ARCHITECTURE.md), "Schema provenance"). Do not vendor or
   invent one.
8. **`pymisp` is the only third-party runtime dependency.** `iocextract` is declared for the
   benchmark baseline and must stay off the request path. A new dependency needs explicit
   approval ([AGENTS.md](../AGENTS.md)).
10. **The module is `ai_connector`** — the file name `expansion/ai_connector.py` is the module
    name misp-modules serves and the name MISP hard-wires (`Module::AI_MODULE_NAME`), with
    `"module-type": ["ai"]`. The prompt galaxy keeps its name `generic-ai-prompts`: it is a
    MISP galaxy identifier that lives on instances.
9. **Docs and `CHANGELOG.md` change in the same commit as the behaviour.**

### PyMISP quirks the module works around

Keep these; each has a test, and the full list is [IMPROVEMENTS.md](IMPROVEMENTS.md) item 11.

| quirk | workaround |
|---|---|
| `MISPEvent.load()` mutates the caller's dict | deep-copy before loading (`_normalise_for_pymisp`) |
| `to_dict(json_format=True)` raises on real exports | serialise with `to_json()` |
| default galaxy clusters carrying `distribution` / `sharing_group_id` are rejected, though MISP emits them | strip those two keys on default clusters before loading |
| the event `timestamp` is dropped on published events | build the event with `force_timestamps=True` |
| `add_event_report()` raises `TypeError` (not `PyMISPError`) without a `name` | catch `TypeError`/`KeyError` alongside `PyMISPError` |

## 5. How to extend it

### 5.1 Add a use-case

The thinnest end-to-end path, in the order that keeps the suite green:

1. **Write the contract first** in [USE-CASES.md](USE-CASES.md) (input, output, filters, error
   cases, tagging) and the EARS requirement in [requirements.md](requirements.md).
2. **Add the prompt** as a new cluster in `clusters/generic-ai-prompts.json` — value
   `<use-case>/<model>-v<n>`, a fixed uuid, `meta.model`, `meta.model_parameters`,
   `meta.prompt`, `meta.prompt_sha256` ([PROMPTS.md](PROMPTS.md)). No prompt text in code.
3. **Write `genai/<use_case>.py`** with one public function taking the `MISPEvent` (plus report,
   settings, prompt) and returning a **metadata dict**. It mutates the event through PyMISP's
   API and calls `prompts.tag_ai_generated(...)` for anything it adds.
4. **Register it**: a key in `DEFAULTS` if it needs a setting, the name in `USE_CASES`, a branch
   in `_run_use_case()` in `expansion/ai_connector.py`. Keep the dispatch flat.
5. **Tests, three layers**: offline in `tests/test_usecases_unit.py` with the LLM monkeypatched
   at `genai.llm.llm_chat` (one test per rejection rule and the tagging rule), through the real
   server in `tests/test_ai_connector_e2e.py`, and a live test with the gate that defines "good
   enough" in `tests/test_llm_live.py`.
6. **Benchmark it** if quality is a number (§7), and **document it**: USE-CASES, this guide's
   status table, `CHANGELOG.md`, and the settings table in
   [OPERATOR_GUIDE.md §2.1](OPERATOR_GUIDE.md#21-settings).

### 5.2 Change a prompt

Never edit a released cluster in place — a golden file and a benchmark round refer to its
sha256. Add `…-v<n+1>`, make it the first cluster of its use-case to make it the default, keep
the old one selectable, bump the galaxy version, re-record the goldens deliberately
(`--update-goldens`, review the diff), and re-run the benchmark for that use-case. Superseded
clusters are marked as such in [PROMPTS.md](PROMPTS.md).

### 5.3 Add a filter or a format check

Extraction's filters live in `reject_reason()` and its helpers in `genai/extract.py`. Add the
check, add the reason string to the metadata table in
[OPERATOR_GUIDE.md §4.1](OPERATOR_GUIDE.md#41-the-per-request-metadata-block-your-metrics), and
add a unit test that a candidate failing only that check is rejected with that reason. Order
matters: the cheapest and most specific reasons come first so the metadata stays diagnostic.

### 5.4 Touch the network

There is exactly one HTTP function, `genai.llm.http_json`. Use it. Tests fake it; a second
network path would have to be faked separately and would escape the `http(s)`-only guard.

## 6. Tests

```bash
.venv/bin/pytest -q                                          # offline layers; live layers skip
MISP_VERIFY_SSL=false .venv/bin/pytest -q                    # dev MISP has a self-signed certificate
MISP_VERIFY_SSL=false .venv/bin/pytest -q --require-live     # pre-tag: unavailable live systems FAIL
.venv/bin/pytest -q tests/test_ai_connector_unit.py -k report  # one file, one keyword
MISP_VERIFY_SSL=false .venv/bin/pytest -q -s tests/test_llm_live.py --update-goldens   # re-record summaries
E2E_SEED=42 MISP_VERIFY_SSL=false .venv/bin/pytest -q -s tests/test_e2e_roundtrip.py   # reproduce a draw
```

| layer | file | needs | proves |
|---|---|---|---|
| unit | `test_ai_connector_unit.py`, `test_usecases_unit.py`, `test_suggest_unit.py`, `test_misp_compare.py`, … | nothing | handler contract, input shapes, every rejection rule, the tagging rule, refang/dates/metrics/benchmark tooling |
| local e2e | `test_ai_connector_e2e.py` | `misp-modules` | the real server lists the module and round-trips every fixture through `/query` |
| live LLM (`-m live_llm`) | `test_llm_live.py` | `.env` + endpoint | determinism, the extraction precision gate, the summary structural gate, goldens |
| live suggest (`-m live_suggest`) | `test_suggest_live.py` | the service | suggested tags exist on the MISP, the event is AI-tagged, abstention changes nothing |
| live MISP (`-m live_misp`) | `test_ai_connector_e2e.py`, `test_e2e_roundtrip.py` | `.env` + MISP | fixture uuids still validate; the round-trip gate |
| live write path | `test_e2e_misp_write.py` | both | the module's output lands in MISP as sent — attributes, new report, tags — on an org-only, unpublished event that is deleted afterwards (`E2E_KEEP=1` keeps it) |

Conventions:

- Live tests **skip** when their system is unavailable, and every run ends with a **live gates**
  summary. `--require-live` turns those skips into failures — use it before every tag and
  whenever you claim "all tests pass". Four fixture uuids no longer exist on the dev instance
  and stay skips: a data problem, not a missing system.
- Markers are added automatically from the fixtures a test uses (`tests/conftest.py`).
- The **round-trip gate** (`test_e2e_roundtrip.py`) fetches 10 random live events, runs each
  through `validate_event()` → `process_event(..., e2etest=True)` and asserts the written file
  is *semantically* identical to the original. "Semantically" is defined in
  `tests/misp_compare.py`: null/empty values, `"1"` vs `1`, whitespace and ISO date-time
  spelling are ignored, plus four allowlisted PyMISP normalisations. **Widen that allowlist only
  with a stated reason, never to make a failing gate pass.**
- Summaries are tested at three strictness levels (byte-identical, whitespace-normalised,
  structural). The **structural** level is the gate; the goldens are pinned to the model digest,
  server version and prompt hash and skip when those differ. Full reasoning:
  [TESTING.md §4](TESTING.md).

Reference runs (2026-09-05, v4): offline 174 passed; `--require-live` 196 passed, 4 skipped,
all three live gates ran.

## 7. Benchmarks

Benchmarks answer "how good is it", tests answer "is it correct". Every quality claim in the
docs comes from a benchmark round; [BENCHMARKS.md](BENCHMARKS.md) holds the current numbers and
the history, `benchmarks/README.md` the per-step details.

What exists:

| benchmark | sample | scored against |
|---|---|---|
| extraction | 100 orkl.eu reports (seeded draw) | a regex superset (`genai/classic.py`) and three hand-labelled gold lists |
| summarization, `report` | the same 100 reports | the module's own structural gate, length, coverage, determinism |
| summarization, `event` | 100 real dev-MISP events (5–300 attributes) | the same, plus the event's own indicators |
| tag suggestion | the same 100 events | the analysts' own tags as gold |

Run a whole round with the pre-seeded samples — one command, about 70 minutes on the reference
host:

```bash
sh benchmarks/run_v4.sh > logs/benchmark-v4.log 2>&1
```

Copy that script for the next round and change the suffix, so previous results stay comparable.
Do not run live tests while a round is running: Ollama serialises requests.

Reading a report: the generated `docs/BENCHMARKS_*.md` files are **never edited by hand**. The
headline numbers go into `BENCHMARKS.md` "Current results" plus one row in its history table,
with what changed and the before → after.

Writing a new benchmark — the contract every existing one follows:

1. a **sampler** that draws a reproducible sample into `benchmarks/data/<source>/` and writes a
   `sample.json` recording seed, filters and ids;
2. a **runner** that calls the module through `dict_handler` and writes one result file per item
   (model digest, prompt sha256, timing, and `{"error", "seconds"}` on failure — no item is ever
   dropped), resumable, logging successes, failures and rows/s to `logs/`;
3. a **comparer** that turns result files into one generated `docs/BENCHMARKS_*.md` plus a
   committed csv;
4. **offline unit tests** for the scoring on synthetic data (`tests/test_compare_unit.py` is the
   pattern), so metric bugs do not need a GPU to find;
5. a **second pass** into another results directory when determinism is part of the claim.

Metric definitions (precision, recall, F1, Jaccard, Cohen's kappa, micro vs macro, the
degenerate cases) are fixed in [BENCHMARKS.md](BENCHMARKS.md) — reuse them, do not invent new
ones. Data and per-item results are gitignored; the csv and the generated report are committed.

## 8. Quality gates before you commit

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check expansion genai tests benchmarks
.venv/bin/pylint expansion genai tests benchmarks          # must be 10/10
.venv/bin/pytest -q                                        # offline layers
```

`ruff format expansion genai tests benchmarks` fixes formatting. CI
(`.github/workflows/checks.yml`) runs ruff (pinned), pylint (`--disable=fixme`), the offline
pytest layers and semgrep (`p/python`, `p/security-audit`) on every push and pull request.
The pytest job also installs `jsonschema` with `--require-hashes` from
`.github/schema-requirements.txt` (CI only, not a project dependency) for the prompt-card check.
Install the local hook once:

```bash
git config core.hooksPath .githooks
```

Before a tag: `MISP_VERIFY_SSL=false .venv/bin/pytest -q --require-live` **and** a benchmark
round, so a regression in quality is caught next to a regression in correctness.

## 9. Contributing

The loop, from [AGENTS.md](../AGENTS.md): read the relevant files → find the thinnest
end-to-end path that proves the change → implement it (less code is more) → run it or state the
blocker concretely → capture commands and data flow in the docs → iterate from the observed
result.

- **Tests first**, and a test per new `.py` file.
- **One CHANGELOG entry per change**, at the top, saying what changed and what it measured.
- **Docs in the same pass.** Behaviour lives in [USE-CASES.md](USE-CASES.md), rules in
  [requirements.md](requirements.md), numbers in [BENCHMARKS.md](BENCHMARKS.md), operational
  consequences in [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md). Backlog items go to
  [IMPROVEMENTS.md](IMPROVEMENTS.md) rather than into a TODO comment.
- **Dependencies:** ask first. Record version, license, release date and hash, and check the
  transitive tree ([AGENTS.md](../AGENTS.md), "Software supply chain checks").
- **Benchmark rounds get a git tag** (`benchmark-YYYY-MM-DD[-topic]`) so a report can be traced
  to the exact tree that produced it.
- **Never commit `.env`.**

The sandboxed developer host from `.env` runs the same loop when the laptop cannot reach the
live systems:

```bash
set -a && source .env
rsync -az --delete --exclude '.git' --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' \
  ./ "${DEVELOPER_USER}@${DEVELOPER_HOST}:${DEVELOPER_HOST_DIRECTORY}/ai-connector/"
ssh "${DEVELOPER_USER}@${DEVELOPER_HOST}" 'export PATH="$HOME/.local/bin:$PATH" && cd "'"${DEVELOPER_HOST_DIRECTORY}"'/ai-connector" && uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -e ".[dev,e2e]" && .venv/bin/pytest -q'
```

## 10. Where the project stands

### Implemented and gated by tests

| area | state |
|---|---|
| MISP's `ai_connector` contract (feature/ai_tools, 7de7581d): the five use-cases, `params` overrides, results shapes, error handling; MISP wires all five (four actions + *Test LLM*) and attaches the provenance tags | done, gated offline (`tests/test_misp_contract_unit.py`) and live (`tests/test_e2e_misp_ai_actions.py`) |
| round-trip guarantee (10 random live events, semantic comparison) | done, gated |
| UC1 extraction — filters, refang, hash re-typing, dates, `to_ids` lowering, `file`/`vulnerability` objects, provenance | done, precision-gated |
| UC2 summarization — `report` and `event` kinds, structural gate, determinism | done, gated |
| UC3 tag suggestion via misp-tag-suggest, abstention handling | done, gated |
| `ai-computer-assisted` tagging from a pinned list, checked against upstream | done |
| prompts + sampling parameters as a MISP galaxy, provenance in the metadata | done |
| settings precedence `params` > `.env` > default, unknown params refused, timeout, no fallback | done |
| test layers incl. the live write path, explicit live gates (`--require-live`) | done |
| benchmarks for all four measurable use-case variants, one-command round | done |
| CI (ruff, pylint, offline pytest, semgrep) and a pre-commit hook | done |

### Partial — works, but the measurement or the polish is missing

| item | gap | reference |
|---|---|---|
| gold data set | three hand-labelled reports carry every precision number | IMPROVEMENTS 32 |
| correctness of LLM-only findings | filenames, actor and malware names are checked to be in the text, not to be indicators | IMPROVEMENTS 27 |
| summary *meaning* | only structure, length and foreign indicators are checked | IMPROVEMENTS 28 |
| tag-suggestion evaluation | the benchmark measures the label policy as much as the model | BENCHMARKS.md |
| golden drift | a wording drift on the same model warns, it does not fail | TESTING.md §9 gap 10 |
| benchmark statistics | one seed, one model, no run history, no confidence intervals | IMPROVEMENTS 31 |
| supply chain | no `pip-audit` on `uv.lock` in CI; no coverage floor | TESTING.md §9 gaps 1, 9 |
| version numbers | `moduleinfo["version"]` is 0.4, `pyproject` says 0.3.0 — bump both together | IMPROVEMENTS 14 |

### Planned — designed, not started

| item | what it means | reference |
|---|---|---|
| galaxy clusters for actors and malware | emit `misp-galaxy:threat-actor="…"` instead of free-text attributes | IMPROVEMENTS 36 |
| content tags only when stated | TLP, kill-chain and confidence tags only if literally in the report (the same rule dates already follow) | IMPROVEMENTS 34 |
| `ObjectReference` links | drops / connects-to / exploits between extracted candidates | IMPROVEMENTS 35 |
| context-size guard | fail instead of letting the model silently truncate a long report | IMPROVEMENTS 29 |
| adversarial fixtures | prompt injection, empty and image-only reports, non-UTF-8, RTL | IMPROVEMENTS 30 |
| TLP governance | refuse to send restricted content to a non-local endpoint | requirements.md R9.4 |
| `urllib` → `requests` | one HTTP library across module, tests and benchmarks | IMPROVEMENTS 33 |

Parked ideas (LLM-based tag proposal, ML pre-publish checker, chatbot, threat-actor clustering,
knowledge graphs, periodic reports, MCP server) are kept as one-liners at the end of
[USE-CASES.md](USE-CASES.md).

## 11. Reference documents

| document | content |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | data flow, schema provenance, design history, misp-modules background |
| [USE-CASES.md](USE-CASES.md) | the contract of each use-case, the tagging rule, the configuration table |
| [requirements.md](requirements.md) | EARS requirements with status |
| [PROMPTS.md](PROMPTS.md) | the prompt galaxy, every shipped prompt text and its version |
| [TESTING.md](TESTING.md) | test layers, per-use-case plan, deterministic summaries, the strategy review |
| [BENCHMARKS.md](BENCHMARKS.md) | method, metric definitions, current results and history |
| [IMPROVEMENTS.md](IMPROVEMENTS.md) | repository analysis and the numbered backlog |
| [INTEGRATION_PLAN.md](INTEGRATION_PLAN.md) | how MISP invokes modules, and the recommended integration |
| [INSTALL.md](INSTALL.md) | installing the module for a MISP instance (Docker, systemd); MISP displays it, one tab per `## ` section |
| [DEPLOY_TAG_SUGGEST.md](DEPLOY_TAG_SUGGEST.md) | the tag-suggestion service on a GPU host, step by step |
| [../AGENTS.md](../AGENTS.md), [../CLAUDE.md](../CLAUDE.md) | contributor and agent guidance |
| [../CHANGELOG.md](../CHANGELOG.md) | what changed, newest first |
