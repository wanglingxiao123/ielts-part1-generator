#!/usr/bin/env python3
"""Build validator fixtures from a real archived script.

Dialogue text comes from material/归档/snap_010_scripts_topic3640.json so the fixtures
exercise realistic prose rather than invented sentences. Narration is rewritten because the
archived sample uses a 1-7/8-10 split and short-mode narration, while the contract requires
a 1-5/6-10 or 1-6/7-10 split. A few dialogue turns are edited to carry a clean correction
and indirect-confirmation chain.

Run from anywhere:  python3 shared/tests/build_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

# parents[3], not [4]. This file was born at skills/ielts-listening-skills/shared/tests/, where
# [4] was the repo root; a3bf922 moved it one level shallower to skills/shared/tests/ and left the
# index alone, so the script has been unrunnable since 2026-07-31 -- it resolved SOURCE to
# ~/Documents/material/归档/, outside the repo. Nothing caught it because run_tests.py reads the
# committed fixture JSONs and never invokes the builder. Hence the assertion below: a path this
# fragile must fail with its own name on it, not with a FileNotFoundError four frames deep.
ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "material" / "归档" / "snap_010_scripts_topic3640.json"
if not SOURCE.is_file():
    raise SystemExit(
        f"archived source script not found at {SOURCE}\n"
        f"(ROOT resolved to {ROOT}; if this file moved, fix the parents[] index)"
    )
OUT = Path(__file__).resolve().parent / "fixtures"

OPENING = (
    "This is the IELTS listening test. You will hear four different recordings and you will "
    "have to answer questions on what you hear. There will be time for you to read the "
    "instructions and the questions, and you will have a chance to check your work. You will "
    "hear each recording once only. The test is in four parts. At the end of the test you "
    "will be given two minutes to check all of your answers. Part one. You will hear a "
    "telephone conversation between a woman who is moving to London and an employee of a "
    "relocation service. First, you have some time to look at questions 1 to 5. Now listen "
    "carefully and answer questions 1 to 5."
)
MIDPOINT = (
    "Before you hear the rest of the conversation, you have some time to look at questions 6 "
    "to 10. Now listen and answer questions 6 to 10."
)
CLOSING = (
    "That is the end of part one. You now have half a minute to check your answers to part "
    "one. Now turn to part two."
)

# Edits to the archived dialogue, keyed by original turn index.
# Purpose: give the script one unambiguous correction chain and one indirect confirmation
# whose answer term is spoken verbatim first, as the spec's 命题铁律 requires.
EDITS = {
    6: "Northern Ireland, just outside Belfast.",
    19: "Is it a secondary school you'll be looking for?",
    20: "I said secondary, but let me correct that. He is still in primary school.",
    31: "So what sort of property do you have in mind, a flat or a house?",
    32: "We have friends in an amazing flat, but as we've always lived in a house, we'll "
        "stick with the latter.",
    # The archived turn crowded two answers ("two-bedroom" and "guest room") into one turn,
    # which spec 4B-2 forbids. Split into two micro-cycles so each point gets its own turn.
    35: "So you definitely need a two-bedroom property.",
}
# Extra turns spliced in after a given original dialogue turn, to separate crowded answers.
INSERTS = {
    35: [
        {"speaker": "speaker3", "text": "That's right, two bedrooms at least."},
        {"speaker": "speaker2", "text": "But how about having a guest room? I might be able to find a three-bedroom property for you."},
    ],
}


def load_turns() -> list[dict]:
    turns = json.loads(SOURCE.read_text(encoding="utf-8"))["listening_material_parts"][0]["script"]["turns"]
    dialogue_indices = [i for i, turn in enumerate(turns) if turn["speaker"] != "speaker1"]
    body: list[dict] = []
    for original in dialogue_indices:
        turn = dict(turns[original])
        if original in EDITS:
            turn["text"] = EDITS[original]
        body.append(turn)
        body.extend(dict(extra) for extra in INSERTS.get(original, []))
    return body


def build_material(body: list[dict], split_at: int) -> dict:
    turns = (
        [{"speaker": "speaker1", "text": OPENING}]
        + body[:split_at]
        + [{"speaker": "speaker1", "text": MIDPOINT}]
        + body[split_at:]
        + [{"speaker": "speaker1", "text": CLOSING}]
    )
    return {
        "model": "fixture",
        "extracted_at": "2026-07-28T00:00:00+00:00",
        "test_package": "Test 1",
        "content_kind": "listening_material",
        "source_htmls": [],
        "listening_material_parts": [
            {
                "reference": "Part 1",
                "test_package": "Test 1",
                "scenario": "A woman moving to London phones a relocation service to register her details and property requirements.",
                "script": {
                    "reference": "Part 1",
                    "test_package": "Test 1",
                    "turns": turns,
                    "speaker_count": 3,
                },
                "source_htmls": [],
            }
        ],
    }


def find_anchor(turns: list[dict], phrase: str) -> int:
    hits = [
        i
        for i, turn in enumerate(turns)
        if turn["speaker"] != "speaker1" and phrase.casefold() in turn["text"].casefold()
    ]
    if len(hits) != 1:
        raise SystemExit(f"fixture evidence must be unique, {phrase!r} matched {hits}")
    return hits[0]


# Ordered by first occurrence in the script; the narration split falls between items 5 and 6.
#
# v2 grouping. Every item belongs to a group, groups are homogeneous, their item numbers are
# contiguous, they are not interleaved in the evidence sequence, and none crosses the narrator
# window (split_after=5, so window 1 is Q1-5 and window 2 is Q6-10). The three note points cannot
# form ONE group for that last reason: Q5 sits in window 1 and Q6/Q7 in window 2, so they split
# into C and D. Turning Q5 into a `form` and folding it into A was the alternative, and it was
# rejected -- A's turn span would become 4..20 = 16, over MAX_GROUP_SPAN, so the fixture that is
# supposed to be the valid example would start emitting a span warning.
#
# `answer_category` classifies the nature of the ANSWER, not the wording of the sentence:
#   - BT14 9BJ is a postcode, i.e. a locator, so `location` rather than `contact`.
#   - guest room / office are conditions asked OF the property, so `requirement`; `facility` is for
#     a place being described (park, primary school). Reading them as facilities would give
#     facility x4 and make this "valid" fixture violate QR-027's same-category limit of <3.
#   - house is chosen after weighing house against flat (distractor=True), hence `preference`.
#   - two-bedroom is a spec, so `quantity`, even though its evidence says "definitely need".
#
# `response_form` counts TOKENS, splitting on whitespace only: two-bedroom is one word because
# word_limit counts a hyphenated compound as one. `numeric` needs every token to be a pure number
# form, which is why BT14 9BJ is a phrase.
ITEM_SPECS = [
    # (type, target, evidence, item_form, form_group, distractor, confirmed, category, response_form)
    ("name", "Anna Woods", "It's Anna Woods.", "form", "A", False, True, "person_name", "phrase"),
    ("address", "118 Fordyce", "It's 118 Fordyce.", "form", "A", False, True, "location", "phrase"),
    ("number", "BT14 9BJ", "It's BT14 9BJ.", "form", "A", False, True, "location", "phrase"),
    ("number", "07840051963", "It's 07840051963.", "form", "A", False, True, "contact", "numeric"),
    ("option", "primary school", "still in primary school", "note", "C", True, False, "facility", "phrase"),
    # `type` stays "option" -- it names the KIND of detail (a preference or chosen alternative),
    # which is still a perfectly good completion answer ("Property type: ______"). Only the
    # dropped item_form was about the multiple-choice question type. Two dimensions, one deleted.
    ("option", "park", "he'd love a park nearby", "note", "D", False, False, "facility", "word"),
    ("option", "house", "always lived in a house", "note", "D", True, False, "preference", "word"),
    ("quantity", "two-bedroom", "you definitely need a two-bedroom property", "table", "B", False, True, "quantity", "word"),
    ("condition", "guest room", "how about having a guest room", "table", "B", True, False, "requirement", "phrase"),
    ("condition", "office", "it'd be handy to have an office", "table", "B", False, False, "requirement", "word"),
]


def build_blueprint(turns: list[dict], split_after: int) -> dict:
    items, coverage = [], {}
    for number, (kind, target, evidence, form, group, distractor, confirmed, category, response_form) in enumerate(ITEM_SPECS, 1):
        items.append({
            "number": number,
            "group": 1 if number <= split_after else 2,
            "type": kind,
            "target": target,
            "evidence": evidence,
            "turn_index": find_anchor(turns, evidence),
            "item_form": form,
            "form_group": group,
            "distractor": distractor,
            "confirmed": confirmed,
            "response_form": response_form,
            "answer_category": category,
            # Derived from split_after rather than written into ITEM_SPECS as a constant. A
            # hand-written window would make this fixture unable to demonstrate the thing the
            # field exists for: that the declared value and the recomputed one agree.
            "narrator_window_id": 1 if number <= split_after else 2,
        })
        coverage.setdefault(form, []).append(number)
    return {
        "blueprint_schema_version": 2,
        "narration_mode": "full",
        "split_after": split_after,
        "completion_layout_coverage": coverage,
        "items": items,
        "correction": {
            "earlier": "I said secondary",
            "final": "still in primary school",
            "marker": "let me correct that",
        },
        "indirect_confirmation": {
            "answer_term": "house",
            "reference_phrase": "the latter",
        },
    }


def build_audit(material: dict, blueprint: dict, drop_item: int | None) -> dict:
    """Build an audit result whose blind map is derived from the script.

    `drop_item` omits one planned point and adds an unplanned detail instead, producing the
    divergence that cross_check.py must catch. With drop_item=None the map agrees exactly.

    Carries no findings: the fixture script is compliant, and a word count outside the typical
    600-650 band belongs in `warnings` per audit-rubric.md, so inventing a minor finding here
    would contradict the rubric and force a wrong PASS_WITH_MINOR_EDITS verdict.
    """
    turns = material["listening_material_parts"][0]["script"]["turns"]
    rows = []
    for item in blueprint["items"]:
        if item["number"] == drop_item:
            continue
        rows.append({
            "seq": 0,
            "type": item["type"],
            "evidence": item["evidence"],
            "turn_index": item["turn_index"],
            "speaker": turns[item["turn_index"]]["speaker"],
            "clarity": "confirmed" if item["confirmed"] else "clear",
            "mechanism": "spelling" if item["type"] == "name" else None,
        })
    if drop_item is not None:
        unplanned = "I'm actually a qualified nurse"
        rows.append({
            "seq": 0,
            "type": "condition",
            "evidence": unplanned,
            "turn_index": find_anchor(turns, unplanned),
            "speaker": "speaker3",
            "clarity": "ambiguous",
            "mechanism": None,
        })
    rows.sort(key=lambda row: row["turn_index"])
    for seq, row in enumerate(rows, 1):
        row["seq"] = seq

    dialogue = [t for t in turns if t["speaker"] != "speaker1"]
    narrator = [t for t in turns if t["speaker"] == "speaker1"]
    midpoint = next(i for i, t in enumerate(turns) if t["speaker"] == "speaker1" and i > 0)
    before = sum(1 for i, t in enumerate(turns) if t["speaker"] != "speaker1" and i < midpoint)
    words = sum(len(t["text"].split()) for t in dialogue)
    return {
        "verdict": "PASS",
        "assessable": True,
        "score": {"total": 88, "dimensions": {
            "scenario_purpose_frame": 19,
            "information_map_quality": 22,
            "role_consistency": 18,
            "naturalness_level": 13,
            "difficulty_distractor_control": 11,
            "transcript_readiness": 5,
        }},
        "findings": [],
        "blind_information_map": rows,
        "metrics": {
            "dialogue_words": words,
            "dialogue_turns": len(dialogue),
            "first_half_turns": before,
            "second_half_turns": len(dialogue) - before,
            "narrator_words": sum(len(t["text"].split()) for t in narrator),
        },
        "warnings": [f"dialogue words outside preferred 600-650: {words}"],
    }


def write(name: str, payload: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote fixtures/{name}")


def locate_split(body: list[dict], sixth_evidence: str, seventh_evidence: str) -> int:
    """Put the midpoint narration between information points 6 and 7.

    Both halves need >=8 turns and each item must sit in the half its group declares, so the
    split point is derived from the item layout rather than copied from the archived sample.
    """
    after_sixth = next(i for i, turn in enumerate(body) if sixth_evidence.casefold() in turn["text"].casefold())
    before_seventh = next(i for i, turn in enumerate(body) if seventh_evidence.casefold() in turn["text"].casefold())
    if not after_sixth < before_seventh:
        raise SystemExit("fixture item 6 must precede item 7")
    return after_sixth + 1


def main() -> None:
    body = load_turns()
    # Split after point 5 rather than 6: the spec also asks for 前后均衡 (roughly even halves),
    # and this fixture is what downstream subtasks will copy, so it should model the right shape
    # rather than merely clear the >=8-turns-per-half floor.
    split_at = locate_split(body, ITEM_SPECS[4][2], ITEM_SPECS[5][2])
    first, second = split_at, len(body) - split_at
    if min(first, second) < 8:
        raise SystemExit(f"fixture halves must each hold 8+ turns; got {first}/{second}")
    if min(first, second) / max(first, second) < 0.6:
        raise SystemExit(f"fixture halves should be roughly even; got {first}/{second}")
    material = build_material(body, split_at)
    turns = material["listening_material_parts"][0]["script"]["turns"]
    blueprint = build_blueprint(turns, split_after=5)

    write("material_valid.json", material)
    write("blueprint_valid.json", blueprint)
    write("audit_valid.json", build_audit(material, blueprint, drop_item=5))
    write("audit_aligned.json", build_audit(material, blueprint, drop_item=None))

    # Defect 1: anchor points at a turn that does not carry the evidence.
    broken = json.loads(json.dumps(blueprint))
    broken["items"][2]["turn_index"] = broken["items"][2]["turn_index"] + 4
    write("blueprint_bad_anchor.json", broken)

    # Defect 2: no form_group large enough to support a table or form question.
    ungrouped = json.loads(json.dumps(blueprint))
    for item in ungrouped["items"]:
        item["form_group"] = None
    write("blueprint_bad_grouping.json", ungrouped)

    # Defect 3: the indirect answer term is never a recordable target.
    unspoken = json.loads(json.dumps(blueprint))
    unspoken["indirect_confirmation"]["answer_term"] = "detached property"
    write("blueprint_bad_answer_term.json", unspoken)

    # Defect 4: only one confirmed point, the pre-change threshold.
    thin = json.loads(json.dumps(blueprint))
    for item in thin["items"][1:]:
        item["confirmed"] = False
    write("blueprint_thin_confirmation.json", thin)

    # --- v2 negatives. Each varies ONE thing so a test can assert the specific message rather than
    # a bare returncode. Built from the same builder as every other fixture, never hand-written.
    def variant(name: str, mutate) -> None:
        copy = json.loads(json.dumps(blueprint))
        mutate(copy)
        write(name, copy)

    # A v1 record: no version field, the v1 coverage name, and the nullable form_group v1 allowed.
    # This is the read-compatibility input, not a defect -- it must PASS with --allow-v1.
    def downgrade(bp: dict) -> None:
        bp.pop("blueprint_schema_version")
        bp["question_type_coverage"] = bp.pop("completion_layout_coverage")
        for item in bp["items"]:
            for key in ("response_form", "answer_category", "narrator_window_id"):
                item.pop(key)
            if item["item_form"] == "note":
                item["form_group"] = None
    variant("blueprint_v1_legacy.json", downgrade)

    # Version detection: an unrecognised version must be reported, never read as v1.
    variant("blueprint_bad_version.json",
            lambda bp: bp.update(blueprint_schema_version=3))

    # Declared-vs-derived, three fields. Item 2's target is "118 Fordyce", a phrase.
    variant("blueprint_bad_response_form.json",
            lambda bp: bp["items"][1].update(response_form="numeric"))
    variant("blueprint_bad_answer_category.json",
            lambda bp: bp["items"][0].update(answer_category="other"))
    # Item 1 is in window 1; declaring 2 must be caught by recomputation, not merely by range.
    variant("blueprint_bad_window.json",
            lambda bp: bp["items"][0].update(narrator_window_id=2))

    # Group constraint 1: v2 forbids the null that v1 allowed.
    variant("blueprint_bad_group_missing.json",
            lambda bp: bp["items"][4].update(form_group=None))
    # Group constraint 2: group A would hold both a form and a note point.
    variant("blueprint_bad_group_mixed.json",
            lambda bp: bp["items"][4].update(form_group="A"))
    # Group constraints 3 and 4 together: A becomes {1,3,4}, so its numbers are non-contiguous AND
    # it is interrupted by E in the evidence sequence. These two cannot be separated -- evidence
    # order is already required to be strictly increasing, so non-contiguous numbers always imply
    # interruption. See design.md D10.
    variant("blueprint_bad_group_split.json",
            lambda bp: bp["items"][1].update(form_group="E"))
    # Group constraint 5: group D would hold Q5 (window 1) and Q6/Q7 (window 2).
    variant("blueprint_bad_group_window.json",
            lambda bp: bp["items"][4].update(form_group="D"))


if __name__ == "__main__":
    main()
