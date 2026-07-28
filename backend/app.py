"""AgentCore Runtime entrypoint (design.md §7).

Protocol only, no business logic. Replacing AgentCore means rewriting this file and nothing else.

The one hard rule here: nothing in the handler may block. It shares an event loop with ``/ping``,
so a synchronous subprocess or file read during a batch would stall the health check and the
platform would kill a healthy instance mid-batch. Every script call goes through
``asyncio.create_subprocess_exec`` (deterministic/runner.py) and the catalogue is loaded in a
thread.

``select`` is the sharpest case of that rule. Synthesising one material is 30-45 Polly requests
plus as many S3 puts -- tens of seconds of synchronous boto3. So it returns a job id immediately
and the work runs in a thread (orchestration/publish.py); the client polls ``audio_status``.
Running it inline would be the one call in this file long enough to lose the instance.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .orchestration.batch import run_batch
from .orchestration.publish import (
    AlreadySelected,
    SelectionError,
    UnknownMaterial,
    audio_status,
    list_candidates,
    select_material,
)
from .orchestration.scenarios import ScenarioCatalogue, load_catalogue
from .request import BadRequest, parse_generate_request

app = BedrockAgentCoreApp()

_catalogue: Optional[ScenarioCatalogue] = None
_catalogue_lock = asyncio.Lock()


async def catalogue() -> ScenarioCatalogue:
    """Load scenarios.yaml once, off the event loop thread."""
    global _catalogue
    async with _catalogue_lock:
        if _catalogue is None:
            _catalogue = await asyncio.to_thread(load_catalogue)
    return _catalogue


@app.entrypoint
async def invoke(payload: Dict[str, Any]):
    """``list_scenarios`` returns a dict (JSON); ``generate`` yields events (SSE).

    BedrockAgentCoreApp picks the content type from whether the handler is an async generator,
    so the two branches cannot share one function -- hence the delegation below.
    """
    action = (payload or {}).get("action", "generate")

    if action == "list_scenarios":
        found = await catalogue()
        return {"scenarios": found.as_dict()}

    if action == "select":
        return await _select(payload or {})

    if action == "audio_status":
        material_id = (payload or {}).get("material_id")
        if not material_id:
            return _error("bad_request", "material_id is required")
        return audio_status(str(material_id))

    if action == "list_candidates":
        return {"candidates": list_candidates()}

    if action == "presign_audio":
        return await _presign(payload or {})

    if action != "generate":
        return {
            "error": "unknown action %r; expected generate, list_scenarios, select, "
                     "audio_status, list_candidates or presign_audio" % action
        }

    return _generate(payload)


def _error(code: str, message: str, **detail: Any) -> Dict[str, Any]:
    """The frontend contract's error shape (frontend design.md §8)."""
    body: Dict[str, Any] = {"code": code, "message": message}
    if detail:
        body["detail"] = detail
    return {"error": body}


async def _select(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Accept the user's pick. Returns a job, does not wait for it.

    ``wait`` is deliberately not exposed through the payload: a caller could otherwise ask the
    handler to block for a minute, which is the failure this whole design avoids.
    """
    material_id = payload.get("material_id")
    if not material_id:
        return _error("bad_request", "material_id is required")
    try:
        return await select_material(str(material_id), actor=str(payload.get("actor") or "user"))
    except AlreadySelected as exc:
        return _error("ALREADY_SELECTED", str(exc), material_id=material_id)
    except UnknownMaterial as exc:
        return _error("UNKNOWN_MATERIAL", str(exc), material_id=material_id)
    except SelectionError as exc:
        return _error("selection_failed", str(exc), material_id=material_id)


async def _presign(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Presigned GET URLs keyed by turn_index. The frontend never sees an S3 key.

    In a thread: presigning is local signing, but ``locate`` behind it lists S3 prefixes, and a
    list call on the event loop is the same class of mistake as synthesising on it.
    """
    material_id = payload.get("material_id")
    if not material_id:
        return _error("bad_request", "material_id is required")
    ttl = payload.get("ttl_seconds") or 3600
    from . import audio

    def work() -> Dict[str, Any]:
        state_store, _ = audio.build_state_store()
        urls = state_store.presign_audio(str(material_id), ttl_seconds=int(ttl))
        return {"material_id": material_id,
                "urls": {str(k): v for k, v in sorted(urls.items())},
                "ttl_seconds": int(ttl)}

    try:
        return await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001 - reported to the caller rather than raised into SSE
        return _error("presign_failed", "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                      material_id=material_id)


async def _generate(payload: Dict[str, Any]):
    found = await catalogue()
    try:
        request = parse_generate_request(found, payload)
    except BadRequest as exc:
        yield {"type": "batch_failed", "reason": "bad_request", "detail": str(exc)}
        return
    async for event in run_batch(request):
        yield event


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    app.run(port=int(os.environ.get("PORT", "8080")))
