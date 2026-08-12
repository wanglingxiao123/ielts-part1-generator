"""AgentCore Runtime entrypoint (design.md §7).

Protocol only, no business logic. Replacing AgentCore means rewriting this file and nothing else.

The one hard rule here: nothing in the handler may block. It shares an event loop with ``/ping``,
so a synchronous subprocess or file read during a batch would stall the health check and the
platform would kill a healthy instance mid-batch. Every script call goes through
``asyncio.create_subprocess_exec`` (deterministic/runner.py) and the catalogue is loaded in a
thread.

``select`` and ``preview_audio`` are the sharpest cases of that rule. Synthesising one material is
30-45 Polly requests plus as many S3 puts -- tens of seconds of synchronous boto3. So both return
a job id immediately and the work runs in a thread (orchestration/publish.py); the client polls
``audio_status``. Running either inline would be the one call in this file long enough to lose the
instance.

The two are separate actions on purpose. ``select`` claims the candidate group and discards the
siblings; ``preview_audio`` only voices one candidate so a reviewer can listen before deciding, and
must therefore leave the alternatives standing. They share the clips, so a select after a preview
costs nothing further.

``generate`` and ``generate_sets`` are likewise separate, and the reason is a contract rather than a
feature: ``generate`` delivers materials and is allowed to deliver fewer than asked,
``generate_sets`` delivers complete material+question sets and is not (§8.2(3)). Both are SSE. The
slot writes ``generate_sets`` makes are small (~1KB) single PUTs of the same class as the candidate
registration ``generate`` already performs on this loop; if the health check ever suffers, they are
what to move to a thread first, and the honest statement today is that neither has been measured
inside the Runtime.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, Optional

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .orchestration.batch import run_batch
from .orchestration.publish import (
    AlreadySelected,
    SelectionError,
    UnknownMaterial,
    audio_status,
    list_candidates,
    preview_audio,
    select_material,
)
from .orchestration.scenarios import ScenarioCatalogue, load_catalogue
from .request import BadRequest, parse_delivery_request, parse_generate_request

app = BedrockAgentCoreApp()

_catalogue: Optional[ScenarioCatalogue] = None
_catalogue_lock = asyncio.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


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

    if action == "preview_audio":
        return await _preview_audio(payload or {})

    if action == "audio_status":
        material_id = (payload or {}).get("material_id")
        if not material_id:
            return _error("bad_request", "material_id is required")
        return audio_status(str(material_id))

    if action == "list_candidates":
        return {"candidates": list_candidates()}

    if action == "presign_audio":
        return await _presign(payload or {})

    if action == "generate_sets":
        return _generate_sets(payload or {})

    if action == "revise_questions_from_comments":
        return _revise_questions_from_comments(payload or {})

    if action == "replan_questions_from_comments":
        return _replan_questions_from_comments(payload or {})

    if action != "generate":
        return {
            "error": "unknown action %r; expected generate, generate_sets, list_scenarios, select, "
                     "preview_audio, audio_status, list_candidates, presign_audio or "
                     "revise_questions_from_comments or replan_questions_from_comments" % action
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


async def _preview_audio(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Voice one candidate so it can be heard, without accepting it. Returns a job, does not wait.

    Deliberately NOT ``select`` with a flag: ``select`` claims the candidate group and deletes the
    siblings, so a reviewer who only wanted to listen would lose the alternative they were still
    comparing against. Hence a separate action with no ``AlreadySelected`` branch -- previewing a
    candidate whose sibling was chosen is still a legitimate request, and the group is not consulted.
    """
    material_id = payload.get("material_id")
    if not material_id:
        return _error("bad_request", "material_id is required")
    try:
        return await preview_audio(str(material_id), actor=str(payload.get("actor") or "user"))
    except UnknownMaterial as exc:
        return _error("UNKNOWN_MATERIAL", str(exc), material_id=material_id)
    except SelectionError as exc:
        return _error("preview_failed", str(exc), material_id=material_id)


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


async def _generate_sets(payload: Dict[str, Any]):
    """``action=generate_sets``: N complete material+question sets under one resumable ``batch_id``.

    A separate action from ``generate``, not a flag on it. ``generate`` delivers materials and may
    deliver fewer than asked (``orchestration/batch.py``); this one delivers complete sets and may not
    (§8.2(3)). Two contracts that opposite in the one field every caller reads must not arrive under one
    name -- and the deployed frontend is on ``generate``, so keeping it untouched is also what lets this
    branch ship without a coordinated frontend release.

    An async generator for the same reason ``_generate`` is: this is the SSE branch, and the stage
    events are the keepalive. Called again with the same ``batch_id`` it resumes from the stored slot
    state rather than regenerating (§8.2(4)); the terminal ``request_completed`` says which of the three
    statuses it reached.
    """
    from .orchestration.delivery import stream_request

    found = await catalogue()
    try:
        request = parse_delivery_request(found, payload)
    except BadRequest as exc:
        # `batch_failed` and not `request_completed`: nothing was planned, so there is no request whose
        # status could be reported. The frontend already treats this shape as a terminal failure, and
        # inventing a `system_failure` summary for a payload that never named a request would put a
        # batch_id nobody issued into the one field resumption keys on.
        yield {"type": "batch_failed", "reason": "bad_request", "detail": str(exc)}
        return
    async for event in stream_request(
        request.slots, request.batch_id, budget=request.budget,
        concurrency=request.concurrency, group_id=request.group_id,
    ):
        yield event


async def _revise_questions_from_comments(payload: Dict[str, Any]):
    """Stream one reviewer-initiated revision; never mutate material or blueprint."""
    from .orchestration.manual_question_revision import revise_from_comments
    from .orchestration.slot_store import build_slot_store

    required = ("material_id", "request_id", "base_version_id",
                "material", "blueprint", "package", "base_version", "comments")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        yield {"type": "question_revision_failed",
               "message": "missing required fields: %s" % ", ".join(missing)}
        return
    for key in ("material_id", "request_id", "base_version_id"):
        if not _SAFE_ID.fullmatch(str(payload.get(key) or "")):
            yield {"type": "question_revision_failed",
                   "message": "%s is invalid" % key}
            return
    if not all(isinstance(payload.get(key), dict)
               for key in ("material", "blueprint", "package", "base_version")):
        yield {"type": "question_revision_failed",
               "message": "material, blueprint, package and base_version must be JSON objects"}
        return
    comments = payload.get("comments")
    if not isinstance(comments, list) or not comments:
        yield {"type": "question_revision_failed", "message": "question comments are required"}
        return
    for row in comments:
        anchor = row.get("anchor") if isinstance(row, dict) else None
        number = anchor.get("index") if isinstance(anchor, dict) else None
        if (not isinstance(row, dict) or not str(row.get("id") or "").strip()
                or not isinstance(anchor, dict) or anchor.get("type") != "question"
                or isinstance(number, bool) or not isinstance(number, int)
                or not 1 <= number <= 10 or not str(row.get("text") or "").strip()):
            yield {"type": "question_revision_failed",
                   "message": "every comment must identify one question from Q1 to Q10"}
            return
    async for event in revise_from_comments(
        store=build_slot_store(),
        material_id=str(payload["material_id"]),
        request_id=str(payload["request_id"]),
        base_version_id=str(payload["base_version_id"]),
        material=payload["material"],
        blueprint=payload["blueprint"],
        package=payload["package"],
        base_version=payload["base_version"],
        comments=comments,
        actor=str(payload.get("actor") or "reviewer"),
    ):
        yield event


async def _replan_questions_from_comments(payload: Dict[str, Any]):
    """Stream a confirmed full replan while keeping the supplied material immutable."""
    from .orchestration.manual_question_replan import replan_from_comments
    from .orchestration.slot_store import build_slot_store

    required = (
        "material_id", "request_id", "source_request_id", "base_version_id",
        "material", "blueprint", "package", "comments",
    )
    missing = [key for key in required if not payload.get(key)]
    if missing:
        yield {
            "type": "question_revision_failed",
            "message": "missing required fields: %s" % ", ".join(missing),
        }
        return
    for key in ("material_id", "request_id", "source_request_id", "base_version_id"):
        if not _SAFE_ID.fullmatch(str(payload.get(key) or "")):
            yield {
                "type": "question_revision_failed",
                "message": "%s is invalid" % key,
            }
            return
    if not all(
        isinstance(payload.get(key), dict)
        for key in ("material", "blueprint", "package")
    ):
        yield {
            "type": "question_revision_failed",
            "message": "material, blueprint and package must be JSON objects",
        }
        return
    comments = payload.get("comments")
    if not isinstance(comments, list) or not comments:
        yield {
            "type": "question_revision_failed",
            "message": "source question comments are required",
        }
        return
    for row in comments:
        anchor = row.get("anchor") if isinstance(row, dict) else None
        number = anchor.get("index") if isinstance(anchor, dict) else None
        if (
            not isinstance(row, dict)
            or not str(row.get("id") or "").strip()
            or not isinstance(anchor, dict)
            or anchor.get("type") != "question"
            or isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= 10
            or not str(row.get("text") or "").strip()
        ):
            yield {
                "type": "question_revision_failed",
                "message": "every source comment must identify one question from Q1 to Q10",
            }
            return
    async for event in replan_from_comments(
        store=build_slot_store(),
        material_id=str(payload["material_id"]),
        request_id=str(payload["request_id"]),
        source_request_id=str(payload["source_request_id"]),
        base_version_id=str(payload["base_version_id"]),
        material=payload["material"],
        blueprint=payload["blueprint"],
        package=payload["package"],
        comments=comments,
        actor=str(payload.get("actor") or "reviewer"),
    ):
        yield event


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    app.run(port=int(os.environ.get("PORT", "8080")))
