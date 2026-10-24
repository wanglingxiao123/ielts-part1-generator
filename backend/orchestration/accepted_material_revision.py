"""Apply teacher-requested edits without re-adjudicating the accepted baseline."""
from __future__ import annotations

import asyncio
import copy
import time

from ..deterministic.validate import validate
from ..deterministic.validate_questions import validate_questions
from ..steps import assessment_edit
from ..steps.call import ModelCallError
from .assessment_patch import apply_patch, introduced_errors
from .manual_material_revision import _event, _field_changes, _material_sha256, _now, _outcomes


async def revise_material_from_comments(
    *, store, material_id, request_id, source_request_id, base_version_id,
    material, blueprint, package, comments, actor,
):
    started = time.time()
    version = store.load_question_version(material_id, request_id)
    if version:
        yield {"type": "question_revision_completed", "request_id": request_id,
               "version_id": request_id, "elapsed_seconds": 0}
        return
    existing = store.load_question_revision(material_id, request_id) or {}
    if existing.get("status") in {"completed", "failed"}:
        yield {"type": "question_revision_failed", "request_id": request_id,
               "message": existing.get("message") or "这次修改已经结束，请按页面操作重试。"}
        return
    if not store.persistent or not store.claim_question_revision(material_id, request_id):
        yield {"type": "question_revision_failed", "request_id": request_id,
               "message": "修改存储不可用或任务已在运行。"}
        return
    request = dict(existing, request_id=request_id, material_id=material_id,
                   operation="revise_material", status="running", stage="revising_material",
                   source_request_id=source_request_id, base_version_id=base_version_id,
                   source_comments=comments, actor=actor, comment_count=len(comments),
                   created_at=existing.get("created_at") or _now(), updated_at=_now(),
                   max_attempts=3, attempts=[], revision_policy="accepted_baseline")
    store.save_question_revision(material_id, request_id, request)
    try:
        # Deterministic before/after comparison, not another model's verdict on the baseline.
        base_validation, base_questions = await asyncio.gather(
            validate(material, blueprint), validate_questions(material, blueprint, package))
        feedback = []
        for attempt in range(1, 4):
            request.update(attempt=attempt, stage="revising_material", updated_at=_now())
            store.save_question_revision(material_id, request_id, request)
            yield _event("revising_material", request_id, attempt)
            # Every retry starts from the same teacher-selected snapshot. No cumulative drift.
            patch = await assessment_edit.propose(material, blueprint, package, comments, feedback)
            request["patch"] = copy.deepcopy(patch)
            store.save_question_revision(material_id, request_id, request)
            if patch.get("outcome") == "needs_scope":
                raise ValueError("修改需要老师确认扩大范围：" + str(patch.get("reason") or ""))
            try:
                revised, revised_blueprint, revised_package, changed = apply_patch(
                    material, blueprint, package, comments, patch)
            except ValueError as exc:
                feedback = [str(exc)]
                request["attempts"].append({"attempt": attempt, "phase": "scope", "blockers": feedback})
                continue
            request.update(stage="validating_material", updated_at=_now())
            store.save_question_revision(material_id, request_id, request)
            yield _event("validating_material", request_id, attempt)
            validation, question_validation = await asyncio.gather(
                validate(revised, revised_blueprint),
                validate_questions(revised, revised_blueprint, revised_package))
            feedback = (introduced_errors(base_validation, validation)
                        + introduced_errors(base_questions, question_validation))
            request["validation_delta"] = {
                "base_material": base_validation.as_dict(),
                "candidate_material": validation.as_dict(),
                "base_questions": base_questions.as_dict(),
                "candidate_questions": question_validation.as_dict(),
                "introduced_errors": feedback,
            }
            store.save_question_revision(material_id, request_id, request)
            if feedback:
                request["attempts"].append({"attempt": attempt, "phase": "validation", "blockers": feedback})
                continue
            request.update(stage="auditing_material", updated_at=_now())
            store.save_question_revision(material_id, request_id, request)
            yield _event("auditing_material", request_id, attempt)
            review = await assessment_edit.review(
                dict(material=material, blueprint=blueprint, package=package),
                dict(material=revised, blueprint=revised_blueprint, package=revised_package),
                comments, patch["turn_edits"])
            request["impact_review"] = review
            store.save_question_revision(material_id, request_id, request)
            feedback = [r["message"] for r in review["blocking_findings"]]
            for key in ("request_satisfied", "scope_preserved", "dependencies_consistent"):
                if not review[key]:
                    feedback.append("Change-impact check failed: " + key)
            if feedback:
                request["attempts"].append({"attempt": attempt, "phase": "impact", "blockers": feedback})
                continue
            request.update(stage="storing", updated_at=_now())
            store.save_question_revision(material_id, request_id, request)
            version = {
                "id": request_id, "material_id": material_id, "created_at": _now(),
                "based_on_version_id": base_version_id, "status": "ready",
                "operation": "revise_material", "revision_policy": "accepted_baseline",
                "source_comment_ids": [str(r.get("id") or "") for r in comments],
                "material": revised, "blueprint": revised_blueprint, "package": revised_package,
                # A scoped review is not a fabricated PASS from a whole-material blind audit.
                "quality": {"impact_review": review, "validation_delta": request["validation_delta"]},
                "baseline_advisories": [], "changed_questions": changed,
                "field_changes": _field_changes(package, revised_package), "created_by": actor,
                "material_sha256": _material_sha256(revised),
                "audio": {"status": "needs_synthesis", "version_key": material_id + "/" + request_id},
                "attempt_count": attempt, "patch": patch,
            }
            store.save_question_version(material_id, request_id, version)
            outcomes = _outcomes(comments)
            for row in outcomes:
                row["reason"] = "Applied the requested changes to the teacher-selected version."
            request.update(status="completed", version_id=request_id, comment_outcomes=outcomes,
                           attempt_count=attempt, completed_at=_now())
            store.save_question_revision(material_id, request_id, request)
            yield {"type": "question_revision_completed", "request_id": request_id,
                   "version_id": request_id, "elapsed_seconds": round(time.time() - started, 1)}
            return
        raise ValueError("本次修改未通过范围或关联检查：" + "; ".join(feedback))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        request.update(status="failed", failure_phase=request["stage"],
                       failure_code=type(exc).__name__, message=str(exc),
                       blockers=[str(exc)], completed_at=_now())
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_failed", "request_id": request_id,
               "message": "修改没有完成，所选版本保持不变。", "blockers": [str(exc)]}
