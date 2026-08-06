"""Question-feasibility verdict: the backend's view of the stage-3A aggregator.

``skills/generate/generate-listening-part1/scripts/question_feasibility_preflight.py`` is the
authoritative implementation -- it is imported and called, never reimplemented. The same reasoning as
``crosscheck.py``: a second copy of the six-gate short-circuit order would be a second source of
truth for a decision that can cost a full material regeneration, and the two would drift without
anyone knowing which one produced a delivered verdict.

Imported in-process rather than shelled out. It is a pure function over two dicts, so a subprocess
would add latency and a JSON round-trip for nothing.

**The import needs its path set up here, and that is not incidental.** The aggregator does
``import validate_part1 as validator`` at module scope, so both files' directory has to be on
``sys.path`` before it loads. Stage 3A's tests got that for free because ``run_tests.py`` inserts the
path for its own reasons; nothing in ``backend/`` does, which makes this module the first place the
requirement actually bites.

What this module does *not* do: decide what to do about a verdict. Delivering the material anyway,
regenerating it, and refusing to start question generation are orchestration decisions.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

from .. import paths

__all__ = [
    "PASS",
    "PASS_WITH_JUSTIFICATION",
    "REGENERATE_MATERIAL",
    "SEMANTICS_MISSING",
    "UNSUPPORTED_VERSION",
    "VALIDATION_INCOMPLETE",
    "CANNOT_DECIDE",
    "preflight_verdict",
]

# The three client-named exits and the three cannot-decide states, restated as strings rather than
# re-exported from the script, so importing this module's names costs nothing at import time. They
# are asserted equal to the script's constants by a test -- a copy that can drift silently would be
# worse than the lazy import it saves.
PASS = "PASS"
PASS_WITH_JUSTIFICATION = "PASS_WITH_JUSTIFICATION"
REGENERATE_MATERIAL = "REGENERATE_MATERIAL"
SEMANTICS_MISSING = "SEMANTICS_MISSING"
VALIDATION_INCOMPLETE = "VALIDATION_INCOMPLETE"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"

# The three states that mean "no decision was reached", as opposed to the three exits above. Named as
# a set because the interesting question downstream is almost always "was this decided?" rather than
# "which of the three ways did it fail?" -- and because folding any of them into
# REGENERATE_MATERIAL would assert a material is unfit in order to report that a system-side problem
# occurred.
CANNOT_DECIDE = frozenset({SEMANTICS_MISSING, VALIDATION_INCOMPLETE, UNSUPPORTED_VERSION})

_preflight = None


def _load_preflight():
    """Import ``preflight`` from the generate pool's scripts directory on first use."""
    global _preflight
    if _preflight is None:
        scripts = str(paths.validate_script().parent)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from question_feasibility_preflight import preflight  # noqa: PLC0415 - path first

        _preflight = preflight
    return _preflight


def preflight_verdict(
    validation: Any, feasibility: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """The verdict for one material, as a plain dict.

    ``validation`` is ``ValidationResult.as_dict()`` (or the equivalent ``--json`` output; both were
    measured to carry the same four top-level keys). ``feasibility`` is the feasibility agent's reply,
    or ``None`` when that call could not be completed -- which the aggregator answers with
    ``SEMANTICS_MISSING`` rather than by guessing, and that is the intended behaviour, not a fallback
    this module adds.

    Returns a dict rather than the ``Verdict`` object so callers do not need the skill script's types
    on their import path, and so the value serialises straight into a slot record.
    """
    return _load_preflight()(validation, feasibility).as_dict()
