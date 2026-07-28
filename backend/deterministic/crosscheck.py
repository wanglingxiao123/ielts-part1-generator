"""Blind cross-check: the only place blueprint and audit legitimately meet (design.md §4).

Pure Python, no model. ``shared/cross_check.py`` is the authoritative implementation -- it is
imported and called, never reimplemented. A second copy of this comparison would be a second
source of truth for the most important quality signal in the system, and the two would drift
apart without anyone noticing which one the delivered verdict came from.

Imported in-process rather than shelled out: it is a pure function with no I/O, so a subprocess
would add latency for nothing and would need the JSON round-trip that ``compare`` avoids.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List

from .. import paths

__all__ = ["CrossCheckResult", "crosscheck"]

_compare = None


def _load_compare():
    """Import ``compare`` from the skill's shared/ directory on first use."""
    global _compare
    if _compare is None:
        shared = str(paths.skills_root() / "shared")
        if shared not in sys.path:
            sys.path.insert(0, shared)
        from cross_check import compare  # noqa: PLC0415 - path must be set up first

        _compare = compare
    return _compare


class CrossCheckResult(object):
    """Structured comparison of planned points against the auditor's blind reconstruction.

    ``unrecoverable`` and ``unintended_target`` are hard defects that go into the revise
    instruction's must-fix section, but they never trigger a regeneration (design.md §3.3):
    they describe a script that can be edited, not one that failed its contract.
    """

    __slots__ = ("ok", "planned", "observed", "matched", "unrecoverable",
                 "unintended_target", "ambiguous")

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.ok = bool(payload.get("ok"))
        self.planned = payload.get("planned", 0)
        self.observed = payload.get("observed", 0)
        self.matched = payload.get("matched", 0)
        self.unrecoverable = [r for r in payload.get("unrecoverable", []) if isinstance(r, dict)]
        self.unintended_target = [
            r for r in payload.get("unintended_target", []) if isinstance(r, dict)
        ]
        self.ambiguous = [r for r in payload.get("ambiguous", []) if isinstance(r, dict)]

    @property
    def hard_defects(self) -> List[Dict[str, Any]]:
        return self.unrecoverable + self.unintended_target

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "planned": self.planned,
            "observed": self.observed,
            "matched": self.matched,
            "unrecoverable": self.unrecoverable,
            "unintended_target": self.unintended_target,
            "ambiguous": self.ambiguous,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CrossCheckResult(matched=%s/%s, unrecoverable=%d, unintended=%d)" % (
            self.matched, self.planned, len(self.unrecoverable), len(self.unintended_target),
        )


def crosscheck(blueprint: Dict[str, Any], audit: Dict[str, Any]) -> CrossCheckResult:
    """Compare the generator's blueprint against the auditor's blind information map."""
    return CrossCheckResult(_load_compare()(blueprint, audit))
