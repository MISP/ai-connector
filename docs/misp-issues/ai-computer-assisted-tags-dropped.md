Branch: `feature/ai_tools` (head 46403c4a). Module side: https://github.com/MISP/ai-connector.

## What happens

`ai_connector` marks everything the model produced with the two `ai-computer-assisted` taxonomy tags, verbatim from the taxonomy:

```
ai-computer-assisted:assistance-level="ai-generated"
ai-computer-assisted:review-level="unreviewed"
```

That is the module's core rule: nothing machine-made may land in MISP looking like analyst work. With the current branch the tags are dropped on the MISP side for every action:

| action | the module answers | MISP reads |
|---|---|---|
| `summarization_on_event` (`Event::aiSummarize`) | `results.EventReport {name, content}` **and** `results.Tag [{name: <the two tags>}]` | only `EventReport.name` / `.content` — the event is not tagged |
| `summarization_on_eventReport` (`EventReport::aiSummarize`) | same | same — neither the event nor the report is tagged |
| `tag_suggest` (`Event::aiRecommendTags`) | `results.Tag` = the suggestions **plus** the two AI tags appended | shows them as two more optional checkboxes; a user who does not tick them attaches AI-suggested tags without any AI provenance |

Verified on a live `feature/ai_tools` instance: after *Summarise event* and *Summarise report* the event carries no `ai-computer-assisted` tag; `tests/test_e2e_misp_ai_actions.py` in the module repo keeps that assertion as `xfail` pointing at this issue.

## Proposal

1. **Summaries:** in `Event::aiSummarize()` and `EventReport::aiSummarize()`, attach every name in `results.Tag` to the event (global tags, `Tag::quickAdd()` + `EventTag::attachTagToEvent()` like an accepted suggestion, one unpublish). The taxonomy is `ai-computer-assisted`, shipped with MISP; if it is not enabled on the instance, enabling it (or creating the tag rows) is the same path `aiAttachTags()` already walks for unknown tags.
2. **Tag recommendation:** in `Event::aiAttachTags()`, when at least one suggestion is accepted, attach the `ai-computer-assisted` names from the module's answer automatically (or render them pre-ticked and not deselectable in the modal). Accepting zero suggestions attaches nothing, as today.
3. Optional: an `ai-computer-assisted:review-level` change is then the analyst's "I reviewed this" action — the taxonomy has `reviewed` / `partially-reviewed` values for exactly that.

This keeps the contract as documented (`results` = only what was produced) — `Tag` is already a documented `results` key, it just needs to be honoured next to `EventReport`.

The module already sends the tags for all three actions, so nothing changes on its side.
