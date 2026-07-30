# Handover: event contract and sample artifacts

For the `07-28-audio-storage` and `07-28-frontend` tasks. The `type` values below are a published
contract: new fields are backward compatible, renames are not.

Samples in `docs/sample/` are real output from a live run, not hand-written:

| File | What it is |
|---|---|
| `material.json` / `blueprint.json` / `audit.json` / `cross_check.json` | one complete material, verdict PASS 100/100 |
| `events-sse-raw.txt` | raw `text/event-stream` bytes from `POST /invocations` |
| `events-batch3.jsonl` | a 3-material concurrent batch, one line per event, including a real failure |

## Endpoint

One endpoint, two behaviours, selected by `action`:

```
POST /invocations  {"action": "list_scenarios"}   -> application/json
POST /invocations  {"action": "generate", ...}    -> text/event-stream
GET  /ping                                        -> {"status":"Healthy"}
```

`list_scenarios` is the only scenario catalogue. Note there is **no `max_batch`** in it: the field
and the concept were removed, because the reason for a ceiling was that a whole batch shared one
invocation. It no longer does — see below.

### One invocation carries ONE material

The web tier (`web/fanout.py`) expands a browser's `generate` request into **N independent
invocations, one per material**, each with its own `runtimeSessionId` (so each lands on its own
microVM), and merges their event streams into the single stream the browser reads. Consequences for
anyone reading this contract:

* **This document describes ONE CHILD's stream**, which is what this Runtime emits. It is not what
  the browser sees. A child is a complete batch of one: it emits its own `batch_started`, calls its
  material `slot-1`, and ends with its own `batch_completed`.
* **The merged stream the browser reads** has exactly one `batch_started` (with the batch total), the
  children's middle events with `slot_id` rewritten to batch-wide `slot-1..slot-N`, and one
  `batch_completed` aggregating every child's counts. The web tier owns that reconciliation and the
  sequence numbering; see `web/fanout.py`'s module docstring for why namespacing the ids instead
  would break the frontend.
* **The 15-minute synchronous wall now bounds one material** (~146–230s measured, `docs/timing.md`),
  not a batch. `Budget` is therefore a backstop against one material's own refills rather than a
  rationer between siblings.
* **Concurrency moved to the web tier** (`WEB_FANOUT_CONCURRENCY`, default 6). `concurrency` in the
  request below still works and is still honoured, but in production every request carries one slot,
  so it is clamped to 1.

The frontend must not keep a local copy of the catalogue: a drifted id means a user selects a
scenario the backend cannot resolve.

Generate request fields (as one child receives them; the web tier rewrites `scenarios`/`counts`/
`count` to name a single material and forwards everything else verbatim):

```jsonc
{
  "action": "generate",
  "scenarios": ["accommodation-rental", "booking-hotel"],
  "count": 1,                                  // default per-scenario count
  "counts": {"booking-hotel": 2},              // per-scenario override
  "custom_scenario": {"prompt_hint": "...", "count": 1},
  "concurrency": 3,                            // optional
  "hard_limit_seconds": 260                    // testing only: shrinks the time budget
}
```

## Events

| `type` | When | Key fields |
|---|---|---|
| `type` | When (per child) | Merged-stream fate | Key fields |
|---|---|---|---|
| `batch_started` | once, first | **swallowed** — the web tier emits its own, with the batch total | `total`, `deadline_at`, `config` |
| `stage` | each stage transition | relayed, `slot_id` rewritten | `slot_id`, `scenario`, `stage`, `attempt` |
| `material_completed` | per material | relayed, `slot_id` rewritten | `material`, `blueprint`, `audit`, `cross_check`, `selected_version`, `route`, `degraded`, `refill_rounds`, `note`, `timings` |
| `material_failed` | per failed or skipped material | relayed, `slot_id` rewritten | `reason`, `detail`, `skipped` |
| `batch_completed` | once, last | **folded** into the single merged summary | `succeeded`, `failed`, `skipped`, `degraded`, `refilled`, `stage_timings`, `slots` |
| `batch_failed` | malformed request only | **becomes `material_failed`** for that child's slot, so the other children continue | `reason`, `detail` |

A child that cannot be invoked at all, or whose stream dies mid-material, likewise appears in the
merged stream as `material_failed` for its own slot only. That isolation is the point of paying for N
invocations: one bad material no longer costs the batch.

`stage` values in order: `generating`, `validating`, `regenerating`, `auditing`, `audited`,
`revising`, `anchors_repaired`, `re_auditing`, `infra_retry`, `refilling`, `refill_abandoned`.

`refilling` / `refill_abandoned` belong to the NOT_ASSESSABLE refill (below). They are
observability, not user-facing state: exactly one `material_completed` or `material_failed` is
still emitted per slot however many attempts it took.

`stage` doubles as the heartbeat. Do not filter it out at the transport layer — AgentCore closes
connections idle for 900s and a single material takes minutes.

## Fields that matter downstream

**`route`** — always `pending`. There is no quarantine state: a user who asked for two materials
receives two, and a `FAIL` one comes back like any other with its shortcomings stated on the card
so the user decides. It is selectable and it gets audio.

`NOT_ASSESSABLE` never reaches the user at all — see refill below.

**`refill_rounds`** / **`refilled`** — a slot whose audit came back `NOT_ASSESSABLE` produced no
usable script: no text to read and no defect list to weigh, so there is nothing on a card to
decide with. That slot is re-run, up to `MAX_REFILL_ROUNDS` (2) further attempts and only while
`Budget.may_start()` allows it, so the user still receives the count they asked for. When the
budget runs out first the batch returns what it has rather than failing; a slot still unassessable
after the last attempt is reported `material_failed` with `reason: "not_assessable"`.

`FAIL` is **not** refilled. It is usable-but-flawed, and regenerating it would spend the user's
budget hiding a material they asked to see.

**`selected_version`** — `"initial"` or `"revised"`. The three artifacts always come from the same
version; `audit` is never the score of a different script than `material`.

**`degraded`** / `degraded_reason`** — `true` with `"time_budget"` means the revision pass was
skipped to finish inside the platform limit. Such a material is routed on its own verdict with no
extra penalty: degrading means one fewer optimisation, not a lower standard. Show the flag so
reviewers know, but do not treat it as a quality signal.

**`cross_check`** — the blind comparison. Read this even when the verdict is `PASS`: the sample
batch contains a material the auditor scored 100/100 with zero findings while the cross-check
found one unrecoverable point and one unintended detail. The verdict and the cross-check answer
different questions.

**`note`** — why this version was chosen: `clean_on_first_pass`, `selected_initial`,
`selected_revised`, `revise_rejected_by_validate`, `revise_rejected_anchor_desync`,
`revision_skipped_time_budget`, `revise_call_failed`, `re_audit_failed`.

**`failed` vs `skipped`** — a `material_failed` event with `skipped: true` and
`reason: "skipped_time_budget"` was never attempted. Present these separately from real failures;
a batch that reports partial results as success would be worse than the timeout it avoids.

## Failure reasons

| `reason` | Meaning |
|---|---|
| `validation_exhausted` | three generations all failed deterministic validation |
| `model_error` | model unreachable after the infrastructure retry budget |
| `audit_failed` | audit step unusable after retries |
| `validator_unavailable` | a skill script crashed or timed out (infrastructure, not content) |
| `skipped_time_budget` | not started; insufficient remaining budget |
| `not_assessable` | every attempt, including refills, produced a script the audit could not read |
| `unhandled_error` | unexpected exception, contained to this slot |

## Candidate card fields

`list_candidates` (and the record behind each `material_id`) carries three derived fields the card
grid renders without loading the scripts. All three are pure Python — `backend/deterministic/cards.py`
— and involve no model call: a per-card request would eat into the 15-minute wall, and a generated
summary would make two identical materials describe themselves differently.

- `preview_first_line` — the first non-narration turn. `speaker1` is the exam narrator, whose turn
  is identical rubric in every material, so it is skipped.
- `preview_summary` — one Chinese line, topic + distraction type, e.g.
  `预订海滨酒店，含拼读 + 价格修正干扰`. Topic comes from the scenario catalogue's `title_zh`;
  features from the blueprint's `correction`, `indirect_confirmation` and per-item `distractor`.
- `flagged_points` — ascending item numbers a reviewer should look at, for the ten-dot strip.
  Anchor mismatch, clustering and out-of-order points only: defects that cost a candidate a written
  answer. Thresholds are `frontend/public/config.json`'s `CLUSTER_SPAN` / `CLUSTER_MIN_POINTS`,
  reused rather than reinvented.

There is no `expects_audio`: every candidate is selectable and every selection is synthesised.
