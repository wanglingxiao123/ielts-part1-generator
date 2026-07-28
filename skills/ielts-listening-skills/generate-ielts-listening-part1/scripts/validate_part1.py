#!/usr/bin/env python3
"""Validate IELTS Part 1 material JSON and its information-point blueprint.

Only errors fail the run. Warnings report deviations from observed typical values that the
spec does not require, and are meant as input to a revision step.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TOP_KEYS = {"model", "extracted_at", "test_package", "content_kind", "source_htmls", "listening_material_parts"}
PART_KEYS = {"reference", "test_package", "scenario", "script", "source_htmls"}
SCRIPT_KEYS = {"reference", "test_package", "turns", "speaker_count"}
FORBIDDEN_KEYS = {"candidate_questions", "questions", "answer_key", "answers", "item_evidence", "analysis", "quality_check"}
BLUEPRINT_KEYS = {"narration_mode", "split_after", "question_type_coverage", "items", "correction", "indirect_confirmation"}
ITEM_KEYS = {"number", "group", "type", "target", "evidence", "turn_index", "item_form", "form_group", "distractor", "confirmed"}
ITEM_FORMS = {"form", "table", "multiple_choice", "note"}
TABLE_FORMS = {"form", "table"}
DETAIL_TYPES = {"name", "number", "address", "price", "datetime", "quantity", "condition", "option"}
SPELLED_TYPES = {"name"}
NUMERIC_TYPES = {"number", "price", "datetime", "quantity", "address"}
MIN_CONFIRMED = 3
MIN_GROUPED_ITEMS = 3
MIN_CHOICE_ITEMS = 2
MAX_GROUP_SPAN = 14
SPELLING_RE = re.compile(r"\b(?:[A-Z]\s*[-,]\s*){2,}[A-Z]\b|\b[A-Z](?:-[A-Z]){2,}\b|\bdouble\s+[A-Z]\b", re.I)
NUMBER_RE = re.compile(r"(?:[$£€]\s*\d|\b\d+\b)")
FIRST_RANGE_RE = re.compile(r"questions?\s+1\s*(?:to|[-~–])\s*(\d+)", re.I)
SECOND_RANGE_RE = re.compile(r"questions?\s+(\d+)\s*(?:to|[-~–])\s*10", re.I)
# Match the requirement semantically, not by literal phrase. The spec asks the opening to
# "naturally cover four recordings" and the closing to give "checking time"; it never fixes the
# wording. Real archived scripts bear this out: 30/30 say "check your answers", but only 4/30 say
# "four different recordings" (most say "a number of different recordings") and 6/30 "four parts".
# Demanding the literal strings rejected valid narration and was the largest single cause of
# regeneration in live runs.
RECORDINGS_RE = re.compile(r"\b(?:four|4|a number of|several|different)\s+(?:different\s+)?recordings?\b", re.I)
PARTS_RE = re.compile(r"\b(?:four|4)\s+(?:parts|sections)\b|\bin\s+(?:four|4)\b", re.I)
CHECKING_RE = re.compile(r"check\s+(?:all\s+of\s+)?your\s+(?:answers|work)", re.I)


def words(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value))


def exact_keys(value: dict, expected: set[str], label: str, errors: list[str]) -> None:
    missing, extra = sorted(expected - value.keys()), sorted(value.keys() - expected)
    if missing:
        errors.append(f"{label} missing fields: {missing}")
    if extra:
        errors.append(f"{label} unexpected fields: {extra}")


def forbidden_paths(value: object, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found.extend(forbidden_paths(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(forbidden_paths(child, f"{path}[{index}]"))
    return found


def read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc


def find_turn(turns: list[dict], phrase: object) -> int:
    needle = str(phrase or "").casefold()
    if not needle:
        return -1
    for index, turn in enumerate(turns):
        if turn.get("speaker") != "speaker1" and needle in str(turn.get("text", "")).casefold():
            return index
    return -1


def find_occurrence(turns: list[dict], phrase: object) -> tuple[int, int]:
    needle = str(phrase or "").casefold()
    if not needle:
        return (-1, -1)
    for index, turn in enumerate(turns):
        if turn.get("speaker") == "speaker1":
            continue
        position = str(turn.get("text", "")).casefold().find(needle)
        if position >= 0:
            return (index, position)
    return (-1, -1)


def anchor_ok(turns: list[dict], index: object, phrase: str) -> bool:
    if not isinstance(index, bool) and isinstance(index, int) and 0 <= index < len(turns):
        turn = turns[index]
        if turn.get("speaker") != "speaker1":
            return phrase.casefold() in str(turn.get("text", "")).casefold()
    return False


def validate_grouping(items: list[dict], coverage: object, errors: list[str], warnings: list[str]) -> None:
    """Check the material can actually support table/form and multiple-choice items.

    Ten scattered gap-fills pass every other check but leave item writers unable to
    build a table question, which is what the spec's 题型适配 requirement is about.
    """
    # Key groups by (item_form, form_group), not form_group alone. Counting a shared group label
    # says nothing about whether a table can be built from it: ten multiple_choice points that
    # happen to share a label, or a "group" mixing form/table/note, would otherwise pass while
    # leaving an item writer unable to lay out a single table.
    groups: dict[tuple, list[int]] = {}
    labels: dict[str, set] = {}
    choice_count = 0
    for item in items:
        form, group = item.get("item_form"), item.get("form_group")
        if form not in ITEM_FORMS:
            errors.append(f"blueprint.items[{item.get('number')}].item_form must be one of {sorted(ITEM_FORMS)}")
        if form == "multiple_choice":
            choice_count += 1
        if isinstance(group, str) and group.strip():
            groups.setdefault((form, group), []).append(item.get("number"))
            labels.setdefault(group, set()).add(form)
        elif group is not None:
            errors.append(f"blueprint.items[{item.get('number')}].form_group must be a non-empty string or null")

    for group, forms in labels.items():
        if len(forms) > 1:
            errors.append(
                f"form_group {group!r} mixes item_form values {sorted(map(str, forms))}; "
                "a group must be homogeneous to become one table or form question"
            )
    largest = max((len(v) for (form, _), v in groups.items() if form in TABLE_FORMS), default=0)
    if largest < MIN_GROUPED_ITEMS:
        errors.append(
            "blueprint needs one homogeneous form/table form_group with %d+ items to support a "
            "table or form question; largest is %d" % (MIN_GROUPED_ITEMS, largest)
        )

    # A group spread across most of the script makes candidates hold answers for half the
    # recording. Advisory: the spec sets no span limit, so this informs revision rather than
    # blocking an otherwise usable material.
    anchors = {item.get("number"): item.get("turn_index") for item in items}
    for (form, group), numbers in groups.items():
        spans = [anchors[n] for n in numbers if isinstance(anchors.get(n), int)]
        if len(spans) >= 2 and max(spans) - min(spans) > MAX_GROUP_SPAN:
            warnings.append(
                f"form_group {group!r} ({form}) spans turns {min(spans)}-{max(spans)}; "
                "points in one table question sit far apart"
            )
    if choice_count < MIN_CHOICE_ITEMS:
        errors.append(f"blueprint needs at least {MIN_CHOICE_ITEMS} multiple_choice items; found {choice_count}")

    if not isinstance(coverage, dict) or not coverage:
        errors.append("blueprint.question_type_coverage must be a non-empty object")
        return
    declared: list[int] = []
    for form, numbers in coverage.items():
        if form not in ITEM_FORMS:
            errors.append(f"question_type_coverage has unknown type {form!r}")
        if not isinstance(numbers, list):
            errors.append(f"question_type_coverage[{form!r}] must be a list")
            continue
        declared.extend(n for n in numbers if isinstance(n, int) and not isinstance(n, bool))
        for number in numbers:
            match = next((i for i in items if i.get("number") == number), None)
            if match is None:
                errors.append(f"question_type_coverage[{form!r}] references unknown item {number}")
            elif match.get("item_form") != form:
                errors.append(
                    f"question_type_coverage[{form!r}] lists item {number} but its item_form is {match.get('item_form')!r}"
                )
    if sorted(declared) != list(range(1, 11)):
        errors.append(f"question_type_coverage must cover items 1-10 exactly once; flattened to {sorted(declared)}")


def validate_blueprint(blueprint: object, turns: list[dict], midpoint: int, first_end: int, second_start: int, errors: list[str], warnings: list[str]) -> str:
    if not isinstance(blueprint, dict):
        errors.append("blueprint must be an object")
        return "full"
    exact_keys(blueprint, BLUEPRINT_KEYS, "blueprint", errors)
    mode = blueprint.get("narration_mode")
    if mode not in {"full", "short"}:
        errors.append("blueprint.narration_mode must be full or short")
        mode = "full"
    split_after = blueprint.get("split_after")
    if split_after not in {5, 6} or split_after != first_end or second_start != first_end + 1:
        errors.append("blueprint split must match a contiguous 1-5/6-10 or 1-6/7-10 narration split")
    items = blueprint.get("items")
    if not isinstance(items, list) or len(items) != 10:
        errors.append("blueprint.items must contain exactly 10 items")
        items = []
    positions, types, distractors, confirmations = [], set(), 0, 0
    checked, targets = [], set()
    for number, item in enumerate(items, 1):
        label = f"blueprint.items[{number - 1}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        exact_keys(item, ITEM_KEYS, label, errors)
        if item.get("number") != number:
            errors.append(f"{label}.number must be {number}")
        expected_group = 1 if number <= split_after else 2
        if item.get("group") != expected_group:
            errors.append(f"{label}.group must be {expected_group}")
        detail_type = item.get("type")
        if not isinstance(detail_type, str) or detail_type.casefold() not in DETAIL_TYPES:
            errors.append(f"{label}.type must be one of {sorted(DETAIL_TYPES)}")
        target, evidence = item.get("target"), item.get("evidence")
        if not isinstance(target, str) or not target.strip() or not isinstance(evidence, str) or not evidence.strip():
            errors.append(f"{label} target/evidence must be non-empty strings")
            continue
        targets.add(target.casefold())
        if target.casefold() not in evidence.casefold():
            errors.append(f"{label}.target must occur inside evidence")

        # turn_index is the anchor the reviewer UI renders against. find_turn returns the
        # first match, so it cannot arbitrate repeated sentences -- that is exactly why the
        # anchor exists. Trust the declared index and verify it, never re-derive it.
        anchor = item.get("turn_index")
        if anchor_ok(turns, anchor, evidence):
            position = anchor
        else:
            fallback = find_turn(turns, evidence)
            if fallback < 0:
                errors.append(f"{label}.evidence not found in any dialogue turn")
            else:
                errors.append(
                    f"{label}.turn_index {anchor!r} does not carry its evidence (found at turn {fallback})"
                )
            position = -1
        if position >= 0:
            positions.append(position)
            if (expected_group == 1 and position >= midpoint) or (expected_group == 2 and position <= midpoint):
                errors.append(f"{label}.evidence is in the wrong dialogue half")
        if isinstance(detail_type, str):
            types.add(detail_type.casefold())
        if item.get("distractor") is True:
            distractors += 1
        elif item.get("distractor") is not False:
            errors.append(f"{label}.distractor must be boolean")
        if item.get("confirmed") is True:
            confirmations += 1
        elif item.get("confirmed") is not False:
            errors.append(f"{label}.confirmed must be boolean")
        checked.append(item)
    if len(positions) == 10 and (positions != sorted(positions) or len(set(positions)) != 10):
        errors.append("item evidence must occur in strictly increasing, distinct dialogue turns")
    if len(types) < 4:
        errors.append("blueprint must use at least four detail types")
    if not 2 <= distractors <= 3:
        errors.append(f"blueprint must mark 2-3 distractor items; found {distractors}")
    if confirmations < MIN_CONFIRMED:
        errors.append(f"blueprint must mark at least {MIN_CONFIRMED} confirmed items; found {confirmations}")
    # At least one confirmed point in each of the two easiest-to-mishear categories.
    # Requiring every numeric item be confirmed would make the dialogue repetitive, which
    # spec 5 warns against; the spec asks for 关键信息 confirmation, not blanket confirmation.
    # Presence is required too: the script must contain a spelling sequence and a numeric
    # detail anyway, so a blueprint with no item of that type has simply failed to record it.
    for label, wanted in (("spelled-name", SPELLED_TYPES), ("numeric", NUMERIC_TYPES)):
        present = [i for i in checked if str(i.get("type", "")).casefold() in wanted]
        if not present:
            errors.append(f"blueprint must record at least one {label} item")
        elif not any(i.get("confirmed") is True for i in present):
            errors.append(
                f"at least one {label} item must be confirmed; "
                "these are the easiest to mishear under once-only listening"
            )
    if checked:
        validate_grouping(checked, blueprint.get("question_type_coverage"), errors, warnings)
    correction = blueprint.get("correction")
    if not isinstance(correction, dict):
        errors.append("blueprint.correction must be an object")
    else:
        exact_keys(correction, {"earlier", "final", "marker"}, "blueprint.correction", errors)
        earlier, final, marker = (find_occurrence(turns, correction.get(key)) for key in ("earlier", "final", "marker"))
        if min(earlier[0], final[0], marker[0]) < 0 or not earlier < marker < final:
            errors.append("correction must contain earlier value, then replacement marker, then final value")
    indirect = blueprint.get("indirect_confirmation")
    if not isinstance(indirect, dict):
        errors.append("blueprint.indirect_confirmation must be an object")
    else:
        exact_keys(indirect, {"answer_term", "reference_phrase"}, "blueprint.indirect_confirmation", errors)
        answer_term = indirect.get("answer_term")
        answer = find_turn(turns, answer_term)
        reference = find_turn(turns, indirect.get("reference_phrase"))
        if answer < 0 or reference < 0 or answer >= reference:
            errors.append("indirect answer term must occur before its reference phrase")
        # 命题铁律 (spec line 172): the answer word itself must be spoken aloud, so a later
        # item writer can use it verbatim as the key. Indirect reference only adds listening
        # difficulty on top -- it must never be the sole carrier of the answer.
        if isinstance(answer_term, str) and answer_term.casefold() not in targets:
            errors.append(
                f"indirect_confirmation.answer_term {answer_term!r} must be the target of one of the ten items; "
                "the answer word has to be recoverable verbatim from the audio"
            )
    return str(mode)


def report(errors: list[str], warnings: list[str], metrics: dict, as_json: bool) -> int:
    """Emit results. Only errors fail the run.

    The 600-650 word / 30-40 turn bands are the observed typical values across 20 real test
    sets (spec 4A), not authoring gates -- the spec sets only 450/750 as limits. Failing on
    those warnings forced regeneration until the model hit a 51-word window, which is
    expensive and not what the spec asks for. Warnings now flow to the revise step as advice.
    """
    if as_json:
        print(json.dumps({
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "metrics": metrics,
        }, ensure_ascii=False, indent=2))
    else:
        for key, value in metrics.items():
            print(f"{key.replace('_', ' ').capitalize()}: {value}")
        for message in errors:
            print(f"ERROR: {message}")
        for message in warnings:
            print(f"WARNING: {message}")
        status = "FAIL" if errors else "PASS"
        print(f"{status}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("material", type=Path)
    parser.add_argument("--blueprint", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()
    try:
        data, blueprint = read_json(args.material, "material"), read_json(args.blueprint, "blueprint")
    except ValueError as exc:
        return report([str(exc)], [], {}, args.json)
    errors, warnings = [], []
    if not isinstance(data, dict):
        return report(["top-level JSON must be an object"], [], {}, args.json)
    exact_keys(data, TOP_KEYS, "top level", errors)
    if not isinstance(data.get("model"), str) or not data.get("model", "").strip():
        errors.append("model must be non-empty")
    if not isinstance(data.get("test_package"), str) or not data.get("test_package", "").strip():
        errors.append("test_package must be non-empty")
    if data.get("content_kind") != "listening_material":
        errors.append("content_kind must be listening_material")
    if data.get("source_htmls") != []:
        errors.append("top-level source_htmls must be []")
    try:
        datetime.fromisoformat(str(data.get("extracted_at", "")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("extracted_at must be ISO 8601")
    forbidden = forbidden_paths(data)
    if forbidden:
        errors.append(f"forbidden output fields: {forbidden}")
    parts = data.get("listening_material_parts")
    if not isinstance(parts, list) or len(parts) != 1:
        errors.append("listening_material_parts must contain exactly one object")
        parts = []
    total_words = total_turns = 0
    # Start empty and only add keys once measured. Zeros would be indistinguishable from a real
    # measurement, so a script that bailed out early would report "0 dialogue words" rather than
    # "not measured" to any UI reading this.
    metrics: dict = {}
    for part in parts:
        if not isinstance(part, dict):
            errors.append("part must be an object")
            continue
        exact_keys(part, PART_KEYS, "part", errors)
        if part.get("reference") != "Part 1" or part.get("test_package") != data.get("test_package"):
            errors.append("part reference/package is invalid")
        if not isinstance(part.get("scenario"), str) or not part.get("scenario", "").strip():
            errors.append("scenario must be non-empty")
        if part.get("source_htmls") != []:
            errors.append("part.source_htmls must be []")
        script = part.get("script")
        if not isinstance(script, dict):
            errors.append("script must be an object")
            continue
        exact_keys(script, SCRIPT_KEYS, "script", errors)
        if script.get("reference") != part.get("reference") or script.get("test_package") != part.get("test_package"):
            errors.append("script reference/package must match part")
        if script.get("speaker_count") != 3:
            errors.append("speaker_count must be 3")
        turns = script.get("turns")
        if not isinstance(turns, list) or not turns:
            errors.append("turns must be non-empty")
            continue
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                errors.append(f"turn[{index}] must be an object")
                continue
            exact_keys(turn, {"speaker", "text"}, f"turn[{index}]", errors)
            if turn.get("speaker") not in {"speaker1", "speaker2", "speaker3"}:
                errors.append(f"turn[{index}] has invalid speaker")
            if not isinstance(turn.get("text"), str) or not turn.get("text", "").strip():
                errors.append(f"turn[{index}] has empty text")
        if {turn.get("speaker") for turn in turns if isinstance(turn, dict)} != {"speaker1", "speaker2", "speaker3"}:
            errors.append("speaker set must be exactly speaker1/2/3")
        # Every check past this point indexes turn["text"], so a malformed turn would raise and
        # leave stdout empty. That turns a recoverable content defect into an orchestration
        # crash for any caller parsing --json, so stop here and report what we have.
        if any(not isinstance(turn, dict) or not isinstance(turn.get("text"), str) for turn in turns):
            continue
        narrator = [i for i, turn in enumerate(turns) if turn.get("speaker") == "speaker1"]
        if len(narrator) != 3:
            errors.append(f"exactly three narrator turns required; found {len(narrator)}")
            continue
        if narrator[0] != 0 or narrator[-1] != len(turns) - 1:
            errors.append("speaker1 must open and close the script")
        opening, midpoint_text, closing = (turns[i]["text"] for i in narrator)
        first, second = FIRST_RANGE_RE.search(opening), SECOND_RANGE_RE.search(midpoint_text)
        first_end, second_start = (int(first.group(1)), int(second.group(1))) if first and second else (0, 0)
        if first_end not in {5, 6} or second_start != first_end + 1:
            errors.append("narration must state 1-5/6-10 or 1-6/7-10")
        if "once only" not in opening.casefold():
            errors.append("opening must include once only")
        if not re.search(r"end of (?:section one|part 1|part one)", closing, re.I) or not CHECKING_RE.search(closing):
            errors.append("closing must identify Part 1 end and checking time")
        if not re.search(r"(?:turn|move on) to (?:section|part) (?:two|2)", closing, re.I):
            errors.append("closing must direct candidates to Part/Section 2")
        mode = validate_blueprint(blueprint, turns, narrator[1], first_end, second_start, errors, warnings)
        narration = " ".join(turns[i]["text"] for i in narrator)
        if mode == "full":
            for label, pattern in (("the set of recordings", RECORDINGS_RE), ("the test's parts", PARTS_RE)):
                if not pattern.search(opening):
                    errors.append(f"full opening must introduce {label}")
            if not 160 <= words(narration) <= 230:
                errors.append(f"full narration must be 160-230 words; found {words(narration)}")
        elif not 70 <= words(narration) <= 110:
            errors.append(f"short narration must be 70-110 words; found {words(narration)}")
        dialogue_turns = [turn for turn in turns if turn.get("speaker") != "speaker1"]
        dialogue = " ".join(turn["text"] for turn in dialogue_turns)
        total_words, total_turns = total_words + words(dialogue), total_turns + len(dialogue_turns)
        if not 450 <= words(dialogue) <= 750:
            errors.append(f"dialogue words outside 450-750: {words(dialogue)}")
        elif not 600 <= words(dialogue) <= 650:
            warnings.append(f"dialogue words outside preferred 600-650: {words(dialogue)}")
        if not 20 <= len(dialogue_turns) <= 48:
            errors.append(f"dialogue turns outside 20-48: {len(dialogue_turns)}")
        elif not 30 <= len(dialogue_turns) <= 40:
            warnings.append(f"dialogue turns outside preferred 30-40: {len(dialogue_turns)}")
        before = sum(i < narrator[1] for i, turn in enumerate(turns) if turn.get("speaker") != "speaker1")
        after = len(dialogue_turns) - before
        if before < 8 or after < 8:
            errors.append(f"each half needs 8 turns; found {before}/{after}")
        # Spec 4A also asks for 前后均衡. Advisory rather than an error: the spec sets no ratio,
        # and a 20/14 split is still usable, so this informs the revise step without blocking.
        elif min(before, after) / max(before, after) < 0.6:
            warnings.append(f"dialogue halves are uneven: {before}/{after}")
        if not SPELLING_RE.search(dialogue):
            errors.append("no spelling sequence detected")
        if not NUMBER_RE.search(dialogue):
            errors.append("no numeric information detected")
        metrics.update({
            "dialogue_words": total_words,
            "dialogue_turns": total_turns,
            "first_half_turns": before,
            "second_half_turns": after,
            "narrator_words": words(narration),
        })
    return report(errors, warnings, metrics, args.json)


if __name__ == "__main__":
    sys.exit(main())
