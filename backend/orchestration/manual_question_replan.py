"""Reviewer-confirmed full question replanning over an immutable material."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from ..deterministic.feasibility import (
    CANNOT_DECIDE,
    PASS,
    PASS_WITH_JUSTIFICATION,
    REGENERATE_MATERIAL,
    preflight_verdict,
)
from ..deterministic.validate import validate
from ..steps import agent_steps
from .manual_question_revision import _field_changes
from .question_loop import run_questions
from .slot_store import SlotStore

MAX_PLAN_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _material_bytes(material: Dict[str, Any]) -> bytes:
    return json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _qr027_counts(validation: Any) -> Dict[str, Any]:
    metrics = getattr(validation, "metrics", None)
    if not isinstance(metrics, dict):
        return {}
    return {
        str(key): value
        for key, value in metrics.items()
        if str(key).startswith("qr027_")
    }


def _comment_outcomes(
    comments: List[Dict[str, Any]], outcome: str, reason: str
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for comment in comments:
        anchor = comment.get("anchor") if isinstance(comment, dict) else None
        rows.append({
            "comment_id": str(comment.get("id") or ""),
            "question_number": (
                anchor.get("index") if isinstance(anchor, dict) else 0
            ),
            "outcome": outcome,
            "reason": reason,
        })
    return rows


def _material_reasons(
    comments: List[Dict[str, Any]], messages: List[str]
) -> List[Dict[str, Any]]:
    detail = "; ".join(str(value) for value in messages if str(value).strip())
    detail = detail or "The unchanged material cannot support a valid replacement set."
    return _comment_outcomes(comments, "revise_material", detail)


def _replan_outcomes(
    comments: List[Dict[str, Any]], reason: str
) -> List[Dict[str, Any]]:
    outcomes: List[Dict[str, Any]] = []
    for comment in comments:
        scope = comment.get("replan_scope") if isinstance(comment, dict) else None
        outcome = _comment_outcomes(
            [comment],
            "replan_questions" if scope else "question_only",
            reason,
        )[0]
        if scope:
            outcome["replan_scope"] = scope
        outcomes.append(outcome)
    return outcomes


def _preserves_information_points(comments: List[Dict[str, Any]]) -> bool:
    scopes = {
        str(comment.get("replan_scope") or "")
        for comment in comments
        if isinstance(comment, dict) and comment.get("replan_scope")
    }
    return "layout_only" in scopes and "retarget" not in scopes


def _information_point_errors(
    current: Dict[str, Any], planned: Dict[str, Any]
) -> List[str]:
    current_items = current.get("items")
    planned_items = planned.get("items")
    if not isinstance(current_items, list) or not isinstance(planned_items, list):
        return ["Layout-only replanning requires complete current and replacement item lists."]
    current_by_number = {
        item.get("number"): item for item in current_items if isinstance(item, dict)
    }
    planned_by_number = {
        item.get("number"): item for item in planned_items if isinstance(item, dict)
    }
    if set(current_by_number) != set(planned_by_number):
        return ["Layout-only replanning changed the set of information-point numbers."]
    errors: List[str] = []
    for number in sorted(current_by_number):
        before = current_by_number[number]
        after = planned_by_number[number]
        changed = [
            field for field in ("target", "evidence", "turn_index")
            if before.get(field) != after.get(field)
        ]
        if changed:
            errors.append(
                "Layout-only replanning changed Q%s information-point field(s): %s."
                % (number, ", ".join(changed))
            )
    return errors


async def replan_from_comments(
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
    """Create one immutable question version with a newly planned blueprint."""
    started = time.time()
    original_material = copy.deepcopy(material)
    original_bytes = _material_bytes(material)
    material_sha256 = hashlib.sha256(original_bytes).hexdigest()
    preserve_information_points = _preserves_information_points(comments)

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
        "completed", "needs_material_revision", "failed",
    }:
        status = existing_request.get("status")
        if status == "completed":
            yield {
                "type": "question_revision_completed",
                "request_id": request_id,
                "version_id": existing_request.get("version_id") or request_id,
                "elapsed_seconds": 0,
            }
        elif status == "needs_material_revision":
            yield {
                "type": "question_revision_needs_material",
                "request_id": request_id,
                "reasons": existing_request.get("reasons") or [],
            }
        else:
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": existing_request.get("message") or "重新命题没有完成。",
            }
        return
    if not store.persistent:
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "题目版本存储未配置，未启动重新命题。",
        }
        return
    if not store.claim_question_revision(material_id, request_id):
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "这次重新命题已经在运行，未启动重复调用。",
        }
        return

    request = dict(existing_request or {})
    request.update({
        "request_id": request_id,
        "material_id": material_id,
        "operation": "replan_questions",
        "source_request_id": source_request_id,
        "status": "running",
        "stage": "planning",
        "base_version_id": base_version_id,
        "source_comments": comments,
        "comment_count": len(comments),
        "actor": actor,
        "material_sha256": material_sha256,
        "created_at": request.get("created_at") or _now(),
        "updated_at": _now(),
    })
    store.save_question_revision(material_id, request_id, request)
    yield {"type": "question_revision_planning", "request_id": request_id}

    try:
        planned: Optional[Dict[str, Any]] = None
        validation: Any = None
        feedback: List[str] = []
        plan_failures: List[str] = []
        plan_accepted = False
        for _attempt in range(MAX_PLAN_ATTEMPTS):
            if _attempt:
                request.update({"stage": "planning", "updated_at": _now()})
                store.save_question_revision(material_id, request_id, request)
                yield {
                    "type": "question_revision_planning",
                    "request_id": request_id,
                }
            replanned = await agent_steps.replan_blueprint(
                material, blueprint, comments, feedback or None)
            if _material_bytes(replanned.material) != original_bytes:
                raise ValueError("question replanning changed the listening material")
            planned = replanned.blueprint
            if material != original_material or _material_bytes(material) != original_bytes:
                raise ValueError("question replanning mutated the listening material")
            if planned == blueprint:
                feedback = [
                    "The replacement blueprint is byte-equivalent to the current blueprint. "
                    "The confirmed comments require a genuine replan."
                ]
                plan_failures.append("unchanged")
                continue
            if preserve_information_points:
                scope_errors = _information_point_errors(blueprint, planned)
                if scope_errors:
                    feedback = scope_errors
                    plan_failures.append("scope")
                    continue
            request.update({"stage": "validating", "updated_at": _now()})
            store.save_question_revision(material_id, request_id, request)
            yield {"type": "question_revision_validating", "request_id": request_id}
            validation = await validate(material, planned)
            if not validation.ok:
                feedback = list(validation.errors)
                plan_failures.append("validation")
                continue

            request.update({"stage": "feasibility", "updated_at": _now()})
            store.save_question_revision(material_id, request_id, request)
            yield {"type": "question_revision_feasibility", "request_id": request_id}
            feasibility = await agent_steps.feasibility_audit(
                material, planned, _qr027_counts(validation))
            verdict = preflight_verdict(validation.as_dict(), feasibility)
            outcome = verdict.get("outcome")
            if outcome in CANNOT_DECIDE or outcome not in {
                PASS, PASS_WITH_JUSTIFICATION, REGENERATE_MATERIAL
            }:
                raise RuntimeError(
                    "question feasibility could not be decided: %s: %s"
                    % (outcome, "; ".join(verdict.get("reasons") or [])))

            feedback = [
                str(reason)
                for reason in verdict.get("reasons") or []
                if str(reason).strip()
            ]
            if feasibility.get("category_semantics_ok") is False:
                plan_failures.append("category_semantics")
                continue
            if outcome == REGENERATE_MATERIAL:
                plan_failures.append(
                    "infeasible" if feasibility.get("feasible") is False
                    else "feasibility"
                )
                continue

            plan_accepted = True
            break

        if not plan_accepted:
            messages = feedback or ["No valid replacement blueprint could be produced."]
            last_failure = plan_failures[-1] if plan_failures else "planning"
            if last_failure == "infeasible" and not preserve_information_points:
                async for event in _finish_needs_material(
                    store, request, comments, messages):
                    yield event
                return
            failure_messages = {
                "category_semantics": (
                    "question blueprint category semantics could not be corrected"
                ),
                "scope": "question blueprint could not satisfy the layout-only boundary",
                "validation": "question blueprint validation could not be satisfied",
                "unchanged": "question blueprint remained unchanged",
                "feasibility": "question blueprint feasibility did not prove material infeasibility",
                "infeasible": (
                    "layout-only replanning cannot change the listening material"
                ),
            }
            await _save_failed(
                store,
                request,
                phase="planning" if last_failure == "unchanged" else last_failure,
                code="REPLAN_%s_EXHAUSTED" % last_failure.upper(),
                blockers=messages,
                message="%s: %s" % (
                    failure_messages.get(
                        last_failure, "question blueprint planning failed"),
                    "; ".join(messages),
                ),
            )
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": "重新命题没有完成，现有版本未改变。",
                "blockers": messages[:8],
            }
            return

        request.update({"stage": "generating", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_generating", "request_id": request_id}

        stage_events: asyncio.Queue[str] = asyncio.Queue()

        async def emit(stage: str, _detail: Optional[Dict[str, Any]] = None) -> None:
            if stage == "question_cross_check" and request.get("stage") != "auditing":
                request.update({"stage": "auditing", "updated_at": _now()})
                store.save_question_revision(material_id, request_id, request)
                await stage_events.put("auditing")

        question_task = asyncio.create_task(
            run_questions(material, planned, emit=emit))
        try:
            while not question_task.done():
                try:
                    stage = await asyncio.wait_for(stage_events.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                yield {
                    "type": "question_revision_%s" % stage,
                    "request_id": request_id,
                }
            questions = await question_task
            while not stage_events.empty():
                stage = stage_events.get_nowait()
                yield {
                    "type": "question_revision_%s" % stage,
                    "request_id": request_id,
                }
        finally:
            if not question_task.done():
                question_task.cancel()
                with suppress(asyncio.CancelledError):
                    await question_task
        if not questions.ok or questions.candidate is None:
            messages = list(questions.blockers)
            if not messages and questions.detail:
                messages = [str(questions.detail)]
            messages = messages or ["The replacement question set failed quality checks."]
            await _save_failed(
                store,
                request,
                phase="question_generation",
                code="QUESTION_GENERATION_QUALITY_FAILED",
                blockers=messages,
                message="replacement question generation failed quality checks: %s"
                % "; ".join(messages),
            )
            yield {
                "type": "question_revision_failed",
                "request_id": request_id,
                "message": "重新命题未通过完整质量检查，现有版本未改变。",
                "blockers": messages[:8],
            }
            return
        if material != original_material or _material_bytes(material) != original_bytes:
            raise ValueError("question generation mutated the listening material")

        request.update({"stage": "storing", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_storing", "request_id": request_id}
        candidate = questions.candidate
        outcomes = _comment_outcomes(
            comments, "replan_questions",
            "Resolved by rebuilding the complete blueprint and question set.")
        version = {
            "id": request_id,
            "material_id": material_id,
            "created_at": _now(),
            "based_on_version_id": base_version_id,
            "source_comment_ids": [str(row.get("id") or "") for row in comments],
            "status": "ready",
            "package": candidate.package,
            "blueprint": planned,
            "quality": candidate.as_dict(),
            "baseline_advisories": list(questions.advisories),
            "changed_questions": list(range(1, 11)),
            "field_changes": _field_changes(package, candidate.package),
            "created_by": actor,
            "material_sha256": material_sha256,
        }
        store.save_question_version(material_id, request_id, version)
        terminal = dict(request)
        terminal.update({
            "status": "completed",
            "stage": "storing",
            "version_id": request_id,
            "comment_outcomes": outcomes,
            "baseline_advisories": list(questions.advisories),
            "changed_questions": list(range(1, 11)),
            "field_changes": version["field_changes"],
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
            "baseline_advisories": list(questions.advisories),
            "elapsed_seconds": round(time.time() - started, 1),
        }
    except Exception as exc:
        try:
            await _save_failed(
                store,
                request,
                phase=str(request.get("stage") or "unknown"),
                code=type(exc).__name__,
                blockers=[str(exc)[:500]],
                message="%s: %s" % (type(exc).__name__, str(exc)[:500]),
            )
        except Exception:
            pass
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "重新命题没有完成，现有版本未改变。",
        }


async def _finish_needs_material(
    store: SlotStore,
    request: Dict[str, Any],
    comments: List[Dict[str, Any]],
    messages: List[str],
) -> AsyncIterator[Dict[str, Any]]:
    reasons = _material_reasons(comments, messages)
    terminal = dict(request)
    terminal.update({
        "status": "needs_material_revision",
        "stage": request.get("stage"),
        "reasons": reasons,
        "comment_outcomes": _replan_outcomes(
            comments,
            "The confirmed replan could not be completed with the unchanged material.",
        ),
        "escalation_reason": "; ".join(messages),
        "failure_phase": "feasibility",
        "failure_code": "MATERIAL_INFEASIBLE",
        "completed_at": _now(),
    })
    store.save_question_revision(
        str(request["material_id"]), str(request["request_id"]), terminal)
    yield {
        "type": "question_revision_needs_material",
        "request_id": request["request_id"],
        "reasons": reasons,
    }


async def _save_failed(
    store: SlotStore,
    request: Dict[str, Any],
    *,
    phase: str,
    code: str,
    blockers: List[str],
    message: str,
) -> None:
    terminal = dict(request)
    terminal.update({
        "status": "failed",
        "failure_phase": phase,
        "failure_code": code,
        "blockers": list(blockers),
        "message": message,
        "completed_at": _now(),
    })
    store.save_question_revision(
        str(request["material_id"]), str(request["request_id"]), terminal)
