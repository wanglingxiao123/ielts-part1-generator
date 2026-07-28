"""Anchor repair tests (design.md §5, prd.md R4).

Three fixtures, as the plan requires: already consistent, index-shifted (repairable), and
multiple matches (must fail). The third is the one that matters -- a "nearest match" heuristic
would make these tests pass while quietly destroying the anchor's purpose.
"""

from __future__ import annotations

from backend.deterministic.anchors import find_evidence_turns, repair_anchors


def _turns(material):
    return material["listening_material_parts"][0]["script"]["turns"]


class TestConsistentAnchors:
    def test_valid_fixture_needs_no_repair(self, material, blueprint):
        result = repair_anchors(material, blueprint)
        assert result.ok
        assert result.repaired == []
        assert result.failures == []

    def test_input_blueprint_is_not_mutated(self, material, blueprint, clone):
        original = clone(blueprint)
        repair_anchors(material, blueprint)
        assert blueprint == original


class TestIndexShiftIsRepaired:
    def test_inserting_a_turn_shifts_indices_and_they_are_repaired(
        self, material, blueprint, clone
    ):
        """The realistic case: a revision adds a turn and every later anchor is off by one."""
        shifted = clone(material)
        turns = _turns(shifted)
        turns.insert(2, {"speaker": "speaker3", "text": "Sorry, could you say that again?"})

        result = repair_anchors(shifted, blueprint)
        assert result.ok, result.failures
        assert result.repaired, "expected the shift to be detected"
        for entry in result.repaired:
            assert entry["to"] == entry["from"] + 1

    def test_repaired_anchors_actually_carry_their_evidence(self, material, blueprint, clone):
        shifted = clone(material)
        _turns(shifted).insert(2, {"speaker": "speaker3", "text": "One moment please."})
        result = repair_anchors(shifted, blueprint)
        turns = _turns(shifted)
        for item in result.blueprint["items"]:
            text = turns[item["turn_index"]]["text"].casefold()
            assert item["evidence"].casefold() in text

    def test_single_item_shift_records_from_and_to(self, material, blueprint, clone):
        broken = clone(blueprint)
        item = broken["items"][0]
        real = item["turn_index"]
        item["turn_index"] = real + 4
        result = repair_anchors(material, broken)
        assert result.ok
        assert result.repaired == [
            {"number": item["number"], "from": real + 4, "to": real, "evidence": item["evidence"]}
        ]


class TestAmbiguityFails:
    def test_two_matching_turns_fail_rather_than_guess(self, material, blueprint, clone):
        """Repeated sentences are exactly why anchors exist. Guessing here forfeits their value."""
        duplicated = clone(material)
        turns = _turns(duplicated)
        target = clone(blueprint)["items"][0]
        # Append a second turn carrying the same evidence, and break the anchor so repair runs.
        turns.insert(len(turns) - 1, {"speaker": "speaker2", "text": target["evidence"]})
        broken = clone(blueprint)
        broken["items"][0]["turn_index"] = 99

        result = repair_anchors(duplicated, broken)
        assert not result.ok
        failure = result.failures[0]
        assert len(failure["matches"]) >= 2
        assert "refusing to guess" in failure["reason"]

    def test_zero_matches_fail(self, material, blueprint, clone):
        broken = clone(blueprint)
        broken["items"][3]["evidence"] = "a sentence that appears nowhere in the script"
        result = repair_anchors(material, broken)
        assert not result.ok
        assert result.failures[0]["matches"] == []
        assert "not found" in result.failures[0]["reason"]

    def test_failure_is_all_or_nothing(self, material, blueprint, clone):
        """One unlocatable anchor invalidates the revision; nine good points do not rescue it."""
        broken = clone(blueprint)
        broken["items"][7]["evidence"] = "nowhere at all"
        assert not repair_anchors(material, broken).ok


class TestMalformedInputIsReported:
    def test_missing_turns_reports_instead_of_raising(self, blueprint):
        result = repair_anchors({"listening_material_parts": []}, blueprint)
        assert not result.ok
        assert "no usable dialogue turns" in result.failures[0]["reason"]

    def test_non_dict_blueprint_reports(self, material):
        result = repair_anchors(material, None)
        assert not result.ok

    def test_empty_items_reports(self, material):
        result = repair_anchors(material, {"items": []})
        assert not result.ok


class TestEvidenceSearch:
    def test_narrator_turns_are_never_eligible(self, material):
        turns = _turns(material)
        narrator_text = turns[0]["text"][:40]
        assert find_evidence_turns(turns, narrator_text) == []

    def test_search_is_case_insensitive(self, material, blueprint):
        turns = _turns(material)
        evidence = blueprint["items"][0]["evidence"]
        assert find_evidence_turns(turns, evidence.upper()) == find_evidence_turns(turns, evidence)

    def test_empty_evidence_matches_nothing(self, material):
        assert find_evidence_turns(_turns(material), "   ") == []
