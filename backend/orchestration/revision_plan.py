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

__all__ = ["ReviseInstruction", "build_revise_instruction"]


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


def _finding_line(finding: Dict[str, Any]) -> str:
    where = finding.get("turn_index")
    location = "turn %s" % where if isinstance(where, int) else "whole script"
    return "[%s] %s (%s) — evidence: %s — fix: %s" % (
        finding.get("severity", "?"),
        finding.get("rule", ""),
        location,
        finding.get("evidence", ""),
        finding.get("fix", ""),
    )


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
