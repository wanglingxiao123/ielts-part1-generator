#!/usr/bin/env python
"""Run the Loop for one material against the live model and record real timings.

    python backend/scripts/run_one.py --scenario accommodation-rental --out /tmp/run1

The timing data this produces is what decides the real batch ceiling. "6 materials in 15
minutes" was an assumption at planning time (prd.md's uncertainty list), not a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.model import provider  # noqa: E402
from backend.orchestration.loop import run_one  # noqa: E402
from backend.orchestration.scenarios import load_catalogue  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="accommodation-rental")
    parser.add_argument("--out", type=Path, default=Path("/tmp/ielts-run"))
    parser.add_argument("--slot", default="slot-1")
    args = parser.parse_args()

    catalogue = load_catalogue()
    scenario = catalogue.get(args.scenario)
    if scenario is None:
        print("unknown scenario %r; available: %s" % (args.scenario, catalogue.ids()))
        return 2

    print("model: %s" % provider.describe())
    print("scenario: %s (%s)" % (scenario.id, scenario.title_zh))
    started = time.monotonic()

    async def emit(stage, detail=None):
        print("  [%6.1fs] %-18s %s" % (time.monotonic() - started, stage,
                                        json.dumps(detail or {}, ensure_ascii=False)[:160]))

    result = await run_one(scenario, args.slot, emit)

    args.out.mkdir(parents=True, exist_ok=True)
    payload = result.as_dict()
    (args.out / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if result.ok:
        for name in ("material", "blueprint", "audit"):
            (args.out / ("%s.json" % name)).write_text(
                json.dumps(payload[name], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (args.out / "cross_check.json").write_text(
            json.dumps(payload["cross_check"], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print("\n--- result ---")
    print("ok: %s" % result.ok)
    if result.ok:
        audit = payload["audit"]
        print("verdict: %s   score: %s" % (audit.get("verdict"),
                                           (audit.get("score") or {}).get("total")))
        print("selected_version: %s   route: %s   note: %s"
              % (payload["selected_version"], payload["route"], payload["note"]))
        print("metrics: %s" % json.dumps(audit.get("metrics"), ensure_ascii=False))
        print("cross_check: %s" % json.dumps(
            {k: v for k, v in payload["cross_check"].items()
             if k in ("ok", "planned", "observed", "matched")}))
        print("information points: %d" % len(payload["blueprint"].get("items", [])))
    else:
        print("reason: %s" % result.reason)
        print("detail: %s" % json.dumps(result.detail, ensure_ascii=False)[:2000])
    print("timings: %s" % json.dumps(result.timings))
    print("artifacts: %s" % args.out)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
