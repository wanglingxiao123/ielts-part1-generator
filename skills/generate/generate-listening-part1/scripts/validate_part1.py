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
BLUEPRINT_KEYS = {"narration_mode", "split_after", "items", "correction"}
# Present in only 4 of the 27 usable archived papers, while the spec (§4B-4) asks for 2-3
# distraction cycles chosen from five mechanisms rather than for this one specifically.
# `blueprint_schema_version` is optional because its ABSENCE is what marks a v1 record; the two
# coverage names are optional here for the same reason and are then required one-per-version by
# `validate_coverage_name` -- `exact_keys` can only demand a fixed set, so version-conditional
# requirements have to live outside it.
BLUEPRINT_OPTIONAL_KEYS = {"indirect_confirmation", "blueprint_schema_version",
                           "question_type_coverage", "completion_layout_coverage"}
V1_COVERAGE_KEY = "question_type_coverage"
V2_COVERAGE_KEY = "completion_layout_coverage"
V_KEY = "blueprint_schema_version"
BLUEPRINT_SCHEMA_VERSION = 2
ITEM_KEYS = {"number", "group", "type", "target", "evidence", "turn_index", "item_form", "form_group", "distractor", "confirmed"}
# v2 only. Kept separate from ITEM_KEYS so the v1 read path keeps rejecting them as unknown keys:
# a v1 record carrying `response_form` is not a lenient v1, it is a v2 that lost its version field.
V2_ITEM_KEYS = {"response_form", "answer_category", "narrator_window_id"}
RESPONSE_FORMS = {"numeric", "word", "phrase"}
# Internal closed taxonomy, NOT a client-supplied enum. QR-027 only asks for an answer category per
# item and offers location / price / service as examples; these 13 values exist so the "no
# micro-category tested by 3+ items" rule can be counted deterministically. There is deliberately no
# `other`: a catch-all would collide unrelated points into one bucket and misfire that count, and it
# would hand the model a "when unsure, pick other" escape hatch that removes the field's only value.
ANSWER_CATEGORIES = {
    "person_name", "contact", "location", "date", "time", "duration", "price",
    "quantity", "service", "facility", "requirement", "preference", "document",
}
# QR-027 thresholds. Counting uses `derive_qr027_class`, not the persisted `response_form`: the two
# split on different axes (token count vs character composition), so `Room 4B` is a `phrase` whose
# QR-027 class is `mixed`. Reported as metrics only in this stage; the gate is stage 3's business.
QR027_MAX_NUMERIC = 4
QR027_MIN_SPELLED = 4
# "不得在十题内无理由地用三题或以上反复测试同一微型答案类别" -- three already violates, so the
# admissible count is strictly below 3, not at most 3.
QR027_MAX_SAME_CATEGORY = 3
# A token counts as numeric only when the WHOLE token is a number/time/date form. "Contains a digit"
# is the trap: it makes the postcode `BT14 9BJ` read as numeric when it is plainly something the
# candidate must spell. Same class of error as `address` sitting in NUMERIC_TYPES below.
NUMERIC_TOKEN_RE = re.compile(r"^[£$€]?\d+(?:[.,:/]\d+)*(?:st|nd|rd|th|am|pm|a\.m\.|p\.m\.)?$", re.I)
# The closed set of standard IELTS rubrics, ordered strictest to loosest, each as
# (max lexical words, permitted purely-numeric tokens). The question stage owns the *choice* of
# rubric per group; this stage owns only the question "does any rubric exist that could carry this
# target at all", and it lives here because both stages must answer it with one arithmetic.
#
# Why a blueprint-stage check at all: the question stage may not replace a blueprint target (the ten
# information points are given input) and may not touch the Script (SR-021). So a target that fits no
# rubric is unfixable there -- the question agent can only cycle. Measured, 2026-08-08: a
# `service-refund` blueprint carried target `9 and 1` from "The driver calls between 9 and 1.", which
# costs 1 lexical word + 2 numeric tokens. No rubric permits 2 numbers, so every attempt failed on
# AR-002/QR-017; rewriting it as `9-1` failed "not blueprint item 7's target"; loosening one group's
# rubric failed "the paper and the marking key would be different tests". The material spent its
# whole 810s budget cycling between those three walls and delivered nothing.
#
# The widest rubric is therefore the admissibility bound for a single target, and it is deliberately
# NOT a per-group choice here: which of the six a group prints depends on its other nine answers and
# is the question stage's decision.
WORD_LIMITS = (
    ("ONE WORD ONLY", 1, 0),
    ("ONE WORD AND/OR A NUMBER", 1, 1),
    ("NO MORE THAN TWO WORDS", 2, 0),
    ("NO MORE THAN TWO WORDS AND/OR A NUMBER", 2, 1),
    ("NO MORE THAN THREE WORDS", 3, 0),
    ("NO MORE THAN THREE WORDS AND/OR A NUMBER", 3, 1),
)
# Part 1 delivers Form / Note / Table completion only. `multiple_choice` was removed when the
# client narrowed the brief; `type: "option"` in DETAIL_TYPES below is a different dimension --
# it names the KIND of detail (a preference or chosen alternative) and remains a fine completion
# answer, so it stays.
ITEM_FORMS = {"form", "table", "note"}
# v1 read path ONLY. `multiple_choice` was a legal layout before the client narrowed the brief, so
# archived records carry it and must stay readable; new generation and every v2 record are still held
# to ITEM_FORMS alone. Use `item_forms_for(version)` rather than testing this set directly -- the
# leniency has to be keyed on version, or it leaks into the write path.
V1_LEGACY_ITEM_FORMS = ITEM_FORMS | {"multiple_choice"}
TABLE_FORMS = {"form", "table"}
DETAIL_TYPES = {"name", "number", "address", "price", "datetime", "quantity", "condition", "option"}
SPELLED_TYPES = {"name"}
NUMERIC_TYPES = {"number", "price", "datetime", "quantity", "address"}
MIN_CONFIRMED = 3
MIN_GROUPED_ITEMS = 3
MAX_GROUP_SPAN = 14
SPELLING_RE = re.compile(r"\b(?:[A-Z]\s*[-,]\s*){2,}[A-Z]\b|\b[A-Z](?:-[A-Z]){2,}\b|\bdouble\s+[A-Z]\b", re.I)
# `\b\d+\b` misses a digit glued to a suffix, which is how real transcripts write ordinals and
# postcodes: snap_002's only numeral is "80th" and snap_054's postcode is "B0241DJ". Measured, the
# word-boundary form rejected snap_002 outright -- a paper whose dialogue is full of dates and
# amounts -- for having no numeric information. Any digit at all is the honest test; the spec
# (§3) asks for "一处数字信息", not for a bare integer token.
NUMBER_RE = re.compile(r"(?:[$£€]\s*\d|\d)")
# `\s+` after "questions" rejects the un-spaced form. Measured: snap_022_scripts_topic8163 writes
# "questions1~4" and was rejected for having no split at all, which then also failed the blueprint
# split rule. The separator is optional, matching the corpus rather than our typography.
FIRST_RANGE_RE = re.compile(r"questions?\s*1\s*(?:to|[-~–])\s*(\d+)", re.I)
SECOND_RANGE_RE = re.compile(r"questions?\s*(\d+)\s*(?:to|[-~–])\s*10", re.I)
# Match the requirement semantically, not by literal phrase. The spec asks the opening to
# "naturally cover four recordings" and the closing to give "checking time"; it never fixes the
# wording. Real archived scripts bear this out: 30/30 say "check your answers", but only 4/30 say
# "four different recordings" (most say "a number of different recordings") and 6/30 "four parts".
# Demanding the literal strings rejected valid narration and was the largest single cause of
# regeneration in live runs.
RECORDINGS_RE = re.compile(r"\b(?:four|4|a number of|several|different)\s+(?:different\s+)?recordings?\b", re.I)
PARTS_RE = re.compile(r"\b(?:four|4)\s+(?:parts|sections)\b|\bin\s+(?:four|4)\b", re.I)
CHECKING_RE = re.compile(r"check\s+(?:all\s+of\s+)?your\s+(?:answers|work)", re.I)
# "now, turn to section two" (comma) occurs in the corpus, so the separator must be tolerant.
NEXT_PART_RE = re.compile(r"(?:turn|move on|go on)\W{0,3}to\s+(?:section|part)\s+(?:two|2)", re.I)


def words(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value))


def exact_keys(
    value: dict, expected: set[str], label: str, errors: list[str],
    optional: set[str] | None = None,
) -> None:
    """Keys must be exactly `expected`, except that `optional` ones may be absent.

    An unexpected key is still an error either way: a typo'd field name would otherwise be
    silently ignored, which is how a blueprint can look complete and carry nothing.
    """
    allowed = expected | (optional or set())
    missing = sorted(expected - (optional or set()) - value.keys())
    extra = sorted(value.keys() - allowed)
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


def blueprint_version(blueprint: dict, errors: list[str]) -> int:
    """Decide the version from the version field ALONE, never from which fields are present.

    Inferring "no response_form, so probably v1" would silently downgrade a v2 record that simply
    forgot the field, and the v2 checks would then never run on it -- the fields would be pure
    added trust surface. An unrecognised value is an error rather than a fallback to v1 for the
    same reason.
    """
    if V_KEY not in blueprint:
        return 1
    declared = blueprint.get(V_KEY)
    if declared == BLUEPRINT_SCHEMA_VERSION:
        return 2
    errors.append(
        f"blueprint.{V_KEY} is {declared!r}; only {BLUEPRINT_SCHEMA_VERSION} is supported "
        "(omit the field entirely for a v1 record -- an unknown version is not read as v1)"
    )
    return 0


def item_forms_for(version: int) -> set[str]:
    """Legal `item_form` values for the version being READ.

    The whole point of keying this on version: v1 records legitimately carry `multiple_choice`, and
    reporting it makes every archived record look malformed, but admitting it unconditionally would
    let new generation write a layout the client removed. One function so the two answers cannot
    drift apart -- the coverage-key check and the per-item check previously disagreed, with the
    coverage side lenient and the item side strict, so a real archived record failed halfway through.

    Deliberately NOT extended to the homogeneity check below. Measured across every archived
    blueprint and captured batch reachable in this repo (3 captured blueprints + backend/docs/sample,
    9 MC points total): every single MC point has `form_group: null`, so no real record needs a mixed
    group to be tolerated. Widening that check too would weaken a v2 constraint to buy compatibility
    nothing asked for.
    """
    return V1_LEGACY_ITEM_FORMS if version == 1 else ITEM_FORMS


def answer_tokens(target: str) -> list[str]:
    """Split on whitespace only.

    Hyphens are deliberately NOT split: IELTS counts a hyphenated compound as one word for
    word_limit, and serving the word_limit decision is the whole purpose of `response_form`.
    `two-bedroom` is therefore one token -- a `word`, not a `phrase`.
    """
    return [token for token in re.split(r"\s+", target.strip()) if token]


def derive_response_form(target: str) -> str:
    """Persisted field, by TOKEN COUNT. Compared against the declaration; a mismatch is an error."""
    tokens = answer_tokens(target)
    if tokens and all(NUMERIC_TOKEN_RE.match(token) for token in tokens):
        return "numeric"
    return "word" if len(tokens) == 1 else "phrase"


def budget_of(target: str) -> tuple[int, int]:
    """(lexical word count, purely-numeric token count) for one answer.

    Third derivation off the same tokens, and separate from the other two for the same reason they
    are separate from each other: this one splits on what a *rubric* charges for. A purely numeric
    token is charged against the "AND/OR A NUMBER" allowance rather than the word count, and a
    hyphenated compound stays one word (AR-014), so `two-bedroom` costs one word and `9 and 1` costs
    one word plus two numbers.
    """
    tokens = answer_tokens(target)
    numeric = sum(1 for token in tokens if NUMERIC_TOKEN_RE.match(token))
    return (len(tokens) - numeric, numeric)


def widest_rubric() -> tuple[str, int, int]:
    """The loosest standard rubric, i.e. the admissibility bound for any single answer."""
    return WORD_LIMITS[-1]


def fits_any_rubric(target: str) -> bool:
    """Whether SOME standard rubric could carry this answer.

    Not "which rubric" -- that needs the group's other answers and belongs to the question stage.
    """
    lexical, numeric = budget_of(target)
    return any(lexical <= max_words and numeric <= allowance
               for _name, max_words, allowance in WORD_LIMITS)


def derive_qr027_class(target: str) -> str:
    """Internal quantity, by CHARACTER COMPOSITION. Feeds the QR-027 counts and is never persisted.

    Separate from `derive_response_form` on purpose: the two split on different axes, and merging
    them into one function returning a pair would teach the next reader that they are one thing.
    `Room 4B` is `phrase` here but `mixed` there.
    """
    tokens = answer_tokens(target)
    if tokens and all(NUMERIC_TOKEN_RE.match(token) for token in tokens):
        return "numeric"
    if any(re.search(r"\d", token) for token in tokens):
        return "mixed"
    return "lexical"


def window_of(number: int, first_end: int) -> int:
    """Recompute the narrator window from the parsed narration, not from the declared field."""
    return 1 if number <= first_end else 2


def validate_v2_item_fields(item: dict, label: str, first_end: int, errors: list[str]) -> None:
    """Recompute all three declared values and compare. Recomputation IS the value of these fields.

    Checking only that `narrator_window_id` is 1 or 2 would hand SC-019's window attribution back to
    the model's own say-so, which is the mistake §9.2 names explicitly. Every message below reports
    field, item number, declared value and computed value, so a failure says which rule caught it.
    """
    target = item.get("target")
    declared_form = item.get("response_form")
    if declared_form not in RESPONSE_FORMS:
        errors.append(f"{label}.response_form must be one of {sorted(RESPONSE_FORMS)}; found {declared_form!r}")
    elif isinstance(target, str) and target.strip():
        computed = derive_response_form(target)
        if declared_form != computed:
            errors.append(
                f"{label}.response_form declares {declared_form!r} but {target!r} derives {computed!r}"
            )

    # Answerability under some standard rubric. Caught here because the question stage cannot fix it:
    # it may not replace this target and may not edit the Script, so its only move is to cycle until
    # the clock runs out (see WORD_LIMITS for the measured case). The fix belongs to whoever chose the
    # information point -- here, while the material is still being written.
    if isinstance(target, str) and target.strip() and not fits_any_rubric(target):
        lexical, numeric = budget_of(target)
        widest, max_words, allowance = widest_rubric()
        errors.append(
            f"{label}.target {target!r} costs {lexical} word(s) and {numeric} number(s), which no "
            f"standard rubric permits -- the loosest is {widest!r} at {max_words} word(s) plus "
            f"{allowance} number(s). The question stage may not replace a target or edit the script, "
            "so this point has to be narrowed now: pick the part of it a candidate writes in the gap "
            "(one endpoint of a range, not the range) and leave the rest in the carrier"
        )

    category = item.get("answer_category")
    if category not in ANSWER_CATEGORIES:
        errors.append(
            f"{label}.answer_category {category!r} is not in the taxonomy {sorted(ANSWER_CATEGORIES)}; "
            "there is no catch-all value -- a point that fits none of these belongs back in the "
            "material stage"
        )

    declared_window = item.get("narrator_window_id")
    number = item.get("number")
    if declared_window not in {1, 2}:
        errors.append(f"{label}.narrator_window_id must be 1 or 2; found {declared_window!r}")
    elif isinstance(number, int):
        computed_window = window_of(number, first_end)
        if declared_window != computed_window:
            errors.append(
                f"{label}.narrator_window_id declares {declared_window!r} but item {number} falls in "
                f"window {computed_window} (narration splits at 1-{first_end}/{first_end + 1}-10)"
            )


def qr027_metrics(items: list[dict]) -> dict:
    """QR-027 counts. Metrics only in this stage -- the gate and its justification are stage 3."""
    classes = [derive_qr027_class(str(item.get("target", ""))) for item in items]
    categories: dict[str, int] = {}
    for item in items:
        category = item.get("answer_category")
        if isinstance(category, str):
            categories[category] = categories.get(category, 0) + 1
    numeric = sum(1 for cls in classes if cls == "numeric")
    spelled = sum(1 for cls in classes if cls in {"lexical", "mixed"})
    worst = max(categories.values(), default=0)
    return {
        "qr027_numeric_answers": numeric,
        "qr027_spelled_answers": spelled,
        "qr027_largest_category": worst,
        "qr027_category_counts": categories,
        "qr027_within_limits": (numeric <= QR027_MAX_NUMERIC
                                and spelled >= QR027_MIN_SPELLED
                                and worst < QR027_MAX_SAME_CATEGORY),
    }


def validate_group_relations(items: list[dict], groups: dict, first_end: int, errors: list[str]) -> None:
    """Validate v2 group continuity without treating narrator windows as page boundaries.

    Constraint 4 needs no turn-span threshold of its own: once item numbers are contiguous, the ten
    evidence turns are strictly increasing (already an error elsewhere), so "the group's points are
    not interleaved with another group's" is decidable from the ordered sequence alone. Narrator
    windows still constrain each item's evidence, but do not split a continuous printed layout.
    """
    for (form, group), numbers in sorted(groups.items(), key=lambda kv: str(kv[0])):
        present = sorted(n for n in numbers if isinstance(n, int))
        # Constraint 3.
        if present and present != list(range(present[0], present[0] + len(present))):
            errors.append(
                f"form_group {group!r} ({form}) covers non-contiguous item numbers {present}; "
                "one question's rows have to be consecutive items"
            )
    # Constraint 4: walk the items in evidence order and check each group occupies one unbroken run.
    ordered = [item for item in items if isinstance(item.get("turn_index"), int)]
    ordered.sort(key=lambda item: item["turn_index"])
    sequence = [item.get("form_group") for item in ordered
                if isinstance(item.get("form_group"), str) and item.get("form_group").strip()]
    runs: list[str] = []
    for label in sequence:
        if not runs or runs[-1] != label:
            runs.append(label)
    repeated = sorted({label for label in runs if runs.count(label) > 1})
    if repeated:
        errors.append(
            f"form_group(s) {repeated} are interrupted in the evidence sequence {runs}; a group's "
            "points must sit together in the dialogue, with no other group's point between them"
        )


def validate_grouping(items: list[dict], coverage: object, errors: list[str], warnings: list[str],
                      version: int = 1, first_end: int = 0, coverage_key: str = V1_COVERAGE_KEY) -> None:
    """Check the material can actually support a table or form layout.

    Ten scattered gap-fills pass every other check but leave item writers unable to
    build a table question, which is what the spec's 题型适配 requirement is about.

    v2 raises this from one threshold to five relational constraints (§5.5). Each has its own
    distinct message: a single error covering all five could not tell a reviewer which property
    the blueprint actually broke, and a test asserting only "returncode == 1" against it would be
    vacuous -- that is exactly how stage 1's grouping test managed to pass while testing nothing.
    """
    # Key groups by (item_form, form_group), not form_group alone. Counting a shared group label
    # says nothing about whether a table can be built from it: ten note points that happen to
    # share a label, or a "group" mixing form/table/note, would otherwise pass while leaving an
    # item writer unable to lay out a single table.
    groups: dict[tuple, list[int]] = {}
    labels: dict[str, set] = {}
    # `blueprint.items[N]` is a **0-based array index** everywhere else in this validator
    # (`validate_blueprint` builds `label = f"blueprint.items[{number - 1}]"`), and the reader parses
    # it that way: `validationNotes.ts` reads the bracket and renders `index + 1` as the item number.
    # These three messages used `item.get("number")`, which is 1-based, so every one of them sent the
    # reviewer's jump button one point too far -- and item 10 fell out of the pattern entirely, since
    # the parser only accepts 0-9. Enumerating rather than reading the field also keeps the label
    # right for an item whose own `number` is wrong or missing, which is a case these very errors
    # co-occur with.
    legal_forms = item_forms_for(version)
    for index, item in enumerate(items):
        form, group = item.get("item_form"), item.get("form_group")
        if form not in legal_forms:
            errors.append(f"blueprint.items[{index}].item_form must be one of {sorted(legal_forms)}")
        if isinstance(group, str) and group.strip():
            groups.setdefault((form, group), []).append(item.get("number"))
            labels.setdefault(group, set()).add(form)
        elif version == 2:
            # Constraint 1. v1 allowed null to mean "standalone gap-fill"; v2 requires every point
            # to belong to a group, so null is no longer a valid answer, only a missing one.
            errors.append(
                f"blueprint.items[{index}].form_group must be a non-empty string in v2; "
                f"found {group!r} (v2 requires every item to belong to a group)"
            )
        elif group is not None:
            errors.append(f"blueprint.items[{index}].form_group must be a non-empty string or null")

    # Constraint 2.
    for group, forms in labels.items():
        if len(forms) > 1:
            errors.append(
                f"form_group {group!r} mixes item_form values {sorted(map(str, forms))}; "
                "a group must be homogeneous to become one table or form question"
            )

    if version == 2:
        validate_group_relations(items, groups, first_end, errors)

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

    if not isinstance(coverage, dict) or not coverage:
        errors.append(f"blueprint.{coverage_key} must be a non-empty object")
        return
    declared: list[int] = []
    for form, numbers in coverage.items():
        # Same accessor as the per-item check above, deliberately. These two were written separately
        # and disagreed: the coverage side exempted v1's `multiple_choice` inline while the item side
        # was unconditionally strict, so a real archived record passed here and failed there.
        if form not in legal_forms:
            errors.append(f"{coverage_key} has unknown layout {form!r}")
        if not isinstance(numbers, list):
            errors.append(f"{coverage_key}[{form!r}] must be a list")
            continue
        declared.extend(n for n in numbers if isinstance(n, int) and not isinstance(n, bool))
        for number in numbers:
            match = next((i for i in items if i.get("number") == number), None)
            if match is None:
                errors.append(f"{coverage_key}[{form!r}] references unknown item {number}")
            elif match.get("item_form") != form:
                errors.append(
                    f"{coverage_key}[{form!r}] lists item {number} but its item_form is {match.get('item_form')!r}"
                )
    if sorted(declared) != list(range(1, 11)):
        errors.append(f"{coverage_key} must cover items 1-10 exactly once; flattened to {sorted(declared)}")


def validate_blueprint(blueprint: object, turns: list[dict], midpoint: int, first_end: int,
                       second_start: int, errors: list[str], warnings: list[str],
                       metrics: dict | None = None, allow_v1: bool = False) -> str:
    if not isinstance(blueprint, dict):
        errors.append("blueprint must be an object")
        return "full"
    version = blueprint_version(blueprint, errors)
    if version == 0:
        # Stop here. Every check below is version-conditional, so continuing would emit a page of
        # consequential errors -- ten "unexpected fields" lines for the v2 fields it cannot know are
        # legal -- and bury the one error that actually explains the failure.
        if metrics is not None:
            metrics["blueprint_schema_version"] = None
        return str(blueprint.get("narration_mode") or "full")
    if version == 1 and not allow_v1:
        errors.append(
            f"blueprint.{V_KEY} is missing; new generation must write "
            f"{BLUEPRINT_SCHEMA_VERSION} (pass --allow-v1 to read an archived record)"
        )
    if metrics is not None:
        metrics["blueprint_schema_version"] = version or None
    coverage_key = V2_COVERAGE_KEY if version == 2 else V1_COVERAGE_KEY
    item_keys = ITEM_KEYS | V2_ITEM_KEYS if version == 2 else ITEM_KEYS
    exact_keys(blueprint, BLUEPRINT_KEYS | {coverage_key}, "blueprint", errors,
               BLUEPRINT_OPTIONAL_KEYS - {coverage_key})
    if version == 2 and V1_COVERAGE_KEY in blueprint:
        errors.append(
            f"blueprint must not carry both coverage names; v2 writes {V2_COVERAGE_KEY} only "
            f"(found {V1_COVERAGE_KEY} as well, which leaves readers no way to know which to trust)"
        )
    mode = blueprint.get("narration_mode")
    if mode not in {"full", "short"}:
        errors.append("blueprint.narration_mode must be full or short")
        mode = "full"
    split_after = blueprint.get("split_after")
    # The bound is 3-7, identical to the narration rule below, and that is the point: this check
    # used to accept only {5, 6} while the narration check accepted 3-7, so a material with a
    # perfectly legal 1-4/5-10 split -- the single commonest split in the archive, 9 of 27 papers --
    # passed the narration rule and was then failed here. The generator could not satisfy both, and
    # the error message named a constraint the sibling rule had already stopped enforcing.
    # What is still enforced is the part that matters: the blueprint's split must be the SAME split
    # the narration announced, so items 1-N are the ones the candidate is told to answer first.
    if not 3 <= (split_after if isinstance(split_after, int) else 0) <= 7 \
            or split_after != first_end or second_start != first_end + 1:
        errors.append(
            "blueprint.split_after must equal the narration's own split point (3-7, contiguous); "
            "narration says 1-{0}/{1}-10 and the blueprint says {2!r}".format(
                first_end, second_start, split_after)
        )
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
        exact_keys(item, item_keys, label, errors)
        if version == 2:
            validate_v2_item_fields(item, label, first_end, errors)
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
        validate_grouping(checked, blueprint.get(coverage_key), errors, warnings,
                          version=version, first_end=first_end, coverage_key=coverage_key)
        if version == 2 and metrics is not None:
            # QR-027 counts are reported, not enforced: the gate and its recorded justification are
            # stage 3's aggregator. Emitting them now means stage 3 inherits measured numbers.
            metrics.update(qr027_metrics(checked))
    correction = blueprint.get("correction")
    if not isinstance(correction, dict):
        errors.append("blueprint.correction must be an object")
    else:
        exact_keys(correction, {"earlier", "final", "marker"}, "blueprint.correction", errors)
        earlier, final, marker = (find_occurrence(turns, correction.get(key)) for key in ("earlier", "final", "marker"))
        if min(earlier[0], final[0], marker[0]) < 0 or not earlier < marker < final:
            errors.append("correction must contain earlier value, then replacement marker, then final value")
    indirect = blueprint.get("indirect_confirmation")
    # Optional, because the spec asks for 2-3 distraction cycles drawn from five mechanisms
    # (§4B-4) and never singles this one out. Measured over the 27 usable archived papers:
    # 先说后改 appears in 24 and a qualifier in 21, but an indirect reference in only 4.
    # Requiring it made the generator chase a convention the real papers rarely use, and it was
    # the single error that exhausted all three attempts on a live batch.
    if indirect is None:
        pass
    elif not isinstance(indirect, dict):
        errors.append("blueprint.indirect_confirmation must be an object when present")
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
    # Default strict, because the write side must only ever produce v2. Reading an archived record
    # is the exception and has to be asked for, so a generation run cannot quietly emit a
    # version-less blueprint and still pass.
    parser.add_argument("--allow-v1", action="store_true",
                        help="accept an archived blueprint with no blueprint_schema_version")
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
        # Read directly rather than waiting for validate_blueprint's return: two checks below are
        # mode-dependent, and an invalid mode is reported there on its own.
        narration_mode = (blueprint or {}).get("narration_mode") if isinstance(blueprint, dict) else None
        first, second = FIRST_RANGE_RE.search(opening), SECOND_RANGE_RE.search(midpoint_text)
        first_end, second_start = (int(first.group(1)), int(second.group(1))) if first and second else (0, 0)
        # The spec writes "如 1–5 / 6–10 或 1–6 / 7–10" -- 如 means "such as", an illustration
        # rather than an enumeration. Measured over the 27 usable archived papers, the real split
        # points are (4,5)x9, (6,7)x8, (5,6)x6, (3,4)x2, (7,8)x1: restricting first_end to {5,6}
        # passed only 14/27, while accepting a contiguous 3-7 passes 26/27. What actually matters
        # is that the two ranges are contiguous and neither group is degenerate, so that is what
        # is checked.
        if not 3 <= first_end <= 7 or second_start != first_end + 1:
            errors.append(
                "narration must split questions 1-10 into two contiguous groups with the first "
                "ending between 3 and 7; found 1-{0}/{1}-10".format(first_end, second_start)
            )
        # "once only" belongs to the whole-test preamble, which the spec says appears once per
        # paper (§4A). Measured: 7/7 full-mode openings contain it and 0/20 short-mode ones do --
        # a short opening starts at "Part one, you will hear...". Demanding it in short mode was
        # unsatisfiable, and the generator spent every retry on it.
        if narration_mode == "full" and "once only" not in opening.casefold():
            errors.append("full opening must include once only")
        if not re.search(r"end of (?:section one|part 1|part one)", closing, re.I) or not CHECKING_RE.search(closing):
            errors.append("closing must identify Part 1 end and checking time")
        # A pointer to Part 2 is optional, and was wrong as a hard rule. Measured over the 30
        # closings in the corpus: of the 12 that use the "part one" naming, ZERO mention part 2,
        # and 6 of the 18 "section one" ones do not either. Requiring it rejected half of the real
        # papers, and the generator exhausted its retries trying to satisfy a rule the source
        # material does not follow.
        if not NEXT_PART_RE.search(closing):
            warnings.append("closing does not direct candidates to Part/Section 2 (optional: "
                            "12/30 real closings omit it)")
        mode = validate_blueprint(blueprint, turns, narrator[1], first_end, second_start, errors,
                                  warnings, metrics, allow_v1=args.allow_v1)
        narration = " ".join(turns[i]["text"] for i in narrator)
        if mode == "full":
            for label, pattern in (("the set of recordings", RECORDINGS_RE), ("the test's parts", PARTS_RE)):
                if not pattern.search(opening):
                    errors.append(f"full opening must introduce {label}")
            if not 160 <= words(narration) <= 230:
                # Same reasoning as the dialogue count: the retry needs the direction and the gap.
                # A full narration falls short when the whole-test preamble is paraphrased instead
                # of quoted, so the remedy is named rather than left to be guessed.
                errors.append(
                    "full narration must be 160-230 words; found {0}. {1}".format(
                        words(narration),
                        "Quote the whole-test preamble in full rather than summarising it."
                        if words(narration) < 160 else "Trim the narration; it carries no answers.",
                    )
                )
        # 70-115, not 70-110. Measured over the 20 short-mode archived papers the counts run
        # 75..111, so the old ceiling rejected the longest real one by a single word -- a band
        # derived from the corpus has to contain the corpus.
        elif not 70 <= words(narration) <= 115:
            errors.append(
                "short narration must be 70-115 words; found {0} ({1} by {2})".format(
                    words(narration),
                    "short" if words(narration) < 70 else "long",
                    70 - words(narration) if words(narration) < 70 else words(narration) - 115,
                )
            )
        dialogue_turns = [turn for turn in turns if turn.get("speaker") != "speaker1"]
        dialogue = " ".join(turn["text"] for turn in dialogue_turns)
        total_words, total_turns = total_words + words(dialogue), total_turns + len(dialogue_turns)
        if not 450 <= words(dialogue) <= 750:
            # The gap is spelled out because this message is fed back to the generator on retry.
            # "outside 450-750: 401" told it only that it failed; a draft 200 words short of the
            # 600-650 target came back 401, 430, 419 across three attempts. Naming the shortfall
            # and the target turns a re-roll into a correction.
            count = words(dialogue)
            short_by = 625 - count
            errors.append(
                "dialogue words outside 450-750: {0} ({1} the 600-650 target by {2} words)".format(
                    count, "under" if short_by > 0 else "over", abs(short_by)
                )
            )
        elif not 600 <= words(dialogue) <= 650:
            warnings.append(f"dialogue words outside preferred 600-650: {words(dialogue)}")
        if not 20 <= len(dialogue_turns) <= 48:
            errors.append(f"dialogue turns outside 20-48: {len(dialogue_turns)}")
        elif not 30 <= len(dialogue_turns) <= 40:
            warnings.append(f"dialogue turns outside preferred 30-40: {len(dialogue_turns)}")
        before = sum(i < narrator[1] for i, turn in enumerate(turns) if turn.get("speaker") != "speaker1")
        after = len(dialogue_turns) - before
        # Floor of 7, not 8. Measured: snap_042 runs 18/7 and was rejected for its 7-turn second
        # half, yet it carries four questions there -- 1.75 turns per question, the loosest ratio in
        # the archive and still workable. 8 was one turn above the observed minimum, so it rejected
        # a real paper for a number the corpus does not support. What the floor is actually for is
        # unchanged: a half with almost no dialogue cannot carry its share of the ten points.
        if before < 7 or after < 7:
            errors.append(f"each half needs 7 turns; found {before}/{after}")
        # Spec 4A also asks for 前后均衡. Advisory rather than an error: the spec sets no ratio,
        # and a 20/14 split is still usable, so this informs the revise step without blocking.
        elif min(before, after) / max(before, after) < 0.6:
            warnings.append(f"dialogue halves are uneven: {before}/{after}")
        # A letter-by-letter spelling sequence is what the spec's §3 checklist asks for, and it is
        # advisory here because the real papers do not deliver it. Measured over the 27 usable
        # archived papers: only 13 contain one, so demanding it rejected 14 genuine exam papers --
        # the single largest source of rejection of any rule in this file, and by the client's rule
        # ("真题能过的，校验就应该过") that makes it wrong as an error.
        #
        # What is NOT relaxed, and is where the requirement actually lives: `validate_blueprint`
        # still fails a blueprint with no `name`-typed item, and still fails one where no
        # name-typed item is `confirmed`. That is the property a 填空题 needs -- the answer word is
        # spoken and then reinforced -- and the real papers DO satisfy it, typically by repeating
        # or confirming the name rather than by reciting its letters.
        if not SPELLING_RE.search(dialogue):
            warnings.append(
                "no letter-by-letter spelling sequence detected (optional: 14/27 real papers "
                "carry none; a confirmed name-typed item is required instead)"
            )
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
