"""Reviewer-initiated question revision with immutable material and blueprint."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List

from ..deterministic.question_crosscheck import crosscheck_questions
from ..deterministic.question_metrics import question_metrics
from ..deterministic.validate_questions import validate_questions
from ..steps import agent_steps
from .question_loop import QuestionCandidate, hard_blockers
from .slot_store import SlotStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def revise_from_comments(
    *,
    store: SlotStore,
    material_id: str,
    request_id: str,
    base_version_id: str,
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
    comments: List[Dict[str, Any]],
    actor: str,
) -> AsyncIterator[Dict[str, Any]]:
    """Run one manual revision and persist only a fully deliverable package."""
    started = time.time()
    existing_version = store.load_question_version(material_id, request_id)
    if existing_version is not None:
        yield {
            "type": "question_revision_completed",
            "request_id": request_id,
            "version_id": request_id,
            "elapsed_seconds": 0,
        }
        return
    existing_request = store.load_question_revision(material_id, request_id)
    if isinstance(existing_request, dict):
        status = existing_request.get("status")
        if status == "needs_material_revision":
            yield {
                "type": "question_revision_needs_material",
                "request_id": request_id,
                "reasons": existing_request.get("reasons") or [],
            }
            return
        if status in ("failed", "completed"):
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": "这次题目修改已经结束，未启动重复调用。",
            }
            return
    if not store.persistent:
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "题目版本存储未配置，未启动修改。",
        }
        return
    if not store.claim_question_revision(material_id, request_id):
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "这次题目修改已经在运行，未启动重复调用。",
        }
        return
    yield {"type": "question_revision_started", "request_id": request_id}
    try:
        result = await agent_steps.revise_questions_from_comments(
            material, blueprint, package, comments)
        if result["outcome"] == "needs_material_revision":
            record = {
                "request_id": request_id,
                "material_id": material_id,
                "status": "needs_material_revision",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "reasons": result["reasons"],
                "actor": actor,
                "completed_at": _now(),
            }
            store.save_question_revision(material_id, request_id, record)
            yield {
                "type": "question_revision_needs_material",
                "request_id": request_id,
                "reasons": result["reasons"],
            }
            return

        revised = result["package"]
        yield {"type": "question_revision_validating", "request_id": request_id}
        validation = await validate_questions(material, blueprint, revised)
        face = revised.get("question_face")
        if not isinstance(face, dict):
            raise ValueError("revised package carries no question_face")

        yield {"type": "question_revision_auditing", "request_id": request_id}
        metrics = question_metrics(material, face)
        review = await agent_steps.audit_questions_blind(material, face, metrics)
        cross = crosscheck_questions(revised, review, material)
        candidate = QuestionCandidate(revised, review, cross, validation, "manual")
        blockers = hard_blockers(candidate)
        if blockers:
            record = {
                "request_id": request_id,
                "material_id": material_id,
                "status": "failed",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "blockers": blockers,
                "actor": actor,
                "completed_at": _now(),
            }
            store.save_question_revision(material_id, request_id, record)
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": "修改后的题目未通过完整质量检查。",
                "blockers": blockers[:8],
            }
            return

        version = {
            "id": request_id,
            "material_id": material_id,
            "created_at": _now(),
            "based_on_version_id": base_version_id,
            "source_comment_ids": [str(row["id"]) for row in comments],
            "status": "ready",
            "package": revised,
            "quality": candidate.as_dict(),
            "created_by": actor,
        }
        store.save_question_version(material_id, request_id, version)
        try:
            store.save_question_revision(material_id, request_id, {
                "request_id": request_id,
                "material_id": material_id,
                "status": "completed",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "version_id": request_id,
                "actor": actor,
                "completed_at": _now(),
            })
        except Exception:
            # The immutable version is the delivered artifact and has already been durably created.
            # A status-sidecar failure must not turn that success into a reported failure; the Web
            # reader also reconciles a stale running pointer against the existing version.
            pass
        yield {
            "type": "question_revision_completed",
            "request_id": request_id,
            "version_id": request_id,
            "elapsed_seconds": round(time.time() - started, 1),
        }
    except Exception as exc:
        try:
            store.save_question_revision(material_id, request_id, {
                "request_id": request_id,
                "material_id": material_id,
                "status": "failed",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "message": "%s: %s" % (type(exc).__name__, str(exc)[:500]),
                "actor": actor,
                "completed_at": _now(),
            })
        except Exception:
            # The terminal event is still owed to the caller even when the status store is the fault.
            pass
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "题目修改没有完成，请保留当前版本后重试。",
        }
