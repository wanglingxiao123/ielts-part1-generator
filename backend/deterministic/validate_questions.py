"""Wrapper around validate_questions_part1.py -- the question stage's counterpart to :mod:`validate`.

Same shape and the same reason for that shape: errors and warnings stay in separate fields the whole
way through, because the two feed different decisions. An error consumes a question-generation
attempt; a warning is advisory input to the revision and must never on its own provoke a rewrite of a
compliant question set.

Deliberately a second function rather than a parameter on :func:`validate.validate`. The two
validators take different arguments (this one also needs ``--questions``), enforce different rule
families, and are activated at different stages -- so a shared entry point would need a branch on
which set of rules to run, and a caller that got the branch wrong would silently validate questions
against script rules and report success.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import paths
from .runner import run_script_json, temp_json
from .validate import ValidationResult

__all__ = ["validate_questions"]


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


async def validate_questions(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
) -> ValidationResult:
    """Run the authoritative question validator over a material/blueprint/package triple.

    Reuses :class:`ValidationResult` rather than defining a question-specific twin. The two stages
    genuinely produce the same three things -- errors, warnings, measured metrics -- and ``.ok`` means
    the same thing in both, so a second class would be two names for one contract and every consumer
    would have to know which one it held.

    The blueprint is required and not optional, unlike in the audit path where withholding it is the
    whole point: this validator's ``validate_blueprint_fidelity`` is what checks that each answer key
    entry still matches the planned information point it was written for. Without it the call
    degrades into a shape check that cannot notice a question written for a different fact.
    """
    with temp_json(material=material, blueprint=blueprint, questions=package) as files:
        payload = await run_script_json(
            paths.question_validate_script(),
            [files["material"], "--blueprint", files["blueprint"],
             "--questions", files["questions"], "--json"],
        )
    metrics = payload.get("metrics")
    return ValidationResult(
        errors=_strings(payload.get("errors")),
        warnings=_strings(payload.get("warnings")),
        # Absent metrics mean "not measured", not zero -- the validator itself only adds a key once it
        # has measured it, for exactly this reason, and filling in defaults here would undo that.
        metrics=metrics if isinstance(metrics, dict) else {},
    )
