# ai_connector

The MISP AI module: the [misp-modules](https://github.com/MISP/misp-modules) module of type
`ai` that MISP's *AI actions* ([MISP `feature/ai_tools`](https://github.com/MISP/MISP/tree/feature/ai_tools))
call to put a Large Language Model to work on a **full MISP Event** ([MISP core
format](https://www.misp-standard.org/rfc/misp-standard-core.html)) — not on a single attribute.

MISP posts a whole event (or one event report) with the LLM settings from its *AI* tab, the
module validates it with [PyMISP](https://github.com/MISP/PyMISP), takes the markdown of the
`EventReport`s (the CTI report an analyst pasted in) and runs the requested use-case. What
comes back is exactly what MISP writes: a new report, a revised report, or tag names.

| use-case | what you get |
|---|---|
| **Summarise the event** (`summarization_on_event`) | a new `EventReport` with an executive summary of the whole event |
| **Summarise a report** (`summarization_on_eventReport`) | the report with a `# AI summary` block on top of the analyst's text |
| **Recommend tags** (`tag_suggest`) | taxonomy and galaxy tags voted from the most similar events already in your MISP (no LLM) |
| **CTI info extraction** (`infoextraction`) | high-confidence MISP attributes read out of the report — IPs, domains, hashes, CVEs, plus `file` / `vulnerability` objects — each one refanged, deduplicated against the event, and carrying the quote it came from (no MISP button yet) |
| **Ping** (`ping`) | is the endpoint up and does it serve the configured model |

Three properties matter more than the feature list:

- **MISP's own contract.** One request shape (`use_case`, `data`, `params`), one answer shape;
  the settings come from MISP's *AI* tab with every request.
- **Everything the model produced is tagged**, verbatim, with
  `ai-computer-assisted:assistance-level="ai-generated"` and
  `ai-computer-assisted:review-level="unreviewed"`. Nothing machine-made can quietly pass as
  analyst work, and existing event content is never modified or re-tagged.
- **Bring your own model.** Any OpenAI-compatible chat endpoint — a local Ollama on your own
  GPU, vLLM, or a commercial API. Prompts and sampling parameters are not hardcoded: they ship
  as a MISP galaxy you can edit and version.

## Documentation

Three guides, one per reader. Start with the one that matches what you want to do.

| guide | for you if you want to | contents |
|---|---|---|
| **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** | understand what it does to an event before deciding to use it | the three jobs with real examples, the rules it always follows, measured quality, where your data goes, what it does *not* do |
| **[docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md)** | install it, run it and keep it alive | install, `.env`, running the server, verification calls, settings, the tag-suggestion service, metrics, logs, failure modes, security, upgrades |
| **[docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)** | change the code | code map, invariants, how to add a use-case or a prompt, the test layers and gates, how to run and write benchmarks, how to contribute, and where the project stands |

**Only installing it for a MISP instance?** [docs/INSTALL.md](docs/INSTALL.md): Docker or a
systemd service, then MISP's settings. It is also the *Installation guide* MISP opens from its
*AI* settings.

Reference documents behind them — the use-case contracts
([docs/USE-CASES.md](docs/USE-CASES.md)), the architecture
([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)), the prompts
([docs/PROMPTS.md](docs/PROMPTS.md)), the test plan ([docs/TESTING.md](docs/TESTING.md)), the
measurements ([docs/BENCHMARKS.md](docs/BENCHMARKS.md)), the requirements
([docs/requirements.md](docs/requirements.md)) and the backlog
([docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md)) — are indexed in the developer guide.

## Why you need it

CTI arrives as prose. A vendor PDF, an ORKL report, a blog post — a human reads it, then retypes
the indicators into MISP one by one and writes a summary for the people who will not read the
full report. That work is slow, it is boring, and it is exactly where indicators get dropped or
mistyped.

This module does the mechanical part and leaves the judgement to you:

- **Less retyping.** The indicators in the report become MISP attributes with the right type and
  category, defanged spellings (`hxxp://`, `1.2.3[.]4`) resolved back to real values.
- **Faster triage.** An event-level summary tells an analyst whether this event is worth opening.
- **Auditable, not magic.** Every generated element is tagged as AI-generated and unreviewed, so
  you can filter, review or purge it later. Precision is measured, not asserted: there is an
  extraction precision gate against hand-checked indicator lists and a benchmark suite.
- **Your data stays where you put it.** Point it at a local model and no report ever leaves your
  network.
- **No lock-in.** It is a plain misp-modules expansion module and a small Python package; the
  only third-party runtime dependency is PyMISP.

## Try it in a minute

Install with five commands on a bare host (uv brings Python 3.14):
[docs/OPERATOR_GUIDE.md §1.2](docs/OPERATOR_GUIDE.md#12-install). Put an OpenAI-compatible
endpoint in `.env` ([§1.3](docs/OPERATOR_GUIDE.md#13-configure)), then start the server:

```bash
.venv/bin/python -m misp_modules -c . -l 127.0.0.1 -p 6667
```

In a second terminal — a summary of the shipped test event, with the AI tags it carries:

```bash
jq -c '{module: "ai_connector", use_case: "summarization_on_event", data: ., params: {}}' fixtures/summary/dummy-event.json \
  | curl -s http://127.0.0.1:6667/query -H 'Content-Type: application/json' --data @- \
  | jq '{summary: .results.EventReport.content, tags: [.results.Tag[].name], meta: .metadata}'
```

## Status

Working and measured: the three use-cases, the round-trip guarantee, the test layers (offline,
local server, live LLM, live MISP, live write path) and the benchmark suite are in place —
current numbers in [docs/BENCHMARKS.md](docs/BENCHMARKS.md), the full picture of what is done,
partial and planned in
[docs/DEVELOPER_GUIDE.md §10](docs/DEVELOPER_GUIDE.md#10-where-the-project-stands).

Not there yet: MISP's buttons live on its `feature/ai_tools` branch only. That branch wires
all five use-cases (summarise event / report, recommend tags, extract indicators, *Test LLM*)
and attaches the `ai-computer-assisted` tags the module returns.

- module name `ai_connector` · module type `ai` · input: `use_case` + `data` (a MISP Event, or
  one EventReport with its event) + `params` · output: `results.EventReport`, `results.Tag`,
  `results.Attribute` / `results.Object`
- licensed under the [GNU AGPL v3](LICENSE); built for [CIRCL](https://www.circl.lu/) by
  [ctitools](https://github.com/ctitools)
