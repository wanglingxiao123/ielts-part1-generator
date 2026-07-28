"""Wrapper around audit_metrics.py.

Feeds the deterministic half of the audit input. The model gets measured counts instead of
being asked to count words itself, which it cannot do reliably -- and the rubric forbids
claiming metrics without calculating them.

Blind by construction: the input is the material only. There is no blueprint parameter here
either, so this wrapper cannot become a side channel into the audit step.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import paths
from .runner import run_script_json, temp_json

__all__ = ["MetricsResult", "run_metrics"]


class MetricsResult(object):
    __slots__ = ("assessable", "issues", "warnings", "parts", "manual_checks_required")

    def __init__(
        self,
        assessable: bool,
        issues: List[Dict[str, Any]],
        warnings: List[str],
        parts: List[Dict[str, Any]],
        manual_checks_required: List[str],
    ) -> None:
        self.assessable = assessable
        self.issues = issues
        self.warnings = warnings
        self.parts = parts
        self.manual_checks_required = manual_checks_required

    def as_dict(self) -> Dict[str, Any]:
        return {
            "assessable": self.assessable,
            "issues": self.issues,
            "warnings": self.warnings,
            "parts": self.parts,
            "manual_checks_required": self.manual_checks_required,
        }

    def audit_metrics(self) -> Dict[str, int]:
        """The five counts audit.json's ``metrics`` block requires.

        Returns {} when nothing was measured, so the audit step is never handed zeros that
        look like real measurements.
        """
        if not self.parts:
            return {}
        part = self.parts[0]
        keys = (
            "dialogue_words",
            "dialogue_turns",
            "first_half_turns",
            "second_half_turns",
            "narrator_words",
        )
        return {key: part[key] for key in keys if isinstance(part.get(key), int)}


async def run_metrics(material: Dict[str, Any]) -> MetricsResult:
    with temp_json(material=material) as files:
        payload = await run_script_json(paths.metrics_script(), [files["material"], "--json"])
    return MetricsResult(
        assessable=bool(payload.get("assessable")),
        issues=[i for i in payload.get("issues", []) if isinstance(i, dict)],
        warnings=[w for w in payload.get("warnings", []) if isinstance(w, str)],
        parts=[p for p in payload.get("parts", []) if isinstance(p, dict)],
        manual_checks_required=[
            m for m in payload.get("manual_checks_required", []) if isinstance(m, str)
        ],
    )
