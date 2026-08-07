"""Sort question-audit and question-cross-check signals into must-fix and advisory. Pure Python.

The sibling of :mod:`revision_plan`, and it lives here for the same stated reason: which defects
oblige a rewrite and which are advice is an orchestration decision, not a model call.

Two differences from the material side, both forced by what a question revision actually is:

**Every must-fix item is a question rewrite, never a script edit.** The script is already recorded --
or will be from exactly this text -- so a fix that changes an audible word is not a fix, it is a
different material (SR-021). Each instruction below therefore names the carrier, the answer key or
the layout as the thing to change, and the revision prompt states the prohibition outright rather
than hoping the generator infers it from the absence of permission.

**Severity is upper-case here.** The material auditor emits ``critical``/``major``; the question
auditor emits ``CRITICAL``/``MAJOR`` because its schema and its rules file are written that way. This
module matches its own auditor rather than normalising, and the two constants sit side by side so the
difference is visible instead of being a bug waiting in a case-sensitive comparison.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .revision_plan import ReviseInstruction

__all__ = ["build_question_revise_instruction"]

# The question auditor's own vocabulary, upper-case, from audit_questions.schema.json.
MUST_FIX_SEVERITIES = ("CRITICAL", "MAJOR")
ADVISORY_SEVERITIES = ("MINOR", "ADVISORY_WARNING")

# Only findings still open describe work to do. A resolved or waived finding put into a must-fix list
# would demand a second fix for something already dealt with, and the likeliest response is a rewrite
# of a sound item.
OPEN = "open"


def _finding_line(finding: Dict[str, Any], scope: str) -> str:
    """One finding as an instruction line, in the material side's format.

    Same layout as ``revision_plan._finding_line`` -- severity, rule, location, evidence, fix -- so a
    reader of either revision prompt is reading the same shape. ``fix`` is the auditor's own proposed
    remedy and is passed through verbatim: it was written by the only party that reconstructed the item
    without the answer key, which makes it the one proposal known not to be reasoning backwards from
    the intended answer.
    """
    return "[%s] %s (%s) — evidence: %s — fix: %s" % (
        finding.get("severity", "?"),
        finding.get("rule_id", ""),
        scope,
        finding.get("evidence", ""),
        finding.get("fix", ""),
    )


def _scope(finding: Dict[str, Any]) -> str:
    number = finding.get("number")
    if isinstance(number, int) and not isinstance(number, bool):
        return "Q%d" % number
    group = finding.get("group_id")
    return "group %s" % group if group else "whole question set"


def build_question_revise_instruction(
    review: Dict[str, Any],
    cross_check: Any,
    validate_warnings: Optional[List[str]] = None,
) -> ReviseInstruction:
    """Sort every question-stage signal into must-fix or advisory.

    must-fix: CRITICAL/MAJOR audit findings (item-level and group-level), plus every cross-check hard
    defect, plus leakage and equally-supported rivals.
    advisory: MINOR and ADVISORY_WARNING findings, one-turn anchor gaps, validator warnings.

    Leakage and rivals are must-fix even though the auditor may already have raised a finding for the
    same item, and the duplication is deliberate. The two arrive by different routes -- the finding is
    the auditor's judgement, the cross-check row is a deterministic consequence of its reconstruction
    disagreeing with the key -- and dropping either one to avoid repetition would mean a silent auditor
    could suppress a defect Python had already proved. A repeated instruction costs a line of prompt;
    a dropped one ships a broken item.

    An anchor one turn away is advisory, matching what the audit rules permit: +-1 is allowed when the
    neighbouring turn confirms the same fact. Making it must-fix would demand rewrites of correct
    items, and the generator is in a position to check the two turns and leave it alone.
    """
    must_fix: List[str] = []
    advisory: List[str] = []

    findings: List[Dict[str, Any]] = []
    for key in ("per_question_findings", "group_findings"):
        value = review.get(key) if isinstance(review, dict) else None
        if isinstance(value, list):
            findings.extend([f for f in value if isinstance(f, dict)])

    for finding in findings:
        if finding.get("state") != OPEN:
            continue
        line = _finding_line(finding, _scope(finding))
        severity = finding.get("severity")
        if severity in MUST_FIX_SEVERITIES:
            must_fix.append(line)
        elif severity in ADVISORY_SEVERITIES:
            advisory.append(line)
        else:
            # INFO, or an unrecognised severity. Advisory rather than dropped: a note the auditor
            # bothered to write is worth showing, and advisory is the side that cannot force a rewrite.
            advisory.append(line)

    for row in getattr(cross_check, "hard_defects", []) or []:
        # The row's own ``reason`` already names both answers and cites the rules, so this line adds
        # only the label. An earlier version prefixed a sentence of its own, and on the real Q1/Q8
        # defects the two said the same thing twice in every instruction -- a doubled sentence in a
        # defect list reads like two defects.
        must_fix.append(
            "[cross-check %s] Q%s — %s"
            % (row.get("outcome"), row.get("number"), row.get("reason"))
        )
    for row in getattr(cross_check, "leakage", []) or []:
        must_fix.append(
            "[cross-check leakage] Q%s — a reader with no recording produced %r from the printed page "
            "alone. Rewrite the carrier or the layout so the gap cannot be filled without listening; "
            "the answer must stay the recorded value."
            % (row.get("number"), row.get("auditor_answer"))
        )
    for row in getattr(cross_check, "equally_supported_rivals", []) or []:
        must_fix.append(
            # The reason is the auditor's own prose and arrives already punctuated, so it goes between
            # em-dashes rather than being spliced into a sentence -- on the real Q8 rival, appending
            # "." produced "...the ideal size.. Narrow the carrier".
            "[cross-check rival] Q%s — %r also fits this carrier and is equally supported — %s — "
            "narrow the carrier so exactly one answer fits, or accept both in the answer key"
            % (row.get("number"), row.get("text"), str(row.get("reason") or "").rstrip(". "))
        )
    for row in getattr(cross_check, "needs_review", []) or []:
        advisory.append(
            "[cross-check anchor] Q%s — %s"
            % (row.get("number"), row.get("reason"))
        )

    consistency = getattr(cross_check, "consistency", None) or {}
    for message in consistency.get("errors") or []:
        # The review contradicting itself is not something the *generator* can fix, so it is advisory
        # here and is expected to have already been raised as a retry inside the audit envelope. It is
        # still shown, because a revision made against a review that miscounted its own findings should
        # be visible as such in the prompt rather than only in a log.
        advisory.append("[review inconsistency] %s" % message)

    for warning in validate_warnings or []:
        advisory.append("[validator warning] %s" % warning)

    return ReviseInstruction(must_fix, advisory)
