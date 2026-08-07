"""One Runtime invocation per material, merged into the single SSE stream the frontend reads.

## Why this module exists

Every material of a batch used to travel inside ONE ``invoke_agent_runtime`` call. That made the
platform's 15-minute synchronous limit a product limit: six materials at ~150-230s each barely fit,
so ``config/scenarios.yaml`` grew a ``max_batch: 6`` and the UI refused a seventh set. The product
owner's objection is correct and structural -- generating a material has no cross-material
dependency, so nothing required them to share a request:

    每套材料是一个独立请求（独立的 AgentCore invoke），不共享 session；并行执行，并发上限自己
    控制；前端通过 SSE 逐套接收；去掉「单批上限」的概念。

So the fan-out moves here. N children, N ``runtimeSessionId``s (a new id lands on a fresh microVM,
which is what makes them genuinely parallel rather than queued behind one warm instance), each
carrying exactly one material. The 900s wall now bounds ~200s of work instead of ~1400s.

## What the merge has to reconcile

Each child is a complete batch as far as the backend is concerned: it emits its own
``batch_started``, its own ``slot-1``, and its own ``batch_completed``. Relayed naively the browser
would see N batches starting, N materials all claiming ``slot-1``, and a batch that finishes N
times. Three rules fix that, and all three are load-bearing:

**One ``batch_started``, emitted by the web tier before any child answers.** The frontend needs the
total up front to lay out skeleton cards, and it must not wait for the first invoke's headers.
Children's own ``batch_started`` frames are swallowed.

**The web tier owns the slot id space.** Child-local ``slot-k`` is rewritten to the batch-wide
``slot-<n>`` this fan-out allotted it. Namespacing (``child-3/slot-1``) was the obvious alternative
and it is wrong: ``frontend/src/api/agentcore.ts`` pre-plans ``slot-1..slot-N`` in request order at
``createBatch`` time and matches wire events to those planned slots by exact id, so a namespaced id
would create a *second* card and leave the skeleton spinning forever. Rewriting to the planned ids
is what lets the frontend contract stay untouched -- and because the plan order here is the same
order ``backend/request.py`` used to expand (scenarios in order, then the custom one), the
per-scenario index the frontend derives is still right.

**One ``batch_completed``, when the last child finishes**, aggregating every child's counts. A
child that fails to invoke at all, or dies mid-stream, or answers ``batch_failed``, contributes
``material_failed`` for its own slot and nothing else: one child's failure must not end the batch
for the other N-1.

## Ordering, back-pressure and the threadpool

Each child's ``iter_lines`` read is blocking (botocore ``StreamingBody``), so each child runs on a
thread and pushes into an ``asyncio.Queue`` through ``call_soon_threadsafe``. The merged stream is
an *async* generator over that queue, which is a deliberate change from the old single-payload
relay: that one had to be a sync generator so ``iterate_in_threadpool`` would keep its blocking
read off the event loop, and here the blocking reads are already on their own threads. The
guarantee is the same and is now structural -- nothing blocking runs on the loop at all.

The threads come from a dedicated executor sized to the concurrency cap, NOT from anyio's default
threadpool. That pool has 40 tokens and is shared with every sync route handler in `web/app.py`
(``/healthz`` included, until it was made async). Six children holding tokens for four minutes
each, times a handful of concurrent users, is how a health check starts timing out -- and an
instance whose health check times out gets killed, taking every in-flight batch with it. A separate
executor makes that arithmetic impossible rather than merely unlikely.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import logging

from .runtime_client import SSE_CONTENT_TYPE, iter_sse_payloads, new_session_id, read_json
from .slot_state import COMPLETE, TERMINAL_SLOT_STATES, SlotStateReader, build_reader

LOG = logging.getLogger(__name__)

__all__ = [
    "FANOUT_CONCURRENCY",
    "PER_MATERIAL_WALL_SECONDS",
    "ChildPlan",
    "FanOut",
    "plan_children",
    "launch_order",
    "HEARTBEAT",
    "HEARTBEAT_SECONDS",
]

# How many materials the web tier will have in flight at once.
#
# Not a product limit -- the user may ask for any number of sets -- but a throughput one: the model
# channel's TPM/RPM is undocumented, and 6 is the value the backend already ran its own slots at
# without seeing a 429. Raise it only with evidence; on 429s lower it, because a retry storm costs
# more wall time than a shorter queue does.
#
# The cost of holding a slot open is one blocked thread, not CPU: the web tier is 0.5 vCPU / 1 GB
# and every one of these threads is parked on a socket read.
FANOUT_CONCURRENCY = max(1, int(os.environ.get("WEB_FANOUT_CONCURRENCY", "6")))

# The platform's synchronous wall on ONE invocation, which now carries ONE material. Used only to
# report an honest `deadline_at`; the backend enforces its own budget inside each child.
PER_MATERIAL_WALL_SECONDS = float(os.environ.get("WEB_PER_MATERIAL_WALL", "900"))

# How long the merged stream may stay silent before it emits a keepalive.
#
# Measured, not guessed: a real 8-material batch went 96 seconds between two consecutive events
# (216.6s -> 313.0s), and a larger batch waits longer -- a child produces nothing between
# `generating` and its terminal frame, and with concurrency 6 a later wave has not started yet.
#
# 96 seconds of silence is longer than every intermediary's idle-read tolerance. CloudFront's
# origin read timeout maxes out at 60s without a support case; ALB's idle timeout defaults to 60s.
# Without a keepalive the connection is severed mid-batch and the browser sees a lost stream for a
# batch that is running perfectly well. 15s leaves a wide margin under both.
HEARTBEAT_SECONDS = float(os.environ.get("WEB_SSE_HEARTBEAT", "15"))

# Marks a child's end on the merge queue. A unique object, so it can never collide with an event.
_CHILD_DONE = object()

# Yielded by `FanOut.events()` when the merge has been silent for `HEARTBEAT_SECONDS`.
#
# A sentinel object rather than a `{"type": "ping"}` dict, and that distinction is the whole design:
# a keepalive must NOT enter the event stream. `web/app.py` frames it as an SSE **comment**
# (`: hb`), which every layer already ignores -- `sseClient.ts:88` and `agentcore.ts:478` both skip
# lines starting with `:`, and `agentcore.test.ts` has a test for it. So it reaches no reducer, mints
# no `seq`, and is invisible to `since_seq` replay and to the batch recorder.
#
# The alternative -- the `ping` event §8 already defines -- would have to take a seq to be
# well-formed, and then a reconnecting client's cursor would sit on a frame that carries no state.
# Every 15 seconds of a 6-minute batch is ~24 such frames; a client resuming from one of them would
# be told "nothing new" for a batch that had in fact delivered materials.
HEARTBEAT = object()


class ChildPlan(object):
    """One child invocation: the payload to send and the batch-wide slot ids it may use.

    ``slot_ids`` is a list rather than a single id because a child is *allowed* to produce more
    than one material -- see `plan_children`. In the normal case it has exactly one entry.
    """

    __slots__ = ("index", "payload", "slot_ids", "scenario", "seats")

    def __init__(self, index: int, payload: Dict[str, Any], slot_ids: List[str],
                 scenario: str, seats: Optional[Dict[str, int]] = None) -> None:
        self.index = index
        self.payload = payload
        self.slot_ids = slot_ids
        self.scenario = scenario
        # slot_id -> the material's position within its scenario. Empty only for a hand-built plan
        # in a test; `plan_children` always supplies it.
        self.seats = dict(seats or {})


def plan_children(
    payload: Dict[str, Any], *, batch_id: str, default_count: int = 1
) -> Tuple[List[ChildPlan], List[str]]:
    """Expand one `generate` payload into one child per material. Returns (children, slot_ids).

    Mirrors `backend/request.py`'s ``_expand`` ordering exactly -- each requested scenario in turn,
    expanded by its count, then the custom scenario -- because that order is also the order the
    frontend pre-planned its skeleton cards in, and the two have to agree on which card is
    ``slot-3``.

    A scenario with no explicit count gets ``default_count``, which is 1 and NOT the catalogue's
    ``default_count: 2``. The difference matters only for a hand-written request -- the frontend
    always sends explicit counts -- and 1 is the safe direction: the web tier would have to fetch the
    catalogue to learn the real default, and a synchronous `list_scenarios` before every batch would
    add a round trip to the one path that must answer immediately. Under-generating is a request the
    user can repeat; over-generating spends their money on materials they did not ask for.

    Anything this function cannot expand -- an unknown scenario id, a bad count, a rejected custom
    scenario -- is left alone and sent to a child verbatim, so the backend's own validation
    produces the error message. Duplicating that validation here would give the user two different
    sentences for the same mistake, and the web tier has no catalogue to check ids against.

    **``action: generate_sets`` children get their own request ids.** That action persists slot state
    under ``_slots/{batch_id}/slots/{slot_id}.json`` and every child calls its own slot ``slot-1``, so
    one shared ``batch_id`` would have N children overwriting one another's records -- and a resumption
    would find one slot where the user asked for N. Each child therefore carries
    ``batch_id: {batch}-{slot}`` (its own resumable request) plus ``group_id: {batch}`` (the candidate
    group they share, so the materials of one submission still compete for one user choice). ``generate``
    children are untouched: that path mints its own group key per invocation and never reads the field.
    """
    counts = payload.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    fallback = payload.get("count")

    def count_for(scenario_id: str) -> int:
        raw = counts.get(scenario_id, fallback if fallback is not None else default_count)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            # Not the web tier's error to report: pass one child through and let the backend say
            # "count for %r must be an integer" in its own words.
            return 1
        return max(1, value)

    requested = payload.get("scenarios")
    requested = requested if isinstance(requested, list) else []

    # Everything except the batch shape is forwarded untouched: `hard_limit_seconds`,
    # `concurrency`, and any field added later all belong to the child.
    base = {k: v for k, v in payload.items()
            if k not in ("scenarios", "counts", "count", "custom_scenario")}

    children: List[ChildPlan] = []
    slot_ids: List[str] = []
    # slot_id -> the material's position WITHIN its scenario. Counted here because each child is
    # planned for ONE material, so a child cannot derive it from its own allotment: every child
    # would answer 0. Both the live grid and batch history seat a card at `(scenario_key, index)`,
    # and with every index 0 the second material of a scenario overwrote the first -- a 3x2 batch
    # rendered three cards and reported 「已完成 3/6，其余未能生成」 while all six sat in S3.
    seat_of: Dict[str, int] = {}
    seen_per_scenario: Dict[str, int] = {}

    delivers_sets = str(payload.get("action") or "generate") == "generate_sets"

    def add(child_payload: Dict[str, Any], scenario: str, materials: int) -> None:
        allotted = [
            "slot-%d" % (len(slot_ids) + offset + 1) for offset in range(materials)
        ]
        for slot in allotted:
            seat_of[slot] = seen_per_scenario.get(scenario, 0)
            seen_per_scenario[scenario] = seat_of[slot] + 1
        slot_ids.extend(allotted)
        if delivers_sets:
            # Per-child request id, batch-wide group id. See the docstring: the first is what slot state
            # is stored under and must be unique per invocation, the second is what decides which
            # candidates compete for one choice and must not be.
            child_payload["batch_id"] = "%s-%s" % (batch_id, allotted[0])
            child_payload["group_id"] = batch_id
        else:
            child_payload["batch_id"] = batch_id
        children.append(ChildPlan(len(children), child_payload, allotted, scenario,
                                  {slot: seat_of[slot] for slot in allotted}))

    for entry in requested:
        if not isinstance(entry, str):
            # Same reasoning as a bad count: forward it and let the backend reject it by name.
            add(dict(base, scenarios=[entry]), str(entry), 1)
            continue
        for _ in range(count_for(entry)):
            add(dict(base, scenarios=[entry], counts={entry: 1}, count=1), entry, 1)

    custom = payload.get("custom_scenario")
    if custom:
        if isinstance(custom, dict):
            try:
                repeat = max(1, int(custom.get("count") or 1))
            except (TypeError, ValueError):
                repeat = 1
            one = dict(custom, count=1)
        else:
            repeat, one = 1, custom
        for _ in range(repeat):
            add(dict(base, scenarios=[], custom_scenario=one), "custom", 1)

    return children, slot_ids


def launch_order(children: List[ChildPlan]) -> List[ChildPlan]:
    """The order to *start* children in: one per scenario, round-robin, until all are started.

    Slot ids stay exactly as `plan_children` allotted them -- they are the frontend's card grid and
    must not move. What moves is who gets a worker first, and with a concurrency cap those are
    different things: the plan groups a scenario's materials together, so a 21-set batch of 3 sets
    each across 7 scenarios put the last scenario in slots 19-21 and it did not begin until two
    waves had finished. The user watched every catalogue scenario complete before their custom one
    started, which reads as "the custom one is stuck" -- and it is also the worst order for the
    results page, whose groups fill one at a time instead of together.

    Round-robin gives every scenario a material in the first wave, so all groups show progress from
    the start. Total wall time is unchanged: the same N invocations through the same gate.
    """
    by_scenario: Dict[str, List[ChildPlan]] = {}
    for child in children:
        by_scenario.setdefault(child.scenario, []).append(child)
    ordered: List[ChildPlan] = []
    while by_scenario:
        for scenario in list(by_scenario):
            queue = by_scenario[scenario]
            ordered.append(queue.pop(0))
            if not queue:
                del by_scenario[scenario]
    return ordered


def outcome_for_state(row: Dict[str, Any]) -> str:
    """One recorded slot state -> the merge's outcome word for it.

    Three inputs and three answers:

    * ``complete`` -> ``ok``. The set was delivered; a lost frame does not unmake it.
    * resumable (anything not terminal) -> ``pending``. The next invocation can carry it further, so
      calling it failed would report recoverable work as lost.
    * ``exhausted`` -> ``failed``. Terminal for the slot. Its position may still be refilled by a
      replacement slot, and that replacement is a slot of its own with its own row -- so reporting
      *this* one as failed does not under-report the position.

    ``resumable`` is read from the row when the row carries it, because on the wire it is the Runtime's
    own judgement (``delivery._slot_row`` states it) and preferring a local re-derivation would ignore
    the one authority on the question. The ``state`` fallback is not a legacy path: rows read back from
    storage come from ``SlotRecord.as_record()``, which stores ``state`` and no ``resumable`` flag, so a
    row fetched by ``web/slot_state.py`` always takes it. Hence ``TERMINAL_SLOT_STATES``, and hence the
    test that pins it against ``slot_store`` -- the fallback is exercised on every silent slot.
    """
    state = str(row.get("state") or "")
    if state == COMPLETE:
        return "ok"
    if row.get("resumable") is not None:
        return "pending" if row.get("resumable") else "failed"
    return "failed" if state in TERMINAL_SLOT_STATES or not state else "pending"


def best_row(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The row that describes a POSITION, out of the slot records for it.

    A position accumulates records: a slot that exhausted its candidate swaps hands over to a
    replacement, and both are stored. What the card shows is the position's outcome, so the records are
    ranked by how much they claim -- delivered beats still-working beats given-up -- and the winner is
    reported. Ties go to the newest, which is the replacement rather than what it replaced.
    """
    order = {"ok": 2, "pending": 1, "failed": 0}

    def rank(row: Dict[str, Any]):
        return (order.get(outcome_for_state(row), 0), float(row.get("created_at") or 0))

    return max(rows, key=rank)


class _Merge(object):
    """Aggregate state for one fanned-out batch. Mutated only from the event loop.

    **The counts are derived from the terminal events actually relayed, not from the children's own
    summaries.** That is the whole design of this class and it is not the obvious choice, so:

    Summing the children's `batch_completed` counts is one lost frame away from lying. A child whose
    connection closes after `material_completed` but before `batch_completed` has delivered a
    material the browser is rendering, while contributing 0 to the sum -- so `batch_completed` would
    say "0 succeeded" over a grid of cards, and the frontend would render `status: 'partial'` with
    `completed: 0`. Counting what was relayed makes the summary agree with the cards by
    construction: every slot that produced a terminal event is counted exactly once, and a slot that
    produced none is counted as a failure at the end, because the browser is otherwise left with a
    skeleton that never resolves.

    The children's summaries are still absorbed, for the fields that genuinely only they know:
    `degraded`, `refilled`, `stage_timings`, and the per-slot detail rows.
    """

    __slots__ = ("degraded", "refilled", "rows", "timings", "config", "outcomes", "requests")

    def __init__(self) -> None:
        self.degraded = 0
        self.refilled = 0
        # slot_id -> the child's own detail row for it, when one arrived.
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.timings: Dict[str, Dict[str, Any]] = {}
        self.config: Dict[str, Any] = {}
        # slot_id -> "ok" | "failed" | "skipped" | "pending". One entry per slot that reached a
        # terminal state; a dict rather than counters so a duplicate terminal event for one slot cannot
        # double-count. `pending` is the state Stage 4 added: a slot the Runtime has recorded as
        # resumable, which is neither delivered nor given up on.
        self.outcomes: Dict[str, str] = {}
        # batch_id -> the `request_completed` summary that child reported, for `generate_sets` children.
        # Keyed by the child's own request id because that is what a resumption addresses.
        self.requests: Dict[str, Dict[str, Any]] = {}

    def record(self, slot_id: str, outcome: str) -> None:
        self.outcomes.setdefault(slot_id, outcome)

    def absorb_request(self, child: "ChildPlan", summary: Dict[str, Any]) -> None:
        """Fold one `generate_sets` child's terminal summary in, including its per-slot states.

        This is what makes a checkpointed slot distinguishable from a stuck one without an S3 read:
        the child that owns the state says so on the wire (`delivery._slot_row`). Storage is consulted
        only for a child that never answered at all.

        The child's rows are collapsed to one per POSITION, by `best_row`, rather than renamed one by
        one. A child is planned for one material and may hold several records for it -- an exhausted slot
        and the replacement that delivered -- so mapping them individually would either invent cards the
        frontend never planned or report the abandoned record over the delivered one.
        """
        batch_id = str(child.payload.get("batch_id") or "")
        if batch_id:
            self.requests[batch_id] = dict(summary)
        rows = [row for row in (summary.get("slots") or [])
                if isinstance(row, dict) and row.get("slot_id")]
        if not rows or not child.slot_ids:
            return
        slot_id = child.slot_ids[0]
        row = best_row(rows)
        self.record(slot_id, outcome_for_state(row))
        merged = dict(self.rows.get(slot_id) or {})
        merged.update({k: v for k, v in row.items() if k != "slot_id"})
        self.rows[slot_id] = merged

    def summarise(self, slot_ids: List[str], scenarios: Dict[str, str],
                  states: Optional[Dict[str, Dict[str, Any]]] = None,
                  exact_count: bool = False) -> Dict[str, Any]:
        """The summary counts and per-slot rows, over every slot the batch PLANNED.

        Driven by the plan rather than by what arrived, so the summary has exactly one row per card
        the browser drew.

        **A slot with no terminal event is `failed` only when nothing is recorded about it.** That used
        to be unconditional, and the reasoning was sound while a slot could not outlive one SSE stream:
        a child that produced neither a material nor a stated failure had produced nothing, and a clean
        batch drawn over a spinning card is worse than an honest failure. `generate_sets` breaks the
        premise -- a slot is a persistent record now, and a request that ran out of clock is resumable --
        so `states` (from `web/slot_state.py`, consulted only for the silent slots) can answer
        `pending` instead, and a resumable slot is reported as such rather than as lost work.

        `exact_count` comes from the PLAN, not from what arrived. It has to: a `generate_sets` batch
        whose children all died reported no `request_status` at all while it was inferred from the
        children's summaries, which is precisely the batch that most needs to say `incomplete` -- the
        frontend would have been handed a batch that never states whether the request it made was
        delivered, on the one path where the answer is definitely no.
        """
        for slot_id in slot_ids:
            if slot_id in self.outcomes:
                continue
            row = (states or {}).get(slot_id)
            if row is None:
                # Nothing arrived and nothing is recorded: the original case, and still `failed`.
                self.outcomes[slot_id] = "failed"
                continue
            self.outcomes[slot_id] = outcome_for_state(row)
            merged = dict(self.rows.get(slot_id) or {})
            merged.update({k: v for k, v in row.items() if k != "slot_id"})
            self.rows[slot_id] = merged
        outcomes = [self.outcomes[slot_id] for slot_id in slot_ids]
        summary = {
            "succeeded": outcomes.count("ok"),
            "failed": outcomes.count("failed"),
            "skipped": outcomes.count("skipped"),
            "slots": [self._row(slot_id, scenarios) for slot_id in slot_ids],
        }
        if exact_count:
            summary.update(self.request_status(slot_ids, outcomes))
        return summary

    def request_status(self, slot_ids: List[str], outcomes: List[str]) -> Dict[str, Any]:
        """The exact-count fields, added only for a `generate_sets` batch.

        Absent on a plain `generate` batch rather than zeroed, because these describe a contract that
        path does not make: `generate` may legitimately deliver fewer materials than asked, so a
        `request_status: "incomplete"` on one of its batches would report a normal outcome as a
        shortfall.

        `succeeded` requires N -- every planned slot delivered -- and nothing else does. A batch with any
        resumable slot is `incomplete` and a batch with any system fault is `system_failure`, with the
        count checked first for the reason `delivery._status` gives: a delivery that reached N is not
        demoted by a fault on a position that was later refilled.
        """
        faults = [fault for summary in self.requests.values()
                  for fault in (summary.get("system_faults") or [])]
        stated = {str(summary.get("status") or "") for summary in self.requests.values()}
        if outcomes.count("ok") >= len(slot_ids) and slot_ids:
            status = "succeeded"
        elif faults or "system_failure" in stated:
            # The children's own word for it, not re-derived: a child reports `system_failure` for
            # causes the web tier cannot see (storage refusing a write, a validator that is absent), and
            # inferring the status from slot states alone would report those as merely incomplete.
            status = "system_failure"
        else:
            status = "incomplete"
        return {
            "request_status": status,
            "requested": len(slot_ids),
            "delivered": outcomes.count("ok"),
            # Slots the next invocation could carry further, by the child's own account. A non-empty
            # list with `request_status: "incomplete"` is the checkpoint case; an empty one with the
            # same status means the shortfall is not resumable.
            "resumable_slots": [slot_ids[i] for i, outcome in enumerate(outcomes)
                                if outcome == "pending"],
            "system_faults": faults,
            # The per-child request ids, so an operator (or a resume) can address the requests this
            # batch was made of. The browser batch id is not one of them -- see `plan_children`.
            "request_ids": sorted(self.requests),
        }

    def _row(self, slot_id: str, scenarios: Dict[str, str]) -> Dict[str, Any]:
        """One summary row: the child's detail where it exists, the plan's knowledge where it does not.

        The child's row wins on every field it carries -- it knows the route, the timings and the real
        failure reason -- while `slot_id`, `scenario` and `ok` are overwritten from what was actually
        relayed, because those three are what the frontend joins on and the merge is authoritative
        about them.
        """
        outcome = self.outcomes[slot_id]
        row = dict(self.rows.get(slot_id) or {})
        row["slot_id"] = slot_id
        row["scenario"] = row.get("scenario") or scenarios.get(slot_id, "")
        row["ok"] = outcome == "ok"
        # `reason` is the child's own phrasing when it gave one; the outcome word is the fallback for
        # a slot that never spoke, where "failed" is all anyone can honestly say.
        #
        # A `pending` slot is the exception: it has not failed, so it gets no failure reason. Its
        # `last_failure` (why the last attempt stopped) is already on the row from the slot state, and
        # promoting that into `reason` would present a slot mid-retry as a finished failure -- which is
        # the misreport this whole change exists to remove.
        if outcome == "pending":
            row["reason"] = row.get("reason")
            row["pending"] = True
        else:
            row["reason"] = row.get("reason") or (None if outcome == "ok" else outcome)
        return row

    def absorb_completed(self, event: Dict[str, Any], rename) -> None:
        """Fold one child's `batch_completed` into the aggregate.

        Only the fields the child alone knows. `succeeded` / `failed` / `skipped` are deliberately
        NOT read from here -- see the class docstring.
        """
        for field in ("degraded", "refilled"):
            try:
                setattr(self, field, getattr(self, field) + int(event.get(field) or 0))
            except (TypeError, ValueError):
                pass
        for row in event.get("slots") or []:
            if isinstance(row, dict) and row.get("slot_id"):
                self.rows[rename(str(row["slot_id"]))] = dict(row)
        self._merge_timings(event.get("stage_timings") or {})

    def _merge_timings(self, incoming: Dict[str, Any]) -> None:
        """Combine per-stage aggregates without needing the raw samples.

        count/min/max compose exactly; the mean is recombined as a count-weighted average, which is
        the true mean of the union rather than a mean of means. Getting that wrong would quietly
        skew the one number `docs/timing.md` calibrates the estimate against.
        """
        for name, value in (incoming or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                count = int(value.get("count") or 0)
                low = float(value.get("min"))
                high = float(value.get("max"))
                mean = float(value.get("mean"))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            current = self.timings.get(name)
            if current is None:
                self.timings[name] = {"count": count, "min": low, "max": high, "mean": mean}
                continue
            total = current["count"] + count
            current["mean"] = round(
                (current["mean"] * current["count"] + mean * count) / total, 2
            )
            current["count"] = total
            current["min"] = min(current["min"], low)
            current["max"] = max(current["max"], high)


class FanOut(object):
    """Runs one batch as N independent invocations and yields one merged event stream.

    Construct per request. The executor is shared across requests (it is the concurrency cap) and
    is owned by the caller, so a batch that finishes does not tear down another batch's threads.
    """

    def __init__(self, runtime: Any, children: List[ChildPlan], slot_ids: List[str], *,
                 executor: ThreadPoolExecutor, concurrency: int = FANOUT_CONCURRENCY,
                 batch_id: str = "", slot_state: Optional[SlotStateReader] = None) -> None:
        self.runtime = runtime
        self.children = children
        self.slot_ids = slot_ids
        # Consulted only for a slot that produced no terminal event, and only for a `generate_sets`
        # batch. Injected rather than built here so a test can supply one and the deployed tier gets
        # the S3-backed reader without this class knowing about buckets.
        self.slot_state = slot_state if slot_state is not None else build_reader()
        # The id `web/app.py` minted for this batch, and the id `web/batch_history.py` keys the
        # record on. Carried here for one reason: it has to reach the browser in `batch_started`.
        # See the `events()` docstring.
        self.batch_id = batch_id
        self.executor = executor
        self.concurrency = max(1, min(concurrency, len(children) or 1))
        self._bodies: Dict[int, Any] = {}
        self._bodies_lock = threading.Lock()
        self._stopped = threading.Event()

    @staticmethod
    def _delivers_sets(child: ChildPlan) -> bool:
        return str(child.payload.get("action") or "") == "generate_sets"

    @property
    def delivers_sets(self) -> bool:
        """Whether this batch promised N complete sets, by the PLAN rather than by what arrived.

        Read from the children's payloads because that is what was actually sent, and `any` rather
        than `all` because a mixed batch is not a shape this module produces -- if one ever appeared,
        under-reporting the exact-count fields would hide the promise the request made.
        """
        return any(self._delivers_sets(child) for child in self.children)

    # ── slot identity ────────────────────────────────────────────────────────

    def _rename(self, child: ChildPlan, extra: Dict[str, str], slot_id: str) -> str:
        """Child-local slot id -> the batch-wide id this fan-out allotted it.

        A child that emits more slot ids than it was allotted gets fresh ids appended rather than
        colliding with another child's. That cannot happen while every child carries one material,
        and silently mapping two materials onto one card is a worse failure than an extra card.
        """
        mapped = extra.get(slot_id)
        if mapped is not None:
            return mapped
        used = len(extra)
        if used < len(child.slot_ids):
            mapped = child.slot_ids[used]
        else:
            mapped = "slot-%d-%d" % (child.index + 1, used + 1)
        extra[slot_id] = mapped
        return mapped

    # ── the child worker (runs on a thread) ──────────────────────────────────

    def _pump(self, child: ChildPlan, push) -> None:
        """Invoke one child and push its raw events onto the merge queue. Never raises.

        Runs on the dedicated executor. Everything here is blocking on purpose: the boto3 call, and
        then ``iter_lines`` for as long as the material takes.
        """
        try:
            if self._stopped.is_set():
                push({"type": "__child_aborted__"})
                return
            # A fresh session id per child, and one that names the card it belongs to.
            #
            # Fresh is the load-bearing half: a reused id routes to the one warm microVM and
            # serialises the batch this module exists to parallelise. The slot prefix is the cheap
            # half -- `runtimeSessionId` is what AgentCore stamps on its log streams, so it is the
            # only field that lets an operator get from "the third card failed" to that child's
            # logs. `new_session_id` pads past the API's 33-character floor either way.
            content_type, body, _ = self.runtime.invoke(
                child.payload,
                session_id=new_session_id("ielts-%s" % child.slot_ids[0]),
            )
            with self._bodies_lock:
                self._bodies[child.index] = body
            try:
                if SSE_CONTENT_TYPE not in content_type:
                    # A unary answer to `generate` means the backend rejected the payload before it
                    # became a generator (app.py returns a dict for an unknown action, and an
                    # error body is JSON). Surface it rather than dropping it on the floor.
                    push({"type": "__child_error__",
                          "detail": json.dumps(read_json(body))[:400]})
                    return
                for text in iter_sse_payloads(body):
                    if self._stopped.is_set():
                        return
                    try:
                        event = json.loads(text)
                    except ValueError:
                        # A non-JSON line on an event-stream response: the Runtime printed
                        # something. Report it as this child's failure instead of discarding it.
                        push({"type": "__child_error__", "detail": text[:400]})
                        continue
                    if isinstance(event, dict):
                        push(event)
            finally:
                closer = getattr(body, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 - close failures are not the batch's problem
                        pass
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point of this method
            push({"type": "__child_error__",
                  "detail": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
        finally:
            push(_CHILD_DONE)

    def close(self) -> None:
        """Stop the children. Called when the browser goes away, or on the way out.

        Closing the ``StreamingBody`` is what unblocks a thread parked in ``iter_lines``; the flag
        alone would only be noticed after the next line arrived, which for an abandoned batch could
        be minutes. The threads are not joined -- the response is already over, and waiting for a
        socket teardown would hold the loop for no benefit.
        """
        self._stopped.set()
        with self._bodies_lock:
            bodies = list(self._bodies.values())
            self._bodies.clear()
        for body in bodies:
            closer = getattr(body, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    # ── the merged stream ────────────────────────────────────────────────────


    def custom_label(self) -> str:
        """The text the user typed for the custom scenario, or "" when there was none."""
        for child in self.children:
            custom = child.payload.get("custom_scenario")
            if isinstance(custom, dict):
                hint = str(custom.get("prompt_hint") or custom.get("text") or "").strip()
                if hint:
                    return hint
            elif isinstance(custom, str) and custom.strip():
                return custom.strip()
        return ""

    async def events(self) -> AsyncIterator[Any]:
        """Yield one coherent batch's worth of events: one start, the middles, one completion.

        Yields event dicts, plus the `HEARTBEAT` sentinel whenever the merge has been silent for
        `HEARTBEAT_SECONDS`. The caller frames that as an SSE comment -- see `HEARTBEAT`.

        ``batch_started`` carries ``batch_id``, and that field is not decoration. The web tier mints
        the id (`new_batch_id`), keys the S3 record on it (`web/batch_history.py`) and plans the
        children with it -- but until this field existed it never told the browser, so
        `frontend/src/api/agentcore.ts` minted its own `batch-<ms36>-<n>` and put THAT in the URL.
        The two id spaces never intersected, so after a reload the history panel asked
        `/api/batch-history/batch-ms713fnc-1` about a batch recorded as `web-1785386619156-1` and got
        「没有找到批次 ... 的历史记录」 for a batch sitting in S3. Same class of bug as the
        `placeholderId` one: the frontend inventing an identifier the backend never issued.
        """
        total = len(self.slot_ids)
        waves = (total + self.concurrency - 1) // self.concurrency if total else 0
        yield {
            "type": "batch_started",
            # The authoritative id. Emitted first among the fields because everything downstream --
            # the URL, the history lookup, the candidate group keys -- has to agree with it.
            "batch_id": self.batch_id,
            "total": total,
            # 用户为自定义场景输入的原文。必须从这里传出去：`material.scenario` 是模型自己扩写
            # 的完整英文句（输入「餐厅点餐」会变成一整句 "A customer phones a restaurant to..."），
            # 拿它当标题就是把模型的改写当成用户的话。历史记录里也没有别处存过这段原文。
            "custom_label": self.custom_label(),
            # An upper bound the web tier can actually stand behind: each child gets its own 900s
            # wall, and at most `waves` of them run in series. The old value was one shared wall
            # for the whole batch, which is the constraint this change removed.
            "deadline_at": round(time.time() + PER_MATERIAL_WALL_SECONDS * max(waves, 1), 3),
            "config": {
                "fanout": "per_material_invoke",
                "children": len(self.children),
                "web_concurrency": self.concurrency,
                "per_material_wall_seconds": PER_MATERIAL_WALL_SECONDS,
            },
            "at": time.time(),
        }
        if total == 0:
            yield {"type": "batch_failed", "reason": "bad_request",
                   "detail": "no scenarios requested"}
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        gate = threading.Semaphore(self.concurrency)
        merge = _Merge()
        renames: Dict[int, Dict[str, str]] = {}
        remaining = len(self.children)
        # Known from the plan, so the summary can name a scenario even for a slot whose child said
        # nothing at all -- the frontend groups its cards by scenario, and a blank one would be
        # ungroupable.
        scenario_of = {
            slot_id: child.scenario for child in self.children for slot_id in child.slot_ids
        }

        def make_push(child: ChildPlan):
            def push(item: Any) -> None:
                # The only thread->loop handoff in this module. `call_soon_threadsafe` rather than
                # `run_coroutine_threadsafe` because `put_nowait` on an unbounded queue cannot
                # block, so there is nothing to await and no way for a child to deadlock on a
                # browser that stopped reading.
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, (child, item))
                except RuntimeError:
                    # Loop already closed: the request is over and nobody is listening.
                    self._stopped.set()
            return push

        def run(child: ChildPlan) -> None:
            # The gate is held for the child's whole lifetime, which is what caps concurrency:
            # a slot is a material in flight, not a queued task.
            with gate:
                self._pump(child, make_push(child))

        futures = [self.executor.submit(run, child) for child in launch_order(self.children)]

        try:
            while remaining > 0:
                try:
                    # A timeout rather than a plain `get()`: the merge is silent for as long as
                    # every in-flight child is silent, and a child says nothing between
                    # `generating` and its terminal frame -- measured at 96s on a real batch.
                    child, item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Nothing arrived. Keep the connection alive and go back to waiting; the loop
                    # condition is unchanged, so this cannot terminate the stream early.
                    yield HEARTBEAT
                    continue
                if item is _CHILD_DONE:
                    remaining -= 1
                    continue
                extra = renames.setdefault(child.index, {})

                def rename(slot_id: str, _child=child, _extra=extra) -> str:
                    return self._rename(_child, _extra, slot_id)

                for event in self._translate(child, item, merge, rename):
                    yield event

            yield dict(
                merge.summarise(self.slot_ids, scenario_of,
                                await self._recorded_states(merge),
                                exact_count=self.delivers_sets),
                type="batch_completed",
                degraded=merge.degraded,
                refilled=merge.refilled,
                stage_timings=merge.timings,
                at=time.time(),
            )
        finally:
            # Reached on normal completion, on a client disconnect (GeneratorExit) and on an
            # unexpected error. Children must not outlive the response either way: an orphaned
            # invocation keeps paying the model for output nobody will read.
            self.close()
            for future in futures:
                future.cancel()

    async def _recorded_states(self, merge: _Merge) -> Dict[str, Dict[str, Any]]:
        """Slot state from `_slots/` for the slots no terminal event covered. `{}` when none is needed.

        Reads nothing on the ordinary path -- a batch whose children all answered has every slot in
        `merge.outcomes`, so this returns immediately -- and reads nothing at all for a `generate`
        batch, which has no `_slots/` records to find. That matters because this runs while the
        browser is waiting for the last frame of a batch it has already seen the materials of.

        In a thread: an S3 GET is blocking, and the rule this whole module is built around is that
        nothing blocking runs on the loop. `run_in_threadpool`'s pool is anyio's default one, which the
        module docstring says not to take *children's* threads from -- the objection there is a token
        held for four minutes, and this is one GET per child at the very end of the batch.
        """
        missing = [child for child in self.children
                   if self._delivers_sets(child)
                   and any(slot_id not in merge.outcomes for slot_id in child.slot_ids)]
        if not missing or not self.slot_state.available:
            return {}

        def read() -> Dict[str, Dict[str, Any]]:
            found: Dict[str, Dict[str, Any]] = {}
            for child in missing:
                rows = self.slot_state.load_slots(str(child.payload.get("batch_id") or ""))
                if not rows or not child.slot_ids:
                    continue
                # The child's own slot ids, mapped onto this fan-out's. Not `self._rename`: that
                # allocates ids for unknown ones and mutates the per-child map, which is the event
                # loop's state.
                #
                # A `generate_sets` child is planned for ONE material, and its request may hold several
                # slot records for that one position -- an exhausted slot plus the replacements that
                # took over from it (`delivery._replacement_for`). So the records are collapsed to the
                # best outcome for the position rather than mapped positionally: an exhausted original
                # beside a completed replacement means the position was delivered, and reporting the
                # original's state would call a delivered set a failure.
                found[child.slot_ids[0]] = best_row(rows)
            return found

        from starlette.concurrency import run_in_threadpool

        try:
            return await run_in_threadpool(read)
        except Exception:  # noqa: BLE001 - an unread state is "unknown", never a broken stream
            LOG.warning("could not read slot state for the batch summary", exc_info=True)
            return {}

    def _translate(self, child: ChildPlan, event: Dict[str, Any], merge: _Merge,
                   rename) -> List[Dict[str, Any]]:
        """One child event -> zero or more merged events. Where the reconciliation happens."""
        kind = str(event.get("type") or "")

        if kind == "batch_started":
            # Swallowed: the merged stream already announced itself, and a second `batch_started`
            # would reset the frontend's total to this child's 1. The child's `config` names the
            # model and region, which is worth keeping once for an operator.
            if not merge.config and isinstance(event.get("config"), dict):
                merge.config = dict(event["config"])
            return []

        if kind == "batch_completed":
            merge.absorb_completed(event, rename)
            return []

        if kind == "request_completed":
            # A `generate_sets` child's terminal event. Swallowed like `batch_completed`, and for the
            # same reason: one merged batch has one terminal event, and N children each announcing a
            # finished request would tell the frontend the batch ended N times. Its per-slot states are
            # folded into the merge instead, where they decide the merged summary's counts.
            merge.absorb_request(child, event)
            return []

        if kind == "__child_aborted__":
            # The batch was abandoned before this child started. No card, no count: the response
            # is already gone.
            return []

        if kind in ("__child_error__", "batch_failed"):
            # The child never produced a usable stream (invoke refused, connection died, backend
            # rejected the payload). Report it per slot so the other children carry on, and record
            # it as failed so `batch_completed` cannot present a partial batch as a success.
            #
            # Skipped if the slot already reached a terminal state: a child that delivered its
            # material and *then* had its connection die must not have that material replaced by a
            # failure card. `record` is setdefault-based, but the frame would still be relayed.
            reason = str(event.get("reason") or "runtime_invoke_failed")
            detail = event.get("detail")
            # Logged, not only relayed. This module used to have no logging at all, so a child that
            # died left the user looking at 「其余未能生成」 while the task log held nothing to
            # explain it -- the failure reached the browser and nowhere else. Two investigations
            # stalled on exactly that. WARNING because a lost material is the user's problem too.
            LOG.warning("fanout child %d (%s, slots %s) failed: reason=%s detail=%s",
                        child.index, child.scenario, ",".join(child.slot_ids), reason,
                        str(detail)[:400])
            out: List[Dict[str, Any]] = []
            for slot_id in child.slot_ids:
                if slot_id in merge.outcomes:
                    continue
                merge.record(slot_id, "failed")
                merge.rows[slot_id] = {"slot_id": slot_id, "scenario": child.scenario,
                                       "ok": False, "reason": reason}
                out.append({"type": "material_failed", "slot_id": slot_id,
                            "scenario": child.scenario, "ok": False, "reason": reason,
                            "detail": detail, "at": time.time()})
            return out

        if event.get("slot_id"):
            event = dict(event)
            slot_id = rename(str(event["slot_id"]))
            event["slot_id"] = slot_id
            # `index` is the material's position WITHIN its scenario, and it has to travel with the
            # event because two separate consumers key on it:
            #
            #   * the live grid seats a material at `(scenario_key, index)` (resultSlots.ts), and
            #   * batch history replays that seating after a reload.
            #
            # Without it both fell back to 0, so the second material of a scenario overwrote the
            # first: a 3x2 batch rendered as three cards and read as "已完成 3/6，其余未能生成"
            # even though all six were generated and all six sidecars were in S3.
            #
            # The child knows this for free -- `slot_ids` is exactly its own allotment, in order --
            # so it is a lookup rather than a guess.
            if slot_id in child.seats:
                event["index"] = child.seats[slot_id]
            # The summary counts come from here, not from the children's own summaries -- see
            # `_Merge`. `material_failed` with `skipped: true` is the time-budget case, which the
            # frontend distinguishes and the summary must not report as an outright failure.
            if kind == "material_completed":
                merge.record(slot_id, "ok")
            elif kind == "material_failed":
                merge.record(slot_id, "skipped" if event.get("skipped") else "failed")
        return [event]


def build_executor(concurrency: int = FANOUT_CONCURRENCY) -> ThreadPoolExecutor:
    """The dedicated pool the children run on.

    Sized to the concurrency cap and no larger: the semaphore already refuses to start a seventh
    material, so extra workers could only sit idle. Kept out of anyio's default threadpool for the
    reason in the module docstring -- a child parked for four minutes must not be able to consume a
    token that `/healthz` or a static file needs.
    """
    return ThreadPoolExecutor(
        max_workers=max(1, concurrency), thread_name_prefix="ielts-fanout"
    )
