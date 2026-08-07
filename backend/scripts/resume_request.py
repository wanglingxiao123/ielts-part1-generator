#!/usr/bin/env python
"""One INVOCATION of an exact-count request, against the live model and real S3.

    # invocation 1: a budget too small for the question stage -- stops at the checkpoint
    IELTS_AUDIO_BUCKET=... python backend/scripts/resume_request.py \
        --batch-id ckpt-$(date +%s) --scenario accommodation-rental --hard-limit 390

    # invocation 2: same id, full budget -- must resume, not regenerate
    IELTS_AUDIO_BUCKET=... python backend/scripts/resume_request.py --batch-id <same>

**One invocation per process run, deliberately.** Two calls inside one process would share the
candidate registry, the module-level caches and the loaded material, so a runner that resumed from
memory instead of from storage would pass. §8.2(4) says resumption reads storage, and the only way to
observe that is a second process that has none of the first one's memory. Which is also the real
shape: each invocation is a fresh AgentCore microVM.

``--hard-limit 390`` is how the checkpoint is reached without waiting for a 900s wall: the budget
subtracts a 90s margin, leaving 300s, which authorises a material (p95 240s) and then refuses the
question stage (p95 420s) once the material has spent its time. That is exactly the state a real 900s
invocation reaches after one slow material, and the two thresholds being separate is what produces it.

Writes to real S3 under ``_slots/{batch_id}/``, so pass a fresh ``--batch-id`` per experiment. Nothing
here touches existing material or audio keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.model import provider  # noqa: E402
from backend.orchestration.delivery import DeliveryBudget, stream_request  # noqa: E402
from backend.orchestration.scenarios import load_catalogue  # noqa: E402
from backend.orchestration.slot_store import (  # noqa: E402
    build_slot_store,
    describe_slot_store,
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True,
                        help="the resumption key; pass the SAME one to resume")
    parser.add_argument("--scenario", nargs="+", default=["accommodation-rental"])
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--hard-limit", type=float, default=None,
                        help="shrink the budget to reach the checkpoint (390 refuses questions)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    catalogue = load_catalogue()
    scenarios = []
    for scenario_id in args.scenario:
        scenario = catalogue.get(scenario_id)
        if scenario is None:
            print("unknown scenario %r; available: %s" % (scenario_id, catalogue.ids()))
            return 2
        scenarios.append(scenario)

    store = build_slot_store()
    print("model:  %s" % provider.describe())
    print("store:  %s" % describe_slot_store(store))
    if not store.persistent:
        # An in-memory store would make the second run start from nothing and look like a
        # regeneration bug. Refused rather than warned: the whole point of this script is durability.
        print("REFUSING: the slot store is not durable. Set IELTS_AUDIO_BUCKET.")
        return 2
    print("request: %s  (%d slot(s))" % (args.batch_id, len(scenarios)))

    # What was already on disk before this invocation. Printed first because it is the difference
    # between "a first run" and "a resume", and the whole claim is about the second.
    before = {r.slot_id: r.state for r in store.list_slots(args.batch_id)}
    print("before:  %s" % (json.dumps(before) if before else "nothing recorded"))

    budget = DeliveryBudget(hard_limit=args.hard_limit) if args.hard_limit else DeliveryBudget()
    print("budget:  %.0fs remaining, may_start_material=%s may_start_questions=%s"
          % (budget.remaining(), budget.may_start_material(), budget.may_start_questions()))

    started = time.monotonic()
    events = []
    summary = None
    async for event in stream_request(scenarios, args.batch_id, store=store, budget=budget,
                                     group_id=args.group_id):
        events.append(event)
        elapsed = time.monotonic() - started
        kind = event.get("type")
        if kind == "stage":
            print("  [%6.1fs] %-10s %-20s %s"
                  % (elapsed, event.get("slot_id"), event.get("stage"),
                     json.dumps(event.get("detail") or {}, ensure_ascii=False)[:120]))
        elif kind == "request_completed":
            summary = event
        else:
            print("  [%6.1fs] %s" % (elapsed, json.dumps(event, ensure_ascii=False)[:200]))

    if summary is None:
        print("\nFAIL: the stream ended with no request_completed event")
        return 1

    print("\nstatus:  %s  delivered=%s/%s paused=%s"
          % (summary["status"], summary["delivered"], summary["requested"], summary["paused"]))
    for row in summary["slots"]:
        print("  %-10s %-18s resumable=%-5s checkpointed=%-5s material=%s last_failure=%s"
              % (row["slot_id"], row["state"], row["resumable"], row["checkpointed"],
                 row["material_id"], (row["last_failure"] or {}).get("reason")))
    if summary["system_faults"]:
        print("  faults: %s" % json.dumps(summary["system_faults"], ensure_ascii=False))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        path = args.out / ("invocation-%d.json" % stamp)
        path.write_text(json.dumps(
            {"batch_id": args.batch_id, "before": before, "hard_limit": args.hard_limit,
             "elapsed": round(time.monotonic() - started, 1),
             "summary": summary, "events": events},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nrecorded: %s" % path)

    print("\nelapsed: %.1fs" % (time.monotonic() - started))
    print("bucket:  s3://%s/_slots/%s/" % (os.environ.get("IELTS_AUDIO_BUCKET"), args.batch_id))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
