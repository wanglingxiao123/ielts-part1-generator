"""Deterministic anchor repair after a revision (design.md §5).

`turn_index` anchors each blueprint item to the dialogue turn carrying its evidence. Revising
the script shifts turn positions, and a stale anchor puts a reviewer's annotation beside the
wrong sentence -- a defect that is almost impossible to notice downstream, because both the
annotation and the sentence look plausible on their own.

The rule, exactly as designed and deliberately narrow:

* evidence already inside ``turns[turn_index]``  -> anchor is fine, leave it;
* evidence found at exactly one other turn       -> repair the index, record it;
* evidence found at zero or two-or-more turns    -> the revision failed.

No "nearest match" heuristic on multiple hits. Repeated sentences are the whole reason anchors
exist; guessing once here throws away the anchor's entire value, and it would guess silently.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["AnchorRepairResult", "repair_anchors", "find_evidence_turns", "anchor_holds",
           "dialogue_turns"]

NARRATOR = "speaker1"


class AnchorRepairResult(object):
    """Outcome of an anchor-repair pass.

    ``ok is False`` means the caller must discard the revision and keep the pre-revise
    version. There is no partial-success mode: a blueprint with one unlocatable anchor is not
    safe to ship, and shipping the other nine points with a wrong tenth is worse than shipping
    the original.
    """

    __slots__ = ("ok", "blueprint", "repaired", "failures")

    def __init__(
        self,
        ok: bool,
        blueprint: Dict[str, Any],
        repaired: Optional[List[Dict[str, Any]]] = None,
        failures: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.ok = ok
        self.blueprint = blueprint
        self.repaired = repaired or []
        self.failures = failures or []

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "repaired": self.repaired, "failures": self.failures}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "AnchorRepairResult(ok=%r, repaired=%d, failures=%d)" % (
            self.ok,
            len(self.repaired),
            len(self.failures),
        )


def dialogue_turns(material: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the material's turn list, or [] when the shape is unusable.

    Never raises: a malformed material is a content defect the validator reports, and turning
    it into an exception here would crash the orchestrator instead (skill-contract's D1).
    """
    parts = material.get("listening_material_parts") if isinstance(material, dict) else None
    if not isinstance(parts, list) or not parts:
        return []
    script = parts[0].get("script") if isinstance(parts[0], dict) else None
    turns = script.get("turns") if isinstance(script, dict) else None
    if not isinstance(turns, list):
        return []
    return [turn for turn in turns if isinstance(turn, dict)]


def _carries(turn: Dict[str, Any], needle: str) -> bool:
    """Does this turn carry the evidence, and is it a turn an item may anchor to?

    Narrator turns are excluded: the contract requires anchors to point at non-speaker1 turns,
    and narration must not carry answer information in the first place.
    """
    if turn.get("speaker") == NARRATOR:
        return False
    text = turn.get("text")
    if not isinstance(text, str):
        return False
    return needle in text.casefold()


def find_evidence_turns(turns: List[Dict[str, Any]], evidence: str) -> List[int]:
    """Indices of every eligible turn whose text contains ``evidence``.

    Case-insensitive exact substring, matching validate_part1.py's ``anchor_ok`` so the two
    never disagree about whether an anchor holds. Casefold only -- no whitespace or punctuation
    normalisation, because a looser match here could "repair" an anchor onto a turn the
    authoritative validator would then reject, producing a confusing double failure.
    """
    if not isinstance(evidence, str) or not evidence.strip():
        return []
    needle = evidence.casefold()
    return [index for index, turn in enumerate(turns) if _carries(turn, needle)]


def anchor_holds(turns: List[Dict[str, Any]], index: Any, evidence: str) -> bool:
    """Does ``turns[index]`` really carry ``evidence``?

    The single definition of "the annotation is beside the right sentence". Exported because the
    card summary needs the same answer that repair does, and a second copy of this predicate is
    exactly how the card and the reader would come to disagree about which points are suspect.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return False
    if not 0 <= index < len(turns):
        return False
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    return _carries(turns[index], evidence.casefold())


# Retained as a private alias: this module's own call sites read better with the underscore, and
# renaming them would churn the repair logic for no behavioural gain.
_anchor_holds = anchor_holds


def repair_anchors(material: Dict[str, Any], blueprint: Dict[str, Any]) -> AnchorRepairResult:
    """Re-sync blueprint anchors against a revised material.

    Returns a *copy* of the blueprint; the input is never mutated, so a failed repair leaves
    the caller's pre-revise objects untouched and the rollback in loop.py is trivially correct.

    Repair is advisory, not authoritative: validate_part1.py re-checks anchor consistency
    afterwards and has the final say (design.md §5 step 3). This pass exists to rescue the
    common "text unchanged, index shifted" case without a regeneration.
    """
    if not isinstance(blueprint, dict):
        return AnchorRepairResult(
            False, blueprint if isinstance(blueprint, dict) else {},
            failures=[{"reason": "blueprint is not an object"}],
        )

    patched = copy.deepcopy(blueprint)
    items = patched.get("items")
    if not isinstance(items, list) or not items:
        return AnchorRepairResult(
            False, patched, failures=[{"reason": "blueprint.items is missing or empty"}]
        )

    turns = dialogue_turns(material)
    if not turns:
        return AnchorRepairResult(
            False, patched, failures=[{"reason": "material carries no usable dialogue turns"}]
        )

    repaired: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for position, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append({"position": position, "reason": "item is not an object"})
            continue
        number, evidence = item.get("number"), item.get("evidence")
        old = item.get("turn_index")

        if not isinstance(evidence, str) or not evidence.strip():
            failures.append(
                {"number": number, "position": position, "reason": "evidence is missing or empty"}
            )
            continue

        if _anchor_holds(turns, old, evidence):
            continue

        hits = find_evidence_turns(turns, evidence)
        if len(hits) == 1:
            item["turn_index"] = hits[0]
            repaired.append(
                {"number": number, "from": old, "to": hits[0], "evidence": evidence}
            )
        else:
            # 0 hits: the revision changed or dropped the sentence the anchor described.
            # >=2 hits: the evidence is no longer unique, so any choice is a guess.
            failures.append(
                {
                    "number": number,
                    "position": position,
                    "turn_index": old,
                    "evidence": evidence,
                    "matches": hits,
                    "reason": (
                        "evidence not found in any dialogue turn"
                        if not hits
                        else "evidence matches %d turns; refusing to guess an anchor" % len(hits)
                    ),
                }
            )

    return AnchorRepairResult(not failures, patched, repaired, failures)


def anchor_summary(result: AnchorRepairResult) -> Tuple[int, int]:
    """(repaired, failed) counts, for event payloads and logs."""
    return len(result.repaired), len(result.failures)
