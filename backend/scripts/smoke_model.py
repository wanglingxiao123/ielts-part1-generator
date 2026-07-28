#!/usr/bin/env python
"""Minimal model reachability check (implement.md phase 2).

Run this before debugging anything in the Loop. Failures here come from region, permissions, API
shape or package version -- causes with nothing in common with orchestration bugs, and very hard
to separate once the two are mixed together.

    python backend/scripts/smoke_model.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.model import provider  # noqa: E402
from backend.steps.call import call_json  # noqa: E402


async def main() -> int:
    print("config: %s" % provider.describe())
    model = provider.build_model(max_output_tokens=2000, reasoning_effort="low")
    started = time.monotonic()
    try:
        payload = await call_json(
            model,
            "You return only JSON.",
            'Return exactly this JSON object: {"status": "ok"}',
        )
    except Exception as exc:  # noqa: BLE001 - the point is to print the real cause
        print("FAIL %s: %s" % (type(exc).__name__, exc))
        if "invalid_api_key" in str(exc) or "security token" in str(exc):
            print(
                "\nHint: with IELTS_MODEL_AUTH=mantle, Strands mints a bearer token from AWS\n"
                "SigV4 credentials. A 401 here usually means those credentials expired, not\n"
                "that model access was revoked. Check `aws sts get-caller-identity`; for local\n"
                "work with a pre-minted token, set IELTS_MODEL_AUTH=bearer."
            )
        return 1
    print("response: %s" % payload)
    print("elapsed: %.2fs" % (time.monotonic() - started))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
