"""Sort audit and cross-check signals into must-fix and advisory. Pure Python, no model.

Lives in ``orchestration/`` rather than beside the revision step because it is an orchestration
decision, not a model call: which defects oblige a rewrite and which are advice is the same kind of
judgement as how many attempts to allow, and it belongs where the other such judgements are.

The grading is load-bearing. A hard defect must be fixed; an advisory item must not provoke a rewrite
of an otherwise compliant script. A flat list of "issues" loses that distinction, and the cost was
measured -- materials were being rewritten repeatedly to chase a typical word-count band the
specification never required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["ReviseInstruction", "build_revise_instruction", "compliance_severities"]


class ReviseInstruction(object):
    """Must-fix and advisory items, kept apart by construction."""

    __slots__ = ("must_fix", "advisory")

    def __init__(self, must_fix: List[str], advisory: List[str]) -> None:
        self.must_fix = must_fix
        self.advisory = advisory

    @property
    def empty(self) -> bool:
        return not self.must_fix and not self.advisory

    def as_dict(self) -> Dict[str, Any]:
        return {"must_fix": self.must_fix, "advisory": self.advisory}


def _location(where: Any) -> str:
    return "turn %s" % where if isinstance(where, int) else "whole script"


def _finding_line(finding: Dict[str, Any]) -> str:
    return "[%s] %s (%s) — evidence: %s — fix: %s" % (
        finding.get("severity", "?"),
        finding.get("rule", ""),
        _location(finding.get("turn_index")),
        finding.get("evidence", ""),
        finding.get("fix", ""),
    )


def _compliance_line(item: Dict[str, Any]) -> str:
    return "[%s] %s specification compliance (%s) — evidence: %s — fix: %s" % (
        item.get("severity", "?"),
        item.get("code", "?"),
        _location(item.get("turn_index")),
        item.get("evidence", ""),
        item.get("fix", ""),
    )


def compliance_severities(audit: Dict[str, Any]) -> List[str]:
    """Severities of the non-compliant C1-C6 items. Empty when the review is absent or clean.

    Separate from ``findings`` because the auditor is instructed to keep them separate (its SKILL.md
    says "Report this in ``compliance_review``, not mixed into ``findings``"), which means every
    consumer of audit severity has to read both or silently ignore half the audit.
    """
    if not isinstance(audit, dict):
        return []
    review = audit.get("compliance_review")
    if not isinstance(review, dict):
        return []
    severities: List[str] = []
    for item in review.get("items") or []:
        if isinstance(item, dict) and item.get("compliant") is False:
            severity = item.get("severity")
            if isinstance(severity, str):
                severities.append(severity)
    return severities


def build_revise_instruction(
    audit: Dict[str, Any],
    cross_check: Any,
    validate_warnings: Optional[List[str]] = None,
) -> ReviseInstruction:
    """Sort every signal into must-fix or advisory per design.md §3.3.

    must-fix: audit critical/major, plus both classes of cross-check hard defect.
    advisory: audit minor, audit warnings, validator warnings.

    Cross-check defects are must-fix but never cause a regeneration: an information point the
    auditor could not recover is an editing problem, not a contract violation, and discarding
    the whole script would throw away nine good points to fix one.
    """
    must_fix: List[str] = []
    advisory: List[str] = []

    findings = audit.get("findings") if isinstance(audit, dict) else None
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        line = _finding_line(finding)
        if finding.get("severity") in ("critical", "major"):
            must_fix.append(line)
        elif finding.get("severity") == "minor":
            advisory.append(line)

    # The C1-C6 review, graded exactly like `findings`. It arrives in its own block because the
    # auditor is told to keep it out of `findings`, and reading only `findings` therefore drops half
    # the audit: a critical register breach the auditor reported in full would reach neither the
    # revision instructions nor `is_clean`, and the material would ship at its uncapped score.
    review = audit.get("compliance_review") if isinstance(audit, dict) else None
    for item in (review or {}).get("items") or []:
        if not isinstance(item, dict) or item.get("compliant") is not False:
            continue
        line = _compliance_line(item)
        if item.get("severity") in ("critical", "major"):
            must_fix.append(line)
        else:
            # Minor, or absent -- an item the auditor flagged without grading is still a note the
            # generator should see, and advisory is the side that cannot force a pointless rewrite.
            advisory.append(line)

    for row in getattr(cross_check, "unrecoverable", []) or []:
        must_fix.append(
            "[unrecoverable] information point %s (%s) at turn %s could not be recovered by a "
            "reader working from the script alone — make it explicit and clearly cued. "
            "Evidence as planned: %s"
            % (row.get("number"), row.get("type"), row.get("turn_index"), row.get("evidence"))
        )
    for row in getattr(cross_check, "unintended_target", []) or []:
        must_fix.append(
            "[unintended] an unplanned recordable detail was found at turn %s (%s): %s — it may "
            "create a second defensible answer; make it vague or remove it"
            % (row.get("turn_index"), row.get("type"), row.get("evidence"))
        )
    for row in getattr(cross_check, "ambiguous", []) or []:
        advisory.append(
            "[ambiguous] information point %s at turn %s was recoverable but read as ambiguous"
            % (row.get("number"), row.get("turn_index"))
        )

    for warning in validate_warnings or []:
        advisory.append("[validator warning] %s" % warning)
    if isinstance(audit, dict):
        for warning in audit.get("warnings") or []:
            if isinstance(warning, str):
                advisory.append("[audit warning] %s" % warning)

    return ReviseInstruction(must_fix, advisory)
