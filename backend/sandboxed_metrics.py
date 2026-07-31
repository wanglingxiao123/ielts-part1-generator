"""Run the audit metrics script in AgentCore's Code Interpreter, not in this container.

This module is where the blindness guarantee is enforced, and it is enforced by *omission*: the
only things uploaded to the remote environment are the audit script and the material. The
blueprint is never written, never passed, and has no parameter here to arrive through.

**Why a remote sandbox rather than a local subprocess.** The audit agent needs to run a script,
and the obvious way to let it is `strands_tools.shell`. Measured, that tool does not go through
`agent.sandbox` at all -- it calls `pty.fork()` directly, and its signature
(`shell(command, parallel, ignore_errors, timeout, work_dir)`) has no `agent` parameter, so no
sandbox can constrain it. A `shell`-equipped audit agent can therefore `cat` the generator's
blueprint schema and its validator. That is exactly the one thing the audit side cannot survive.

Code Interpreter inverts the problem. Isolation is a whitelist rather than a blacklist: the remote
environment starts empty and contains only what is uploaded. There is no path traversal to defend
against, because the repository is not there. Measured against the live service:

    init_session      2.5s   (once per material, then reused)
    write_files       0.6s
    execute_command   1.2s
    end to end        4.5s   cold, ~1.2s warm

Python 3.12.13 with the standard library, which is all `audit_metrics.py` needs.

**What this module does not do.** It does not decide anything about the material. It runs one
script and returns its JSON. Every judgement stays in `orchestration/loop.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "MetricsError",
    "SandboxedMetrics",
    "run_metrics_sandboxed",
]

# The built-in interpreter. No resource to provision, no ARN to thread through deployment.
DEFAULT_IDENTIFIER = "aws.codeinterpreter.v1"

REGION_ENV = "IELTS_CODE_INTERPRETER_REGION"
IDENTIFIER_ENV = "IELTS_CODE_INTERPRETER_ID"

# Uploaded filenames. Fixed rather than derived from the local paths: the remote working directory
# is flat, and a name carrying a local directory component would fail to resolve there.
SCRIPT_NAME = "audit_metrics.py"
MATERIAL_NAME = "material.json"

# A session outlives one material's audit and re-audit, which is the point of reusing it -- but the
# platform's own timeout is 900s, so nothing here may assume a session survives a whole batch.
SESSION_TIMEOUT_SECONDS = 900


class MetricsError(RuntimeError):
    """The metrics script could not be run, or produced no usable JSON.

    Raised rather than returning a degraded result: `run_metrics` is called through
    `_with_infra_retries`, so an infrastructure failure here retries on the infrastructure budget
    instead of being mistaken for a statement about the material.
    """


def _safe_session_name(material_id: str) -> str:
    """A session name derived from the material, so concurrent slots cannot collide.

    Sanitised because the platform rejects most punctuation, and truncated because a long id plus a
    prefix exceeds the accepted length. The suffix keeps ids distinguishable after truncation.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", material_id or "unknown")
    return ("ielts-audit-%s" % cleaned)[:60]


class SandboxedMetrics(object):
    """One material's metrics runs, sharing a single remote session.

    Constructed per material rather than per process: a shared session would make two concurrent
    audits overwrite each other's `material.json`, and the failure would be a metrics result
    computed from the wrong script -- silent, and attributed to the wrong material.
    """

    __slots__ = ("_material_id", "_script_path", "_client", "_session", "_started")

    def __init__(self, material_id: str, script_path: Path) -> None:
        self._material_id = material_id
        self._script_path = Path(script_path)
        self._client: Optional[Any] = None
        self._session = _safe_session_name(material_id)
        self._started = False

    def _build_client(self) -> Any:
        # Imported here rather than at module scope: `strands_tools` pulls in a large dependency
        # tree, and the deterministic layer must stay importable without it.
        from strands_tools.code_interpreter import AgentCoreCodeInterpreter

        region = (os.environ.get(REGION_ENV) or os.environ.get("AWS_REGION") or "us-east-1").strip()
        identifier = (os.environ.get(IDENTIFIER_ENV) or DEFAULT_IDENTIFIER).strip()
        return AgentCoreCodeInterpreter(
            region=region,
            identifier=identifier,
            session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
        )

    def _start_blocking(self, material: Dict[str, Any]) -> None:
        """Create the session and upload exactly two files. Blocking; called in a thread.

        The upload list is the blindness boundary. It has two entries, and a third would have to be
        added here deliberately -- there is no caller-supplied path that could add one.
        """
        from strands_tools.code_interpreter.models import (
            FileContent,
            InitSessionAction,
            WriteFilesAction,
        )

        self._client = self._build_client()
        self._client.init_session(InitSessionAction(
            type="initSession",
            session_name=self._session,
            description="IELTS audit metrics for %s" % self._material_id,
        ))
        result = self._client.write_files(WriteFilesAction(
            type="writeFiles",
            session_name=self._session,
            content=[
                FileContent(
                    path=SCRIPT_NAME,
                    text=self._script_path.read_text(encoding="utf-8"),
                ),
                FileContent(
                    path=MATERIAL_NAME,
                    text=json.dumps(material, ensure_ascii=False),
                ),
            ],
        ))
        if result.get("status") != "success":
            raise MetricsError("upload failed: %s" % str(result)[:300])
        self._started = True

    def _run_blocking(self) -> Dict[str, Any]:
        """Execute the script and parse its stdout. Blocking; called in a thread."""
        from strands_tools.code_interpreter.models import ExecuteCommandAction

        result = self._client.execute_command(ExecuteCommandAction(
            type="executeCommand",
            session_name=self._session,
            command="python3 %s --json %s" % (SCRIPT_NAME, MATERIAL_NAME),
        ))
        text = _collect_text(result)
        if result.get("status") != "success":
            raise MetricsError("execution failed: %s" % text[:300])
        return _extract_json(text)

    async def run(self, material: Dict[str, Any]) -> Dict[str, Any]:
        """Upload if needed, run the script, return its parsed output.

        In a thread throughout: the AgentCore client is synchronous boto3, and this runs on the
        event loop that also serves `/ping`. A blocking call here would stall the health check and
        the platform would kill a healthy instance mid-batch.
        """
        if not self._started:
            await asyncio.to_thread(self._start_blocking, material)
        else:
            await asyncio.to_thread(self._upload_material_blocking, material)
        return await asyncio.to_thread(self._run_blocking)

    def _upload_material_blocking(self, material: Dict[str, Any]) -> None:
        """Replace `material.json` for a second run in the same session (the re-audit)."""
        from strands_tools.code_interpreter.models import FileContent, WriteFilesAction

        result = self._client.write_files(WriteFilesAction(
            type="writeFiles",
            session_name=self._session,
            content=[FileContent(path=MATERIAL_NAME,
                                 text=json.dumps(material, ensure_ascii=False))],
        ))
        if result.get("status") != "success":
            raise MetricsError("material re-upload failed: %s" % str(result)[:300])

    async def close(self) -> None:
        """Release the remote session.

        Best-effort: the platform reclaims a session after its timeout anyway, so a failure here
        costs an idle session rather than correctness, and raising would turn cleanup into the
        reason a finished material fails.
        """
        if self._client is None:
            return
        try:
            await asyncio.to_thread(self._client.cleanup_platform)
        except Exception:  # noqa: BLE001 - see docstring
            pass
        finally:
            self._client = None
            self._started = False


def _collect_text(result: Dict[str, Any]) -> str:
    """Flatten the platform's content blocks into one string."""
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(block["json"], ensure_ascii=False))
    return "".join(parts)


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull the script's JSON out of captured stdout.

    Two layers of wrapping have to be peeled, and the second one is easy to miss. The platform
    returns content blocks whose `text` value is itself the `str()` of a Python list of blocks:

        {'text': '[{\\'type\\': \\'text\\', \\'text\\': \\'{"assessable": true...}\\\\r\\\\n\\'}]'}

    So a naive scan for the first `{` lands on the *outer* dict literal, which is not JSON (single
    quotes), and every subsequent brace is inside a string. Hence: unescape first, then scan for the
    first balanced object that parses AND looks like the metrics result.

    Output also arrives over a PTY, so line endings are `\\r\\n`.
    """
    cleaned = (text or "").replace("\\r\\n", "\n").replace("\\n", "\n")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise MetricsError("metrics script produced no output")

    decoder = json.JSONDecoder()
    fallback: Optional[Dict[str, Any]] = None
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        # `assessable` is audit_metrics.py's own top-level key, so it distinguishes the result from
        # any JSON the platform wraps around it. Anything else is kept only as a last resort.
        if "assessable" in value:
            return value
        if fallback is None:
            fallback = value
    if fallback is not None:
        return fallback
    raise MetricsError("metrics output contained no JSON object: %r" % cleaned[:300])


async def run_metrics_sandboxed(
    material: Dict[str, Any], script_path: Path, material_id: str = "unknown"
) -> Dict[str, Any]:
    """One-shot convenience: run the metrics script and drop the session.

    For a single audit. When a material will be audited twice (original and revision), hold a
    `SandboxedMetrics` instance instead so the 2.5s session setup is paid once.
    """
    runner = SandboxedMetrics(material_id, script_path)
    try:
        return await runner.run(material)
    finally:
        await runner.close()
