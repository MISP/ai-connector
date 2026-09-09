Branch: `feature/ai_tools` (head 46403c4a). Module side: https://github.com/MISP/ai-connector (the `ai_connector` misp-module, module-type `ai`).

`ai_connector` implements the contract of `docs/dev/ai_integration.md` for the three actions MISP wires today (`summarization_on_event`, `summarization_on_eventReport`, `tag_suggest`). Two more use-cases exist on the module side and have no MISP path yet. This issue gives their request/answer shapes so the MISP side can be built against something fixed.

## 1. `infoextraction` — CTI info extraction from the event's reports

The module reads the event's EventReports, asks the LLM for indicators as JSON, and keeps only high-confidence candidates that appear literally in the (refanged) report text. Everything it adds carries the two `ai-computer-assisted` tags.

**Request** (same envelope as the others, `Module::buildAiRequest()`):

```json
{"module": "ai_connector", "use_case": "infoextraction",
 "data": {"Event": {...}},
 "params": {"openai_api_base": "...", "api_key": "...", "model_id": "...", "temperature": 0,
            "request_timeout": 120, "min_confidence": 0.9},
 "timeout": 300}
```

`min_confidence` (0..1, default 0.9) is the only extra knob; a `Plugin.AI_min_confidence` float setting would map onto it like the others.

**Answer** — only what this run added, in MISP core format, each attribute AI-tagged and carrying a `comment` with the quote it came from and the EventReport uuid:

```json
{"results": {
   "Attribute": [{"type": "domain", "value": "acme-bank-secure.example", "category": "Network activity",
                  "to_ids": true, "comment": "extracted by ai_connector from EventReport <uuid>",
                  "Tag": [{"name": "ai-computer-assisted:assistance-level=\"ai-generated\""},
                          {"name": "ai-computer-assisted:review-level=\"unreviewed\""}]}],
   "Object":    [{"name": "file", "Attribute": [...]}, {"name": "vulnerability", "Attribute": [...]}],
   "Event":     {"Event": {...}}
 },
 "metadata": {"added": 3, "objects": 1, "rejected": [{"type": "...", "value": "...", "reason": "not-in-source"}], "model": {...}, "prompt": {...}}}
```

`results.Event` is the whole processed event (for scripts); MISP only needs `Attribute` and `Object`.

**Proposal for the MISP side** — an *Extract indicators with AI* entry in the *AI actions* chooser (and a workflow node), running like A1/A2 as a background job, that:

1. takes `results.Attribute` and `results.Object`,
2. shows them in the existing review screen used by import modules ("Populate from…", `EventsController::addIoc`-style: the user unticks what is wrong) **or**, for the workflow node, attaches them directly and unpublishes the event once,
3. keeps the `Tag` list on each element (that is where the `ai-computer-assisted` provenance lives — see the companion issue on tags),
4. leaves the source EventReport untouched (the module never modifies it).

Nothing else in the module's contract changes.

## 2. `ping` — the greyed *Test LLM* button

The settings tab reserves a `ping` use-case "the module may offer later". The module offers it now. No `data`; the `params` are the same `Plugin.AI_*` values, so the button tests exactly what the actions will use:

```json
{"module": "ai_connector", "use_case": "ping", "params": {"openai_api_base": "...", "api_key": "...", "model_id": "..."}}
```

```json
{"results": {"ok": true, "endpoint": "http://10.72.0.4:11434/v1",
             "model": {"name": "qwen3.8:latest", "server": "ollama 0.33.2", "digest": "22130167c4c2", "quantization": "Q4_K_M"},
             "latency_ms": 4, "models_listed": 18,
             "tag_suggest": {"url": "http://127.0.0.1:8000", "reachable": true}}}
```

It does one `GET /models` on the endpoint (the model must be listed — an unlisted model is `{"error": "model 'x' is not served by ..."}`) and one `/health` on the tag-suggestion service; no generation, so it answers in milliseconds. An unreachable endpoint is `{"error": "LLM endpoint unreachable or timed out (120s): ..."}`.

`POST /servers/aiDryRun` with `use_case=ping` and no event id would be enough to wire the button.

## Where this is tested on the module side

`tests/test_misp_contract_unit.py` replays the `Module::buildAiRequest()` envelopes for all five use-cases with a faked LLM and checks the answers the way `Event.php` / `EventReport.php` read them (including the `AI_SUMMARY_BLOCK_REGEX` ported verbatim); `tests/test_e2e_misp_ai_actions.py` drives the three wired actions on a live `feature/ai_tools` instance.
