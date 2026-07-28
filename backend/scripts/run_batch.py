#!/usr/bin/env python
"""Run a batch locally and record the SSE event stream (implement.md phases 5 and 8).

    python backend/scripts/run_batch.py --scenarios accommodation-rental booking-hotel \
        --count 1 --out /tmp/ielts-batch

The recorded event stream is the handover artifact for the frontend and audio-storage tasks:
they consume these event types, so a real recording is a better contract document than a table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.orchestration.batch import BatchRequest, Budget  # noqa: E402
from backend.orchestration.batch import run_batch  # noqa: E402
from backend.orchestration.scenarios import load_catalogue  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", nargs="+", required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--hard-limit", type=float, default=None,
                        help="shrink the budget to exercise the degradation path")
    parser.add_argument("--out", type=Path, default=Path("/tmp/ielts-batch"))
    args = parser.parse_args()

    catalogue = load_catalogue()
    slots = []
    for scenario_id in args.scenarios:
        scenario = catalogue.get(scenario_id)
        if scenario is None:
            print("unknown scenario %r" % scenario_id)
            return 2
        slots.extend([scenario] * args.count)

    budget = Budget(hard_limit=args.hard_limit) if args.hard_limit else Budget()
    request = BatchRequest(slots=slots, concurrency=args.concurrency, budget=budget)

    args.out.mkdir(parents=True, exist_ok=True)
    stream = args.out / "events.jsonl"
    started = time.monotonic()
    with stream.open("w", encoding="utf-8") as handle:
        async for event in run_batch(request):
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            elapsed = time.monotonic() - started
            if event["type"] == "stage":
                print("[%6.1fs] %-8s %-18s %s" % (elapsed, event["slot_id"], event["stage"],
                                                  json.dumps(event.get("detail") or {})[:100]))
            elif event["type"] == "material_completed":
                print("[%6.1fs] %-8s DONE verdict=%s score=%s version=%s route=%s (%ss)" % (
                    elapsed, event["slot_id"], event["audit"].get("verdict"),
                    (event["audit"].get("score") or {}).get("total"),
                    event["selected_version"], event["route"],
                    event["timings"].get("total")))
                slot_dir = args.out / event["slot_id"]
                slot_dir.mkdir(exist_ok=True)
                for name in ("material", "blueprint", "audit", "cross_check"):
                    (slot_dir / ("%s.json" % name)).write_text(
                        json.dumps(event[name], ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            elif event["type"] == "material_failed":
                print("[%6.1fs] %-8s FAILED %s" % (elapsed, event["slot_id"], event["reason"]))
            else:
                print("[%6.1fs] %s" % (elapsed, json.dumps(event, ensure_ascii=False)[:400]))

    print("\nevents: %s" % stream)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
