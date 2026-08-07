"""Deterministic counts handed to the question auditor, computed from the face and the script.

The auditor is told not to count, so somebody has to. This is that somebody, and it runs in local
Runtime Python rather than a remote sandbox -- a decision worth recording, because the material side
does the opposite and the reason there does not transfer. ``sandboxed_metrics.py`` runs remotely
because the *material auditor* has to run a script itself, and its only tool for that
(``strands_tools.shell``) bypasses ``agent.sandbox`` entirely, so no local arrangement can bound it.
Nothing in the question stage asks the agent to run anything: the orchestrator computes these counts
and puts the *result* in the payload. What keeps the question audit blind is the frozen input type, the
agent having no shell, and ``guards.assert_answer_blind`` -- none of which depends on where this code
executes. Running locally also skips a session setup and an upload per call.

**Every count here is derivable from the candidate-visible page, or from the page plus the narration.**
That is a hard constraint, not an accident of what was easy. ``validate_questions_part1.py`` computes a
richer set for its own use -- the QR-027 tallies, which split on the *canonical answers* -- and those
must not be reused here at any cost: an aggregate over the answers is still information about the
answers, and this payload is the one place a leak would be silent. Two of them are recoverable from the
face anyway, because ``response_form`` and ``answer_category`` are printed-page metadata, and those are
recomputed here from the face rather than lifted from the validator's report so the provenance stays
obvious. The spelling-burden tally has no face-side equivalent and is therefore simply absent; the
auditor judges QR-043 by reading, which is what the rule asks for.

``classify_blank`` and the narration parsing are imported from the question validator, never copied.
Two implementations of "is this blank at the end of its line" would drift, and the auditor would then be
told a distribution the validator disagrees with -- with no way to tell which number was wrong.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from .. import paths

__all__ = ["question_metrics"]

_helpers = None


def _load_helpers():
    """Import the validator's helpers on first use, with its directory on ``sys.path``.

    Lazy and cached for the same reason ``crosscheck`` does it: importing at module scope would make
    every import of ``deterministic/`` pay for a skill-script load, and ``validate_questions_part1``
    itself inserts a second path entry at import time.
    """
    global _helpers
    if _helpers is None:
        script = paths.question_validate_script()
        directory = str(script.parent)
        if directory not in sys.path:
            sys.path.insert(0, directory)
        import validate_questions_part1 as validator  # noqa: PLC0415 - path set up above

        _helpers = validator
    return _helpers


def _numbers(questions: List[Any]) -> List[int]:
    return [question["number"] for question in questions
            if isinstance(question, dict) and isinstance(question.get("number"), int)]


def _blank_positions(validator: Any, questions: List[Any]) -> Dict[str, int]:
    """QR-026 position classes, recomputed from the carriers rather than read back.

    The face carries a declared ``blank_position`` per item. Reading it would hand the distribution to
    whoever wrote the face, which is the one thing the field exists to allow checking.
    """
    counts = {"initial": 0, "medial": 0, "final": 0}
    for question in questions:
        if not isinstance(question, dict):
            continue
        position = validator.classify_blank(
            str(question.get("carrier_before") or ""), str(question.get("carrier_after") or ""))
        counts[position] = counts.get(position, 0) + 1
    return counts


def _tally(values: List[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        if isinstance(value, str) and value:
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _groups(face: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per group: its shape and how much of the page it prints.

    Label and signpost counts rather than the labels themselves -- the auditor already has every
    printed string in ``question_face`` and does not need them restated. What it cannot get by reading
    is the arithmetic: which items fall in which group, and how many navigation lines a window offers.
    """
    groups = [group for group in face.get("groups") or [] if isinstance(group, dict)]
    limit_of = {instruction.get("group_id"): instruction.get("word_limit")
                for instruction in face.get("instructions") or []
                if isinstance(instruction, dict)}
    members: Dict[Any, List[int]] = {}
    for question in face.get("questions") or []:
        if isinstance(question, dict) and isinstance(question.get("number"), int):
            members.setdefault(question.get("group_id"), []).append(question["number"])

    rows = []
    for group in groups:
        structure = group.get("structure") if isinstance(group.get("structure"), dict) else {}
        rows.append({
            "group_id": group.get("group_id"),
            "layout": group.get("layout"),
            "window": group.get("narrator_window_id"),
            "question_numbers": sorted(members.get(group.get("group_id"), [])),
            "word_limit": limit_of.get(group.get("group_id")),
            "has_title": bool(str(group.get("title") or "").strip()),
            "signposts": len([value for value in group.get("signposts") or []
                              if str(value or "").strip()]),
            "row_labels": len(structure.get("row_labels") or []),
            "column_labels": len(structure.get("column_labels") or []),
            "hierarchy_lines": len(structure.get("hierarchy") or []),
        })
    return rows


def _windows(validator: Any, material: Dict[str, Any], numbers: List[int]) -> Optional[Dict[str, Any]]:
    """Narrator windows parsed from the narration, or ``None`` when it cannot be parsed.

    ``None`` rather than a guess, and the caller then omits the key entirely. A window attribution
    invented from a half-read narration would be worse than none: the auditor is told to treat these
    numbers as settled, so a wrong one is a wrong answer it has been asked not to re-derive.
    """
    problems: List[str] = []
    turns, _package = validator.material_turns(material, problems)
    if not turns or problems:
        return None
    first_end, ranges = validator.narrator_windows(turns, problems)
    if problems or not first_end or not ranges:
        return None
    return {
        "first_window_last_question": first_end,
        "turn_ranges": {str(window): list(bounds) for window, bounds in sorted(ranges.items())},
        "membership": {str(number): validator.window_of(number, first_end)
                       for number in sorted(numbers)},
    }


def question_metrics(material: Dict[str, Any], question_face: Dict[str, Any]) -> Dict[str, Any]:
    """The counts the question auditor is handed instead of being asked to count.

    Starts empty and only gains a key once that key is measured, which is
    ``validate_questions_part1.main``'s convention and matters for the same reason: a zero is
    indistinguishable from a real count, so an unparseable narration must produce a missing
    ``narrator_windows`` rather than a plausible-looking wrong one.
    """
    validator = _load_helpers()
    face = question_face if isinstance(question_face, dict) else {}
    questions = [item for item in face.get("questions") or [] if isinstance(item, dict)]
    numbers = _numbers(questions)

    metrics: Dict[str, Any] = {
        "item_count": len(questions),
        "question_numbers": sorted(numbers),
        "blank_positions": _blank_positions(validator, questions),
        "groups": _groups(face),
        # Both tallies are over face fields, so they describe the printed page rather than the answers.
        # See the module docstring: the validator's QR-027 counts split on the canonicals and are
        # deliberately not reused, and the spelling-burden tally has no face-side equivalent at all.
        "response_forms": _tally([question.get("response_form") for question in questions]),
        "answer_categories": _tally([question.get("answer_category") for question in questions]),
    }
    metrics["final_blanks"] = metrics["blank_positions"].get("final", 0)

    windows = _windows(validator, material if isinstance(material, dict) else {}, numbers)
    if windows is not None:
        metrics["narrator_windows"] = windows
    return metrics
