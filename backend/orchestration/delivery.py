"""Exact-count delivery: N complete material+question sets, or an honest non-success (§8.2).

The state machine over :mod:`slot_store` records. It is the answer to a requirement the material-only
pipeline was explicitly built the other way round for: ``batch.py`` states "fewer materials than asked
for beats a 504 that loses all of them", and the product owner has since ruled the opposite --
**a request for N is satisfied by N complete sets or by nothing that calls itself satisfied**
(§8.1, §8.2(3), decision 13).

``batch.py`` is left alone. It still schedules the material-only path that is deployed today, and
rewriting its budget prose in place would leave the running system half-converted while this stage is
unmerged. This module is the layer that owns the new contract; wiring the Runtime action to it is
stage 11's step, not this one.

**What the two levels of bound actually bound** (§8.2(2)). They are not two sizes of the same dial:

* *inner* -- targeted repair of the artifact in hand. Three generation attempts inside ``run_one``,
  two revision rounds inside ``run_questions``. Both already enforced where the work happens; this
  module counts them into the slot record and never re-imposes them.
* *outer* -- abandoning the material and drawing another. Spent by every verdict that says "this
  material is the problem": a feasibility ``REGENERATE_MATERIAL``, an unassessable script, a
  material-stage failure that produced no content -- and a question stage that failed *twice* on one
  qualified material.

**A qualified material is never regenerated because the question stage failed once** (§8.2(1)). The
``material_done`` transition is a write, and the question stage runs from what that write records, so
both a crash and a not-deliverable verdict re-enter the question stage on the same material
(``question_restarts``); only the second failure on that material spends a candidate swap.

Getting this backwards was measured, not feared. A question verdict reads as a statement about the
material, and charging the first one outward cost batch ``web-1786166271869-1`` 43 minutes: 11
materials for 1 delivered set, because 8 rejections each withdrew a material that had passed its blind
audit and its feasibility preflight. What made the swap wrong is that the restart is not a third
revision -- the question loop re-enters at generation, so it writes a genuinely new set against the
same blueprint, which is far cheaper than a new blueprint and no less likely to succeed. See
:func:`_questions_not_deliverable`.

**Exhausting the swap budget opens a replacement slot; it never lowers the bar** (§8.2(3)). The
second-round design here was a delivery of "the best set we managed, with its defects listed", and
that was withdrawn for being the same thing as under-delivering, written more politely.

**Three terminal states, and only one of them is success.** ``succeeded`` requires N complete sets on
disk. Anything else is ``incomplete`` (resumable -- the clock ran out) or ``system_failure`` (not
resumable without a human). There is deliberately no state meaning "fewer than N, but finished":
§8.2(5) removes that exit, and a vocabulary that lacks the word is what stops a caller inventing it.

**Resumption reads storage, not memory** (§8.2(4)). ``run_request`` with a ``batch_id`` that already
has a request record continues from the slot records, which is the same code path a first run takes
with none -- so the resumable path is not a rarely-exercised branch.

**Two ids, because a fanned-out child is one request and part of one batch.** ``batch_id`` names the
*request*: it is the key every slot record hangs off and the name the next invocation resumes by, so
each child of a web fan-out needs its own (they would otherwise all write ``slot-1`` to one key and
overwrite each other). ``group_id`` names the *candidate group*: which materials compete for one
user choice, which is still "the same scenario in the same browser batch" and therefore shared across
children. Collapsing them would either break resumption or break selection, in opposite directions.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..deterministic.feasibility import (
    CANNOT_DECIDE,
    PASS,
    PASS_WITH_JUSTIFICATION,
    REGENERATE_MATERIAL,
)
from . import events
from .batch import REFILLABLE_FAILURES
from .loop import is_assessable, run_one
from .question_loop import run_questions
from .slot_store import (
    COMPLETE,
    EXHAUSTED,
    INCOMPLETE,
    MATERIAL_DONE,
    MATERIAL_PENDING,
    QUESTIONS_PENDING,
    RUNNING,
    SUCCEEDED,
    SYSTEM_FAILURE,
    SlotPersistenceError,
    SlotRecord,
    build_slot_store,
    describe_slot_store,
)

__all__ = [
    "DeliveryBudget",
    "MAX_CANDIDATE_SWAPS",
    "MAX_QUESTION_RESTARTS",
    "MAX_REPLACEMENT_SLOTS",
    "QUESTION_P95_SECONDS",
    "run_request",
    "stream_request",
]

# --- the outer bound (§8.2(2)) ---------------------------------------------------------------
# How many materials one slot may draw before it gives up and hands its position to a replacement.
# Two, matching the number ``MAX_REFILL_ROUNDS`` already used for a narrower purpose -- but this is a
# different quantity and gets its own name rather than reusing that constant: refills answered "the
# audit could not read this script", and a candidate swap answers "this material cannot carry ten fair
# questions", which is a judgement three model calls deep.
MAX_CANDIDATE_SWAPS = int(os.environ.get("IELTS_MAX_CANDIDATE_SWAPS", "2"))

# How many replacement slots one position may open before the request stops claiming the position is
# still progressing. Bounded, and the bound does NOT create a "few is fine" exit: the request goes to
# ``incomplete``, never ``succeeded``. Unbounded replacement would not deliver more -- it would spin
# until the platform killed the invocation, and the honest version of that is a recorded incomplete
# request the next invoke can pick up.
MAX_REPLACEMENT_SLOTS = int(os.environ.get("IELTS_MAX_REPLACEMENT_SLOTS", "2"))

# How many times the question stage may be re-entered on a material that already qualified -- after a
# crash OR after a not-deliverable verdict, which is the cheaper of the two ways to answer either
# (`_questions_not_deliverable`). One, per material: a first failure deserves a fresh attempt against
# the same qualified blueprint, and a second failure on the same input has become evidence rather than
# noise -- a crash that repeats is a defect somebody has to look at, and a verdict that repeats is the
# statement about the material that spends the swap.
MAX_QUESTION_RESTARTS = int(os.environ.get("IELTS_MAX_QUESTION_RESTARTS", "1"))

# What the question stage costs, for the budget check before starting one.
#
# **Not measured, and deliberately not derived from ``P95_PER_MATERIAL``** (risk 4). The measured 240s
# covers material generation only; the question stage is generate + validate + audit + up to two
# rounds of revise + revalidate + re-audit, and the one real run took ~170s for a single round. 420s
# is a conservative placeholder for two rounds plus overhead, and it is an environment knob precisely
# because the right number comes from the timing table once the chain has run end to end.
QUESTION_P95_SECONDS = float(os.environ.get("IELTS_P95_QUESTIONS", "420"))

HARD_LIMIT_SECONDS = float(os.environ.get("IELTS_HARD_LIMIT", "900"))
SAFETY_MARGIN_SECONDS = float(os.environ.get("IELTS_SAFETY_MARGIN", "90"))
P95_PER_MATERIAL = float(os.environ.get("IELTS_P95_PER_MATERIAL", "240"))
REVISION_COST_SECONDS = float(os.environ.get("IELTS_REVISION_COST", "120"))

# How many slots advance at once. Clamped to the number of slots, as in ``batch.py`` -- and for the
# same reason: a config event advertising six-way parallelism for a two-slot request is a misleading
# number in exactly the log an operator reads first.
MAX_CONCURRENCY = int(os.environ.get("IELTS_CONCURRENCY", "6"))

# Slot-stopping reasons that are about the system rather than about the material. Kept as a frozenset
# beside ``REFILLABLE_FAILURES`` rather than as an ``else`` branch, because the two answer different
# questions and the wrong default is costly in opposite directions: an unrecognised material fault
# treated as a system fault fails a whole request that a swap would have saved, and a system fault
# treated as a material fault burns the swap budget on a validator that is simply absent.
SYSTEM_FAILURE_REASONS = frozenset({"validator_unavailable"})


class DeliveryBudget(object):
    """Wall-clock budget for one INVOCATION of a request, which may carry several stages.

    Three predicates rather than ``batch.Budget``'s two, because this invocation starts two kinds of
    work with very different costs and the material number must not be used to authorise a question
    stage: at the measured material p95 of 240s a check that says yes would routinely be starting a
    420s stage with 300s left, which is precisely the "start work that cannot finish" that ``may_start``
    exists to prevent.

    Refusing is not a failure here. A refusal writes a checkpoint and returns the request as
    ``incomplete``, which the next invocation resumes (§8.2(4)) -- so the cost of stopping early is one
    extra invocation, and the cost of starting too late is a stage killed mid-flight with nothing
    recorded.
    """

    __slots__ = ("started", "deadline", "p95_material", "p95_questions", "revision_cost")

    def __init__(
        self,
        hard_limit: float = HARD_LIMIT_SECONDS,
        margin: float = SAFETY_MARGIN_SECONDS,
        p95_material: float = P95_PER_MATERIAL,
        p95_questions: float = QUESTION_P95_SECONDS,
        revision_cost: float = REVISION_COST_SECONDS,
        now: Optional[float] = None,
    ) -> None:
        self.started = now if now is not None else time.monotonic()
        self.deadline = self.started + max(hard_limit - margin, 0.0)
        self.p95_material = p95_material
        self.p95_questions = p95_questions
        self.revision_cost = revision_cost

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def may_start_material(self) -> bool:
        return self.remaining() > self.p95_material

    def may_start_questions(self) -> bool:
        return self.remaining() > self.p95_questions

    def may_revise(self) -> bool:
        return self.remaining() > self.revision_cost


class _Paused(Exception):
    """The budget refused the next stage. Carries no data: the checkpoint is already written.

    An exception rather than a return code because a pause can happen at either of two points inside
    one slot's loop, and both must leave the slot exactly as the last completed transition left it.
    A sentinel return value would have to be threaded through the caller of each, and a caller that
    forgets to check it would run the next stage anyway -- with no clock left.
    """


class _Context(object):
    """Everything one slot's state machine needs that is not its own record."""

    __slots__ = ("store", "budget", "queue", "batch_id", "group_id", "scenarios", "run_material",
                 "run_question_stage", "faults", "paused")

    def __init__(self, store, budget, queue, batch_id, scenarios,
                 run_material, run_question_stage, group_id=None) -> None:
        self.store = store
        self.budget = budget
        self.queue = queue
        self.batch_id = batch_id
        # The candidate-group namespace, which is NOT the request id. See the module docstring: with
        # one Runtime invocation per child, every child has its own `batch_id` (its slot records must
        # not collide) while the materials of one browser batch must still compete for one user
        # choice. Defaults to `batch_id`, which is right for a single-request run.
        self.group_id = group_id or batch_id
        # slot_id -> the Scenario that slot generates from. A replacement slot inherits its
        # predecessor's scenario: the position in the request is what is being refilled, and a
        # replacement that quietly generated a different scenario would answer a request the user
        # never made.
        self.scenarios: Dict[str, Any] = dict(scenarios)
        self.run_material = run_material
        self.run_question_stage = run_question_stage
        # slot_id -> stated system fault. Collected rather than raised so one faulted slot does not
        # abandon the sets its siblings have already completed.
        self.faults: Dict[str, str] = {}
        self.paused = False

    async def emit(self, slot_id: str, name: str, detail: Optional[Dict[str, Any]] = None) -> None:
        await self.queue.put(
            events.stage(slot_id, getattr(self.scenarios.get(slot_id), "id", ""), name, detail))

    def save(self, record: SlotRecord) -> None:
        self.store.save_slot(record)


async def run_request(
    scenarios: List[Any],
    batch_id: str,
    store: Any = None,
    budget: Optional[DeliveryBudget] = None,
    emit: Optional[Callable] = None,
    concurrency: Optional[int] = None,
    run_material: Optional[Callable] = None,
    run_question_stage: Optional[Callable] = None,
    group_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Deliver one complete set per entry in ``scenarios``, or return an honest non-success.

    ``len(scenarios)`` IS N: the request asks for one complete material+question set per entry, and
    the count is never read from a separate field that could disagree with the list.

    Called again with the same ``batch_id`` after an ``incomplete`` result, it resumes: slots already
    ``complete`` are counted and not re-run, and a slot sitting at ``material_done`` starts at its
    question stage rather than at generation (§8.2(1), §8.2(4)).

    ``run_material`` / ``run_question_stage`` are injection points for tests, defaulting to
    :func:`loop.run_one` and :func:`question_loop.run_questions`. Parameters rather than module globals
    to patch, because the three key paths this stage has to prove -- replacement slots, cross-invoke
    resumption, and fewer-than-N never succeeding -- are all about *what the runner does with* a
    stage's answer, and each needs to script those answers precisely.

    ``group_id`` namespaces the candidate group and defaults to ``batch_id``; see the module docstring
    for why the two are separate.

    Returns the request summary. It is also written to ``_slots/{batch_id}/request.json`` before being
    returned, so the status a caller reports and the status stored are the same document rather than
    two renderings of it.

    ``emit`` is awaited for every event as it happens, which is what makes this usable behind SSE; the
    return value is the same summary the last event carries. A caller that wants the events *as a
    stream* rather than through a callback uses :func:`stream_request`, which is this function with the
    queue exposed instead of drained.
    """
    emit = emit or _noop
    summary: Dict[str, Any] = {}
    async for event in stream_request(
            scenarios, batch_id, store=store, budget=budget, concurrency=concurrency,
            run_material=run_material, run_question_stage=run_question_stage, group_id=group_id):
        await emit(event)
        if event.get("type") == "request_completed":
            summary = {k: v for k, v in event.items() if k not in ("type", "at")}
    return summary


async def stream_request(
    scenarios: List[Any],
    batch_id: str,
    store: Any = None,
    budget: Optional[DeliveryBudget] = None,
    concurrency: Optional[int] = None,
    run_material: Optional[Callable] = None,
    run_question_stage: Optional[Callable] = None,
    group_id: Optional[str] = None,
):
    """:func:`run_request` as an async generator: every event yielded as it occurs.

    The Runtime action needs this shape rather than a callback. AgentCore picks JSON or SSE from
    whether the handler is an async generator (``backend/app.py``), and the events have to reach the
    browser *while* the request runs -- a request that yielded nothing for six minutes would be
    dropped by the first intermediary with an idle-read timeout.

    The terminal ``request_completed`` event carries the summary, so no consumer needs a second
    channel to learn the status. It is emitted on every path, including the two persistence failures:
    a stream that ends without it is a lost connection, and that must not be confusable with a
    request that finished short.
    """
    store = store or build_slot_store()
    budget = budget or DeliveryBudget()
    wanted = len(scenarios)

    queue: asyncio.Queue = asyncio.Queue()
    ctx = _Context(store, budget, queue, batch_id, {},
                   run_material or run_one, run_question_stage or run_questions,
                   group_id=group_id)

    records = _plan(store, batch_id, scenarios, ctx)
    try:
        store.save_request(_request_document(
            batch_id, wanted, RUNNING, records, ctx,
            store_backend=describe_slot_store(store)))
    except SlotPersistenceError as exc:
        # Nothing has been generated yet, and nothing may be: a request that cannot record its own
        # progress cannot honour §8.2(1) or §8.2(4), and running anyway would produce work whose only
        # trace is an SSE stream nobody can resume from.
        yield events.request_completed(
            _summary(batch_id, wanted, SYSTEM_FAILURE, [], ctx, records,
                     detail="slot state could not be persisted: %s" % exc))
        return

    async for row in _pump(queue, _advance_all(records, ctx, concurrency)):
        yield row

    delivered: List[Dict[str, Any]] = []
    for record in store.list_slots(batch_id):
        if record.state == COMPLETE and record.material_id:
            payload = store.load_questions(record.material_id)
            if payload is not None:
                delivered.append({"slot_id": record.slot_id,
                                  "material_id": record.material_id,
                                  "questions": payload})

    final = store.list_slots(batch_id)
    status = _status(final, wanted, ctx)
    summary = _summary(batch_id, wanted, status, delivered, ctx, final)
    try:
        store.save_request(_request_document(
            batch_id, wanted, status, final, ctx,
            store_backend=describe_slot_store(store)))
    except SlotPersistenceError as exc:
        # The work happened; only the final status write failed. Reported as a system failure over the
        # top of whatever the run achieved, because a summary a resumption cannot read is a summary
        # that will be recomputed from slot records -- and if those writes were failing too, the count
        # this function just returned is not one anybody should trust.
        summary["status"] = SYSTEM_FAILURE
        summary["detail"] = "request status could not be persisted: %s" % exc
    yield events.request_completed(summary)


def _plan(store, batch_id: str, scenarios: List[Any], ctx: _Context) -> List[SlotRecord]:
    """The slot records to advance, from storage where they exist and new where they do not.

    A resumption finds every record already written, including replacements, and returns the ones not
    yet terminal. A first run finds none and creates one per requested scenario.

    Terminal records are excluded from the returned work list but their scenarios are still registered
    in the context: an ``exhausted`` slot is what a replacement inherits its scenario from, and the
    summary needs a scenario id for every row.
    """
    existing = store.list_slots(batch_id)
    if not existing:
        made = [SlotRecord(batch_id, "slot-%d" % (index + 1), getattr(scenario, "id", ""))
                for index, scenario in enumerate(scenarios)]
        for record, scenario in zip(made, scenarios):
            ctx.scenarios[record.slot_id] = scenario
            store.save_slot(record)
        return made

    by_id = {getattr(scenario, "id", ""): scenario for scenario in scenarios}
    pending: List[SlotRecord] = []
    for record in existing:
        # Matched by scenario id rather than by slot index: replacement slots have no index in the
        # original request, and a positional match would hand `slot-2r1` whatever scenario happened to
        # sit second in the list.
        ctx.scenarios[record.slot_id] = by_id.get(record.scenario_id)
        if record.state not in (COMPLETE, EXHAUSTED):
            pending.append(record)
    return pending


async def _advance_all(records: List[SlotRecord], ctx: _Context,
                       concurrency: Optional[int]) -> None:
    """Advance every slot, opening replacement slots as positions exhaust.

    Waves rather than a dynamic worker pool. A replacement slot only exists once its predecessor has
    exhausted, so the work list genuinely does arrive in generations, and a barrier between them costs
    a little wall time in exchange for the scheduling being readable -- which matters here because the
    thing being scheduled is the requirement that N sets exist.

    Bounded by ``MAX_REPLACEMENT_SLOTS + 1`` waves, so it terminates on the count as well as on the
    clock -- the property ``MAX_REFILL_ROUNDS`` provides in ``batch.py``, for the same reason: a test
    with a generous fake clock must not be able to spin here.
    """
    wave = records
    limit = max(1, min(concurrency or MAX_CONCURRENCY, len(wave) or 1))
    semaphore = asyncio.Semaphore(limit)

    for _ in range(MAX_REPLACEMENT_SLOTS + 1):
        if not wave:
            break
        await asyncio.gather(*[_advance_slot(record, ctx, semaphore) for record in wave],
                             return_exceptions=False)
        if ctx.paused:
            # The clock stopped this invocation. Opening replacements now would create slots that
            # cannot be started, and a slot record whose first state was written by a run that had no
            # time to act on it is noise in the next invocation's plan.
            break
        wave = [replacement for record in wave
                for replacement in _replacement_for(record, ctx)]


def _replacement_for(record: SlotRecord, ctx: _Context) -> List[SlotRecord]:
    """A replacement slot for an exhausted position, if this position may still have one.

    Returns a list so the caller can flatten without a None check -- the two answers are "one
    replacement" and "no replacement", and the second is not an error.

    A slot stopped by a system fault gets NO replacement. Retrying a position whose failure was a
    validator that is not there, or storage that refused a write, would spend a full material on a
    fault that has nothing to do with materials, and would convert a diagnosable ``system_failure``
    into a request that merely looks slow.
    """
    if record.state != EXHAUSTED or record.system_fault:
        return []
    generation = _generation_of(record)
    if generation >= MAX_REPLACEMENT_SLOTS:
        return []
    # Named off the POSITION, not off the slot being replaced: `slot-1r2` rather than `slot-1r1r1`.
    # The chain's identity is the position in the request, and a name that accumulated one suffix per
    # generation would make `_generation_of` a parse of unbounded depth.
    replacement = SlotRecord(
        record.batch_id, "%sr%d" % (_root_of(record.slot_id), generation + 1),
        record.scenario_id, replaces=record.slot_id)
    ctx.scenarios[replacement.slot_id] = ctx.scenarios.get(record.slot_id)
    record.replaced_by = replacement.slot_id
    ctx.store.save_slot(record)
    ctx.store.save_slot(replacement)
    return [replacement]


def _root_of(slot_id: str) -> str:
    """``slot-2r1`` -> ``slot-2``. The position a replacement chain belongs to."""
    return slot_id.split("r")[0] if "r" in slot_id.split("-")[-1] else slot_id


def _generation_of(record: SlotRecord) -> int:
    """How many replacements deep this slot already is. 0 for an original slot."""
    tail = record.slot_id.split("-")[-1]
    if "r" not in tail:
        return 0
    try:
        return int(tail.split("r")[1])
    except (IndexError, ValueError):
        return 0


async def _advance_slot(record: SlotRecord, ctx: _Context, semaphore: asyncio.Semaphore) -> None:
    """Run one slot's state machine until it is terminal, paused, or faulted.

    Every transition is written before the next stage starts, so the state on disk is always a state
    some stage actually reached -- never one this process intends to reach. That is what makes a
    process death mid-material recoverable as ``material_pending`` rather than as a slot that claims a
    material it does not have.
    """
    async with semaphore:
        try:
            while record.state not in (COMPLETE, EXHAUSTED):
                if record.state == MATERIAL_PENDING:
                    await _do_material(record, ctx)
                else:
                    await _do_questions(record, ctx)
        except _Paused:
            ctx.paused = True
        except SlotPersistenceError as exc:
            # Storage refused. Not chargeable to the material, and not retryable here: the next write
            # would be the one that just failed. The slot stops with the fault stated so the request
            # reports `system_failure` rather than a count nothing recorded.
            _fault(record, ctx, "slot_state_unwritable", str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - one slot's crash must not lose its siblings' sets
            _fault(record, ctx, "unhandled_error",
                   "%s: %s" % (type(exc).__name__, str(exc)[:300]))


async def _do_material(record: SlotRecord, ctx: _Context) -> None:
    """Generate and qualify one material, or spend a candidate swap deciding not to."""
    if not ctx.budget.may_start_material():
        _checkpoint(record, ctx, "material")
        raise _Paused()

    scenario = ctx.scenarios.get(record.slot_id)
    if scenario is None:
        # A resumption whose request no longer carries this slot's scenario. Not a material fault:
        # nothing about the material is known. Reported rather than guessed, because substituting any
        # other scenario would answer a request the user did not make.
        _fault(record, ctx, "scenario_missing",
               "slot %s references scenario %r, which is not in this request"
               % (record.slot_id, record.scenario_id))
        return

    await ctx.emit(record.slot_id, "material_started",
                   {"swaps_used": record.attempts["candidate_swaps"]})
    result = await ctx.run_material(
        scenario, record.slot_id,
        lambda name, detail=None: ctx.emit(record.slot_id, name, detail),
        ctx.budget.may_revise)

    verdict = _material_verdict(result)
    if verdict is not None:
        reason, detail, system = verdict
        if system:
            _fault(record, ctx, reason, detail)
            return
        _swap_candidate(record, ctx, reason, detail)
        return

    # Qualified. Registered and recorded as `material_done` BEFORE the question stage is entered --
    # this write is the checkpoint §8.2(1) is about, and a question stage that started first would be
    # able to lose a material by crashing.
    _register_material(result, scenario, record, ctx)
    record.state = MATERIAL_DONE
    record.material_id = result.material_id
    record.group_key = result.group_key
    record.last_failure = None
    ctx.save(record)
    await ctx.emit(record.slot_id, "material_done",
                   {"material_id": result.material_id,
                    "feasibility": (result.feasibility or {}).get("outcome")})


def _material_verdict(result: Any) -> Optional[Tuple[str, Any, bool]]:
    """Why this material cannot go to the question stage, or None if it can.

    Returns ``(reason, detail, is_system_fault)``. Four ways to fail, in the order they are cheapest
    to establish:

    * the slot failed outright -- a swap when it produced no content for a transport reason
      (``REFILLABLE_FAILURES``), a system fault when the cause is local and permanent, and a swap for
      anything unrecognised: an unknown material-stage reason is more likely to be about the material
      than about the machine, and the cost of guessing wrong that way is one material rather than a
      whole request;
    * unassessable -- the audit found no readable script, so there is nothing to write questions
      against. Exactly ``batch.py``'s refill trigger, spending the outer budget here instead;
    * ``REGENERATE_MATERIAL`` from the feasibility preflight -- the one verdict that is *designed* to
      spend a candidate swap (§8.2(2));
    * cannot-decide -- the preflight reached no verdict, which is a statement about the system and not
      about the material (``feasibility.CANNOT_DECIDE``'s own reasoning). Charging it to the material
      would assert a material is unfit in order to report that a call did not complete.
    """
    if not result.ok:
        if result.reason in REFILLABLE_FAILURES:
            return (result.reason, result.detail, False)
        if result.reason in SYSTEM_FAILURE_REASONS:
            return (result.reason, result.detail, True)
        return (result.reason or "material_failed", result.detail, False)
    if not is_assessable(result):
        return ("not_assessable",
                {"verdict": result.candidate.verdict if result.candidate else None}, False)
    outcome = (result.feasibility or {}).get("outcome")
    if outcome == REGENERATE_MATERIAL:
        return ("feasibility_regenerate",
                {"reasons": (result.feasibility or {}).get("reasons", [])[:3]}, False)
    if outcome in CANNOT_DECIDE:
        return ("feasibility_undecided", {"outcome": outcome}, True)
    if outcome not in (PASS, PASS_WITH_JUSTIFICATION):
        # An outcome outside the six the aggregator defines. Treated as undecided rather than as a
        # pass: question generation must not start on a verdict nothing here recognises.
        return ("feasibility_unrecognised", {"outcome": outcome}, True)
    return None


async def _do_questions(record: SlotRecord, ctx: _Context) -> None:
    """Run the question stage against the material this slot already qualified.

    The material is loaded from the candidate registry rather than carried in memory, which is what
    makes this reachable in a later invocation than the one that generated it. It is also the reason
    a missing candidate is a system fault and not a swap: the material qualified, so if it cannot be
    read back the fault is in storage, and regenerating would silently discard the checkpoint.
    """
    if not ctx.budget.may_start_questions():
        _checkpoint(record, ctx, "questions")
        raise _Paused()

    material, blueprint = _load_material(record, ctx)
    if material is None:
        _fault(record, ctx, "material_unreadable",
               "slot %s is %s but candidate %r cannot be loaded"
               % (record.slot_id, record.state, record.material_id))
        return

    record.state = QUESTIONS_PENDING
    ctx.save(record)
    await ctx.emit(record.slot_id, "questions_started",
                   {"material_id": record.material_id,
                    "restarts_used": record.attempts["question_restarts"]})

    try:
        result = await ctx.run_question_stage(
            material, blueprint,
            lambda name, detail=None: ctx.emit(record.slot_id, name, detail))
    except Exception as exc:  # noqa: BLE001 - classified below, not swallowed
        # A crash, not a verdict. The material still qualifies, so this must not spend a candidate
        # swap (§8.2(1)); it spends the restart budget and re-enters the question stage on the same
        # material. When that budget is gone the slot faults rather than quietly drawing a new
        # material -- two identical crashes on one input is a defect, not a material problem.
        if record.attempts["question_restarts"] < MAX_QUESTION_RESTARTS:
            record.bump("question_restarts")
            record.state = MATERIAL_DONE
            record.last_failure = {"stage": "questions", "reason": "question_stage_crashed",
                                   "detail": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
            ctx.save(record)
            await ctx.emit(record.slot_id, "questions_restarting",
                           {"restart": record.attempts["question_restarts"],
                            "of": MAX_QUESTION_RESTARTS,
                            "material_id": record.material_id})
            return
        _fault(record, ctx, "question_stage_crashed",
               "%s: %s" % (type(exc).__name__, str(exc)[:300]))
        return

    record.attempts["question_repairs"] = int(getattr(result, "rounds", 0) or 0)

    if not result.ok:
        await _questions_not_deliverable(record, ctx, result)
        return

    ctx.store.save_questions(record.material_id, result.as_dict())
    record.state = COMPLETE
    record.last_failure = None
    record.checkpoint_at = None
    ctx.save(record)
    await ctx.emit(record.slot_id, "set_complete",
                   {"material_id": record.material_id, "rounds": result.rounds})


def _load_material(record: SlotRecord, ctx: _Context) -> Tuple[Optional[Dict], Optional[Dict]]:
    from .publish import REGISTRY

    try:
        candidate = REGISTRY.get(str(record.material_id))
    except Exception:  # noqa: BLE001 - UnknownMaterial and any storage error mean the same here
        return None, None
    return candidate.material, candidate.blueprint


def _register_material(result: Any, scenario: Any, record: SlotRecord, ctx: _Context) -> None:
    """Offer the qualified material and mint its ``material_id``.

    ``batch._register`` is reused rather than reimplemented. It mints the id, registers the candidate
    and assigns the three fields back onto the result, and a second copy of that here would be a
    second place minting join keys for the same object -- the `material_id` is what `_questions/`,
    audio and selection all key on.

    Failure is raised, not downgraded. ``batch.py`` turns a registration error into a warning on the
    material and it is right to: there, the material is delivered either way. Here the registration IS
    the ``material_done`` checkpoint, so a material that is not registered is a material the next
    invocation cannot resume from -- and continuing would run the question stage on something no
    later process can find.
    """
    from .batch import _register

    _register(result, scenario, "%s:%s" % (ctx.group_id, record.scenario_id))
    if not result.material_id:
        raise SlotPersistenceError("material for slot %s was registered without an id"
                                   % record.slot_id)


async def _questions_not_deliverable(record: SlotRecord, ctx: _Context, result: Any) -> None:
    """The question stage produced nothing deliverable. Charge the question budget, keep the material.

    **This used to spend a candidate swap, and that was measured to be the expensive mistake.** The
    old reading was that ``REGENERATE_MATERIAL`` is a statement about the material, so it belongs to
    the outer budget. On batch ``web-1786166271869-1`` that reading cost 43 minutes: 11 materials were
    generated and 1 was delivered, because 8 question-stage rejections each withdrew a material that
    had passed its blind audit *and* its feasibility preflight, and the replacement then paid the full
    material price again for a fresh blueprint that the question stage was no likelier to satisfy --
    the rejections were dominated by one recurring anchor-adjacency reading, not by anything the
    blueprints had in common.

    So the charge follows the same rule the crash path above already states, extended from a crash to
    a verdict: a material that PASSED and cleared feasibility is qualified, and this stage does not get
    to un-qualify it. ``material_done`` and ``material_id`` both stay, the candidate is NOT withdrawn,
    and the slot re-enters the question stage against the same registered material on the restart
    budget. What that buys is a genuinely different attempt -- the question loop starts from
    generation, so the restart is a fresh set rather than a third revision of the rejected one, which
    is the objection ``QUESTIONS_NOT_DELIVERABLE`` raises against charging inward.

    The difference from the crash path is where exhaustion lands. A repeated crash is a defect in this
    machine, so it faults. A second rejection is evidence about the *material*: two independent
    question attempts, each with its own revisions, could not write a fair set against this blueprint.
    That is the reading ``REGENERATE_MATERIAL`` was always making, and it is credible now that it is
    made twice, so the swap happens then -- one attempt later than before, not never.
    """
    detail = {"outcome": result.outcome, "rounds": result.rounds,
              "blockers": (result.blockers or [])[:3]}
    reason = result.reason or "questions_not_deliverable"
    if record.attempts["question_restarts"] < MAX_QUESTION_RESTARTS:
        record.bump("question_restarts")
        record.state = MATERIAL_DONE
        record.last_failure = {"stage": "questions", "reason": reason, "detail": detail}
        ctx.save(record)
        await ctx.emit(record.slot_id, "questions_restarting",
                       {"restart": record.attempts["question_restarts"],
                        "of": MAX_QUESTION_RESTARTS,
                        "material_id": record.material_id,
                        "reason": reason})
        return
    _swap_candidate(record, ctx, reason, detail)


def _swap_candidate(record: SlotRecord, ctx: _Context, reason: str, detail: Any) -> None:
    """Charge the outer budget and either draw another material or exhaust the slot.

    The material this slot was holding is withdrawn from the offer list. It was never announced --
    ``material_completed`` is emitted only for a slot that reaches ``complete`` -- so nothing the user
    saw disappears, and leaving it would offer a reviewer a material whose questions this stage has
    just concluded cannot be made fair.
    """
    record.bump("candidate_swaps")
    record.last_failure = {"stage": "material" if record.state == MATERIAL_PENDING else "questions",
                           "reason": reason, "detail": detail}
    # The restart budget is spent per MATERIAL -- `slot_store` defines it as re-entering the question
    # stage "on the SAME qualified material" -- and this is the line where the material stops being the
    # same one. Carrying the count across would give the replacement material one attempt fewer than
    # the material before it got, for a reason that is about its predecessor. Still bounded: at most
    # `MAX_QUESTION_RESTARTS` per material and `MAX_CANDIDATE_SWAPS + 1` materials per slot.
    record.attempts["question_restarts"] = 0
    _withdraw_material(record, ctx)
    record.material_id = None
    if record.attempts["candidate_swaps"] > MAX_CANDIDATE_SWAPS:
        record.state = EXHAUSTED
    else:
        record.state = MATERIAL_PENDING
    ctx.save(record)


def _withdraw_material(record: SlotRecord, ctx: _Context) -> None:
    """Drop an unannounced candidate. Best-effort: a leftover offer is not worth failing a slot for."""
    if not record.material_id:
        return
    try:
        from .publish import REGISTRY

        REGISTRY.store.drop(str(record.material_id))
    except Exception:  # noqa: BLE001 - see docstring
        import logging

        logging.getLogger(__name__).warning(
            "could not withdraw candidate %s for slot %s", record.material_id, record.slot_id,
            exc_info=True)


def _fault(record: SlotRecord, ctx: _Context, reason: str, detail: Any) -> None:
    """Stop this slot for a reason that is not about the material, and say so on the record.

    ``system_fault`` is what stops a replacement being opened and what turns a short request into
    ``system_failure`` rather than ``incomplete``. Both consequences are wrong for a material defect
    and right here, which is why the flag is set at the one place that decides the distinction.
    """
    record.state = EXHAUSTED
    record.system_fault = True
    record.last_failure = {"stage": record.state, "reason": reason, "detail": detail}
    ctx.faults[record.slot_id] = reason
    try:
        ctx.save(record)
    except SlotPersistenceError:
        # Already the failure being reported, or a new one of the same kind. The in-memory fault is
        # what the summary reads, so the request still reports honestly with an unwritten record.
        pass


def _checkpoint(record: SlotRecord, ctx: _Context, next_stage: str) -> None:
    """Record that this slot stopped on the clock, not on a defect (§8.2(4))."""
    record.checkpoint_at = time.time()
    record.last_failure = {"stage": next_stage, "reason": "time_budget",
                           "detail": {"remaining": round(ctx.budget.remaining(), 1)}}
    ctx.save(record)


def _status(records: List[SlotRecord], wanted: int, ctx: _Context) -> str:
    """The request's terminal state. Success requires N complete sets and nothing else does.

    Order matters: the count is checked first, so a request that did deliver N sets is not demoted by
    a fault on a position that was later refilled. Faults are still reported in the summary either
    way -- reporting a complete delivery as complete is honest, and hiding the fault would not be.
    """
    complete = sum(1 for record in records if record.state == COMPLETE)
    if complete >= wanted:
        return SUCCEEDED
    if ctx.faults or any(record.system_fault for record in records):
        return SYSTEM_FAILURE
    return INCOMPLETE


def _summary(batch_id: str, wanted: int, status: str, delivered: List[Dict[str, Any]],
             ctx: _Context, records: Optional[List[SlotRecord]] = None,
             detail: Optional[str] = None) -> Dict[str, Any]:
    """The request summary, which is also the wire's terminal event and the stored request document.

    ``slots`` carries every slot's state, and it is on the wire rather than only in storage because of
    what the web tier has to do with it (§8.1). ``web/fanout.py`` used to call a slot with no terminal
    event ``failed``; once a slot can outlive one invocation, a checkpointed or still-retrying slot has
    to be distinguishable from a stuck one, and this is the field that distinguishes them. Sending it
    means the common case costs no storage read at all -- the child that owns the state reports it.
    """
    payload = {
        "batch_id": batch_id,
        "status": status,
        "requested": wanted,
        "delivered": len(delivered),
        "sets": delivered,
        # Stated on every summary, empty included, so "no faults" and "faults not reported" cannot
        # look the same to a reader.
        "system_faults": [{"slot_id": slot, "reason": reason}
                          for slot, reason in sorted(ctx.faults.items())],
        "paused": ctx.paused,
        "slots": [_slot_row(record, ctx) for record in (records or [])],
    }
    if detail:
        payload["detail"] = detail
    return payload


def _slot_row(record: SlotRecord, ctx: _Context) -> Dict[str, Any]:
    """One slot as the wire describes it: enough to draw a card and to decide it is not stuck.

    A trimmed projection of ``SlotRecord.as_record()`` rather than the record itself. The record grows
    fields for the runner's own bookkeeping, and a projection is what stops each of those becoming a
    published field the frontend can start depending on.
    """
    return {
        "slot_id": record.slot_id,
        "scenario": record.scenario_id,
        "state": record.state,
        "material_id": record.material_id,
        # Included because a POSITION can hold several records -- an exhausted slot and the replacement
        # that took over -- and a reader deciding which one describes the position needs to know which
        # came last. `list_slots` already returns them in this order, but a reader that has to rely on
        # list order cannot tell a reordering from a replacement.
        "created_at": record.created_at,
        # True while this slot has work the next invocation can pick up. Stated rather than derived
        # from `state` by the reader, because "resumable" is this module's judgement: an exhausted slot
        # is terminal for the slot and its position may still be refilled, and a reader reconstructing
        # that rule from state names would be a second copy of it.
        "resumable": record.state not in (COMPLETE, EXHAUSTED),
        "checkpointed": record.checkpoint_at is not None,
        "system_fault": record.system_fault,
        "last_failure": record.last_failure,
        "attempts": dict(record.attempts),
        "replaces": record.replaces,
        "replaced_by": record.replaced_by,
    }


def _request_document(batch_id: str, wanted: int, status: str, records: List[SlotRecord],
                      ctx: _Context, store_backend: str) -> Dict[str, Any]:
    """The persistent request record: what was asked for, where every slot stands, and what backs it.

    ``store_backend`` is recorded because the in-memory fallback is correct locally and a defect in the
    Runtime, and the way that defect used to present itself was data missing minutes later in another
    process. A request document that says ``memory`` explains that at the time it happened.
    """
    return {
        "batch_id": batch_id,
        "requested": wanted,
        "status": status,
        "complete": sum(1 for record in records if record.state == COMPLETE),
        "store_backend": store_backend,
        "updated_at": time.time(),
        "slots": [record.as_record() for record in records],
    }


async def _pump(queue: asyncio.Queue, work):
    """Run ``work`` and yield every event it queues, in order, as it is queued.

    A sentinel-terminated drain, the same shape ``run_batch`` uses and for the same measured reason:
    racing ``queue.get()`` against the worker and cancelling the pending get can drop an item already
    handed to it.

    Yielded rather than accumulated, because the events are the heartbeat. Collecting them and
    replaying at the end would leave the connection silent for the whole request, which is what the
    ``stage``-as-keepalive contract (``events.py``) exists to prevent.

    ``work`` is awaited in the ``finally``, so a consumer that stops reading (a browser that went
    away, closing the generator) does not leave the slot tasks running unattended. The sentinel is
    queued only after ``work`` has finished, so on the normal path the cancel below has nothing left
    to cancel; it matters only on the abandoned path, where waiting for the remaining stages would
    hold the response open for a client that is gone.
    """
    sentinel = object()

    async def close() -> None:
        try:
            await work
        finally:
            await queue.put(sentinel)

    closer = asyncio.ensure_future(close())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield item
    finally:
        closer.cancel()
        try:
            await closer
        except asyncio.CancelledError:
            pass


async def _noop(_event: Dict[str, Any]) -> None:
    return None
