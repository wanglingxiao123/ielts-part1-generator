#!/usr/bin/env python3
"""Run the exporter's suite. Mirrors web/tests/run_tests.py.

    python3 qti_export/tests/run_tests.py

`-c /dev/null` because the repo's root `pytest.ini` points `testpaths` at backend/tests.

The XSD tests need the local schema mirror (`python3 -m qti_export.fetch_schemas`, or
`QTI_XSD_MIRROR=...`) and lxml; without them they SKIP rather than fail, and say so.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main() -> int:
    return subprocess.call(
        [sys.executable, "-m", "pytest", str(HERE), "-q", "-rs",
         "-c", "/dev/null", "-p", "no:cacheprovider", *sys.argv[1:]],
        cwd=str(ROOT),
    )


if __name__ == "__main__":
    raise SystemExit(main())
