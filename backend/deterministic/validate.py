"""Wrapper around validate_part1.py (design.md §1, error grading in §3.3).

Errors and warnings stay in separate fields all the way through. Collapsing them would either
make every typical-band deviation trigger a regeneration -- skill-contract measured that as
forcing rewrites until the model happened to land in a 51-word window -- or hide real contract
violations. The grading table in design.md §3.3 depends on this split:

    errors   -> regenerate, consumes a generation attempt
    warnings -> advisory input to the revise step, never a failure
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import paths
from .runner import run_script_json, temp_json

__all__ = ["ValidationResult", "validate"]


class ValidationResult(object):
    __slots__ = ("errors", "warnings", "metrics")

    def __init__(
        self,
        errors: List[str],
        warnings: List[str],
        metrics: Dict[str, Any],
    ) -> None:
        self.errors = errors
        self.warnings = warnings
        self.metrics = metrics

    @property
    def ok(self) -> bool:
        """True when nothing blocks delivery. Warnings do not affect this."""
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "metrics": self.metrics}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ValidationResult(errors=%d, warnings=%d)" % (len(self.errors), len(self.warnings))


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


async def validate(material: Dict[str, Any], blueprint: Dict[str, Any]) -> ValidationResult:
    """Run the authoritative validator over a material/blueprint pair."""
    with temp_json(material=material, blueprint=blueprint) as files:
        payload = await run_script_json(
            paths.validate_script(),
            [files["material"], "--blueprint", files["blueprint"], "--json"],
        )
    metrics = payload.get("metrics")
    return ValidationResult(
        errors=_strings(payload.get("errors")),
        warnings=_strings(payload.get("warnings")),
        # Absent metrics mean "not measured", not zero (skill-contract D7). Pass the omission
        # through rather than filling in defaults a UI would render as "0 words".
        metrics=metrics if isinstance(metrics, dict) else {},
    )
