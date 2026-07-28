#!/usr/bin/env python3
"""Run the web tier's suite. Mirrors the other packages' `run_tests.py` entry points.

    .venv-backend/bin/python web/tests/run_tests.py

These tests are pytest-based (unlike audio_storage's hand-rolled runner) because the SSE relay
tests need `pytest-asyncio` to drive the ASGI app directly. Nothing here touches AWS: the runtime
client is stubbed and the user store is in memory.

`-c /dev/null` is deliberate. The repo's root `pytest.ini` sets `testpaths = backend/tests`, so a
bare `pytest web/tests` from the repo root would silently collect the wrong directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main() -> int:
    return subprocess.call(
        [sys.executable, "-m", "pytest", str(HERE), "-q",
         "-c", "/dev/null", "-o", "asyncio_mode=auto", "-p", "no:cacheprovider",
         *sys.argv[1:]],
        cwd=str(ROOT),
    )


if __name__ == "__main__":
    raise SystemExit(main())
