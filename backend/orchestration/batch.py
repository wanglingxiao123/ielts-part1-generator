"""Slot scheduling, concurrency gate, time budget and NOT_ASSESSABLE refill (design.md §8, §9).

The 15-minute synchronous limit on AgentCore Runtime is not adjustable, so it is a real constraint.
What changed is what it constrains. **The web tier now issues one invocation per material**
(`web/fanout.py`), so an invocation carries one slot and the 900s wall bounds one ~150-230s material
instead of a whole batch. This module still schedules a list of slots -- the CLI (`scripts/run_batch.py`)
and the tests use it that way, and it is what makes the code testable at all -- but in production
``len(slots)`` is 1 and ``concurrency`` is 1.

The consequence for the budget is not "the same rationing with more headroom", it is a different
job. Reading, with the measured numbers (docs/timing.md: ~150s typical, ~250s with two
regenerations):

* **It was a rationer.** Six materials shared 810 usable seconds, so a refill for slot 4 competed
  with a first attempt for slot 6, ``may_revise`` routinely said no, and ``skipped_time_budget`` was
  an ordinary outcome the batch summary had to report honestly.
* **It is now a backstop.** One material has all 810 seconds. Three attempts at the 240s p95 cost
  720s, so the clock and ``MAX_REFILL_ROUNDS`` now bind at roughly the same point -- neither is
  vestigial, and neither fires on a healthy material. ``skipped_time_budget`` stops meaning "a
  sibling was slow" and starts meaning "this material's own attempts ran the wall down", which is a
  fault worth seeing rather than routine degradation.

Nothing about ``may_start`` / ``may_revise`` needed rewriting to get there: they were always
expressed in seconds remaining against the cost of the next step, which is exactly right for one
material. What needed correcting is the *claims* around them -- see `Budget`.

**Refill.** A slot that ends NOT_ASSESSABLE produced nothing a user can act on: the audit could
not find a usable script, so there is no full text to read and no defect list to weigh. Returning
it would hand the user a blank card, so the slot is re-run instead and the user simply receives
the count they asked for. FAIL is *not* refilled -- a FAIL material is usable-but-flawed, and the
product owner's rule is that it comes back with its shortcomings stated.

This all still lives INSIDE one invocation, which is why the fan-out did not disturb it: a child
generates one material and refills it up to ``MAX_REFILL_ROUNDS`` times before answering, so the
web tier never sees a discarded attempt and the user still gets one card per set requested.

The refill is bounded twice over, because an unbounded one would hang the request until the
platform kills it:

* ``MAX_REFILL_ROUNDS`` rounds, whatever happens. Each round re-runs at most the outstanding
  slots, so the total extra work is bounded by rounds x slots and cannot grow.
* ``Budget.may_start()`` before every attempt. When it refuses, the batch returns what it has.
  Fewer materials than asked for beats a 504 that loses all of them.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from . import events
from .loop import MaterialResult, is_assessable, run_one
from .scenarios import Scenario

__all__ = ["BatchRequest", "Budget", "run_batch", "MAX_REFILL_ROUNDS"]

# Concurrency follows the slot count up to this ceiling, rather than being pinned at 3. The comment
# here used to claim it defaulted to the scenario count while the code said 3, so a batch of 4 ran
# three materials and then one alone -- the last slot paying full latency by itself and roughly
# doubling perceived wall time.
#
# In production this is now effectively dead: the web tier sends one slot per invocation, so
# `BatchRequest` clamps concurrency to 1 and the parallelism that used to live here lives in
# `web/fanout.py`'s `FANOUT_CONCURRENCY`. Kept because the CLI still runs multi-slot batches, and
# because the clamp is what makes it harmless rather than misleading.
#
# Per-material latency is dominated by waiting on the model, not by local CPU, so slots cost
# little to hold open. The ceiling exists only because GPT-5.6's TPM/RPM on the mantle channel is
# undocumented: on 429s, lower the web tier's WEB_FANOUT_CONCURRENCY rather than adding retries,
# since retries push a single material's elapsed time toward its 15-minute wall.
MAX_CONCURRENCY = int(os.environ.get("IELTS_CONCURRENCY", "6"))
# The platform's synchronous wall on one invocation, which now carries ONE material.
HARD_LIMIT_SECONDS = float(os.environ.get("IELTS_HARD_LIMIT", "900"))
SAFETY_MARGIN_SECONDS = float(os.environ.get("IELTS_SAFETY_MARGIN", "90"))
# Conservative starting estimate, replaced by measured p95 (see docs/timing.md). Too low a value
# starts work that cannot finish; too high skips work that could have.
P95_PER_MATERIAL = float(os.environ.get("IELTS_P95_PER_MATERIAL", "240"))
# A revision plus re-audit is roughly half the calls for a material.
#
# Deliberately NOT lowered now that one material owns the whole 810s. `may_revise` compares against
# what a revision costs, not against what is spare, so a smaller number would not buy more revisions
# -- it would only make the check answer yes when there is no longer time to finish. The measured
# cost is ~44s (docs/timing.md); 120 stays cautious, and with one material per invocation it is
# essentially never the binding constraint anyway.
REVISION_COST_SECONDS = float(os.environ.get("IELTS_REVISION_COST", "120"))

# How many times the batch will re-run NOT_ASSESSABLE slots before giving up.
#
# 2 is a judgement, not a measurement: one round covers the transient case (a truncated response,
# a model that lost the schema once), and by the third identical outcome the scenario itself is
# more likely at fault than the attempt. The number matters less than its existence -- with the
# budget check as the real governor, this bound is what guarantees termination even if the clock
# is generous, e.g. a small batch with hours of headroom in a test.
MAX_REFILL_ROUNDS = int(os.environ.get("IELTS_MAX_REFILL_ROUNDS", "2"))

# Failure reasons a silent refill can plausibly fix, i.e. the ones that produced NO CONTENT for
# reasons unrelated to the script: the model call itself failed, or the slot crashed.
#
# The product owner's rule for these: "如果是 API 调用本身失败（网络超时等真正没内容的情况），后台
# 静默补跑，补不上就少返回一套，不放空卡片". So they are re-run exactly like NOT_ASSESSABLE, and if
# the re-run also fails the batch returns fewer materials rather than an empty card.
#
# Deliberately NOT in this set:
#   * `skipped_time_budget` -- attempted nothing, and re-running it is what the budget just refused.
#   * `validator_unavailable` -- the validator is a local script; if it is gone it is gone for every
#     retry too, and an operator has to see that rather than have it burn the clock three times.
#   * validation failures of any kind -- they no longer produce a failure at all. The Loop delivers
#     the material with its findings, which is the whole point of change ①.
REFILLABLE_FAILURES = frozenset({"model_error", "audit_failed", "unhandled_error",
                                 "no_material_generated"})

# Marks the end of the event stream. A unique object, so it can never collide with an event dict.
_SENTINEL = object()


class Budget(object):
    """Wall-clock budget for ONE INVOCATION, which now carries one material.

    The arithmetic did not change with the fan-out; the meaning did. Both predicates ask "is there
    time left for the next step", which was a rationing question when six materials shared the
    clock and is a safety question when one owns it:

    * ``may_start`` refused the sixth material because five siblings had spent the budget. It now
      refuses a THIRD attempt at the same material because that material's own two attempts ran
      810s down -- so ``skipped_time_budget`` stopped being routine degradation and became a signal.
    * ``may_revise`` was the routine casualty: a late slot skipped its revision pass to let the
      batch finish. With 810s for one ~150s material it should now essentially always say yes, and
      a run where it says no is worth investigating rather than expected.

    What this class must NOT become is a per-batch budget again. It is constructed per
    ``BatchRequest``, and a request is one invocation; a caller that shared one Budget across
    several invocations would re-import the constraint the fan-out removed.
    """

    __slots__ = ("started", "deadline", "p95", "revision_cost")

    def __init__(
        self,
        hard_limit: float = HARD_LIMIT_SECONDS,
        margin: float = SAFETY_MARGIN_SECONDS,
        p95: float = P95_PER_MATERIAL,
        revision_cost: float = REVISION_COST_SECONDS,
        now: Optional[float] = None,
    ) -> None:
        self.started = now if now is not None else time.monotonic()
        self.deadline = self.started + max(hard_limit - margin, 0.0)
        self.p95 = p95
        self.revision_cost = revision_cost

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def may_start(self) -> bool:
        """Only begin an attempt we expect to be able to finish.

        With one material per invocation this gates the refill rounds rather than the sibling
        slots: three attempts at the 240s p95 cost 720s of the 810s available, so this and
        ``MAX_REFILL_ROUNDS`` bind at about the same place instead of the clock always winning.
        """
        return self.remaining() > self.p95

    def may_revise(self) -> bool:
        """Whether an in-flight material can still afford the optional revision pass."""
        return self.remaining() > self.revision_cost


class BatchRequest(object):
    __slots__ = ("slots", "concurrency", "budget")

    def __init__(
        self,
        slots: List[Scenario],
        concurrency: Optional[int] = None,
        budget: Optional[Budget] = None,
    ) -> None:
        self.slots = slots
        # No point holding more slots open than there is work: a batch of 2 with concurrency 6
        # would report a misleading 6 in its config event. In production `slots` is length 1, so
        # this clamp is also what keeps MAX_CONCURRENCY from advertising parallelism that has moved
        # to the web tier.
        self.concurrency = max(1, min(concurrency or MAX_CONCURRENCY, len(slots) or 1))
        # One Budget per request, and a request is one invocation. See Budget's docstring for why
        # sharing one across invocations would undo the fan-out.
        self.budget = budget or Budget()


async def _attempt_slot(
    scenario: Scenario,
    slot_id: str,
    semaphore: asyncio.Semaphore,
    budget: Budget,
    queue: asyncio.Queue,
) -> MaterialResult:
    """One pass of the Loop for one slot. Emits stage events; no terminal event, no registration.

    Terminal events and registration belong to ``_run_slot``, which knows whether this pass is the
    one being kept. Registering here would publish a NOT_ASSESSABLE candidate the refill is about
    to replace -- it would show up in ``list_candidates`` and compete for the group's single
    selection against the material that actually gets returned.

    Exceptions are caught rather than raised. One material's crash must not take down a batch that
    five other materials are still progressing through.
    """

    async def emit(name: str, detail: Optional[Dict[str, Any]] = None) -> None:
        await queue.put(events.stage(slot_id, scenario.id, name, detail))

    async with semaphore:
        if not budget.may_start():
            return MaterialResult(
                slot_id, scenario.id, False, reason="skipped_time_budget",
                detail={"remaining": round(budget.remaining(), 1), "needed": budget.p95},
            )
        try:
            return await run_one(scenario, slot_id, emit, allow_revision=budget.may_revise)
        except Exception as exc:  # noqa: BLE001 - isolation is the requirement
            return MaterialResult(
                slot_id, scenario.id, False, reason="unhandled_error",
                detail="%s: %s" % (type(exc).__name__, str(exc)[:400]),
            )


async def _run_slot(
    scenario: Scenario,
    slot_id: str,
    semaphore: asyncio.Semaphore,
    budget: Budget,
    queue: asyncio.Queue,
    group_key: str = "",
    max_refill_rounds: int = MAX_REFILL_ROUNDS,
) -> MaterialResult:
    """Deliver one material for this slot, re-running the Loop while it is NOT_ASSESSABLE.

    Bounded by ``1 + max_refill_rounds`` attempts *and* by ``budget.may_start()`` before each one,
    so it terminates on the clock in production and on the count even when the clock is generous.

    The user sees one card per slot either way: a discarded attempt emits a ``refilling`` stage
    event for observability and no ``material_completed`` / ``material_failed``. Emitting a failure
    per discarded attempt would show the user a broken material and then a good one for the same
    slot, which is exactly the internal machinery the product owner asked to keep off the page.
    """
    result = await _attempt_slot(scenario, slot_id, semaphore, budget, queue)
    rounds_used = 0

    for round_number in range(1, max_refill_rounds + 1):
        # Two things get refilled, and both are "the user has nothing to look at":
        #
        #   * an unassessable success -- a script the audit could not read;
        #   * a REFILLABLE_FAILURES failure -- the model call or the slot itself blew up, so no
        #     content exists at all. This is the client's "API 调用本身失败" case, and it is refilled
        #     silently for the same reason: a transient network fault is not something the user
        #     should be shown, and the honest outcome when the retry also fails is one fewer
        #     material, never an empty card.
        #
        # A validation failure is not here because it is no longer a failure: the Loop delivers the
        # material with the validator's findings attached.
        if result.ok and is_assessable(result):
            break
        if not result.ok and result.reason not in REFILLABLE_FAILURES:
            break
        if not budget.may_start():
            # Out of clock. Return what exists: fewer materials than requested beats a 504 that
            # loses the whole batch, and the slots that did finish are already registered.
            await queue.put(events.stage(
                slot_id, scenario.id, "refill_abandoned",
                {"round": round_number, "remaining": round(budget.remaining(), 1),
                 "needed": budget.p95},
            ))
            break
        await queue.put(events.stage(
            slot_id, scenario.id, "refilling",
            {"round": round_number, "of": max_refill_rounds,
             # Which of the two refill causes this is. The user never sees it; an operator needs it,
             # because a batch refilling on `model_error` is a different problem from one refilling
             # on `not_assessable` and the remedies are nothing alike.
             "cause": result.reason if not result.ok else "not_assessable"},
        ))
        rounds_used = round_number
        attempt = await _attempt_slot(scenario, slot_id, semaphore, budget, queue)
        if attempt.reason == "skipped_time_budget":
            # The budget drained while this attempt waited on the semaphore. Keep the previous
            # result: reporting `skipped_time_budget` here would tell an operator nothing was
            # attempted for this slot, when in fact a whole material was generated and audited.
            await queue.put(events.stage(
                slot_id, scenario.id, "refill_abandoned",
                {"round": round_number, "remaining": round(budget.remaining(), 1),
                 "needed": budget.p95},
            ))
            rounds_used = round_number - 1
            break
        result = attempt

    if result.ok and not is_assessable(result):
        # Every attempt came back unassessable and the bound was reached. Reported as a slot
        # failure rather than handed over: a material the audit could not read has no full text
        # for the user to check and no defect list for them to weigh, so there is nothing on the
        # card to decide with.
        result = MaterialResult(
            slot_id, scenario.id, False, reason="not_assessable",
            detail={"attempts": rounds_used + 1,
                    "verdict": result.candidate.verdict if result.candidate else None},
            timings=result.timings,
        )
    result.refill_rounds = rounds_used

    if result.reason == "skipped_time_budget":
        await queue.put(events.material_skipped(slot_id, scenario.id, "skipped_time_budget"))
        return result

    if result.ok:
        # A material_id is minted here, not at selection time, because the id is what the
        # frontend uses to refer to a candidate before any audio exists. Registering also
        # decides which candidates compete for one choice (group_key).
        try:
            _register(result, scenario, group_key)
        except Exception as exc:  # noqa: BLE001 - see _register's docstring
            # Name the exception type as well as the message: the failure that mattered in
            # practice was a silent fallback to in-memory storage, and "AudioNotConfigured"
            # says which of the many possible causes it was.
            result.warnings.append(
                "candidate_not_registered: %s: %s" % (type(exc).__name__, str(exc)[:200])
            )
            import logging
            logging.getLogger(__name__).warning(
                "candidate registration failed for slot %s", result.slot_id, exc_info=True
            )
    await queue.put(
        events.material_completed(result) if result.ok else events.material_failed(result)
    )
    return result


def _register(result: MaterialResult, scenario: Scenario, group_key: str) -> None:
    """Offer a completed material as a selectable candidate.

    Failure here must not fail the slot: the material is generated and valid, and losing the
    ability to select it is a lesser outcome than reporting a good material as failed.
    """
    from audio_storage.state_store import new_material_id

    from .publish import REGISTRY, Candidate, scenario_key_for

    candidate = result.candidate
    scenario_key = scenario_key_for(scenario)
    material_id = new_material_id(scenario_key)
    result.scenario_key = scenario_key
    result.group_key = group_key
    # Assigned only after registration succeeds. Setting it first published an id the frontend
    # would offer for selection while no candidate backed it, so `select` answered "unknown
    # material" for something the UI had just displayed as ready.
    REGISTRY.register(
        Candidate(
            material_id=material_id,
            scenario_key=scenario_key,
            group_key=group_key,
            slot_id=result.slot_id,
            material=candidate.gen.material,
            blueprint=candidate.gen.blueprint,
            audit=candidate.audit,
            cross_check=candidate.cross_check,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
            validation_findings=result.validation_findings,
        )
    )
    result.material_id = material_id


async def run_batch(request: BatchRequest) -> AsyncIterator[Dict[str, Any]]:
    """Drive a batch and yield events as they occur.

    Events flow through a queue rather than ``as_completed`` alone, because stage events must
    reach the client *while* a material is running -- the heartbeat requirement. ``as_completed``
    only surfaces finished work, which would leave the connection silent for minutes.
    """
    from ..model import provider

    budget = request.budget
    yield events.batch_started(
        total=len(request.slots),
        deadline_at=time.time() + budget.remaining(),
        config=dict(provider.describe(), concurrency=request.concurrency),
    )

    queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(request.concurrency)
    # Candidates competing for one user choice are those generated for the same scenario within
    # one batch. Keyed by batch so two batches over the same scenario do not discard each other's
    # materials -- a user comparing yesterday's output against today's would lose one silently.
    batch_id = "batch-%d" % int(time.time() * 1000)
    tasks = [
        asyncio.ensure_future(
            _run_slot(
                scenario, "slot-%d" % (index + 1), semaphore, budget, queue,
                "%s:%s" % (batch_id, scenario.id),
            )
        )
        for index, scenario in enumerate(request.slots)
    ]

    # A single sentinel-terminated drain, rather than racing ``queue.get()`` against the slot
    # tasks on every iteration. Cancelling a pending ``get()`` can drop an item that was already
    # handed to it, which would silently lose a completed material's payload -- the one event the
    # client cannot do without. Waiting for all slots and then reading until the sentinel removes
    # the race entirely.
    async def close_queue() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(_SENTINEL)

    closer = asyncio.ensure_future(close_queue())
    try:
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                break
            yield event
    finally:
        await closer

    results = [task.result() for task in tasks]
    yield events.batch_completed(
        succeeded=sum(1 for r in results if r.ok),
        failed=sum(1 for r in results if not r.ok and r.reason != "skipped_time_budget"),
        skipped=sum(1 for r in results if r.reason == "skipped_time_budget"),
        degraded=sum(1 for r in results if r.degraded),
        # Refills are counted, not hidden. The user is not shown them, but a batch that spent half
        # its budget re-running unassessable slots is the single most useful thing an operator can
        # know about it, and it is invisible from the outside otherwise.
        refilled=sum(r.refill_rounds for r in results),
        stage_timings=_aggregate_timings(results),
        slots=[
            {"slot_id": r.slot_id, "scenario": r.scenario_id, "ok": r.ok,
             "route": r.route, "note": r.note, "reason": r.reason,
             "degraded": r.degraded, "refill_rounds": r.refill_rounds,
             "total_seconds": r.timings.get("total")}
            for r in results
        ],
    )


def _aggregate_timings(results: List[MaterialResult]) -> Dict[str, Any]:
    """Per-stage totals for calibration. This is the data that decides the real batch ceiling."""
    buckets: Dict[str, List[float]] = {}
    for result in results:
        for name, value in result.timings.items():
            buckets.setdefault(name, []).append(value)
    summary: Dict[str, Any] = {}
    for name, values in buckets.items():
        ordered = sorted(values)
        summary[name] = {
            "count": len(ordered),
            "min": ordered[0],
            "max": ordered[-1],
            "mean": round(sum(ordered) / len(ordered), 2),
        }
    return summary
