"""Non-blocking subprocess helper for the three skill scripts.

The AgentCore entrypoint runs on the same event loop that answers ``/ping``, so a blocking
``subprocess.run`` during a batch would stall the health check and the platform would kill the
instance mid-batch (design.md §7). Everything here goes through
``asyncio.create_subprocess_exec``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["ScriptError", "run_script_json", "temp_json"]

# The container may ship a newer interpreter than the skill scripts were written against; they
# are 3.9-compatible so any 3.9+ works. Overridable for the same reason the model id is.
SCRIPT_PYTHON = os.environ.get("IELTS_SCRIPT_PYTHON", sys.executable)
SCRIPT_TIMEOUT = float(os.environ.get("IELTS_SCRIPT_TIMEOUT", "60"))


class ScriptError(RuntimeError):
    """A skill script produced no parseable JSON.

    This is infrastructure, not a material defect: skill-contract's D1 fix guarantees content
    problems come back as ``{"ok": false, ...}`` on stdout. Reaching this class means the
    script crashed, timed out, or is missing -- so the Loop retries on its infrastructure
    budget instead of burning a generation attempt.
    """


class temp_json(object):
    """Write JSON documents to a scratch directory for CLI consumption.

    The skill scripts take file paths, not stdin. Kept as a context manager so a failure
    anywhere in the Loop cannot leave temp files behind across a long-lived Runtime instance.
    """

    def __init__(self, **documents: Any) -> None:
        self._documents = documents
        self._dir: Optional[tempfile.TemporaryDirectory] = None
        self.paths: Dict[str, Path] = {}

    def __enter__(self) -> Dict[str, Path]:
        self._dir = tempfile.TemporaryDirectory(prefix="ielts-")
        root = Path(self._dir.name)
        for name, document in self._documents.items():
            path = root / ("%s.json" % name)
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.paths[name] = path
        return self.paths

    def __exit__(self, *exc_info: Any) -> None:
        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None


async def run_script_json(
    script: Path, args: Sequence[str], timeout: float = SCRIPT_TIMEOUT
) -> Dict[str, Any]:
    """Run a skill script with ``--json`` and return its parsed stdout.

    Exit code is deliberately ignored for parsing purposes: every one of these scripts exits 1
    to mean "found problems", which is a normal, expected result the Loop acts on. Only an
    unparseable stdout is an error.
    """
    command: List[str] = [SCRIPT_PYTHON, str(script)] + [str(a) for a in args]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ScriptError("could not launch %s: %s" % (script.name, exc))

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ScriptError("%s timed out after %.0fs" % (script.name, timeout))

    text = (stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        raise ScriptError(
            "%s produced no output (exit %s): %s"
            % (script.name, process.returncode, (stderr or b"").decode("utf-8", "replace")[-400:])
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptError("%s output was not JSON: %s" % (script.name, exc))
    if not isinstance(parsed, dict):
        raise ScriptError("%s output was not a JSON object" % script.name)
    return parsed
