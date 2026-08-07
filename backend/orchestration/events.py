"""SSE event constructors (design.md §10).

The five ``type`` values are a published contract: adding fields is backward compatible,
renaming them is not. Both the frontend and the audio-storage task consume these.

``stage`` doubles as a heartbeat. AgentCore closes a connection idle for 900s and a single
material takes minutes, so a batch with no intermediate events would look idle and be dropped
mid-flight.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

__all__ = ["batch_started", "stage", "material_completed", "material_failed",
           "material_skipped", "batch_completed", "request_completed"]


def batch_started(total: int, deadline_at: float, config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "batch_started",
        "total": total,
        "deadline_at": round(deadline_at, 3),
        "config": config,
        "at": time.time(),
    }


def stage(
    slot_id: str,
    scenario: str,
    name: str,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event = {
        "type": "stage",
        "slot_id": slot_id,
        "scenario": scenario,
        "stage": name,
        "at": time.time(),
    }
    if detail:
        # attempt is promoted to a top-level field because the frontend renders it directly.
        if "attempt" in detail:
            event["attempt"] = detail["attempt"]
        event["detail"] = detail
    return event


def material_completed(result: Any) -> Dict[str, Any]:
    payload = result.as_dict()
    payload["type"] = "material_completed"
    payload["at"] = time.time()
    return payload


def material_failed(result: Any) -> Dict[str, Any]:
    payload = result.as_dict()
    payload["type"] = "material_failed"
    payload["at"] = time.time()
    return payload


def material_skipped(slot_id: str, scenario: str, reason: str) -> Dict[str, Any]:
    return {
        "type": "material_failed",
        "slot_id": slot_id,
        "scenario": scenario,
        "ok": False,
        "reason": reason,
        "skipped": True,
        "at": time.time(),
    }


def batch_completed(
    succeeded: int,
    failed: int,
    skipped: int,
    degraded: int,
    stage_timings: Dict[str, Any],
    slots: List[Dict[str, Any]],
    refilled: int = 0,
) -> Dict[str, Any]:
    """Batch summary. Skipped, degraded and refilled counts are reported, never hidden.

    Presenting a partial batch as a success would make the time-budget mechanism worse than the
    504 it replaces -- at least a 504 is visibly a failure.

    ``refilled`` is the total number of NOT_ASSESSABLE attempts the batch discarded and re-ran.
    Invisible to the user by design (they asked for N materials and got N), but an operator needs
    it: a rising refill count is how a degrading scenario prompt announces itself, and without this
    number it would only show up as batches mysteriously running out of time.
    """
    return {
        "type": "batch_completed",
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "degraded": degraded,
        "refilled": refilled,
        "stage_timings": stage_timings,
        "slots": slots,
        "at": time.time(),
    }


def request_completed(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Terminal event for an exact-count request (orchestration/delivery.py).

    A sixth ``type``, not a sixth meaning for ``batch_completed``. That event's ``succeeded`` /
    ``failed`` / ``skipped`` counts describe slots that each produced a card, and the frontend renders
    a finished batch from them; a request has one status for the whole delivery and can legitimately
    end ``incomplete`` with work still pending, which those three counts have no way to express. Reusing
    the type would make an unfinished request indistinguishable from a partly-failed batch to every
    existing consumer.

    ``summary`` is passed through verbatim -- it is the same document written to
    ``_slots/{batch_id}/request.json``, so what the client is told and what a resumption reads cannot
    diverge.
    """
    payload = dict(summary)
    payload["type"] = "request_completed"
    payload["at"] = time.time()
    return payload
