#!/usr/bin/env python3
"""Validate a Part 1 question package against its material and its blueprint.

Deterministic checks only. Everything a rule reduces to counting, matching or recomputing lives
here; naturalness, answer uniqueness, register and proposition-level judgement belong to the
question audit agent, which never sees this file's inputs beyond the question face.

Two design points worth stating, because both were arrived at rather than assumed:

**The helpers come from `validate_part1.py` by import, not by copy.** `NUMERIC_TOKEN_RE`,
`answer_tokens`, `derive_response_form`, `derive_qr027_class`, `anchor_ok`, the QR-027 thresholds and
the narrator-range regexes all already exist there and already decide the same questions. A second
copy would be a second source of truth for "is this token numeric" and for "how many numeric answers
are too many" -- and the two would drift silently, with this file passing a package the material
stage would have failed. The sibling skill's `scripts/` directory is on `sys.path` for that reason;
both skills live in the same pool, so nothing crosses a boundary.

**Every declared value that can be recomputed is recomputed and compared, never read back.**
`response_form`, `numeral_allowance`, `blank_position`, `narrator_window_id` and each group's
`word_limit` are all derivable from the package's own content. Checking only that they are
well-formed strings would hand the rules they encode back to the model's own say-so, which is the
exact failure the material-side v2 fields were built to prevent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SIBLING = Path(__file__).resolve().parents[2] / "generate-listening-part1" / "scripts"
if str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))

import validate_part1 as material_validator  # noqa: E402  (path set up immediately above)

from validate_part1 import (  # noqa: E402
    FIRST_RANGE_RE,
    NUMERIC_TOKEN_RE,
    SECOND_RANGE_RE,
    anchor_ok,
    answer_tokens,
    derive_qr027_class,
    derive_response_form,
    exact_keys,
    read_json,
    report,
    window_of,
)

PACKAGE_KEYS = {"reference", "test_package", "material_id", "question_face", "answer_key", "evidence"}
FACE_KEYS = {"instructions", "groups", "questions"}
ITEM_COUNT = 10
LAYOUTS = {"form", "note", "table"}

# The closed set of standard rubrics, ordered from strictest to loosest. Each maps to
# (max lexical words, permitted purely-numeric tokens). Decision §6.4 #3: there is no global default
# limit -- the validator derives the strictest entry every canonical in a group satisfies and reports
# a group that declared a looser one, because a loose rubric silently accepts answers the key marks
# wrong.
WORD_LIMITS = (
    ("ONE WORD ONLY", 1, 0),
    ("ONE WORD AND/OR A NUMBER", 1, 1),
    ("NO MORE THAN TWO WORDS", 2, 0),
    ("NO MORE THAN TWO WORDS AND/OR A NUMBER", 2, 1),
    ("NO MORE THAN THREE WORDS", 3, 0),
    ("NO MORE THAN THREE WORDS AND/OR A NUMBER", 3, 1),
)
LIMIT_INDEX = {name: position for position, (name, _w, _n) in enumerate(WORD_LIMITS)}
LIMIT_BUDGET = {name: (max_words, allowance) for name, max_words, allowance in WORD_LIMITS}
NUMBER_PHRASE = "AND/OR A NUMBER"

# QR-026 caps the number of blanks that may sit at the end of their line.
MAX_FINAL_BLANKS = 7

# QR-025's 实义词 criterion, which QR-026 borrows for Part 1: position is decided by content words
# around the blank, not by character offset. Function words are listed rather than derived because
# there is no way to derive them, and the list only has to be good enough to answer "is there
# anything of substance on this side of the blank".
FUNCTION_WORDS = {
    "a", "an", "the", "of", "for", "in", "on", "at", "to", "and", "or", "but", "if", "is", "are",
    "was", "were", "be", "been", "being", "am", "with", "by", "from", "as", "that", "this", "these",
    "those", "it", "its", "your", "my", "his", "her", "their", "our", "you", "i", "he", "she",
    "they", "we", "will", "would", "shall", "should", "can", "could", "may", "might", "must", "do",
    "does", "did", "not", "no", "per", "up", "out", "than", "then", "there", "here", "about",
    "into", "any", "all", "each", "more", "most", "so", "very", "just", "also", "s",
}
TOKEN_RE = re.compile(r"[\w'’-]+")
PLACEHOLDER_RE = re.compile(r"^(?:tbd|todo|n/?a|xxx+|\?+|-+|\.+)$", re.I)

# Maximal digit runs, so a blank's printed number is compared as a whole numeral. A substring test
# would let `10 ....` satisfy Q1: with ten items, "1" is a substring of "10" and the two items whose
# numbers overlap are exactly the pair a mislabelled form line confuses.
DIGIT_RUN_RE = re.compile(r"\d+")


def norm(value: object) -> str:
    return str(value or "").strip()


def ambiguous_anchor(turns: list, index: object, phrase: str) -> bool:
    """Does ``phrase`` occur in a turn ADJACENT to ``index`` as well as in ``index`` itself?

    Only meaningful once :func:`anchor_ok` has passed, and only checked against the two neighbours
    rather than the whole script: a span repeated in a distant turn is ordinary English, and the anchor
    is unambiguous because nobody resolves an anchor across the whole script. The +-1 neighbourhood is
    different, because that is exactly the width the question-stage cross-check searches when it
    reconciles the writer's anchor with the auditor's -- so a span occurring twice inside that window
    leaves the reconciliation with two candidates and no way to choose.

    ``anchor_ok``'s speaker rule is deliberately reused rather than reimplemented: a neighbouring
    *narration* turn containing the same words is not a rival anchor, because narration can never be
    anchored on in the first place.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return False
    return any(anchor_ok(turns, index + offset, phrase) for offset in (-1, 1))


def tokens_of(text: str) -> list:
    """Whole orthographic tokens, casefolded. Hyphens are kept inside a token (AR-014)."""
    return [match.group(0).casefold().strip("'’-") for match in TOKEN_RE.finditer(text or "")
            if match.group(0).strip("'’-")]


def content_words(text: str) -> list:
    return [token for token in tokens_of(text) if token not in FUNCTION_WORDS]


def classify_blank(before: str, after: str) -> str:
    """QR-026 position class. The two branches are disjoint, so their order carries no meaning.

    `initial` demands content AFTER the blank and `final` demands none, so a form line such as
    "Name: ____" is final -- one content word before, nothing after -- which is the honest reading:
    it is exactly the systematic end-of-line blanking the rule caps at 7 of 10.
    """
    before_content, after_content = content_words(before), content_words(after)
    if not after_content and norm(before):
        return "final"
    if len(before_content) <= 1 and norm(after):
        return "initial"
    return "medial"


def budget_of(canonical: str) -> tuple:
    """(lexical word count, purely-numeric token count) for one canonical.

    Whitespace splits tokens and a hyphenated compound stays one word (AR-014), so `two-bedroom`
    costs one word rather than two. A purely numeric token is charged against the rubric's number
    allowance instead of its word count, which is what "AND/OR A NUMBER" means.
    """
    tokens = answer_tokens(canonical)
    numeric = sum(1 for token in tokens if NUMERIC_TOKEN_RE.match(token))
    return (len(tokens) - numeric, numeric)


def strictest_limit(canonicals: list) -> str:
    """The strictest standard rubric every canonical in a group satisfies, or "" if none does."""
    for name, max_words, allowance in WORD_LIMITS:
        if all(lexical <= max_words and numeric <= allowance
               for lexical, numeric in (budget_of(value) for value in canonicals)):
            return name
    return ""


def inflections(token: str) -> set:
    """The simple inflections QR-040's leakage audit names, plus the bare stem of a plural."""
    forms = {token, token + "s", token + "es", token + "ing", token + "ed"}
    if token.endswith("s") and len(token) > 3:
        forms.add(token[:-1])
    return forms


def narrator_windows(turns: list, errors: list) -> tuple:
    """(first_end, window turn ranges) parsed from the narration, never from a declared field.

    Returns first_end == 0 when the narration cannot be parsed, in which case every window check is
    skipped rather than run against a guess -- reporting ten window failures for one unparseable
    narration would bury the actual cause.
    """
    narrator = [index for index, turn in enumerate(turns)
                if isinstance(turn, dict) and turn.get("speaker") == "speaker1"]
    if len(narrator) != 3:
        errors.append("material must carry exactly three narrator turns to resolve question "
                      "windows; found %d" % len(narrator))
        return (0, {})
    first = FIRST_RANGE_RE.search(str(turns[narrator[0]].get("text", "")))
    second = SECOND_RANGE_RE.search(str(turns[narrator[1]].get("text", "")))
    if not first or not second or int(first.group(1)) + 1 != int(second.group(1)):
        errors.append("narration does not state two contiguous question ranges, so no group's "
                      "window membership can be checked")
        return (0, {})
    return (int(first.group(1)), {1: (narrator[0], narrator[1]), 2: (narrator[1], narrator[2])})


def material_turns(data: object, errors: list) -> tuple:
    """(turns, test_package). Reports rather than raises on a malformed material."""
    if not isinstance(data, dict):
        errors.append("material JSON must be an object")
        return ([], "")
    parts = data.get("listening_material_parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        errors.append("material must carry exactly one listening_material_parts entry")
        return ([], norm(data.get("test_package")))
    script = parts[0].get("script")
    turns = script.get("turns") if isinstance(script, dict) else None
    if not isinstance(turns, list) or not turns \
            or any(not isinstance(turn, dict) or not isinstance(turn.get("text"), str)
                   for turn in turns):
        errors.append("material script turns are missing or malformed")
        return ([], norm(data.get("test_package")))
    return (turns, norm(data.get("test_package")))


def blueprint_items(blueprint: object, errors: list) -> dict:
    """The ten planned points by number. They are given input: this file never re-plans them."""
    items = blueprint.get("items") if isinstance(blueprint, dict) else None
    if not isinstance(items, list) or len(items) != ITEM_COUNT:
        errors.append("blueprint must carry exactly %d items; the question set is written for a "
                      "specific ten" % ITEM_COUNT)
        return {}
    by_number = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("number"), int):
            by_number[item["number"]] = item
    if sorted(by_number) != list(range(1, ITEM_COUNT + 1)):
        errors.append("blueprint items must be numbered 1-%d exactly once each; found %s"
                      % (ITEM_COUNT, sorted(by_number)))
        return {}
    return by_number


def validate_shape(package: object, material_package: str, errors: list) -> tuple:
    """Top level and the three blocks' index sets. Returns ({}, {}, {}, {}, {}) when unusable."""
    empty = ({}, {}, {}, {}, {})
    if not isinstance(package, dict):
        errors.append("question package JSON must be an object")
        return empty
    exact_keys(package, PACKAGE_KEYS, "question package", errors)
    if package.get("reference") != "Part 1":
        errors.append("question package reference must be 'Part 1'")
    if material_package and norm(package.get("test_package")) != material_package:
        errors.append("question package test_package %r does not match the material's %r; a "
                      "question set belongs to one material"
                      % (norm(package.get("test_package")), material_package))
    if not norm(package.get("material_id")):
        errors.append("material_id must be non-empty; it is the only record of which recording "
                      "these questions were written for")
    face = package.get("question_face")
    if not isinstance(face, dict):
        errors.append("question_face must be an object")
        return empty
    exact_keys(face, FACE_KEYS, "question_face", errors)

    def indexed(rows: object, label: str) -> dict:
        if not isinstance(rows, list):
            errors.append("%s must be an array" % label)
            return {}
        out = {}
        for position, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append("%s[%d] must be an object" % (label, position))
                continue
            number = row.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                errors.append("%s[%d] has no integer number" % (label, position))
                continue
            if number in out:
                errors.append("%s carries number %d twice" % (label, number))
                continue
            out[number] = row
        return out

    questions = indexed(face.get("questions"), "question_face.questions")
    answers = indexed(package.get("answer_key"), "answer_key")
    evidence = indexed(package.get("evidence"), "evidence")
    groups, instructions = {}, {}
    for label, rows, store in (("groups", face.get("groups"), groups),
                               ("instructions", face.get("instructions"), instructions)):
        if not isinstance(rows, list):
            errors.append("question_face.%s must be an array" % label)
            continue
        for position, row in enumerate(rows):
            if not isinstance(row, dict) or not norm(row.get("group_id")):
                errors.append("question_face.%s[%d] has no group_id" % (label, position))
                continue
            key = norm(row["group_id"])
            if key in store:
                errors.append("question_face.%s declares group %r twice" % (label, key))
                continue
            store[key] = row
    return (questions, answers, evidence, groups, instructions)


def validate_mapping(questions: dict, answers: dict, evidence: dict, errors: list) -> list:
    """The three blocks must describe the same ten items. An orphan in any of them is an error.

    AL-001 / AL-010: a question with no key is unmarkable, a key with no question marks nothing, and
    an evidence entry with no question is a claim about an item that does not exist -- all three are
    silent, because each block is read by a different consumer.
    """
    expected = list(range(1, ITEM_COUNT + 1))
    for label, block in (("question_face.questions", questions), ("answer_key", answers),
                         ("evidence", evidence)):
        if sorted(block) != expected:
            missing = sorted(set(expected) - set(block))
            extra = sorted(set(block) - set(expected))
            errors.append("%s must cover questions 1-%d exactly once; missing %s, unexpected %s"
                          % (label, ITEM_COUNT, missing, extra))
    return sorted(set(questions) & set(answers) & set(evidence))


def validate_blueprint_fidelity(numbers: list, questions: dict, answers: dict, items: dict,
                                errors: list) -> None:
    """§1.4: all ten planned points, in the planned numbering, unchanged.

    Compared field by field rather than as a whole, because the three ways this goes wrong are
    different bugs: a swapped answer means a point was replaced, a moved number means the set was
    reordered, and a changed category moves the answer-variety counts away from the set the
    feasibility preflight actually approved.
    """
    for number in numbers:
        item = items.get(number)
        if not isinstance(item, dict):
            continue
        target, canonical = norm(item.get("target")), norm(answers[number].get("canonical"))
        if target.casefold() != canonical.casefold():
            errors.append("Q%d canonical %r is not blueprint item %d's target %r; the ten "
                          "information points are given input and may not be replaced"
                          % (number, canonical, number, target))
        declared = norm(questions[number].get("answer_category"))
        planned = norm(item.get("answer_category"))
        if planned and declared != planned:
            errors.append("Q%d answer_category %r does not match blueprint item %d's %r; "
                          "relabelling a point here moves the QR-027 counts away from the set the "
                          "feasibility preflight approved" % (number, declared, number, planned))


def validate_groups(numbers: list, questions: dict, groups: dict, instructions: dict,
                    evidence: dict, first_end: int, errors: list) -> dict:
    """§5.5's five group constraints, minus homogeneity -- which the schema makes structural.

    `layout` is declared once, on the group, so "every item in a group shares a layout" cannot be
    false. What remains is what JSON Schema cannot express: membership, contiguous numbering,
    contiguity in the ordered evidence sequence, and containment in one narrator window.
    """
    members: dict = {}
    for number in numbers:
        key = norm(questions[number].get("group_id"))
        if key not in groups:
            errors.append("Q%d belongs to group %r, which is not declared in "
                          "question_face.groups; every item must belong to a group" % (number, key))
            continue
        members.setdefault(key, []).append(number)

    for key in sorted(set(groups) - set(members)):
        errors.append("group %r holds no questions; an empty group prints a heading with nothing "
                      "under it" % key)
    for key in sorted(set(instructions) - set(groups)):
        errors.append("instruction declared for group %r, which is not a declared group" % key)
    for key in sorted(set(groups) - set(instructions)):
        errors.append("group %r has no instruction; a group without a rubric does not tell the "
                      "candidate what to write" % key)

    for key in sorted(members):
        present = sorted(members[key])
        group = groups[key]
        layout = group.get("layout")
        if layout not in LAYOUTS:
            errors.append("group %r declares layout %r; Part 1 delivers form, note or table "
                          "completion only" % (key, layout))
        # Constraint 3.
        if present != list(range(present[0], present[0] + len(present))):
            errors.append("group %r covers non-contiguous question numbers %s; one printed "
                          "form/note/table has to hold consecutive items" % (key, present))
        # Constraint 5, plus the declaration it is compared against.
        if first_end:
            computed = sorted({window_of(number, first_end) for number in present})
            if len(computed) > 1:
                errors.append("group %r spans narrator windows %s (questions %s, narration splits "
                              "at 1-%d/%d-10); a group cannot straddle the point where candidates "
                              "are told to move on" % (key, computed, present, first_end,
                                                       first_end + 1))
            elif group.get("narrator_window_id") != computed[0]:
                errors.append("group %r declares narrator_window_id %r but its questions %s fall "
                              "in window %d" % (key, group.get("narrator_window_id"), present,
                                                computed[0]))
        # The rubric's printed range must be the group's actual numbering.
        instruction = instructions.get(key)
        if isinstance(instruction, dict):
            expected = ("%d-%d" % (present[0], present[-1])) if len(present) > 1 else str(present[0])
            if norm(instruction.get("question_range")) != expected:
                errors.append("group %r declares question_range %r but covers %s; the printed "
                              "range is what the candidate reads"
                              % (key, norm(instruction.get("question_range")), expected))
        validate_layout_structure(key, group, errors)

    # Constraint 4: walk the items in evidence order; each group must occupy one unbroken run.
    ordered = sorted((number for number in numbers
                      if isinstance(evidence[number].get("turn_index"), int)
                      and not isinstance(evidence[number].get("turn_index"), bool)),
                     key=lambda number: evidence[number]["turn_index"])
    runs: list = []
    for number in ordered:
        key = norm(questions[number].get("group_id"))
        if not runs or runs[-1] != key:
            runs.append(key)
    interrupted = sorted({key for key in runs if runs.count(key) > 1})
    if interrupted:
        errors.append("group(s) %s are interrupted in the evidence sequence %s; a group's points "
                      "must sit together in the dialogue, with no other group's point between them"
                      % (interrupted, runs))
    return members


def validate_layout_structure(key: str, group: dict, errors: list) -> None:
    """QR-015 content accessibility, and QR-031 for note groups.

    Content only. Border style, font and pagination are visual polish and are deliberately not
    checked: they can be fixed after content review, whereas an unlabelled table column makes the
    item unanswerable.
    """
    layout = group.get("layout")
    structure = group.get("structure") if isinstance(group.get("structure"), dict) else {}
    rows = structure.get("row_labels")
    columns = structure.get("column_labels")
    hierarchy = structure.get("hierarchy")
    if layout == "note":
        if not norm(group.get("title")):
            errors.append("note group %r has no title; QR-031 requires a short, specific, "
                          "non-leaking scenario heading" % key)
        if not isinstance(hierarchy, list) or not hierarchy:
            errors.append("note group %r declares no structure.hierarchy; a note without headings "
                          "is a list of sentences, not a record structure" % key)
    elif layout == "form":
        if not isinstance(rows, list) or not rows:
            errors.append("form group %r declares no structure.row_labels; a form's labels are "
                          "what tell the candidate which detail goes where" % key)
    elif layout == "table":
        missing = [name for name, value in (("row_labels", rows), ("column_labels", columns))
                   if not isinstance(value, list) or not value]
        if missing:
            errors.append("table group %r declares no structure.%s; an unlabelled axis makes the "
                          "cell unanswerable" % (key, " and no structure.".join(missing)))


def validate_questions(numbers: list, questions: dict, answers: dict, errors: list,
                       warnings: list) -> dict:
    """Per-question face checks: the blank, its recomputed position, and the QR-026 distribution."""
    positions: dict = {}
    for number in numbers:
        question = questions[number]
        before, after = str(question.get("carrier_before") or ""), str(question.get("carrier_after") or "")
        blank = norm(question.get("blank"))
        if str(number) not in DIGIT_RUN_RE.findall(blank):
            errors.append("Q%d's blank %r does not carry its own question number as a whole "
                          "numeral; the number is how an answer sheet is matched to an item"
                          % (number, blank))
        if not norm(before) and not norm(after):
            errors.append("Q%d has no carrier text on either side of the blank; an isolated blank "
                          "is answerable only by guessing" % number)
        computed = classify_blank(before, after)
        positions[number] = computed
        if norm(question.get("blank_position")) != computed:
            errors.append("Q%d declares blank_position %r but its carriers make it %r (classified "
                          "by content words either side of the blank, QR-025/QR-026)"
                          % (number, norm(question.get("blank_position")), computed))
        canonical = norm(answers[number].get("canonical"))
        derived = derive_response_form(canonical) if canonical else ""
        if canonical and norm(question.get("response_form")) != derived:
            errors.append("Q%d declares response_form %r but canonical %r tokenises as %r"
                          % (number, norm(question.get("response_form")), canonical, derived))
    counts = {name: sum(1 for value in positions.values() if value == name)
              for name in ("initial", "medial", "final")}
    if positions:
        absent = sorted(name for name, count in counts.items() if not count)
        if absent:
            errors.append("blank positions do not cover %s; QR-026 asks for initial, medial and "
                          "final blanks across the ten items (found %s)" % (absent, counts))
        if counts["final"] > MAX_FINAL_BLANKS:
            errors.append("%d of %d blanks sit at the end of their line; QR-026 caps end-of-line "
                          "blanking at %d" % (counts["final"], len(positions), MAX_FINAL_BLANKS))
        elif counts["final"] == MAX_FINAL_BLANKS:
            warnings.append("end-of-line blanks are at the QR-026 ceiling (%d of %d); one more "
                            "would fail" % (counts["final"], len(positions)))
    return counts


def validate_answers(numbers: list, questions: dict, answers: dict, groups: dict,
                     instructions: dict, members: dict, errors: list) -> None:
    """AR-002 / AR-013 / QR-017, and the per-group rubric choice (§6.4 #3)."""
    for number in numbers:
        answer = answers[number]
        canonical = norm(answer.get("canonical"))
        if not canonical or PLACEHOLDER_RE.match(canonical):
            errors.append("Q%d canonical is empty or a placeholder (%r); an unfinished key marks "
                          "every candidate wrong" % (number, canonical))
        if not norm(answer.get("counting_rule")):
            errors.append("Q%d states no counting_rule; QR-017 requires the word-count basis used "
                          "to be recorded" % number)
        for alternative in (answer.get("alternatives") or []):
            if not norm(alternative):
                errors.append("Q%d carries an empty accepted alternative" % number)

    for key in sorted(members):
        instruction = instructions.get(key)
        if not isinstance(instruction, dict):
            continue
        declared = norm(instruction.get("word_limit"))
        text = str(instruction.get("instruction_text") or "").upper()
        if declared not in LIMIT_INDEX:
            errors.append("group %r declares word_limit %r, which is not one of the standard "
                          "rubrics %s" % (key, declared, [name for name, _w, _n in WORD_LIMITS]))
            continue
        max_words, allowance = LIMIT_BUDGET[declared]
        if instruction.get("numeral_allowance") != allowance:
            errors.append("group %r declares numeral_allowance %r but its rubric %r permits %d"
                          % (key, instruction.get("numeral_allowance"), declared, allowance))
        if declared not in text:
            errors.append("group %r's instruction_text does not contain its word_limit %r "
                          "verbatim; the printed rubric is the one the candidate obeys (LG-006)"
                          % (key, declared))
        elif not allowance and NUMBER_PHRASE in text:
            errors.append("group %r's instruction_text permits a number while its word_limit %r "
                          "does not" % (key, declared))

        canonicals = [norm(answers[number].get("canonical")) for number in members[key]
                      if number in answers]
        for number in members[key]:
            if number not in answers:
                continue
            canonical = norm(answers[number].get("canonical"))
            if not canonical:
                continue
            lexical, numeric = budget_of(canonical)
            if lexical > max_words or numeric > allowance:
                errors.append("Q%d canonical %r is %d word(s) and %d number(s), which its group's "
                              "rubric %r does not permit (AR-002/QR-017)"
                              % (number, canonical, lexical, numeric, declared))
            entry_limit = norm(answers[number].get("word_limit"))
            if entry_limit != declared:
                errors.append("Q%d's answer_key word_limit %r differs from its group's printed %r; "
                              "the paper and the marking key would be different tests"
                              % (number, entry_limit, declared))
            if answers[number].get("numeral_allowance") != allowance:
                errors.append("Q%d's answer_key numeral_allowance %r differs from its group's %d"
                              % (number, answers[number].get("numeral_allowance"), allowance))
        strictest = strictest_limit([value for value in canonicals if value])
        if not strictest:
            errors.append("group %r's answers fit no standard rubric; at least one canonical is "
                          "longer than three words plus a number" % key)
        elif LIMIT_INDEX[declared] > LIMIT_INDEX[strictest]:
            errors.append("group %r declares %r but every one of its answers satisfies the "
                          "stricter %r; a looser rubric accepts answers the key marks wrong "
                          "(§6.4 #3: no default limit, pick the strictest that fits)"
                          % (key, declared, strictest))


def validate_evidence(numbers: list, questions: dict, answers: dict, evidence: dict, items: dict,
                      turns: list, first_end: int, ranges: dict, errors: list) -> None:
    """AL-007 / AL-003 / AL-017 / AL-018, all strict: none of these dimensions gets a tolerance.

    `turn_index` may legitimately differ from the blueprint item's anchor -- the decisive evidence is
    often the confirmation turn rather than the first mention, which is standard Part 1 writing -- so
    it is not compared against the plan. What is checked is that the quote really is in the turn
    named, that the ten turns advance with the question numbers, and that the turn is inside the
    item's own narrator window.
    """
    previous = -1
    for number in numbers:
        entry = evidence[number]
        index, quote = entry.get("turn_index"), norm(entry.get("quote"))
        if isinstance(index, bool) or not isinstance(index, int):
            errors.append("Q%d evidence has no integer turn_index" % number)
            continue
        if not quote or not anchor_ok(turns, index, quote):
            errors.append("Q%d evidence quote %r does not occur in dialogue turn %r; a quote that "
                          "is not in the turn it names proves nothing (AL-007)"
                          % (number, quote, index))
        elif ambiguous_anchor(turns, index, quote):
            # The quote IS in the turn it names, and also in a neighbour. AL-007 is satisfied and the
            # anchor is still unusable: the downstream cross-check locates the auditor's quote within +-1
            # and normalises a one-turn index gap when the span pins exactly one turn. A span occurring
            # twice pins nothing, so an item anchored this way can never be settled deterministically and
            # is parked for human reading on the strength of a quote the writer could have made longer.
            errors.append("Q%d evidence quote %r occurs in turn %d and also in an adjacent turn, so it "
                          "identifies no single sentence; lengthen it until it occurs once (AL-007)"
                          % (number, quote, index))
        if index <= previous:
            errors.append("Q%d evidence is at turn %d, not after Q%d's turn %d; question order "
                          "must follow the recording (QR-004/AL-003)"
                          % (number, index, number - 1, previous))
        previous = max(previous, index)
        if first_end:
            expected = window_of(number, first_end)
            if entry.get("narrator_window_id") != expected:
                errors.append("Q%d evidence declares narrator_window_id %r but question %d falls "
                              "in window %d" % (number, entry.get("narrator_window_id"), number,
                                                expected))
            bounds = ranges.get(expected)
            if bounds and not bounds[0] < index < bounds[1]:
                errors.append("Q%d's evidence is at turn %d, outside window %d (turns %d-%d); "
                              "SC-019's window boundary has no tolerance"
                              % (number, index, expected, bounds[0] + 1, bounds[1] - 1))
        # The third of the three agreements the schema names. The blueprint's own window_id was
        # already recomputed against this same narration upstream, so a disagreement here is not a
        # second opinion on the narration -- it means the package and the plan describe different
        # splits, and §1.4 makes the plan the one that stands.
        planned_window = (items.get(number) or {}).get("narrator_window_id")
        if planned_window is not None and entry.get("narrator_window_id") != planned_window:
            errors.append("Q%d evidence declares narrator_window_id %r but blueprint item %d is in "
                          "window %r; the plan's window split is not the question stage's to change"
                          % (number, entry.get("narrator_window_id"), number, planned_window))
        if entry.get("proposition_alignment_result") != "aligned":
            errors.append("Q%d reports proposition_alignment_result %r; a package that records its "
                          "own AL-018 failure must not be delivered as passing"
                          % (number, entry.get("proposition_alignment_result")))
        validate_ar003(number, norm(answers[number].get("canonical")), quote,
                       questions[number], errors)


def validate_ar003(number: int, canonical: str, quote: str, question: dict, errors: list) -> None:
    """AR-003 in tiers, and which tier applies is decided by tokenising the CANONICAL.

    Not by the declared word_limit: a group printed as NO MORE THAN TWO WORDS may perfectly well
    have a one-word answer, and that answer is held to the strict single-token rule. The first
    version of this check read the rubric instead and let every one-word answer inside a two-word
    group take the loose path.

    One token  -> word-for-word identity with one complete orthographic token of the evidence, with
                  no substring credit in either direction (Educational cannot key education).
    Many tokens -> every component word must be a complete token of the evidence; it is never
                  required to equal a single token, which the first draft demanded and which fails
                  every legitimate multi-word answer.
    Hyphenated -> one word, whole token kept, and the carrier may not pre-fill a half (AR-014).
    """
    if not canonical or not quote:
        return
    evidence_tokens = set(tokens_of(quote))
    answer = tokens_of(canonical)
    if not answer:
        return
    missing = [token for token in answer if token not in evidence_tokens]
    if missing:
        tier = "one-word" if len(answer) == 1 else "multi-word"
        errors.append("Q%d canonical %r is not carried by its evidence %r: %s appears as no "
                      "complete token there (%s AR-003; derivation, synonym substitution and "
                      "substring matches are all refused)"
                      % (number, canonical, quote, missing, tier))
    for token in answer:
        if "-" not in token:
            continue
        carrier = " ".join([str(question.get("carrier_before") or ""),
                            str(question.get("carrier_after") or "")])
        printed = set(tokens_of(carrier))
        leaked = sorted(piece for piece in token.split("-") if len(piece) > 1 and piece in printed)
        if leaked:
            errors.append("Q%d's carrier already prints %s of the hyphenated answer %r; AR-014 "
                          "counts it as one word and the whole token must be the candidate's to "
                          "write" % (number, leaked, canonical))


def validate_variety(numbers: list, questions: dict, answers: dict, errors: list) -> dict:
    """QR-027. Thresholds are read from `validate_part1` at call time, never copied.

    Copying them would put the answer-variety limits in two places, and the copy is the one that
    stops matching. Counting uses `derive_qr027_class` (character composition) rather than the
    persisted `response_form` (token count) -- `Room 4B` is a phrase whose QR-027 class is mixed.
    """
    max_numeric = material_validator.QR027_MAX_NUMERIC
    min_spelled = material_validator.QR027_MIN_SPELLED
    max_same = material_validator.QR027_MAX_SAME_CATEGORY
    classes = [derive_qr027_class(norm(answers[number].get("canonical"))) for number in numbers]
    numeric = sum(1 for value in classes if value == "numeric")
    spelled = sum(1 for value in classes if value in {"lexical", "mixed"})
    categories: dict = {}
    for number in numbers:
        category = norm(questions[number].get("answer_category"))
        if category:
            categories[category] = categories.get(category, 0) + 1
    worst = max(categories.values()) if categories else 0
    if numeric > max_numeric:
        errors.append("%d of the ten answers are purely numeric; QR-027 allows at most %d without "
                      "a recorded justification" % (numeric, max_numeric))
    if spelled < min_spelled:
        errors.append("only %d answers require spelling a word or phrase; QR-027 asks for at least "
                      "%d" % (spelled, min_spelled))
    if worst >= max_same:
        heaviest = sorted(name for name, count in categories.items() if count == worst)
        errors.append("answer category %s is tested by %d of the ten items; QR-027 admits fewer "
                      "than %d without a recorded justification" % (heaviest, worst, max_same))
    return {
        "qr027_numeric_answers": numeric,
        "qr027_spelled_answers": spelled,
        "qr027_largest_category": worst,
        "qr027_category_counts": categories,
    }


def validate_leakage(members: dict, questions: dict, answers: dict, groups: dict, errors: list,
                     warnings: list) -> None:
    """QR-040 / SC-012 group-scope leakage, over the group's whole candidate-visible surface.

    Word-level only. An answer stated in different words is a semantic leak and belongs to the audit
    agent, which rebuilds the answers itself and is therefore in a better position to see it: this
    check catches the original word and its simple inflections, which is what can be decided
    mechanically.
    """
    for key in sorted(members):
        group = groups.get(key)
        if not isinstance(group, dict):
            continue
        structure = group.get("structure") if isinstance(group.get("structure"), dict) else {}
        visible = [norm(group.get("title"))]
        visible += [str(value) for value in (group.get("signposts") or [])]
        for name in ("row_labels", "column_labels", "hierarchy"):
            visible += [str(value) for value in (structure.get(name) or [])]
        for number in members[key]:
            question = questions.get(number, {})
            visible += [str(question.get("carrier_before") or ""),
                        str(question.get("carrier_after") or "")]
        surface = " \n ".join(visible)
        printed = set(tokens_of(surface))
        for number in members[key]:
            canonical = norm(answers.get(number, {}).get("canonical"))
            if not canonical:
                continue
            if canonical.casefold() in surface.casefold():
                errors.append("Q%d's answer %r is printed in group %r's own visible text; the "
                              "group can be answered without listening (QR-040/SC-012)"
                              % (number, canonical, key))
                continue
            answer = [token for token in tokens_of(canonical) if token not in FUNCTION_WORDS]
            if not answer:
                continue
            hits = [token for token in answer if inflections(token) & printed]
            if len(hits) == len(answer):
                errors.append("every word of Q%d's answer %r appears in group %r's visible text "
                              "(%s, allowing simple inflections); QR-040 covers inflected forms"
                              % (number, canonical, key, hits))
            elif hits:
                warnings.append("part of Q%d's answer %r appears in group %r's visible text (%s); "
                                "check it does not narrow the answer to one candidate"
                                % (number, canonical, key, hits))


def validate_signposts(groups: dict, members: dict, errors: list) -> None:
    """QR-026: each narrator window needs at least one blank-free, script-grounded signpost.

    Counted per WINDOW rather than per group, because that is what the rule says and because a
    single-item group inside a well-signposted window needs no line of its own.
    """
    by_window: dict = {}
    for key in sorted(members):
        group = groups.get(key)
        if not isinstance(group, dict):
            continue
        window = group.get("narrator_window_id")
        signposts = [norm(value) for value in (group.get("signposts") or [])]
        by_window.setdefault(window, []).extend(value for value in signposts if value)
    for window in sorted(by_window, key=str):
        if not by_window[window]:
            errors.append("narrator window %r carries no blank-free signpost; QR-026 asks for at "
                          "least one specific, script-grounded navigation line per window"
                          % (window,))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("material", type=Path)
    parser.add_argument("--blueprint", required=True, type=Path)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()
    try:
        material = read_json(args.material, "material")
        blueprint = read_json(args.blueprint, "blueprint")
        package = read_json(args.questions, "question package")
    except ValueError as exc:
        return report([str(exc)], [], {}, args.json)

    errors: list = []
    warnings: list = []
    # Start empty and only add a key once it is measured: a zero is indistinguishable from a real
    # count, so a run that bailed out early would report "0 end-of-line blanks" rather than
    # "not measured" to whatever reads --json.
    metrics: dict = {}

    turns, material_package = material_turns(material, errors)
    items = blueprint_items(blueprint, errors)
    questions, answers, evidence, groups, instructions = validate_shape(
        package, material_package, errors)
    if not questions or not answers or not evidence:
        return report(errors or ["question package carries no usable items"], warnings, metrics,
                      args.json)

    first_end, ranges = narrator_windows(turns, errors) if turns else (0, {})
    numbers = validate_mapping(questions, answers, evidence, errors)
    if items:
        validate_blueprint_fidelity(numbers, questions, answers, items, errors)
    members = validate_groups(numbers, questions, groups, instructions, evidence, first_end, errors)
    metrics["groups"] = len(members)
    metrics["layouts"] = sorted({str(groups[key].get("layout")) for key in members
                                 if isinstance(groups.get(key), dict)})
    metrics["blank_positions"] = validate_questions(numbers, questions, answers, errors, warnings)
    validate_answers(numbers, questions, answers, groups, instructions, members, errors)
    if turns:
        validate_evidence(numbers, questions, answers, evidence, items, turns, first_end, ranges,
                          errors)
    metrics.update(validate_variety(numbers, questions, answers, errors))
    validate_leakage(members, questions, answers, groups, errors, warnings)
    validate_signposts(groups, members, errors)
    return report(errors, warnings, metrics, args.json)


if __name__ == "__main__":
    sys.exit(main())
