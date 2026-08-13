"""Reviewer-confirmed material revision producing one immutable assessment version."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from ..deterministic.crosscheck import crosscheck
from ..deterministic.feasibility import (
    CANNOT_DECIDE,
    PASS,
    PASS_WITH_JUSTIFICATION,
    REGENERATE_MATERIAL,
    preflight_verdict,
)
from ..deterministic.metrics import run_metrics_remote
from ..deterministic.validate import validate
from ..steps import agent_steps
from .loop import Candidate, _build_metrics_runner
from .manual_question_revision import _field_changes
from .question_loop import run_questions
from .revision_plan import build_revise_instruction
from .slot_store import SlotStore

MAX_MATERIAL_REVISION_ATTEMPTS = 3


@dataclass
class _ContentFailure:
    phase: str
    code: str
    category: str
    blockers: List[str]
    retryable: bool = True
    feedback_rows: Optional[List[str]] = None
    extra_feedback: Optional[Dict[str, List[str]]] = None


def _unique(rows: List[str]) -> List[str]:
    seen = set()
    result = []
    for row in rows:
        value = str(row).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _failure(
    phase: str,
    code: str,
    category: str,
    blockers: List[str],
    retryable: bool = True,
    feedback_rows: Optional[List[str]] = None,
    extra_feedback: Optional[Dict[str, List[str]]] = None,
) -> _ContentFailure:
    return _ContentFailure(
        phase, code, category, _unique(blockers) or ["No detailed blocker was returned."],
        retryable, feedback_rows, extra_feedback,
    )


def _event(name: str, request_id: str, attempt: int) -> Dict[str, Any]:
    return {
        "type": "question_revision_%s" % name,
        "request_id": request_id,
        "attempt": attempt,
        "max_attempts": MAX_MATERIAL_REVISION_ATTEMPTS,
    }


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
                "blockers": list(existing_request.get("blockers") or [])[:8],
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
        "attempt": 0,
        "max_attempts": MAX_MATERIAL_REVISION_ATTEMPTS,
        "created_at": request.get("created_at") or _now(),
        "updated_at": _now(),
    })
    store.save_question_revision(material_id, request_id, request)

    metrics_runner: Optional[Any] = None
    feedback: Dict[str, List[str]] = {}
    attempt_summaries: List[Dict[str, Any]] = []
    current_material = material
    current_blueprint = blueprint
    accepted: Optional[Dict[str, Any]] = None
    last_failure: Optional[_ContentFailure] = None
    try:
        for attempt in range(1, MAX_MATERIAL_REVISION_ATTEMPTS + 1):
            request.update({
                "stage": "revising_material",
                "attempt": attempt,
                "attempts": list(attempt_summaries),
                "updated_at": _now(),
            })
            store.save_question_revision(material_id, request_id, request)
            event = _event("revising_material", request_id, attempt)
            if last_failure is not None:
                event.update({
                    "retry_phase": last_failure.phase,
                    "blocker_count": len(last_failure.blockers),
                })
            yield event

            revised = await agent_steps.revise_material_from_comments(
                current_material, current_blueprint, comments, feedback or None)
            no_progress = revised.material == current_material
            if no_progress:
                last_failure = _failure(
                    "revising_material",
                    "MATERIAL_REVISION_UNCHANGED",
                    "no_progress",
                    ["The candidate did not change the listening material; make the "
                     "substantive material changes required by the reviewer comments."],
                )
            else:
                request.update({"stage": "validating_material", "updated_at": _now()})
                store.save_question_revision(material_id, request_id, request)
                yield _event("validating_material", request_id, attempt)
                validation = await validate(revised.material, revised.blueprint)
                if not validation.ok:
                    last_failure = _failure(
                        "validating_material",
                        "MATERIAL_VALIDATION_EXHAUSTED",
                        "validation",
                        list(validation.errors),
                    )
                else:
                    request.update({"stage": "auditing_material", "updated_at": _now()})
                    store.save_question_revision(material_id, request_id, request)
                    yield _event("auditing_material", request_id, attempt)
                    if metrics_runner is None:
                        metrics_runner = _build_metrics_runner(material_id, request_id)
                    metrics = await run_metrics_remote(revised.material, metrics_runner)
                    audit = await agent_steps.audit_blind(
                        revised.material, metrics.audit_metrics())
                    material_cross_check = crosscheck(revised.blueprint, audit)
                    material_candidate = Candidate(
                        revised, audit, material_cross_check,
                        "revised-%d" % attempt, validation)
                    instruction = build_revise_instruction(
                        audit, material_cross_check,
                        list(getattr(validation, "warnings", []) or []))
                    if material_cross_check.hard_defects:
                        cross_instruction = build_revise_instruction(
                            {}, material_cross_check)
                        audit_instruction = build_revise_instruction(
                            audit, None,
                            list(getattr(validation, "warnings", []) or []))
                        cross_blockers = cross_instruction.must_fix or [
                            str(row) for row in material_cross_check.hard_defects
                        ]
                        last_failure = _failure(
                            "auditing_material",
                            "MATERIAL_CROSS_CHECK_EXHAUSTED",
                            "cross_check",
                            cross_blockers + audit_instruction.must_fix,
                            feedback_rows=cross_blockers,
                            extra_feedback=(
                                {"audit": audit_instruction.must_fix}
                                if audit_instruction.must_fix else None
                            ),
                        )
                    elif material_candidate.verdict == "FAIL":
                        last_failure = _failure(
                            "auditing_material",
                            "MATERIAL_AUDIT_EXHAUSTED",
                            "audit",
                            instruction.must_fix,
                            bool(instruction.must_fix),
                        )
                    elif material_candidate.verdict == "NOT_ASSESSABLE":
                        last_failure = _failure(
                            "auditing_material",
                            "MATERIAL_AUDIT_NOT_ASSESSABLE",
                            "audit",
                            instruction.must_fix or [
                                "The blind audit could not assess the revised material."
                            ],
                            bool(instruction.must_fix),
                        )
                    elif material_candidate.verdict not in {
                        "PASS", "PASS_WITH_MINOR_EDITS",
                    }:
                        last_failure = _failure(
                            "auditing_material",
                            "MATERIAL_AUDIT_UNKNOWN_VERDICT",
                            "audit",
                            ["Unknown blind-audit verdict: %s"
                             % material_candidate.verdict],
                            False,
                        )
                    else:
                        request.update({"stage": "feasibility", "updated_at": _now()})
                        store.save_question_revision(material_id, request_id, request)
                        yield _event("feasibility", request_id, attempt)
                        feasibility = await agent_steps.feasibility_audit(
                            revised.material, revised.blueprint,
                            _qr027_counts(validation))
                        verdict = preflight_verdict(
                            validation.as_dict(), feasibility)
                        outcome = verdict.get("outcome")
                        reasons = [
                            str(row) for row in verdict.get("reasons") or []
                            if str(row).strip()
                        ]
                        if outcome in CANNOT_DECIDE or outcome not in {
                            PASS, PASS_WITH_JUSTIFICATION, REGENERATE_MATERIAL,
                        }:
                            last_failure = _failure(
                                "feasibility",
                                "MATERIAL_FEASIBILITY_UNDECIDED",
                                "feasibility",
                                reasons or ["Unknown feasibility outcome: %s" % outcome],
                                False,
                            )
                        elif outcome == REGENERATE_MATERIAL:
                            last_failure = _failure(
                                "feasibility",
                                "MATERIAL_FEASIBILITY_EXHAUSTED",
                                "feasibility",
                                reasons,
                            )
                        else:
                            request.update({
                                "stage": "generating",
                                "updated_at": _now(),
                            })
                            store.save_question_revision(
                                material_id, request_id, request)
                            yield _event("generating", request_id, attempt)
                            stage_events: asyncio.Queue[str] = asyncio.Queue()

                            async def emit(
                                stage: str, _detail: Optional[Dict[str, Any]] = None
                            ) -> None:
                                if stage == "question_cross_check":
                                    await stage_events.put("auditing")

                            question_task = asyncio.create_task(run_questions(
                                revised.material, revised.blueprint, emit=emit))
                            try:
                                while not question_task.done():
                                    try:
                                        stage = await asyncio.wait_for(
                                            stage_events.get(), timeout=0.25)
                                    except asyncio.TimeoutError:
                                        continue
                                    request.update({
                                        "stage": stage,
                                        "updated_at": _now(),
                                    })
                                    store.save_question_revision(
                                        material_id, request_id, request)
                                    yield _event(stage, request_id, attempt)
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
                                last_failure = _failure(
                                    "question_generation",
                                    "MATERIAL_QUESTION_QUALITY_EXHAUSTED",
                                    "questions",
                                    messages,
                                    getattr(questions, "outcome", None)
                                    == REGENERATE_MATERIAL,
                                )
                            else:
                                accepted = {
                                    "revised": revised,
                                    "validation": validation,
                                    "audit": audit,
                                    "cross_check": material_cross_check,
                                    "verdict": verdict,
                                    "questions": questions,
                                    "attempt": attempt,
                                }
                                break

            if accepted is not None:
                break
            assert last_failure is not None
            attempt_summaries.append({
                "attempt": attempt,
                "failure_phase": last_failure.phase,
                "failure_code": last_failure.code,
                "blockers": list(last_failure.blockers),
            })
            feedback[last_failure.category] = _unique(
                feedback.get(last_failure.category, [])
                + (last_failure.feedback_rows or last_failure.blockers))
            for category, rows in (last_failure.extra_feedback or {}).items():
                feedback[category] = _unique(
                    feedback.get(category, []) + rows)
            request["attempts"] = list(attempt_summaries)
            store.save_question_revision(material_id, request_id, request)
            if not last_failure.retryable or attempt == MAX_MATERIAL_REVISION_ATTEMPTS:
                break
            current_material = revised.material
            current_blueprint = revised.blueprint

        if accepted is None:
            assert last_failure is not None
            blockers = _unique([
                blocker
                for summary in attempt_summaries
                for blocker in summary["blockers"]
            ])
            failed = dict(request)
            failed.update({
                "status": "failed",
                "failure_phase": last_failure.phase,
                "failure_code": last_failure.code,
                "blockers": blockers,
                "attempt_count": int(request.get("attempt") or 0),
                "attempts": attempt_summaries,
                "message": "%s: %s" % (
                    last_failure.code, "; ".join(last_failure.blockers[:5])),
                "completed_at": _now(),
            })
            store.save_question_revision(material_id, request_id, failed)
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": "材料修改未通过完整质量检查，现有版本未改变。",
                "blockers": blockers[:8],
            }
            return

        revised = accepted["revised"]
        validation = accepted["validation"]
        audit = accepted["audit"]
        material_cross_check = accepted["cross_check"]
        verdict = accepted["verdict"]
        questions = accepted["questions"]
        attempt = accepted["attempt"]
        request.update({"stage": "storing", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield _event("storing", request_id, attempt)
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
            "attempt_count": attempt,
        }
        store.save_question_version(material_id, request_id, version)
        terminal = dict(request)
        terminal.update({
            "status": "completed",
            "version_id": request_id,
            "comment_outcomes": _outcomes(comments),
            "attempt_count": attempt,
            "attempts": attempt_summaries,
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
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed = dict(request)
        failed.update({
            "status": "failed",
            "failure_phase": str(request.get("stage") or "unknown"),
            "failure_code": type(exc).__name__,
            "blockers": [str(exc)[:500]],
            "attempt_count": int(request.get("attempt") or 0),
            "attempts": attempt_summaries,
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
            "blockers": [str(exc)[:500]],
        }
    finally:
        if metrics_runner is not None:
            await metrics_runner.close()
