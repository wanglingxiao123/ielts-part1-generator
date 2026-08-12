"""Reviewer-confirmed material revision producing one immutable assessment version."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from ..deterministic.crosscheck import crosscheck
from ..deterministic.feasibility import (
    CANNOT_DECIDE,
    PASS,
    PASS_WITH_JUSTIFICATION,
    preflight_verdict,
)
from ..deterministic.metrics import run_metrics_remote
from ..deterministic.validate import validate
from ..steps import agent_steps
from .loop import Candidate, _build_metrics_runner
from .manual_question_revision import _field_changes
from .question_loop import run_questions
from .slot_store import SlotStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _material_sha256(material: Dict[str, Any]) -> str:
    body = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _qr027_counts(validation: Any) -> Dict[str, Any]:
    metrics = getattr(validation, "metrics", None)
    if not isinstance(metrics, dict):
        return {}
    return {
        str(key): value
        for key, value in metrics.items()
        if str(key).startswith("qr027_")
    }


def _outcomes(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "comment_id": str(row.get("id") or ""),
        "question_number": (
            (row.get("anchor") or {}).get("index")
            if (row.get("anchor") or {}).get("type") == "question" else None
        ),
        "outcome": "revise_material",
        "reason": "Resolved by revising the listening material and rebuilding the complete set.",
    } for row in comments]


async def revise_material_from_comments(
    *,
    store: SlotStore,
    material_id: str,
    request_id: str,
    source_request_id: str,
    base_version_id: str,
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
    comments: List[Dict[str, Any]],
    actor: str,
) -> AsyncIterator[Dict[str, Any]]:
    """Create a new material, blueprint and ten-question package as one version."""
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
    if isinstance(existing_request, dict) and existing_request.get("status") in {
        "completed", "failed",
    }:
        if existing_request.get("status") == "completed":
            yield {
                "type": "question_revision_completed",
                "request_id": request_id,
                "version_id": existing_request.get("version_id") or request_id,
                "elapsed_seconds": 0,
            }
        else:
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": existing_request.get("message") or "材料修改没有完成。",
            }
        return
    if not store.persistent:
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "题目版本存储未配置，未启动材料修改。",
        }
        return
    if not store.claim_question_revision(material_id, request_id):
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "这次材料修改已经在运行，未启动重复调用。",
        }
        return

    request = dict(existing_request or {})
    request.update({
        "request_id": request_id,
        "material_id": material_id,
        "operation": "revise_material",
        "source_request_id": source_request_id,
        "status": "running",
        "stage": "revising_material",
        "base_version_id": base_version_id,
        "source_comments": comments,
        "comment_count": len(comments),
        "actor": actor,
        "created_at": request.get("created_at") or _now(),
        "updated_at": _now(),
    })
    store.save_question_revision(material_id, request_id, request)
    yield {"type": "question_revision_revising_material", "request_id": request_id}

    metrics_runner: Optional[Any] = None
    try:
        revised = await agent_steps.revise_material_from_comments(
            material, blueprint, comments)
        if revised.material == material:
            raise ValueError("material revision returned the original material unchanged")

        request.update({"stage": "validating_material", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_validating_material", "request_id": request_id}
        validation = await validate(revised.material, revised.blueprint)
        if not validation.ok:
            raise ValueError(
                "revised material validation failed: %s"
                % "; ".join(validation.errors[:5]))

        request.update({"stage": "auditing_material", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_auditing_material", "request_id": request_id}
        metrics_runner = _build_metrics_runner(material_id, request_id)
        metrics = await run_metrics_remote(revised.material, metrics_runner)
        audit = await agent_steps.audit_blind(
            revised.material, metrics.audit_metrics())
        material_cross_check = crosscheck(revised.blueprint, audit)
        material_candidate = Candidate(
            revised, audit, material_cross_check, "revised", validation)
        if (
            material_candidate.verdict in {"FAIL", "NOT_ASSESSABLE"}
            or material_cross_check.hard_defects
        ):
            raise ValueError(
                "revised material audit did not pass: %s"
                % material_candidate.verdict)

        request.update({"stage": "feasibility", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_feasibility", "request_id": request_id}
        feasibility = await agent_steps.feasibility_audit(
            revised.material, revised.blueprint, _qr027_counts(validation))
        verdict = preflight_verdict(validation.as_dict(), feasibility)
        outcome = verdict.get("outcome")
        if outcome in CANNOT_DECIDE or outcome not in {
            PASS, PASS_WITH_JUSTIFICATION,
        }:
            raise ValueError(
                "revised material cannot support a complete question set: %s"
                % "; ".join(verdict.get("reasons") or []))

        request.update({"stage": "generating", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_generating", "request_id": request_id}
        stage_events: asyncio.Queue[str] = asyncio.Queue()

        async def emit(stage: str, _detail: Optional[Dict[str, Any]] = None) -> None:
            if stage == "question_cross_check":
                await stage_events.put("auditing")

        question_task = asyncio.create_task(
            run_questions(revised.material, revised.blueprint, emit=emit))
        try:
            while not question_task.done():
                try:
                    stage = await asyncio.wait_for(stage_events.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                request.update({"stage": stage, "updated_at": _now()})
                store.save_question_revision(material_id, request_id, request)
                yield {
                    "type": "question_revision_%s" % stage,
                    "request_id": request_id,
                }
            questions = await question_task
        finally:
            if not question_task.done():
                question_task.cancel()
                with suppress(asyncio.CancelledError):
                    await question_task
        if not questions.ok or questions.candidate is None:
            messages = list(questions.blockers)
            if not messages and questions.detail:
                messages = [str(questions.detail)]
            raise ValueError(
                "revised material question generation failed: %s"
                % "; ".join(messages[:5]))

        request.update({"stage": "storing", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_storing", "request_id": request_id}
        candidate = questions.candidate
        material_sha256 = _material_sha256(revised.material)
        version = {
            "id": request_id,
            "material_id": material_id,
            "created_at": _now(),
            "based_on_version_id": base_version_id,
            "source_comment_ids": [str(row.get("id") or "") for row in comments],
            "status": "ready",
            "operation": "revise_material",
            "material": revised.material,
            "blueprint": revised.blueprint,
            "package": candidate.package,
            "quality": {
                "material": {
                    "audit": audit,
                    "cross_check": material_cross_check.as_dict(),
                    "validation": validation.as_dict(),
                    "feasibility": verdict,
                },
                "questions": candidate.as_dict(),
            },
            "baseline_advisories": list(questions.advisories),
            "changed_questions": list(range(1, 11)),
            "field_changes": _field_changes(package, candidate.package),
            "created_by": actor,
            "material_sha256": material_sha256,
            "audio": {
                "status": "needs_synthesis",
                "version_key": "%s/%s" % (material_id, request_id),
            },
        }
        store.save_question_version(material_id, request_id, version)
        terminal = dict(request)
        terminal.update({
            "status": "completed",
            "version_id": request_id,
            "comment_outcomes": _outcomes(comments),
            "completed_at": _now(),
        })
        try:
            store.save_question_revision(material_id, request_id, terminal)
        except Exception:
            pass
        yield {
            "type": "question_revision_completed",
            "request_id": request_id,
            "version_id": request_id,
            "elapsed_seconds": round(time.time() - started, 1),
        }
    except Exception as exc:
        failed = dict(request)
        failed.update({
            "status": "failed",
            "message": "%s: %s" % (type(exc).__name__, str(exc)[:500]),
            "completed_at": _now(),
        })
        try:
            store.save_question_revision(material_id, request_id, failed)
        except Exception:
            pass
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "材料修改没有完成，现有版本未改变。",
        }
    finally:
        if metrics_runner is not None:
            await metrics_runner.close()
