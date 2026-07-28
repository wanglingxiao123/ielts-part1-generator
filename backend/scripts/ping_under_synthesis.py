#!/usr/bin/env python
"""Measure /ping latency while a real synthesis runs (prd.md R2, acceptance item 5).

    IELTS_AUDIO_BUCKET=... .venv-backend/bin/python backend/scripts/ping_under_synthesis.py \
        --material <path> --blueprint <path> --audit <path> --scenario-key <key>

Why this script rather than backend/scripts/check_ping.sh: that one samples /ping while a
*generation* batch runs, which is model calls -- already awaited, already known good. Synthesis
is different in kind. It is CPU and blocking-IO inside boto3, and if it were run on the event
loop the health check would not merely slow down, it would stop for the whole duration. That is
the failure AgentCore reads as an unhealthy instance and terminates mid-batch.

So this starts the real ASGI app, POSTs a real selection, and samples /ping throughout. The
numbers reported are min/mean/p95/max during synthesis, next to a quiet baseline. Without the
baseline a "12ms" figure means nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
import uvicorn  # noqa: E402


async def sample_ping(client: "httpx.AsyncClient", url: str) -> float:
    started = time.perf_counter()
    response = await client.get(url, timeout=5.0)
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        raise RuntimeError("/ping returned {0}".format(response.status_code))
    return elapsed


def stats(samples: "list[float]") -> dict:
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 1),
        "mean_ms": round(statistics.fmean(ordered), 1),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max_ms": round(ordered[-1], 1),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--material", type=Path, required=True)
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--scenario-key", required=True)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    from audio_storage.state_store import new_material_id
    from backend.app import app as agentcore_app
    from backend.orchestration.publish import REGISTRY, Candidate, audio_status

    material = json.loads(args.material.read_text(encoding="utf-8"))
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    material_id = new_material_id(args.scenario_key)

    REGISTRY.register(
        Candidate(
            material_id=material_id, scenario_key=args.scenario_key,
            group_key="ping-probe", slot_id="slot-1", material=material,
            blueprint=blueprint, audit=audit,
        )
    )

    # The real ASGI app, so /ping is the framework's own handler on the same loop as the
    # entrypoint. Testing a hand-rolled server would prove nothing about the deployed shape.
    asgi = getattr(agentcore_app, "app", None) or agentcore_app
    config = uvicorn.Config(asgi, host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    base = "http://127.0.0.1:{0}".format(args.port)

    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        print("server did not start")
        return 1

    async with httpx.AsyncClient() as client:
        print("== baseline: /ping with nothing running ==")
        baseline = [await sample_ping(client, base + "/ping") for _ in range(20)]
        for value in baseline[:5]:
            print("  {0:.1f}ms".format(value))
        print("  ", json.dumps(stats(baseline)))

        print("\n== POST /invocations action=select (must return immediately) ==")
        started = time.perf_counter()
        response = await client.post(
            base + "/invocations",
            json={"action": "select", "material_id": material_id},
            timeout=30.0,
        )
        handoff_ms = (time.perf_counter() - started) * 1000
        payload = response.json()
        print("  handoff: {0:.0f}ms".format(handoff_ms))
        print("  response:", json.dumps(payload)[:300])
        if payload.get("error"):
            print("SELECTION FAILED")
            server.should_exit = True
            await server_task
            return 1

        print("\n== /ping sampled DURING synthesis ==")
        during: list = []
        progress = []
        while True:
            status = audio_status(material_id)
            if status["status"] in ("ready", "failed", "quarantined"):
                break
            during.append(await sample_ping(client, base + "/ping"))
            progress.append(status["progress"]["done"])
            await asyncio.sleep(args.interval)
            if len(during) > 3000:
                break

        final = audio_status(material_id)
        print("  samples while synthesising:", len(during))
        if during:
            for value in during[: min(8, len(during))]:
                print("    {0:.1f}ms".format(value))
            print("  ", json.dumps(stats(during)))
        print("  progress observed:", progress[:12], "...", progress[-3:] if progress else [])
        print("  final status:", final["status"], json.dumps(final["progress"]))
        print("  synthesis elapsed:", final["elapsed_seconds"], "s")
        print("  polly calls:", final["polly_calls"], " cost: ${0}".format(final["cost_usd"]))

        print("\n== baseline again, after synthesis ==")
        after = [await sample_ping(client, base + "/ping") for _ in range(10)]
        print("  ", json.dumps(stats(after)))

        # The health check must stay well inside the platform's budget. A stalled loop shows up
        # here as a multi-second max, not as a slightly higher mean.
        verdict = bool(during) and stats(during)["max_ms"] < 1000
        print("\nVERDICT:", "PASS -- /ping stayed healthy throughout"
              if verdict else "FAIL -- /ping degraded during synthesis")

        report = {
            "material_id": material_id,
            "baseline_before": stats(baseline),
            "during_synthesis": stats(during) if during else None,
            "baseline_after": stats(after),
            "select_handoff_ms": round(handoff_ms, 1),
            "synthesis": final,
            "verdict": verdict,
        }
        Path("/tmp/ielts-e2e").mkdir(parents=True, exist_ok=True)
        Path("/tmp/ielts-e2e/ping-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("report: /tmp/ielts-e2e/ping-report.json")

    if not args.keep and final["status"] == "ready":
        from audio_storage.object_store import S3ObjectStore
        from backend import audio as audio_config

        backing = S3ObjectStore(audio_config.bucket_name())
        prefix = "pending/{0}/{1}/".format(args.scenario_key, material_id)
        keys = backing.list_keys(prefix)
        backing.delete(keys)
        print("cleaned up", len(keys), "objects")

    server.should_exit = True
    await server_task
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
