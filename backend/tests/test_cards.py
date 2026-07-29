"""Card-grid summary fields (backend/deterministic/cards.py).

Two kinds of fixture, on purpose:

* the real 40-turn material/blueprint pair, which is balanced and validator-passing -- it pins
  that a *good* material flags nothing, which is the assertion a threshold bug breaks first;
* small synthetic scripts for clustering and out-of-order, because the real fixture deliberately
  has neither defect and there is no way to provoke one in it without also breaking its anchors.

The synthetic scripts are built with real turn text so `anchor_holds` genuinely resolves. A
fixture whose anchors did not hold would flag every point as an anchor mismatch and the cluster
assertion would pass for the wrong reason.
"""

from __future__ import annotations

from backend.deterministic import cards
from backend.orchestration.publish import Candidate

SCENARIO_KEY = "accommodation-rental"


def _material(turns):
    return {
        "model": "fixture",
        "extracted_at": "2026-07-28T00:00:00+00:00",
        "test_package": "Test 1",
        "content_kind": "listening_material",
        "source_htmls": [],
        "listening_material_parts": [{
            "reference": "Part 1",
            "test_package": "Test 1",
            "scenario": "A traveller phones a hotel.",
            "script": {"reference": "Part 1", "test_package": "Test 1", "speaker_count": 3,
                       "turns": turns},
            "source_htmls": [],
        }],
    }


def _blueprint(items, **extra):
    blueprint = {
        "narration_mode": "full",
        "split_after": 5,
        "question_type_coverage": {},
        "items": items,
        "correction": {},
        "indirect_confirmation": {},
    }
    blueprint.update(extra)
    return blueprint


def _item(number, turn_index, evidence, **extra):
    item = {"number": number, "group": 1 if number <= 5 else 2, "type": "condition",
            "target": evidence, "evidence": evidence, "turn_index": turn_index,
            "item_form": "note", "form_group": None, "distractor": False, "confirmed": True}
    item.update(extra)
    return item


class TestPreviewFirstLine:
    def test_narration_is_skipped(self, material):
        """speaker1 is the exam narrator. Its turn is identical rubric in every material, so a
        card that showed it would open with the same paragraph for every candidate in the grid."""
        turns = material["listening_material_parts"][0]["script"]["turns"]
        assert turns[0]["speaker"] == "speaker1", "fixture must start with narration or this proves nothing"
        line = cards.preview_first_line(material)
        assert line == turns[1]["text"]
        assert "IELTS listening test" not in line
        assert not line.startswith("This is the")

    def test_multiple_leading_narration_turns_are_all_skipped(self):
        first = "Good morning, Hillside Hotel."
        found = cards.preview_first_line(_material([
            {"speaker": "speaker1", "text": "Part one. You will hear a conversation."},
            {"speaker": "speaker1", "text": "Now listen carefully and answer questions 1 to 5."},
            {"speaker": "speaker2", "text": first},
        ]))
        assert found == first

    def test_a_blank_dialogue_turn_is_not_returned(self):
        """An empty string would render as an empty card, which is the thing being removed."""
        assert cards.preview_first_line(_material([
            {"speaker": "speaker2", "text": "   "},
            {"speaker": "speaker3", "text": "Hello?"},
        ])) == "Hello?"

    def test_a_material_with_no_dialogue_yields_an_empty_string(self):
        """Never raises: a malformed material is the validator's business, not a crash here."""
        assert cards.preview_first_line(_material([
            {"speaker": "speaker1", "text": "Part one."}
        ])) == ""
        assert cards.preview_first_line({}) == ""
        assert cards.preview_first_line(None) == ""


class TestPreviewSummary:
    def test_the_real_fixture_reads_as_topic_plus_distraction(self, material, blueprint):
        summary = cards.preview_summary(material, blueprint, "租房咨询")
        assert summary.startswith("租房咨询，")
        # The fixture spells F-O-R-D-Y-C-E and carries a correction cycle plus an indirect
        # confirmation, so all three features are genuinely present and must be named.
        assert "含拼读" in summary
        assert "修正干扰" in summary
        assert "间接指代确认" in summary

    def test_the_correction_is_named_by_the_type_it_attacks(self):
        """"价格修正干扰", not a bare "自我修正干扰": the blueprint records the correction as raw
        strings, and the item carrying them is what says which kind of detail was corrected."""
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "That's ninety pounds. Sorry, I mean eighty pounds."},
        ])
        blueprint = _blueprint(
            [_item(1, 1, "eighty pounds", type="price")],
            correction={"earlier": "ninety pounds", "final": "eighty pounds",
                        "marker": "Sorry, I mean"},
        )
        assert cards.preview_summary(material, blueprint, "酒店预订") == "酒店预订，价格修正干扰"

    def test_distractor_items_are_named_even_without_a_correction(self):
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "The rate is eighty pounds a night."},
        ])
        blueprint = _blueprint([_item(1, 1, "eighty pounds", type="price", distractor=True)])
        assert cards.preview_summary(material, blueprint, "酒店预订") == "酒店预订，价格干扰"

    def test_a_feature_is_never_named_twice(self):
        """A price correction plus a price distractor must not read "价格修正干扰 + 价格干扰"."""
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "Ninety pounds. Sorry, I mean eighty pounds."},
        ])
        blueprint = _blueprint(
            [_item(1, 1, "eighty pounds", type="price", distractor=True)],
            correction={"earlier": "ninety pounds", "final": "eighty pounds",
                        "marker": "Sorry, I mean"},
        )
        summary = cards.preview_summary(material, blueprint, "酒店预订")
        assert summary.count("价格") == 1, summary

    def test_it_stays_one_short_line(self, material, blueprint):
        """The card has one line. A summary that wraps stops being scannable, which is all it is
        for, so the feature list is capped rather than allowed to grow with the blueprint."""
        summary = cards.preview_summary(material, blueprint, "租房咨询")
        assert summary.count("+") < cards.MAX_SUMMARY_FEATURES
        assert "\n" not in summary

    def test_a_missing_topic_leaves_the_features_alone_rather_than_inventing_one(
        self, material, blueprint
    ):
        """A custom scenario's key is a hash with no title_zh, and guessing a topic from the
        English scenario sentence would put English on a Chinese card."""
        summary = cards.preview_summary(material, blueprint, "")
        assert summary and not summary.startswith("，")
        assert "含拼读" in summary

    def test_a_blueprint_with_no_recorded_distraction_yields_the_topic_alone(self):
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "We open at nine."},
        ])
        assert cards.preview_summary(material, _blueprint([]), "展览参观") == "展览参观"

    def test_it_is_chinese_and_deterministic(self, material, blueprint):
        first = cards.preview_summary(material, blueprint, "租房咨询")
        assert first == cards.preview_summary(material, blueprint, "租房咨询")
        assert any("一" <= ch <= "鿿" for ch in first)


class TestFlaggedPoints:
    def test_a_balanced_material_flags_nothing(self, material, blueprint):
        """The real validator-passing fixture. If a threshold is wrong this is what breaks: ten
        dots that light up on a good material would train reviewers to ignore them."""
        assert cards.flagged_points(material, blueprint) == []

    def test_an_anchor_pointing_at_the_wrong_turn_is_flagged(self, material, blueprint, clone):
        """Same predicate the repair pass and validate_part1.py use, so the card and the reader
        can never disagree about which annotation sits beside the wrong sentence."""
        broken = clone(blueprint)
        broken["items"][2]["turn_index"] = broken["items"][2]["turn_index"] + 5
        flagged = cards.flagged_points(material, broken)
        assert broken["items"][2]["number"] in flagged
        assert cards.flagged_point_reasons(material, broken)[broken["items"][2]["number"]] == [
            "anchor_mismatch"
        ]

    def test_an_out_of_range_anchor_is_flagged_rather_than_crashing(self, material, blueprint, clone):
        broken = clone(blueprint)
        broken["items"][0]["turn_index"] = 9999
        assert broken["items"][0]["number"] in cards.flagged_points(material, broken)

    def test_an_anchor_on_narration_is_flagged(self, material, blueprint, clone):
        """Narration must not carry answer information at all, so an anchor there is a defect
        even though the evidence string might happen to appear in the rubric."""
        broken = clone(blueprint)
        broken["items"][0]["turn_index"] = 0
        assert broken["items"][0]["number"] in cards.flagged_points(material, broken)

    def test_points_on_adjacent_turns_are_flagged_as_clustered(self):
        """Three points inside CLUSTER_SPAN turns: a candidate has no time to write all three.

        Reuses distribution.ts's rule -- a maximal run of >= CLUSTER_MIN_POINTS points spanning
        <= CLUSTER_SPAN TURN indexes -- and its thresholds, rather than inventing a new one.
        """
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "The rate is eighty pounds."},
            {"speaker": "speaker3", "text": "And the deposit is fifty pounds."},
            {"speaker": "speaker2", "text": "Available from March."},
        ])
        blueprint = _blueprint([
            _item(1, 1, "eighty pounds"),
            _item(2, 2, "fifty pounds"),
            _item(3, 3, "from March"),
        ])
        reasons = cards.flagged_point_reasons(material, blueprint)
        assert sorted(reasons) == [1, 2, 3]
        assert all("clustered" in v for v in reasons.values())

    def test_two_adjacent_points_are_not_a_cluster(self):
        """CLUSTER_MIN_POINTS is 3, not 2: a point often spans ask/answer/confirm turns, so at 2
        every real material would report clusters and the signal would be worthless."""
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "The rate is eighty pounds."},
            {"speaker": "speaker3", "text": "And the deposit is fifty pounds."},
        ])
        blueprint = _blueprint([
            _item(1, 1, "eighty pounds"),
            _item(2, 2, "fifty pounds"),
        ])
        assert cards.flagged_points(material, blueprint) == []

    def test_points_spread_past_the_span_are_not_a_cluster(self):
        turns = [{"speaker": "speaker1", "text": "Part one."}]
        turns += [{"speaker": "speaker2", "text": "Filler line %d." % i} for i in range(1, 16)]
        turns[1]["text"] = "The rate is eighty pounds."
        turns[8]["text"] = "The deposit is fifty pounds."
        turns[15]["text"] = "Available from March."
        blueprint = _blueprint([
            _item(1, 1, "eighty pounds"),
            _item(2, 8, "fifty pounds"),
            _item(3, 15, "from March"),
        ])
        assert cards.flagged_points(_material(turns), blueprint) == []

    def test_a_later_question_spoken_first_is_flagged_out_of_order(self):
        """spec §4B-2 线性顺序性: audio order must equal question order. Item 2's information is
        spoken after item 3's here, so a candidate hears the answers in the wrong order."""
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "The rate is eighty pounds."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Available from March."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker3", "text": "And the deposit is fifty pounds."},
        ])
        blueprint = _blueprint([
            _item(1, 1, "eighty pounds"),
            _item(3, 5, "from March"),
            _item(2, 9, "fifty pounds"),
        ])
        reasons = cards.flagged_point_reasons(material, blueprint)
        # Both ends are flagged: from a reviewer's seat either could be the one out of place.
        assert sorted(reasons) == [2, 3]
        assert all("out_of_order" in v for v in reasons.values())

    def test_two_points_on_one_turn_are_not_an_ordering_defect(self):
        """distribution.ts tie-breaks equal ordinals by number, so one turn carrying two points
        is never reported as a jump back. Without that a form question over one sentence -- a
        perfectly normal shape -- would flag itself."""
        material = _material([
            {"speaker": "speaker1", "text": "Part one."},
            {"speaker": "speaker2", "text": "That's Anna Woods on 07840051963."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Filler."},
            {"speaker": "speaker2", "text": "Available from March."},
        ])
        blueprint = _blueprint([
            _item(2, 1, "07840051963"),
            _item(1, 1, "Anna Woods"),
            _item(3, 5, "from March"),
        ])
        reasons = cards.flagged_point_reasons(material, blueprint)
        assert not any("out_of_order" in v for v in reasons.values()), reasons

    def test_uniformity_is_not_turned_into_a_dot(self):
        """CV/uniformity thresholds are explicitly uncalibrated (runtimeConfig.ts CALIBRATED:
        false). Ten points bunched in the middle with long empty stretches at both ends scores
        badly on uniformity but costs nobody a written answer, so it is not flagged."""
        turns = [{"speaker": "speaker1", "text": "Part one."}]
        turns += [{"speaker": "speaker2", "text": "Filler line %d." % i} for i in range(1, 31)]
        turns[14]["text"] = "The rate is eighty pounds."
        turns[22]["text"] = "Available from March."
        blueprint = _blueprint([
            _item(1, 14, "eighty pounds"),
            _item(2, 22, "from March"),
        ])
        assert cards.flagged_points(_material(turns), blueprint) == []

    def test_the_result_is_ascending_and_deduplicated(self, material, blueprint, clone):
        broken = clone(blueprint)
        for index in (0, 4, 7):
            broken["items"][index]["turn_index"] = 9999
        flagged = cards.flagged_points(material, broken)
        assert flagged == sorted(flagged) == sorted(set(flagged))

    def test_a_malformed_blueprint_yields_no_flags_rather_than_raising(self, material):
        assert cards.flagged_points(material, {}) == []
        assert cards.flagged_points(material, None) == []
        assert cards.flagged_points({}, {}) == []


class TestCardFieldsOnTheCandidate:
    """as_dict is what the frontend's card grid reads, so the three fields must be in it."""

    def _candidate(self, material, blueprint, audit):
        return Candidate(
            material_id="20260728-accommodation-rental-7f3a1c2d",
            scenario_key=SCENARIO_KEY, group_key="batch-1:accommodation-rental",
            slot_id="slot-1", material=material, blueprint=blueprint, audit=audit,
        )

    def test_all_three_fields_are_present_and_populated(self, material, blueprint, audit_aligned):
        payload = self._candidate(material, blueprint, audit_aligned).as_dict()
        assert payload["preview_first_line"] == cards.preview_first_line(material)
        assert payload["preview_summary"]
        assert payload["flagged_points"] == []

    def test_the_summary_carries_the_scenario_topic_from_the_catalogue(
        self, material, blueprint, audit_aligned
    ):
        """scenario_key -> title_zh. The catalogue is the single source of scenario names, so the
        card cannot drift from what the user ticked."""
        payload = self._candidate(material, blueprint, audit_aligned).as_dict()
        assert payload["preview_summary"].startswith("租房咨询，")

    def test_a_custom_scenario_key_has_no_topic_and_says_nothing_false(
        self, material, blueprint, audit_aligned
    ):
        candidate = self._candidate(material, blueprint, audit_aligned)
        candidate.scenario_key = "custom-1a2b3c4d"
        summary = candidate.as_dict()["preview_summary"]
        assert summary and not summary.startswith("，")

    def test_a_fail_candidate_still_carries_its_card_fields(
        self, material, blueprint, audit_aligned, clone
    ):
        """A FAIL material is returned to the user, so the card that states its shortcomings needs
        the same three fields as any other -- previously it would have gone to quarantine."""
        audit = clone(audit_aligned)
        audit["verdict"] = "FAIL"
        payload = self._candidate(material, blueprint, audit).as_dict()
        assert payload["verdict"] == "FAIL"
        assert payload["preview_first_line"] and payload["preview_summary"]

    def test_the_fields_survive_a_round_trip_through_shared_storage(
        self, material, blueprint, audit_aligned
    ):
        """A select served by a different microVM rebuilds the Candidate from its record."""
        from backend.orchestration.publish import Candidate as C

        original = self._candidate(material, blueprint, audit_aligned)
        rebuilt = C.from_record(original.as_record())
        assert rebuilt.as_dict()["preview_first_line"] == original.as_dict()["preview_first_line"]
        assert rebuilt.as_dict()["preview_summary"] == original.as_dict()["preview_summary"]

    def test_expects_audio_is_gone(self, material, blueprint, audit_aligned):
        """The gate that withheld synthesis. Every candidate is selectable now, so a field
        answering "does this one get audio" would only ever be true."""
        assert "expects_audio" not in self._candidate(material, blueprint, audit_aligned).as_dict()

    def test_a_broken_material_degrades_to_empty_fields_rather_than_losing_the_candidate(
        self, blueprint, audit_aligned
    ):
        """These are display strings. An exception here would propagate into `register` and cost
        the user a material they could otherwise select and listen to."""
        payload = self._candidate({"listening_material_parts": "not a list"}, blueprint,
                                  audit_aligned).as_dict()
        assert payload["preview_first_line"] == ""
        assert payload["flagged_points"] == []
        assert payload["material_id"]
