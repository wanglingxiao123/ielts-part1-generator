"""Card-grid summary fields for one candidate. Pure Python, no model call.

The frontend's card grid shows a user three things about a material before they open it: the
first line they would hear, one line saying what the material is and what makes it hard, and ten
dots marking which item numbers a reviewer should look at. All three are derived here, from the
material and the blueprint, and never from a model:

* a model call per card would add a request to a batch already up against the 15-minute
  synchronous wall, for a line of text the artifacts already determine;
* a generated summary would be unreproducible, so two identical materials could describe
  themselves differently and the grid would stop being comparable.

``flagged_points`` deliberately reuses two existing definitions rather than adding a third:

* anchor mismatch is ``anchors.anchor_holds`` -- the same predicate the repair pass and
  ``validate_part1.py`` agree on. A card that called a point suspect while the reader showed its
  annotation as fine (or the reverse) would be worse than no dots at all.
* clustering and out-of-order come from ``frontend/src/domain/distribution.ts``, ported
  literally: the cluster run over TURN indexes with ``CLUSTER_SPAN`` / ``CLUSTER_MIN_POINTS``,
  and the "a later question's information is spoken first" scan over dialogue ordinals. The
  thresholds below are that file's, not new ones.

Only defects that cost a candidate a *written answer* are flagged. Uniformity/CV is a
presentation metric with an explicitly uncalibrated threshold (frontend runtimeConfig.ts,
``CALIBRATED: false``), so it is not turned into a dot here -- ten dots that light up on an
admittedly arbitrary number would train reviewers to ignore them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .anchors import NARRATOR, anchor_holds, dialogue_turns

__all__ = [
    "CLUSTER_SPAN",
    "CLUSTER_MIN_POINTS",
    "MAX_SUMMARY_FEATURES",
    "preview_first_line",
    "preview_summary",
    "flagged_points",
    "flagged_point_reasons",
]

# Both from frontend/public/config.json (defaults mirrored in runtimeConfig.ts). Points within
# this many TURN indexes count as one cluster; a run needs this many points before it is called
# one. CLUSTER_MIN_POINTS is 3 rather than 2 because the real balanced fixture legitimately has
# three 2-point pairs within 3 turns -- a point often spans ask/answer/confirm turns -- so at 2
# every material reports clusters and the signal is worthless.
CLUSTER_SPAN = 3
CLUSTER_MIN_POINTS = 3

# Detail types as the blueprint schema enumerates them. Used for the distraction phrase, so the
# summary names what is being distracted (价格 / 人名) instead of only that something is.
_TYPE_ZH = {
    "name": "人名",
    "number": "数字",
    "address": "地址",
    "price": "价格",
    "datetime": "时间",
    "quantity": "数量",
    "condition": "条件",
    "option": "选项",
}

# At most this many features after the topic. The card has one line; a summary that wraps stops
# being scannable, which is the only thing it is for.
MAX_SUMMARY_FEATURES = 3


def _items(blueprint: Any) -> List[Dict[str, Any]]:
    if not isinstance(blueprint, dict):
        return []
    items = blueprint.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def preview_first_line(material: Any) -> str:
    """The first line of real dialogue.

    speaker1 is the exam narrator, and its turn is a paragraph of test rubric identical across
    every material. Showing it would make every card in the grid open with the same words, so
    narration is skipped rather than truncated.
    """
    for turn in dialogue_turns(material):
        if turn.get("speaker") == NARRATOR:
            continue
        text = turn.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _dialogue_text(material: Any) -> str:
    return " ".join(
        turn.get("text", "")
        for turn in dialogue_turns(material)
        if turn.get("speaker") != NARRATOR and isinstance(turn.get("text"), str)
    )


def _has_spelling_run(material: Any) -> bool:
    """Is a letter-by-letter spelling sequence spoken (F-O-R-D-Y-C-E)?

    Matches ``validate_part1.py``'s ``SPELLING_RE`` intent: three or more single letters joined
    by hyphens. Kept as a scan of the dialogue rather than an inference from ``type == "name"``
    because a name can perfectly well be given without being spelled, and the point of the
    phrase on the card is that the listener has to transcribe letters.
    """
    import re

    return bool(re.search(r"\b(?:[A-Za-z]-){2,}[A-Za-z]\b", _dialogue_text(material)))


def _type_of_value(items: List[Dict[str, Any]], value: Any) -> Optional[str]:
    """The detail type of the item whose target or evidence contains ``value``.

    This is how "修正干扰" becomes "价格修正干扰": the blueprint records the correction as raw
    strings, and the item carrying them is what says which kind of detail was corrected.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    needle = value.casefold()
    for item in items:
        target = item.get("target")
        if isinstance(target, str) and target.strip() and (
            target.casefold() in needle or needle in target.casefold()
        ):
            return _TYPE_ZH.get(str(item.get("type")))
    for item in items:
        evidence = item.get("evidence")
        if isinstance(evidence, str) and needle in evidence.casefold():
            return _TYPE_ZH.get(str(item.get("type")))
    return None


def preview_summary(material: Any, blueprint: Any, topic: str = "") -> str:
    """One line: topic + what makes the material hard. Chinese, deterministic.

    ``topic`` comes from the scenario catalogue (``scenario_key`` -> ``title_zh``). It is passed
    in rather than looked up here so this module stays free of file I/O: it is called once per
    candidate on a request that has a hard wall.

    Falls back gracefully -- an empty topic yields the feature list alone, and a blueprint with
    no recorded distraction yields the topic alone. A card must never render an empty string
    where a sentence belongs, but it also must not claim a feature the artifacts do not show.
    """
    items = _items(blueprint)
    features: List[str] = []
    # Detail types already spoken for. Tracked as types rather than as rendered phrases because
    # "价格修正干扰" and "价格干扰" share no substring, so comparing the strings would let the
    # summary say the same thing twice in two different words.
    named_types = set()

    if _has_spelling_run(material):
        features.append("含拼读")

    correction = blueprint.get("correction") if isinstance(blueprint, dict) else None
    if isinstance(correction, dict) and correction.get("final"):
        kind = _type_of_value(items, correction.get("final")) or _type_of_value(
            items, correction.get("earlier")
        )
        features.append("{0}修正干扰".format(kind) if kind else "自我修正干扰")
        if kind:
            named_types.add(kind)

    indirect = blueprint.get("indirect_confirmation") if isinstance(blueprint, dict) else None
    if isinstance(indirect, dict) and indirect.get("reference_phrase"):
        features.append("间接指代确认")

    # Whatever distractor cycles remain, named by the type they attack.
    for item in items:
        if not item.get("distractor"):
            continue
        kind = _TYPE_ZH.get(str(item.get("type")))
        if not kind or kind in named_types:
            continue
        named_types.add(kind)
        features.append("{0}干扰".format(kind))

    tail = " + ".join(features[:MAX_SUMMARY_FEATURES])
    topic = (topic or "").strip()
    if topic and tail:
        return "{0}，{1}".format(topic, tail)
    return topic or tail


def _placed_points(material: Any, blueprint: Any):
    """(placed, unanchored). ``placed`` carries the dialogue ordinal each point is spoken at.

    Narration is excluded from the ordinal axis, exactly as ``joinArtifacts.ts`` does: the
    "no time to write" question is about how many dialogue turns apart two points are, and a
    narrator paragraph between them gives a candidate no writing time at all.
    """
    turns = dialogue_turns(material)
    if not turns:
        # A material with no usable turns is broken as a whole, not defective in ten specific
        # places. Flagging every item would put ten red dots on a card whose real problem is that
        # there is no script -- which the validator reports and which makes the audit
        # NOT_ASSESSABLE, so batch.py re-runs the slot rather than showing this card at all.
        return [], []

    ordinals: Dict[int, int] = {}
    running = 0
    for index, turn in enumerate(turns):
        if turn.get("speaker") == NARRATOR:
            continue
        ordinals[index] = running
        running += 1

    placed: List[Dict[str, Any]] = []
    unanchored: List[int] = []
    for item in _items(blueprint):
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        index = item.get("turn_index")
        if not anchor_holds(turns, index, item.get("evidence")) or index not in ordinals:
            # Out of range, pointing at narration, or the evidence is simply not in the turn it
            # claims. All three mean the annotation sits beside the wrong sentence, and all three
            # are excluded from the distribution maths (distribution.ts's `unplacedNumbers`) --
            # placing them would compute gaps from a position nothing supports.
            unanchored.append(number)
            continue
        placed.append({"number": number, "turn_index": index, "ordinal": ordinals[index]})

    placed.sort(key=lambda p: (p["ordinal"], p["number"]))
    return placed, unanchored


def flagged_point_reasons(material: Any, blueprint: Any) -> Dict[int, List[str]]:
    """{item number: reasons}. The reasons exist so a tooltip can say why a dot is marked."""
    placed, unanchored = _placed_points(material, blueprint)
    reasons: Dict[int, List[str]] = {}

    def flag(number: int, reason: str) -> None:
        bucket = reasons.setdefault(number, [])
        if reason not in bucket:
            bucket.append(reason)

    for number in unanchored:
        flag(number, "anchor_mismatch")

    # Clusters: maximal runs of >= CLUSTER_MIN_POINTS consecutive points spanning <=
    # CLUSTER_SPAN turn indexes. Turn span, not ordinal span, because that is what the reviewer
    # reads off the script -- and it is distribution.ts's rule verbatim.
    run_start = 0
    for i in range(1, len(placed) + 1):
        at_end = i == len(placed)
        still_tight = (
            not at_end
            and placed[i]["turn_index"] - placed[run_start]["turn_index"] <= CLUSTER_SPAN
        )
        if not still_tight:
            run = placed[run_start:i]
            if len(run) >= CLUSTER_MIN_POINTS:
                for point in run:
                    flag(point["number"], "clustered")
            run_start = i

    # Out of order: `placed` is sorted by ordinal, so a decreasing item number means a later
    # question's information is spoken before an earlier one's. Both ends are flagged -- from a
    # reviewer's seat either point could be the one in the wrong place.
    for i in range(1, len(placed)):
        previous, current = placed[i - 1], placed[i]
        if current["number"] < previous["number"] and current["ordinal"] > previous["ordinal"]:
            flag(previous["number"], "out_of_order")
            flag(current["number"], "out_of_order")

    return reasons


def flagged_points(material: Any, blueprint: Any) -> List[int]:
    """Ascending item numbers a reviewer should look at. Empty when the material is clean."""
    return sorted(flagged_point_reasons(material, blueprint))
