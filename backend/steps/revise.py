"""Revision step: one model call producing a complete new material + blueprint.

Full replacement, not a patch (design.md §12). A patch would need the orchestrator to reconcile
edited text against turn anchors by diffing, and anchor drift under diffing is not controllable.
Letting the model re-emit both artifacts keeps the anchors its own responsibility -- and then
``deterministic/anchors.py`` verifies rather than trusts the result.

The instruction separates must-fix items from advisory ones and never merges them. The grading
in design.md §3.3 exists because the two demand different behaviour: a hard defect must be
fixed, while an advisory item must not provoke a rewrite of an otherwise compliant script. A
flat list of "issues" loses that, and skill-contract already measured the cost -- materials were
being rewritten repeatedly to chase a typical word-count band the specification never required.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..model import provider
from .call import ModelCallError, call_json
from .generate import GenOutput, _stamp
from .skill_prompts import revise_system_prompt

__all__ = ["ReviseInstruction", "build_revise_instruction", "revise"]

REVISE_MAX_TOKENS = 32000
# Lower effort than the audit: the task is a bounded edit against an explicit defect list, not
# an open-ended judgement.
REVISE_EFFORT = "medium"


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


def build_revise_message(
    material: Dict[str, Any], blueprint: Dict[str, Any], instruction: ReviseInstruction
) -> str:
    sections = [
        "Revise the IELTS Listening Part 1 material below. Return the COMPLETE revised material "
        "and blueprint, not a patch or a diff.",
        "## Current material.json\n\n%s" % json.dumps(material, ensure_ascii=False, indent=2),
        "## Current blueprint.json\n\n%s" % json.dumps(blueprint, ensure_ascii=False, indent=2),
    ]
    sections.append(
        "## Must fix\n\n" + ("\n".join("- %s" % item for item in instruction.must_fix)
                             if instruction.must_fix else "- (none)")
    )
    sections.append(
        "## Advisory only — do NOT rewrite compliant content to satisfy these\n\n"
        "These are observed-typical deviations and minor notes. The script already satisfies "
        "the hard limits. Address them only where it costs nothing.\n\n"
        + ("\n".join("- %s" % item for item in instruction.advisory)
           if instruction.advisory else "- (none)")
    )
    sections.append(
        "## Output\n\n"
        "Return ONE JSON object with exactly two top-level keys, \"material\" and "
        "\"blueprint\".\n"
        "Make the smallest change that resolves every must-fix item.\n"
        "CRITICAL: every blueprint item's turn_index must be re-checked against the REVISED "
        "turns array. Editing the script shifts turn positions; a stale anchor puts a "
        "reviewer's annotation beside the wrong sentence. Each evidence string must remain an "
        "exact substring of the turn its turn_index points to."
    )
    return "\n\n".join(sections)


async def revise(
    material: Dict[str, Any], blueprint: Dict[str, Any], instruction: ReviseInstruction
) -> GenOutput:
    """Produce a revised material + blueprint pair."""
    model = provider.build_model(
        max_output_tokens=REVISE_MAX_TOKENS, reasoning_effort=REVISE_EFFORT
    )
    payload = await call_json(
        model, revise_system_prompt(), build_revise_message(material, blueprint, instruction)
    )
    new_material, new_blueprint = payload.get("material"), payload.get("blueprint")
    if not isinstance(new_material, dict) or not isinstance(new_blueprint, dict):
        raise ModelCallError(
            "revision response lacked material/blueprint objects; keys=%s"
            % sorted(payload.keys())[:8]
        )
    parts = material.get("listening_material_parts")
    scenario_text = ""
    if isinstance(parts, list) and parts and isinstance(parts[0], dict):
        scenario_text = str(parts[0].get("scenario") or "")
    return GenOutput(_stamp(new_material, scenario_text), new_blueprint)
