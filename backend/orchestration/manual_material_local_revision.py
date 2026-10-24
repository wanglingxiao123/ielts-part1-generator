"""Turn-scoped material revision with deterministic projection and immutable versions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
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


_CONFIRMATION_DENSITY_ERROR = re.compile(
    r"^blueprint must mark at least \d+ confirmed items; found \d+$"
)
_QUESTION_NUMBER = re.compile(r"\bQ(\d+)\b")


def _material_audit_passed(audit: Dict[str, Any]) -> bool:
    findings = audit.get("findings", [])
    return (
        audit.get("assessable") is True
        and audit.get("verdict") in {"PASS", "PASS_WITH_MINOR_EDITS"}
        and isinstance(findings, list)
        and all(
            isinstance(row, dict)
            and str(row.get("severity") or "").lower() in {"minor", "advisory", "info"}
            for row in findings
        )
    )


def _local_material_gate(
    check, baseline_check, material: Dict[str, Any],
    projected: Dict[str, Any], turn_index: int,
) -> tuple[List[str], List[str]]:
    """Reconcile extra details only on literally unchanged source turns.

    Both independent audits must recover every planned point. Their lists of additional
    contextual details need not be identical: literal source comparison establishes
    whether those details predate the edit. Missing answers, unknown anchors, changed
    turns and unsupported quotations remain blocking.
    """
    blockers, advisories = [], []
    before, after = material_turns(material), material_turns(projected)
    for row in check.unrecoverable:
        blockers.append("材料盲审未恢复 Q%s（Turn %s）" % (
            row.get("number"), row.get("turn_index")))
    baseline_complete = (
        baseline_check is not None and baseline_check.planned > 0
        and baseline_check.matched == baseline_check.planned
        and not baseline_check.unrecoverable
    )

    def supported(row, turns):
        index = row.get("turn_index")
        if type(index) is not int or not 0 <= index < len(turns):
            return False
        quote = " ".join(str(row.get("evidence") or "").split()).casefold()
        text = " ".join(str(turns[index].get("text") or "").split()).casefold()
        return len(quote) >= 8 and quote in text

    for row in check.unintended_target:
        index = row.get("turn_index")
        existing = (
            type(index) is int and 0 <= index < min(len(before), len(after))
            and index != turn_index and before[index] == after[index]
            and baseline_complete and supported(row, before) and supported(row, after)
        )
        detail = "Turn %s：%s" % (index, str(row.get("evidence") or "")[:160])
        if existing:
            advisories.append("原稿复审已恢复全部考点；额外信息的原句在本次修改中完全未变：" + detail)
        else:
            blockers.append("材料盲审发现未规划的信息：" + detail)
    return blockers, advisories


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


def _apply_confirmed_updates(
    blueprint: Dict[str, Any], raw_updates: Any
) -> tuple[Dict[str, Any], List[int]]:
    """Apply the sole permitted semantic metadata change: confirmed true -> false."""
    revised = copy.deepcopy(blueprint)
    if raw_updates is None:
        return revised, []
    if not isinstance(raw_updates, list):
        raise ValueError("confirmed_updates must be a list")
    items = revised.get("items")
    if not isinstance(items, list):
        raise ValueError("blueprint items are missing")
    by_number = {
        row.get("number"): row for row in items
        if isinstance(row, dict) and isinstance(row.get("number"), int)
    }
    changed: List[int] = []
    seen = set()
    for update in raw_updates:
        if not isinstance(update, dict):
            raise ValueError("confirmed_updates entries must be objects")
        number = update.get("number")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number in seen
            or update.get("confirmed") is not False
        ):
            raise ValueError("confirmed_updates may only set one existing item from true to false")
        item = by_number.get(number)
        if not isinstance(item, dict) or item.get("confirmed") is not True:
            raise ValueError("confirmed_updates may only set one existing item from true to false")
        item["confirmed"] = False
        seen.add(number)
        changed.append(number)
    return revised, changed


def _relax_local_confirmation_density(validation: Any) -> None:
    """Turn generation-only confirmation density into a local-edit advisory."""
    relaxed = [
        message for message in validation.errors
        if _CONFIRMATION_DENSITY_ERROR.fullmatch(message)
    ]
    if not relaxed:
        return
    validation.errors[:] = [
        message for message in validation.errors
        if not _CONFIRMATION_DENSITY_ERROR.fullmatch(message)
    ]
    validation.warnings.extend(
        "manual local edit accepted with advisory: %s" % message
        for message in relaxed
    )


def _local_question_gate(
    candidate: QuestionCandidate, affected_questions: set[int]
) -> tuple[List[str], List[str]]:
    """Keep defects in the edited scope blocking; record unrelated audit variance."""
    blockers: List[str] = []
    advisories: List[str] = []
    for message in hard_blockers(candidate):
        match = _QUESTION_NUMBER.search(message)
        number = int(match.group(1)) if match else None
        if number is not None and number not in affected_questions:
            advisories.append(
                "基础版本未受本次 Turn 修改影响的 Q%d 在复审中出现波动：%s"
                % (number, message)
            )
        else:
            blockers.append(message)
    return blockers, advisories


def _base_question_quality(base_version: Dict[str, Any]) -> Dict[str, Any]:
    """Return the already accepted question-quality snapshot for one assessment version."""
    quality = base_version.get("quality")
    if not isinstance(quality, dict):
        return {}
    questions = quality.get("questions")
    return questions if isinstance(questions, dict) else quality


def _question_quality_for_storage(
    *,
    candidate_quality: Dict[str, Any],
    base_version: Dict[str, Any],
    package_changed: bool,
    baseline_advisories: List[str],
) -> Dict[str, Any]:
    """Keep trusted base quality only when the question package is byte-for-byte unchanged."""
    if not baseline_advisories or package_changed:
        return candidate_quality
    baseline_quality = _base_question_quality(base_version)
    if not baseline_quality:
        raise ValueError(
            "base question quality is missing; cannot reconcile unrelated audit variance")
    return copy.deepcopy(baseline_quality)


async def revise_material_local(
    *,
    store: SlotStore,
    material_id: str,
    request_id: str,
    base_version_id: str,
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
    base_version: Dict[str, Any],
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
        revised_blueprint, confirmed_updates = _apply_confirmed_updates(
            revised_blueprint, candidate.get("confirmed_updates"))
        revised_package = _replace_evidence(copy.deepcopy(package), before, after)
        request["local_patch"] = {
            "turn_index": turn_index, "before": before, "after": after,
        }
        if _blueprint_immutable_surface(revised_blueprint) != _blueprint_immutable_surface(blueprint):
            raise ValueError("local revision changed blueprint targets or structure")
        if _question_surface(revised_package) != _question_surface(package):
            raise ValueError("local revision changed question wording or answers")
        request.update(stage="validating", updated_at=_now())
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_validating", "request_id": request_id}
        from .assessment_patch import introduced_errors
        from ..steps import assessment_edit
        base_validation, validation, base_question_validation, question_validation = await asyncio.gather(
            validate(material, blueprint), validate(projected, revised_blueprint),
            validate_questions(material, blueprint, package),
            validate_questions(projected, revised_blueprint, revised_package),
        )
        _relax_local_confirmation_density(validation)
        blockers = introduced_errors(base_validation, validation) + introduced_errors(
            base_question_validation, question_validation)
        request["material_diagnostics"] = {
            "policy": "accepted_baseline",
            "base_validation": base_validation.as_dict(), "validation": validation.as_dict(),
            "base_question_validation": base_question_validation.as_dict(),
            "question_validation": question_validation.as_dict(), "introduced_errors": blockers,
        }
        store.save_question_revision(material_id, request_id, request)
        if blockers:
            raise ValueError("; ".join(blockers[:8]))
        request.update(stage="auditing", updated_at=_now())
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_auditing", "request_id": request_id}
        audit = await assessment_edit.review(
            {"material": material, "blueprint": blueprint, "package": package},
            {"material": projected, "blueprint": revised_blueprint, "package": revised_package},
            comments, [{"turn_index": turn_index, "before": before, "after": after}],
        )
        request["material_diagnostics"]["impact_review"] = audit
        store.save_question_revision(material_id, request_id, request)
        if (audit["blocking_findings"] or not all(audit[k] for k in (
                "request_satisfied", "scope_preserved", "dependencies_consistent"))):
            raise ValueError("本次修改的关联检查未通过：" + "; ".join(
                r["message"] for r in audit["blocking_findings"]))
        baseline_advisories = []
        question_quality = {"review_scope": "accepted_baseline_local_edit",
                            "impact_review": audit, "validation": question_validation.as_dict()}

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
                "material": request["material_diagnostics"],
                "questions": question_quality,
            },
            "baseline_advisories": baseline_advisories,
            "created_by": actor, "material_sha256": _hash(projected),
            "audio": {"status": "needs_synthesis",
                      "version_key": "%s/%s" % (material_id, request_id)},
            "local_revision": {
                "turn_index": turn_index, "before": before, "after": after,
                "reason": reason, "affected_metadata": (
                    (["blueprint/package evidence text"]
                     if _replace_evidence(copy.deepcopy(blueprint), before, after) != blueprint
                     or revised_package != package else [])
                    + ["Q%s confirmed: true → false" % number
                       for number in confirmed_updates]
                ),
                "questions_unchanged": True, "audio_impact": "needs_synthesis",
            },
        }
        store.save_question_version(material_id, request_id, version)
        terminal = dict(request, status="completed", version_id=request_id,
                        completed_at=_now(), decision_reason=reason,
                        baseline_advisories=baseline_advisories,
                        comment_outcomes=[{
                            "comment_id": comments[0]["id"], "outcome": "local_material_edit",
                            "reason": reason, "turn_index": turn_index,
                        }])
        store.save_question_revision(material_id, request_id, terminal)
        yield {"type": "question_revision_completed", "request_id": request_id,
               "version_id": request_id,
               "baseline_advisories": baseline_advisories}
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
