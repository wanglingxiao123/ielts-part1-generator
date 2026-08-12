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

QUESTION_NUMBERS = tuple(range(1, 11))
BLOCKING_SEVERITIES = ("CRITICAL", "MAJOR")


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
    base_version: Dict[str, Any],
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
        if status in ("no_change", "replan_questions", "needs_material_revision"):
            event_type = {
                "no_change": "question_revision_no_change",
                "replan_questions": "question_revision_needs_replan",
                "needs_material_revision": "question_revision_needs_material",
            }[status]
            yield {
                "type": event_type,
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
    request = dict(existing_request or {})
    request.update({
        "request_id": request_id,
        "material_id": material_id,
        "status": "running",
        "stage": "analysing",
        "base_version_id": base_version_id,
        "source_comments": comments,
        "comment_count": len(comments),
        "actor": actor,
        "created_at": request.get("created_at") or _now(),
        "updated_at": _now(),
    })
    store.save_question_revision(material_id, request_id, request)
    yield {"type": "question_revision_started", "request_id": request_id}
    comment_outcomes: List[Dict[str, Any]] = []
    try:
        classification = await agent_steps.classify_question_revision(
            material, blueprint, package, comments)
        route = classification["outcome"]
        for reason in classification["reasons"]:
            # Compatibility for injected/older classifiers that returned one snapshot-level route.
            reason.setdefault("outcome", route)
        classification["reasons"] = _with_package_references(
            classification["reasons"], package)
        reasons = classification["reasons"]
        comment_outcomes = [
            {
                "comment_id": row["comment_id"],
                "question_number": row["question_number"],
                "outcome": row["outcome"],
                "reason": row["reason"],
                **(
                    {"replan_scope": row["replan_scope"]}
                    if row.get("replan_scope") else {}
                ),
                **({"references": row["references"]} if row.get("references") else {}),
            }
            for row in reasons
        ]
        if route != "question_only":
            status = {
                "no_change": "no_change",
                "replan_questions": "replan_questions",
                "revise_material": "needs_material_revision",
            }[route]
            event_type = {
                "no_change": "question_revision_no_change",
                "replan_questions": "question_revision_needs_replan",
                "revise_material": "question_revision_needs_material",
            }[route]
            record = {
                "request_id": request_id,
                "material_id": material_id,
                "status": status,
                "base_version_id": base_version_id,
                "source_comments": comments,
                "comment_count": len(comments),
                "reasons": [
                    row for row in reasons if row["outcome"] == route
                ],
                "comment_outcomes": comment_outcomes,
                "actor": actor,
                "completed_at": _now(),
            }
            store.save_question_revision(material_id, request_id, record)
            yield {
                "type": event_type,
                "request_id": request_id,
                "reasons": [
                    row for row in reasons if row["outcome"] == route
                ],
            }
            return

        actionable_ids = {
            row["comment_id"] for row in reasons if row["outcome"] == "question_only"
        }
        actionable_comments = [
            row for row in comments if str(row.get("id") or "") in actionable_ids
        ]
        request.update({"stage": "revising", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_revising", "request_id": request_id}
        result = await agent_steps.revise_questions_from_comments(
            material, blueprint, package, actionable_comments)
        revised = result["package"]
        if revised == package:
            raise ValueError(
                "question revision produced a byte-equivalent package despite actionable comments"
            )
        boundary_errors = _question_only_boundary_errors(package, revised, blueprint)
        if boundary_errors:
            raise ValueError("question-only boundary violated: %s" % "; ".join(boundary_errors))
        request.update({"stage": "validating", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_validating", "request_id": request_id}
        validation = await validate_questions(material, blueprint, revised)
        face = revised.get("question_face")
        if not isinstance(face, dict):
            raise ValueError("revised package carries no question_face")

        request.update({"stage": "auditing", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_auditing", "request_id": request_id}
        metrics = question_metrics(material, face)
        review = await agent_steps.audit_questions_blind(material, face, metrics)
        cross = crosscheck_questions(revised, review, material)
        candidate = QuestionCandidate(revised, review, cross, validation, "manual")
        blockers, baseline_advisories, changed_questions = _revision_gate(
            candidate, base_version)
        if blockers:
            record = {
                "request_id": request_id,
                "material_id": material_id,
                "status": "failed",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "blockers": blockers,
                "baseline_advisories": baseline_advisories,
                "changed_questions": changed_questions,
                "comment_outcomes": comment_outcomes,
                "comment_count": len(comments),
                "actor": actor,
                "message": "修改后的题目未通过完整质量检查。",
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

        request.update({"stage": "storing", "updated_at": _now()})
        store.save_question_revision(material_id, request_id, request)
        yield {"type": "question_revision_storing", "request_id": request_id}
        version = {
            "id": request_id,
            "material_id": material_id,
            "created_at": _now(),
            "based_on_version_id": base_version_id,
            "source_comment_ids": [str(row["id"]) for row in actionable_comments],
            "status": "ready",
            "package": revised,
            "blueprint": blueprint,
            "quality": candidate.as_dict(),
            "baseline_advisories": baseline_advisories,
            "changed_questions": changed_questions,
            "field_changes": _field_changes(package, revised),
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
                "comment_count": len(comments),
                "baseline_advisories": baseline_advisories,
                "changed_questions": changed_questions,
                "field_changes": version["field_changes"],
                "comment_outcomes": comment_outcomes,
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
            "baseline_advisories": baseline_advisories,
            "elapsed_seconds": round(time.time() - started, 1),
        }
    except Exception as exc:
        try:
            failed_record = {
                "request_id": request_id,
                "material_id": material_id,
                "status": "failed",
                "base_version_id": base_version_id,
                "source_comments": comments,
                "comment_count": len(comments),
                "message": "%s: %s" % (type(exc).__name__, str(exc)[:500]),
                "actor": actor,
                "completed_at": _now(),
            }
            if comment_outcomes:
                failed_record["comment_outcomes"] = comment_outcomes
            store.save_question_revision(material_id, request_id, failed_record)
        except Exception:
            # The terminal event is still owed to the caller even when the status store is the fault.
            pass
        yield {
            "type": "question_revision_failed",
            "request_id": request_id,
            "message": "题目修改没有完成，请保留当前版本后重试。",
        }


def _revision_gate(
    candidate: QuestionCandidate, base_version: Any
) -> tuple[List[str], List[str], List[int]]:
    """Keep hard checks strict while tolerating audit variance on byte-identical questions."""
    if not _has_quality_baseline(base_version):
        return hard_blockers(candidate), [], []
    base_package = base_version["package"]
    changed_questions, changed_groups = _changed_scope(base_package, candidate.package)
    blockers: List[str] = []
    advisories: List[str] = []

    def audit_issue(number: Any, message: str) -> None:
        if isinstance(number, int) and number in QUESTION_NUMBERS and number not in changed_questions:
            advisories.append("基础版本未改动的 Q%d 在复审中出现波动：%s" % (number, message))
        else:
            blockers.append(message)

    for finding in candidate.review.get("per_question_findings") or []:
        if not isinstance(finding, dict) or finding.get("state", "open") != "open":
            continue
        if finding.get("severity") not in BLOCKING_SEVERITIES:
            continue
        number = finding.get("number")
        audit_issue(
            number,
            "Q%s has an open %s finding %s in the blind audit"
            % (number, finding.get("severity"), finding.get("rule_id")),
        )
    for finding in candidate.review.get("group_findings") or []:
        if not isinstance(finding, dict) or finding.get("state", "open") != "open":
            continue
        if finding.get("severity") not in BLOCKING_SEVERITIES:
            continue
        group_id = str(finding.get("group_id") or "")
        message = "group %s has an open %s finding %s in the blind audit" % (
            group_id, finding.get("severity"), finding.get("rule_id"))
        if group_id and group_id not in changed_groups:
            advisories.append("基础版本未改动的题组 %s 在复审中出现波动：%s" % (group_id, message))
        else:
            blockers.append(message)

    cross = candidate.cross_check
    for row in cross.hard_defects:
        blockers.append(
            "cross-check %s on Q%s" % (row.get("outcome"), row.get("number")))
    for row in cross.leakage:
        blockers.append(
            "Q%s is answerable from the printed page alone (QR-040)" % row.get("number"))
    for row in cross.equally_supported_rivals:
        audit_issue(
            row.get("number"),
            "Q%s has an equally-supported rival answer %r (AR-012)"
            % (row.get("number"), row.get("text")),
        )
    for row in cross.needs_review:
        blockers.append(
            "Q%s's evidence anchor is one turn from the writer's and unconfirmed"
            % row.get("number"))

    reviewed = ((cross.consistency or {}).get("computed") or {}).get(
        "reviewed_question_ids") or []
    if sorted(reviewed) != list(QUESTION_NUMBERS):
        blockers.append("the blind audit covered %s, not all ten items" % sorted(reviewed))
    for message in (cross.consistency or {}).get("errors") or []:
        blockers.append("the review disagrees with itself: %s" % message)
    for error in getattr(candidate.validation, "errors", None) or []:
        blockers.append("validator error: %s" % error)

    named = {
        row.get("number")
        for rows in (cross.hard_defects, cross.needs_review)
        for row in rows
        if isinstance(row, dict)
    }
    for row in getattr(cross, "items", []) or []:
        if not isinstance(row, dict) or row.get("outcome") == "agree":
            continue
        number = row.get("number")
        if number not in named:
            blockers.append("the cross-check does not agree on Q%s" % number)
    shortfall = (cross.compared - cross.agreed) if cross.compared else 0
    if shortfall and shortfall != len(named):
        blockers.append(
            "the cross-check agrees on %d of %d items beyond the %d individually reported"
            % (cross.agreed, cross.compared, len(named)))
    return blockers, advisories, sorted(changed_questions)


def _has_quality_baseline(base_version: Any) -> bool:
    if not isinstance(base_version, dict) or not isinstance(base_version.get("package"), dict):
        return False
    quality = base_version.get("quality")
    if not isinstance(quality, dict):
        return False
    review = quality.get("review")
    cross = quality.get("cross_check")
    validation = quality.get("validation")
    return (
        isinstance(review, dict)
        and isinstance(review.get("per_question_findings"), list)
        and isinstance(review.get("group_findings"), list)
        and isinstance(review.get("question_qc_status"), str)
        and isinstance(cross, dict)
        and isinstance(cross.get("compared"), int)
        and not isinstance(cross.get("compared"), bool)
        and isinstance(cross.get("agreed"), int)
        and not isinstance(cross.get("agreed"), bool)
        and isinstance(validation, dict)
        and isinstance(validation.get("ok"), bool)
        and isinstance(validation.get("errors"), list)
    )


def _changed_scope(
    base: Dict[str, Any], revised: Dict[str, Any]
) -> tuple[set[int], set[str]]:
    base_face = base.get("question_face") if isinstance(base.get("question_face"), dict) else {}
    new_face = (
        revised.get("question_face") if isinstance(revised.get("question_face"), dict) else {}
    )
    base_questions = _numbered(base_face.get("questions"))
    new_questions = _numbered(new_face.get("questions"))
    base_answers = _numbered(base.get("answer_key"))
    new_answers = _numbered(revised.get("answer_key"))
    base_evidence = _numbered(base.get("evidence"))
    new_evidence = _numbered(revised.get("evidence"))
    changed = {
        number for number in QUESTION_NUMBERS
        if (base_questions.get(number) != new_questions.get(number)
            or base_answers.get(number) != new_answers.get(number)
            or base_evidence.get(number) != new_evidence.get(number))
    }

    base_groups = _grouped(base_face.get("groups"))
    new_groups = _grouped(new_face.get("groups"))
    base_instructions = _grouped(base_face.get("instructions"))
    new_instructions = _grouped(new_face.get("instructions"))
    changed_groups = {
        group_id for group_id in set(base_groups) | set(new_groups)
        if (base_groups.get(group_id) != new_groups.get(group_id)
            or base_instructions.get(group_id) != new_instructions.get(group_id))
    }
    for number in tuple(changed):
        for row in (base_questions.get(number), new_questions.get(number)):
            if isinstance(row, dict) and str(row.get("group_id") or ""):
                changed_groups.add(str(row["group_id"]))
    for number in QUESTION_NUMBERS:
        group_ids = {
            str(row.get("group_id") or "")
            for row in (base_questions.get(number), new_questions.get(number))
            if isinstance(row, dict)
        }
        if any(group_id in changed_groups for group_id in group_ids):
            changed.add(number)
    return changed, changed_groups


def _question_only_boundary_errors(
    base: Dict[str, Any], revised: Dict[str, Any], blueprint: Dict[str, Any]
) -> List[str]:
    """Reject retargeting and layout replanning before audit can legitimise it."""
    errors: List[str] = []
    base_face = base.get("question_face") if isinstance(base.get("question_face"), dict) else {}
    new_face = revised.get("question_face") if isinstance(revised.get("question_face"), dict) else {}
    base_groups = _grouped(base_face.get("groups"))
    new_groups = _grouped(new_face.get("groups"))
    base_layouts = {
        group_id: row.get("layout") for group_id, row in base_groups.items()
    }
    new_layouts = {
        group_id: row.get("layout") for group_id, row in new_groups.items()
    }
    if base_layouts != new_layouts:
        errors.append("question groups or layouts changed")
    base_questions = _numbered(base_face.get("questions"))
    new_questions = _numbered(new_face.get("questions"))
    for number in QUESTION_NUMBERS:
        old = base_questions.get(number) or {}
        new = new_questions.get(number) or {}
        if old.get("group_id") != new.get("group_id"):
            errors.append("Q%d moved group" % number)
    planned = {
        int(row.get("number")): row
        for row in blueprint.get("items", [])
        if isinstance(row, dict) and isinstance(row.get("number"), int)
    }
    revised_answers = _numbered(revised.get("answer_key"))
    for number, item in planned.items():
        answer = revised_answers.get(number) or {}
        planned_answer = str(
            item.get("target") or item.get("answer") or item.get("canonical") or ""
        ).strip()
        if (
            planned_answer
            and str(answer.get("canonical") or "").strip().casefold()
            != planned_answer.casefold()
        ):
            errors.append("Q%d changed its blueprint answer target" % number)
    return errors


def _field_changes(base: Dict[str, Any], revised: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Produce a compact, durable field-level summary for the version UI/API."""
    changes: List[Dict[str, Any]] = []
    sections = {
        "question": (
            (base.get("question_face") or {}).get("questions"),
            (revised.get("question_face") or {}).get("questions"),
        ),
        "answer_key": (base.get("answer_key"), revised.get("answer_key")),
        "evidence": (base.get("evidence"), revised.get("evidence")),
    }
    for section, (old_rows, new_rows) in sections.items():
        old = _numbered(old_rows)
        new = _numbered(new_rows)
        for number in QUESTION_NUMBERS:
            before = old.get(number) or {}
            after = new.get(number) or {}
            for field in sorted(set(before) | set(after)):
                if before.get(field) != after.get(field):
                    changes.append({
                        "question_number": number,
                        "section": section,
                        "field": field,
                        "before": before.get(field),
                        "after": after.get(field),
                    })
    base_face = base.get("question_face") if isinstance(base.get("question_face"), dict) else {}
    new_face = revised.get("question_face") if isinstance(revised.get("question_face"), dict) else {}
    questions = _numbered(new_face.get("questions")) or _numbered(base_face.get("questions"))
    for section, key in (("group", "groups"), ("instruction", "instructions")):
        old = _grouped(base_face.get(key))
        new = _grouped(new_face.get(key))
        for group_id in sorted(set(old) | set(new)):
            before = old.get(group_id) or {}
            after = new.get(group_id) or {}
            members = sorted(
                number for number, row in questions.items()
                if str(row.get("group_id") or "") == group_id
            )
            number = members[0] if members else 0
            for field in sorted(set(before) | set(after)):
                if field != "group_id" and before.get(field) != after.get(field):
                    changes.append({
                        "question_number": number,
                        "section": section,
                        "field": field,
                        "before": before.get(field),
                        "after": after.get(field),
                    })
    return changes


def _with_package_references(
    reasons: List[Dict[str, Any]], package: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Attach authoritative no-change evidence; model-supplied references never survive."""
    face = package.get("question_face") if isinstance(package.get("question_face"), dict) else {}
    questions = _numbered(face.get("questions"))
    answers = _numbered(package.get("answer_key"))
    evidence = _numbered(package.get("evidence"))
    enriched: List[Dict[str, Any]] = []
    for reason in reasons:
        number = int(reason["question_number"])
        question = questions.get(number) or {}
        answer = answers.get(number) or {}
        proof = evidence.get(number) or {}
        carrier = "%s [Q%d] %s" % (
            str(question.get("carrier_before") or "").strip(),
            number,
            str(question.get("carrier_after") or "").strip(),
        )
        enriched_reason = {
            "comment_id": str(reason["comment_id"]),
            "question_number": number,
            "outcome": str(reason["outcome"]),
            "reason": str(reason["reason"]),
        }
        if reason.get("replan_scope"):
            enriched_reason["replan_scope"] = str(reason["replan_scope"])
        if reason["outcome"] == "no_change":
            enriched_reason["references"] = [
                "题面：%s" % " ".join(carrier.split()),
                "标准答案：%s" % str(answer.get("canonical") or ""),
                "材料证据（Turn %s）：%s" % (
                    proof.get("turn_index"),
                    str(proof.get("quote") or ""),
                ),
            ]
        enriched.append(enriched_reason)
    return enriched


def _numbered(rows: Any) -> Dict[int, Dict[str, Any]]:
    return {
        int(row["number"]): row
        for row in rows or []
        if isinstance(row, dict)
        and isinstance(row.get("number"), int)
        and not isinstance(row.get("number"), bool)
    }


def _grouped(rows: Any) -> Dict[str, Dict[str, Any]]:
    return {
        str(row["group_id"]): row
        for row in rows or []
        if isinstance(row, dict) and str(row.get("group_id") or "")
    }
