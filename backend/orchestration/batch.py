"""Batch scheduling, concurrency gate and time budget (design.md §8, §9).

The 15-minute synchronous limit on AgentCore Runtime is not adjustable, so it is a real product
constraint. The budget here converts it from "the platform severs the connection and the whole
batch is lost" into "in-flight materials finish, un-started ones are reported as skipped".
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from . import events
from .loop import MaterialResult, run_one
from .scenarios import Scenario

__all__ = ["BatchRequest", "Budget", "run_batch"]

# Concurrency defaults to the scenario count. GPT-5.6's TPM/RPM on the mantle channel was not
# documented at planning time, so this is an environment variable: on 429s, lower it rather than
# adding retries, since retries push total elapsed time toward the 15-minute wall.
DEFAULT_CONCURRENCY = int(os.environ.get("IELTS_CONCURRENCY", "3"))
HARD_LIMIT_SECONDS = float(os.environ.get("IELTS_HARD_LIMIT", "900"))
SAFETY_MARGIN_SECONDS = float(os.environ.get("IELTS_SAFETY_MARGIN", "90"))
# Conservative starting estimate, replaced by measured p95 (see docs/timing.md). Too low a value
# starts work that cannot finish; too high skips work that could have.
P95_PER_MATERIAL = float(os.environ.get("IELTS_P95_PER_MATERIAL", "240"))
# A revision plus re-audit is roughly half the calls for a material.
REVISION_COST_SECONDS = float(os.environ.get("IELTS_REVISION_COST", "120"))

# Marks the end of the event stream. A unique object, so it can never collide with an event dict.
_SENTINEL = object()


class Budget(object):
    """Wall-clock budget for one batch."""

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
        """Only begin a material we expect to be able to finish."""
        return self.remaining() > self.p95

    def may_revise(self) -> bool:
        """Whether an in-flight material can still afford the optional revision pass."""
        return self.remaining() > self.revision_cost


class BatchRequest(object):
    __slots__ = ("slots", "concurrency", "budget")

    def __init__(
        self,
        slots: List[Scenario],
        concurrency: int = DEFAULT_CONCURRENCY,
        budget: Optional[Budget] = None,
    ) -> None:
        self.slots = slots
        self.concurrency = max(1, concurrency)
        self.budget = budget or Budget()


async def _run_slot(
    scenario: Scenario,
    slot_id: str,
    semaphore: asyncio.Semaphore,
    budget: Budget,
    queue: asyncio.Queue,
    group_key: str = "",
) -> MaterialResult:
    """Run one slot, forwarding stage events and never letting its failure escape.

    Each slot's exceptions are caught here. One material's crash must not take down a batch that
    five other materials are still progressing through.
    """

    async def emit(name: str, detail: Optional[Dict[str, Any]] = None) -> None:
        await queue.put(events.stage(slot_id, scenario.id, name, detail))

    async with semaphore:
        if not budget.may_start():
            result = MaterialResult(
                slot_id, scenario.id, False, reason="skipped_time_budget",
                detail={"remaining": round(budget.remaining(), 1), "needed": budget.p95},
            )
            await queue.put(events.material_skipped(slot_id, scenario.id, "skipped_time_budget"))
            return result
        try:
            result = await run_one(scenario, slot_id, emit, allow_revision=budget.may_revise)
        except Exception as exc:  # noqa: BLE001 - isolation is the requirement
            result = MaterialResult(
                slot_id, scenario.id, False, reason="unhandled_error",
                detail="%s: %s" % (type(exc).__name__, str(exc)[:400]),
            )
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
        stage_timings=_aggregate_timings(results),
        slots=[
            {"slot_id": r.slot_id, "scenario": r.scenario_id, "ok": r.ok,
             "route": r.route, "note": r.note, "reason": r.reason,
             "degraded": r.degraded, "total_seconds": r.timings.get("total")}
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
