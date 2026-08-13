"""Question-side blind cross-check: the answer key meets the blind reconstruction here, and nowhere else.

The sibling of :mod:`crosscheck` and built the same way, for the same stated reason:
``shared/cross_check_questions.py`` is the authoritative implementation and is *imported*, never
reimplemented. A second copy of this comparison would be a second source of truth for the most
important quality signal in the system, and the two would drift apart without anyone noticing which
one the delivered verdict came from.

Imported in-process rather than shelled out: pure functions, no I/O, so a subprocess would add
latency for nothing and would need a JSON round-trip that a direct call avoids. (The skill's
``main()`` still exists for a human or a skill-activated agent running it against two files.)

Two functions rather than one, because the two questions are independent and the second must not be
gated on the first:

* :func:`review_consistency` asks *does this review agree with itself* -- coverage, counts, status. It
  needs no answer key, so it can run the moment a review comes back, inside the envelope, where a
  self-contradictory review becomes a retry rather than a result.
* :func:`crosscheck_questions` asks *does the review agree with the writer* -- and needs the key, so
  it can only run in the orchestrator, after the envelope has accepted the review.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from .. import paths

__all__ = [
    "QuestionCrossCheckResult",
    "crosscheck_questions",
    "normalise_unique_quote_anchors",
    "quote_anchor_errors",
    "review_consistency",
]

_compare = None
_review_consistency = None
_quote_anchor_errors = None
_normalise_unique_quote_anchors = None


def _load():
    """Import the shared implementation from the skill's shared/ directory on first use."""
    global _compare, _review_consistency, _quote_anchor_errors, _normalise_unique_quote_anchors
    if _compare is None:
        shared = str(paths.skills_root() / "shared")
        if shared not in sys.path:
            sys.path.insert(0, shared)
        from cross_check_questions import (  # noqa: PLC0415 - path must be set up first
            compare,
            normalise_unique_quote_anchors as normalise_anchors,
            quote_anchor_errors as anchors,
            review_consistency as consistency,
        )

        _compare = compare
        _review_consistency = consistency
        _quote_anchor_errors = anchors
        _normalise_unique_quote_anchors = normalise_anchors
    return _compare, _review_consistency


def review_consistency(review: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute coverage, severity counts and ``question_qc_status`` from the review's own findings.

    Returns ``{"ok": bool, "errors": [str], "computed": {...}}``. A report rather than an exception,
    so the caller decides whether a self-inconsistent review is a retry or a logged warning -- and so
    every disagreement is visible at once instead of only the first.
    """
    return _load()[1](review)


def quote_anchor_errors(review: Dict[str, Any], material: Dict[str, Any]) -> List[str]:
    """Which rebuilt answers quote a span that is not in the ``turn_index`` they declare (AL-007).

    Returns one prose message per drifted row, empty when every quote sits where its row says. Needs no
    answer key -- it compares the review against the script alone -- so unlike
    :func:`crosscheck_questions` it can run inside the envelope, where a mistyped index is a cheap retry
    with a fresh agent instead of an ``anchor_adjacent`` row that three later stages have to interpret.
    """
    _load()
    return _quote_anchor_errors(review, material)


def normalise_unique_quote_anchors(
    review: Dict[str, Any], material: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Correct model-declared turn indices only when the quote has one script location."""
    _load()
    return _normalise_unique_quote_anchors(review, material)


class QuestionCrossCheckResult(object):
    """Structured comparison of the writer's answer key against the auditor's blind reconstruction.

    ``hard_defects`` are items where a candidate could be graded against something the printed page
    does not support: a different answer, no answer, an evidence turn that does not carry the answer,
    or a quote that is not in the script. Like the material side's hard defects they go into the
    revise instruction's must-fix section and never trigger a regeneration -- they describe questions
    that can be rewritten over a script that stays exactly as recorded (SR-021).

    ``needs_review`` is deliberately separate from both. A one-turn anchor gap is permitted *when the
    neighbouring turn confirms the same fact*, and whether it does is a reading of two sentences that
    no integer comparison settles. Folding it into ``hard_defects`` would demand rewrites of sound
    items; folding it into agreement would hide wrong anchors. It is neither, and it is reported.

    ``adjacency_normalised`` is the narrow case where that reading is *not* needed: the auditor's quote
    occurs verbatim in exactly one turn of the neighbourhood, so the sentence it read is known and the
    one-turn gap is a mistyped index. Those rows count in ``agreed`` and are listed here too, because a
    set with several of them is an auditor mis-counting the narration systematically -- a fact worth
    seeing even when no item is defective.
    """

    __slots__ = ("ok", "compared", "agreed", "by_outcome", "items", "hard_defects",
                 "needs_review", "adjacency_normalised", "leakage", "equally_supported_rivals",
                 "quotes_checked", "consistency")

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.ok = bool(payload.get("ok"))
        self.compared = payload.get("compared", 0)
        self.agreed = payload.get("agreed", 0)
        self.by_outcome = payload.get("by_outcome", {})
        self.items = [r for r in payload.get("items", []) if isinstance(r, dict)]
        self.hard_defects = [r for r in payload.get("hard_defects", []) if isinstance(r, dict)]
        self.needs_review = [r for r in payload.get("needs_review", []) if isinstance(r, dict)]
        self.adjacency_normalised = [
            r for r in payload.get("adjacency_normalised", []) if isinstance(r, dict)
        ]
        self.leakage = [r for r in payload.get("leakage", []) if isinstance(r, dict)]
        self.equally_supported_rivals = [
            r for r in payload.get("equally_supported_rivals", []) if isinstance(r, dict)
        ]
        # Whether the script was available for the quote check. Carried through rather than dropped:
        # a reader of this result must not mistake "not checked" for "checked and fine".
        self.quotes_checked = bool(payload.get("quotes_checked"))
        self.consistency = payload.get("consistency") or {}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "compared": self.compared,
            "agreed": self.agreed,
            "by_outcome": self.by_outcome,
            "items": self.items,
            "hard_defects": self.hard_defects,
            "needs_review": self.needs_review,
            "adjacency_normalised": self.adjacency_normalised,
            "leakage": self.leakage,
            "equally_supported_rivals": self.equally_supported_rivals,
            "quotes_checked": self.quotes_checked,
            "consistency": self.consistency,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return ("QuestionCrossCheckResult(agreed=%s/%s, hard=%d, leakage=%d, rivals=%d)"
                % (self.agreed, self.compared, len(self.hard_defects), len(self.leakage),
                   len(self.equally_supported_rivals)))


def crosscheck_questions(
    package: Dict[str, Any],
    review: Dict[str, Any],
    material: Optional[Dict[str, Any]] = None,
) -> QuestionCrossCheckResult:
    """Compare the question package's answer key against the auditor's blind reconstruction.

    ``material`` is optional in the signature only because the comparison genuinely works without it;
    every caller in this repo passes it, because without the script an auditor's quote cannot be
    verified against the turn it names.

    **The consistency recompute is attached here, unconditionally.** It is cheap, pure and needs no key,
    so there is no case for making it a second call the caller must remember -- and the cost of
    forgetting is not an error. It was forgotten in the first version of the question loop: with
    ``consistency`` absent, ``QuestionCandidate.counts`` read ``{}``, every severity came out zero, and
    a set with a MAJOR finding was ranked and reported as clean. Nothing raised. The shared script's
    ``main()`` had always attached it, so the CLI was correct while the in-process path was not, which
    is exactly the divergence this wrapper exists to prevent.
    """
    compare, consistency = _load()
    payload = compare(package, review, material)
    payload["consistency"] = consistency(review)
    payload["ok"] = bool(payload["ok"] and payload["consistency"]["ok"])
    return QuestionCrossCheckResult(payload)
