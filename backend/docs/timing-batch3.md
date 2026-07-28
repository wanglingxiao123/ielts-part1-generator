# 3-material concurrent batch — recorded runs

Two runs of the same three scenarios (`accommodation-student-hall`, `booking-shipping`,
`employment-vacancy`) at `concurrency=3`. The first run motivated a real fix, so both are kept.

## Run A — 0 of 3 succeeded (before the cumulative-feedback fix)

All three materials exhausted their three generation attempts in ~244s and produced nothing.

The pattern, visible in the recorded stage events, was oscillation:

```
slot-1 attempt 1 -> "full opening must include 'four different recordings'"
slot-1 attempt 2 -> "dialogue words outside 450-750: 448"
slot-1 attempt 3 -> "full opening must include 'four different recordings'"
```

Attempt 2 fixed the reported error and regressed on the one attempt 1 had already satisfied,
because the retry prompt carried **only the latest** attempt's errors. Every attempt was solving a
different single problem.

Fixed by accumulating validator errors across attempts in `loop.py` (`seen_errors`) and telling
the model the list is cumulative. Two regression tests cover it. This is a good example of why
the plan insisted on measuring rather than assuming: the logic looked correct and every unit test
passed, and it still could not produce a material.

## Run B — 2 of 3 succeeded, 244s total

| Slot | Scenario | Attempts | Verdict | Score | Cross-check | Total |
|---|---|---:|---|---:|---|---:|
| slot-1 | accommodation-student-hall | 3 (exhausted) | — | — | — | failed |
| slot-2 | booking-shipping | 3 | PASS | 100 | **not ok**: 1 unrecoverable + 1 unintended | 243.5s |
| slot-3 | employment-vacancy | 3 | PASS_WITH_MINOR_EDITS | 98 | ok, 10/10 matched | 233.9s |

Per-stage means across the batch:

| Stage | Mean | Min | Max |
|---|---:|---:|---:|
| generate (per attempt) | 41.7 / 61.5 / 42.8 | 35.5 | 85.3 |
| validate (per attempt) | 0.08 | 0.05 | 0.09 |
| blind audit | 45.7 | 41.9 | 49.5 |
| revise | 15.5 | 13.2 | 17.7 |
| blind re-audit | 38.0 | 34.9 | 41.0 |
| **total per material** | **238.7** | 233.9 | 243.5 |

## Run C — 2 of 3 succeeded, 186s total (after extending anchor repair to generation)

Scenarios `community-environment`, `daily-driving-lessons`, `accommodation-student-hall`.

| Slot | Verdict | Score | Selected | Total |
|---|---|---:|---|---:|
| slot-1 | PASS_WITH_MINOR_EDITS | 98 | **revised** | 166.0s |
| slot-2 | PASS | 99 | **revised** | 185.9s |
| slot-3 | — | — | — | failed (`closing must direct candidates to Part/Section 2`) |

Two changes since run B, both prompted by observed failures rather than review:

1. **Anchor repair now runs on generated output, not only on revisions.** A live attempt failed
   with six `turn_index N does not carry its evidence (found at turn N+1)` errors — a uniform
   off-by-one that `deterministic/anchors.py` resolves exactly. Spending a ~40s model call to
   re-derive an index we can compute with certainty is waste. The rule is unchanged: one unique
   match repairs, zero or several do not, and the validator still has the final say. It fired
   twice in this run (3 and 5 indices), and total batch time dropped from 244s to 186s.
2. **The event queue no longer races.** The drain loop previously cancelled a pending
   `queue.get()` on each iteration, which can discard an item already handed to it — silently
   losing a `material_completed` payload, the one event a client cannot do without. Replaced with
   a sentinel-terminated drain; a 480-event stress test shows zero loss.

Both materials selected the **revised** version here, so `pick_better` and the full 4-call path
are exercised end to end against the live model, not just in unit tests.

## What these runs establish

- **No throttling at concurrency 3.** One `500 server had an error` occurred and was absorbed by
  the infrastructure retry budget without consuming a generation attempt — exactly the split
  design.md §3.3 asks for, observed working on a real fault rather than a simulated one.
- **Three concurrent materials cost about the same wall time as one.** 244s for three vs 146s for
  one; the bottleneck is per-call latency, not local CPU.
- **The deterministic layer is free.** Eight script invocations across the batch totalled ~0.6s.
- **The revision pass is cheap** (~54s for revise + re-audit) relative to a generation attempt,
  which is what makes the time-budget degradation in design.md §9 a sensible trade.

## The dominant failure cause, and why it is not fixed here

Across both batches, 16 regenerations were triggered. The single most common cause, 7 of them:

```
full opening must include 'four different recordings'
```

`validate_part1.py` requires the literal strings `four different recordings`, `four parts` and
`check your answers` in the narration. `specification.md` §2 paraphrases this as *"the opening
should naturally cover four recordings, ..."* — it never states `four different recordings` or
`check your answers` verbatim, and "naturally cover" reads as licence to phrase it freely. A model
following the specification faithfully writes "You will hear four recordings" and fails.

This is a **skill-contract issue, not a backend one**, and design.md §13 is explicit that a
missing or inconsistent contract goes back to that task rather than being worked around in the
backend. Patching the prompt here would create the second source of truth prd.md forbids. Two
options for skill-contract to choose between:

1. quote the required phrases verbatim in `specification.md` §2 (smallest change), or
2. relax the validator to accept `four recordings` and `check your answers` variants.

Until then, expect roughly one in three materials to exhaust its generation budget on this alone.
The cumulative-feedback fix lets a material recover once the phrase appears in an error message,
which is why run B succeeded twice where run A succeeded zero times — but it is recovery from an
avoidable failure, and it costs ~40s per occurrence.
