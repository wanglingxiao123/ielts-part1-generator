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

`list_scenarios` is the only scenario catalogue. The frontend must not keep a local copy: a
drifted id means a user selects a scenario the backend cannot resolve.

Generate request fields:

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
| `batch_started` | once, first | `total`, `deadline_at`, `config` |
| `stage` | each stage transition | `slot_id`, `scenario`, `stage`, `attempt` |
| `material_completed` | per material, first-finished-first | `material`, `blueprint`, `audit`, `cross_check`, `selected_version`, `route`, `degraded`, `note`, `timings` |
| `material_failed` | per failed or skipped material | `reason`, `detail`, `skipped` |
| `batch_completed` | once, last | `succeeded`, `failed`, `skipped`, `degraded`, `stage_timings`, `slots` |
| `batch_failed` | malformed request only | `reason`, `detail` |

`stage` values in order: `generating`, `validating`, `regenerating`, `auditing`, `audited`,
`revising`, `anchors_repaired`, `re_auditing`, `infra_retry`.

`stage` doubles as the heartbeat. Do not filter it out at the transport layer — AgentCore closes
connections idle for 900s and a single material takes minutes.

## Fields that matter downstream

**`route`** — routing advice only; this task never writes S3.

- `pending` — verdict `PASS` or `PASS_WITH_MINOR_EDITS`
- `quarantine` — verdict `FAIL` or `NOT_ASSESSABLE`

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
| `unhandled_error` | unexpected exception, contained to this slot |
