"""Turn-scoped material revision with deterministic projection and immutable versions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List

from ..deterministic.crosscheck import crosscheck
from ..deterministic.question_crosscheck import crosscheck_questions
from ..deterministic.question_metrics import question_metrics
from ..deterministic.validate import validate
from ..deterministic.validate_questions import validate_questions
from ..steps import agent_steps
from .question_loop import QuestionCandidate, hard_blockers
from .slot_store import SlotStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: Dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _replace_evidence(value: Any, before: str, after: str) -> Any:
    if isinstance(value, list):
        return [_replace_evidence(row, before, after) for row in value]
    if isinstance(value, dict):
        return {key: _replace_evidence(row, before, after) for key, row in value.items()}
    if isinstance(value, str) and before in value:
        return value.replace(before, after)
    return value


def _turn_container(material: Dict[str, Any]) -> Any:
    direct = material.get("turns") if isinstance(material, dict) else None
    if isinstance(direct, list):
        return direct
    parts = material.get("listening_material_parts") if isinstance(material, dict) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return []
    script = parts[0].get("script")
    turns = script.get("turns") if isinstance(script, dict) else None
    return turns if isinstance(turns, list) else None


def material_turns(material: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the real script turn list for stored materials and legacy test fixtures."""
    turns = _turn_container(material)
    return turns if isinstance(turns, list) else []


def project_local_candidate(
    base: Dict[str, Any], candidate: Dict[str, Any], turn_index: int
) -> Dict[str, Any]:
    """Accept only the anchored turn's text; restore every other model-produced field."""
    base_turns = _turn_container(base)
    candidate_turns = _turn_container(candidate)
    if not isinstance(base_turns, list) or not isinstance(candidate_turns, list):
        raise ValueError("material turns are missing")
    if not 0 <= turn_index < len(base_turns) or len(candidate_turns) != len(base_turns):
        raise ValueError("candidate changed the turn structure")
    base_turn = base_turns[turn_index]
    candidate_turn = candidate_turns[turn_index]
    if not isinstance(base_turn, dict) or not isinstance(candidate_turn, dict):
        raise ValueError("anchored turn is invalid")
    text = candidate_turn.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("candidate turn text is empty")
    projected = copy.deepcopy(base)
    material_turns(projected)[turn_index]["text"] = text.strip()
    return projected


def _question_surface(package: Dict[str, Any]) -> bytes:
    visible = {
        "question_face": package.get("question_face"),
        "answer_key": package.get("answer_key"),
    }
    return json.dumps(
        visible, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _blueprint_immutable_surface(blueprint: Dict[str, Any]) -> bytes:
    """Exclude only evidence-derived fields from the blueprint comparison."""
    allowed = {"evidence", "evidence_span", "confirmed"}

    def project(value: Any) -> Any:
        if isinstance(value, list):
            return [project(row) for row in value]
        if isinstance(value, dict):
            return {
                key: project(row)
                for key, row in value.items()
                if key not in allowed
            }
        return value

    return json.dumps(
        project(blueprint), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


async def revise_material_local(
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
    existing_version = store.load_question_version(material_id, request_id)
    if existing_version is not None:
        yield {"type": "question_revision_completed", "request_id": request_id,
               "version_id": request_id}
        return
    existing_request = store.load_question_revision(material_id, request_id)
    if isinstance(existing_request, dict) and existing_request.get("status") != "running":
        status = str(existing_request.get("status") or "")
        if status in {"no_change", "affects_questions", "out_of_scope"}:
            yield {
                "type": "material_local_revision_%s" % status,
                "request_id": request_id,
                "reason": str(existing_request.get("decision_reason") or ""),
            }
        else:
            yield {"type": "question_revision_failed", "request_id": request_id,
                   "message": "这次局部材料修改已经结束，未启动重复调用。"}
        return
    if not store.persistent:
        yield {"type": "question_revision_failed", "request_id": request_id,
               "message": "材料版本存储未配置，未启动修改。"}
        return
    if not store.claim_question_revision(material_id, request_id):
        yield {"type": "question_revision_failed", "request_id": request_id,
               "message": "这次局部材料修改已经在运行，未启动重复调用。"}
        return
    request = {
        "request_id": request_id, "material_id": material_id,
        "operation": "revise_material_local", "status": "running",
        "stage": "analysing", "base_version_id": base_version_id,
        "source_comments": comments, "comment_count": len(comments),
        "actor": actor, "created_at": _now(), "updated_at": _now(),
    }
    store.save_question_revision(material_id, request_id, request)
    try:
        anchors = {
            row["anchor"]["index"] for row in comments
            if isinstance(row, dict) and isinstance(row.get("anchor"), dict)
        }
        if len(comments) != 1 or len(anchors) != 1:
            raise ValueError("first-phase local revision requires exactly one turn comment")
        turn_index = next(iter(anchors))
        turns = material_turns(material)
        if not turns or not 0 <= turn_index < len(turns):
            raise ValueError("comment turn is outside the material")
        turn = turns[turn_index]
        if not isinstance(turn, dict) or str(turn.get("speaker") or "") == "speaker1":
            raise ValueError("narrator turns cannot be revised locally")

        yield {"type": "question_revision_analysing", "request_id": request_id}
        candidate = await agent_steps.revise_material_turn(
            material, blueprint, package, comments[0])
        outcome = str(candidate.get("outcome") or "")
        reason = str(candidate.get("reason") or "").strip()
        if outcome in {"no_change", "affects_questions", "out_of_scope"}:
            terminal = dict(request, status=outcome, completed_at=_now(), decision_reason=reason)
            terminal["comment_outcomes"] = [{
                "comment_id": comments[0]["id"], "outcome": outcome, "reason": reason,
                "turn_index": turn_index,
            }]
            store.save_question_revision(material_id, request_id, terminal)
            yield {"type": "material_local_revision_%s" % outcome,
                   "request_id": request_id, "reason": reason}
            return
        if outcome != "local_material_edit" or not isinstance(candidate.get("material"), dict):
            raise ValueError("local revision model returned an invalid outcome")

        projected = project_local_candidate(material, candidate["material"], turn_index)
        before = str(turn.get("text") or "")
        after = str(material_turns(projected)[turn_index]["text"])
        if before == after:
            terminal = dict(request, status="no_change", completed_at=_now(),
                            decision_reason=reason or "投影后没有可见文字变化。")
            store.save_question_revision(material_id, request_id, terminal)
            yield {"type": "material_local_revision_no_change", "request_id": request_id,
                   "reason": terminal["decision_reason"]}
            return

        revised_blueprint = _replace_evidence(copy.deepcopy(blueprint), before, after)
        revised_package = _replace_evidence(copy.deepcopy(package), before, after)
        if _blueprint_immutable_surface(revised_blueprint) != _blueprint_immutable_surface(blueprint):
            raise ValueError("local revision changed blueprint targets or structure")
        if _question_surface(revised_package) != _question_surface(package):
            raise ValueError("local revision changed question wording or answers")
        request.update(stage="validating", updated_at=_now())
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_validating", "request_id": request_id}
        validation = await validate(projected, revised_blueprint)
        if not validation.ok:
            raise ValueError("; ".join(validation.errors[:8]))
        request.update(stage="auditing", updated_at=_now())
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_auditing", "request_id": request_id}
        audit = await agent_steps.audit_blind(projected, {})
        check = crosscheck(revised_blueprint, audit)
        if check.hard_defects:
            raise ValueError("material/blueprint cross-check rejected the local patch")
        question_validation = await validate_questions(
            projected, revised_blueprint, revised_package)
        face = revised_package.get("question_face")
        if not isinstance(face, dict):
            raise ValueError("local revision carries no question_face")
        metrics = question_metrics(projected, face)
        question_review = await agent_steps.audit_questions_blind(
            projected, face, metrics)
        question_cross_check = crosscheck_questions(
            revised_package, question_review, projected)
        question_candidate = QuestionCandidate(
            revised_package, question_review, question_cross_check,
            question_validation, "manual_material_local")
        question_blockers = hard_blockers(question_candidate)
        if question_blockers:
            raise ValueError(
                "question quality rejected the local patch: %s"
                % "; ".join(question_blockers[:8]))

        request.update(stage="storing", updated_at=_now())
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_storing", "request_id": request_id}
        version = {
            "id": request_id, "material_id": material_id, "created_at": _now(),
            "based_on_version_id": base_version_id,
            "source_comment_ids": [comments[0]["id"]], "status": "ready",
            "operation": "revise_material_local", "material": projected,
            "blueprint": revised_blueprint, "package": revised_package,
            "quality": {
                "material": {"audit": audit, "cross_check": check.as_dict(),
                             "validation": validation.as_dict()},
                "questions": question_candidate.as_dict(),
            },
            "created_by": actor, "material_sha256": _hash(projected),
            "audio": {"status": "needs_synthesis",
                      "version_key": "%s/%s" % (material_id, request_id)},
            "local_revision": {
                "turn_index": turn_index, "before": before, "after": after,
                "reason": reason, "affected_metadata": (
                    ["blueprint/package evidence text"] if revised_blueprint != blueprint
                    or revised_package != package else []
                ),
                "questions_unchanged": True, "audio_impact": "needs_synthesis",
            },
        }
        store.save_question_version(material_id, request_id, version)
        terminal = dict(request, status="completed", version_id=request_id,
                        completed_at=_now(), decision_reason=reason,
                        comment_outcomes=[{
                            "comment_id": comments[0]["id"], "outcome": "local_material_edit",
                            "reason": reason, "turn_index": turn_index,
                        }])
        store.save_question_revision(material_id, request_id, terminal)
        yield {"type": "question_revision_completed", "request_id": request_id,
               "version_id": request_id}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed = dict(request, status="failed", completed_at=_now(),
                      failure_phase=request.get("stage"), failure_code=type(exc).__name__,
                      message=str(exc), blockers=[str(exc)])
        store.save_question_revision(material_id, request_id, failed)
        yield {"type": "question_revision_failed", "request_id": request_id,
               "message": "局部材料修改没有完成，现有版本未改变。",
               "blockers": [str(exc)]}
