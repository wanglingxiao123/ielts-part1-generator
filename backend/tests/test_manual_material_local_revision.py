from __future__ import annotations

import pytest

from backend.orchestration.manual_material_local_revision import (
    _apply_confirmed_updates,
    _blueprint_immutable_surface,
    _local_question_gate,
    _relax_local_confirmation_density,
    material_turns,
    project_local_candidate,
)
from backend.deterministic.validate import ValidationResult


def test_projection_keeps_only_anchored_turn_text():
    base = {
        "material_id": "mat-1",
        "scenario": "booking",
        "turns": [
            {"speaker": "speaker2", "text": "Hello."},
            {"speaker": "speaker3", "text": "The room is fifty pounds."},
        ],
    }
    candidate = {
        "material_id": "changed",
        "scenario": "rewritten",
        "turns": [
            {"speaker": "speaker3", "text": "Changed outside scope."},
            {"speaker": "speaker2", "text": "The room costs fifty pounds."},
        ],
    }

    projected = project_local_candidate(base, candidate, 1)

    assert projected["material_id"] == "mat-1"
    assert projected["scenario"] == "booking"
    assert projected["turns"] == [
        {"speaker": "speaker2", "text": "Hello."},
        {"speaker": "speaker3", "text": "The room costs fifty pounds."},
    ]


@pytest.mark.parametrize("turns", [[], [{"speaker": "speaker2", "text": "Only one"}]])
def test_projection_rejects_turn_structure_changes(turns):
    base = {"turns": [
        {"speaker": "speaker2", "text": "One"},
        {"speaker": "speaker3", "text": "Two"},
    ]}

    with pytest.raises(ValueError, match="turn structure"):
        project_local_candidate(base, {"turns": turns}, 1)


def test_projection_supports_the_stored_listening_material_shape():
    base = {
        "content_kind": "listening_material",
        "listening_material_parts": [{
            "script": {"turns": [
                {"speaker": "speaker2", "text": "One"},
                {"speaker": "speaker3", "text": "Original wording."},
            ]},
        }],
    }
    candidate = {
        "content_kind": "listening_material",
        "listening_material_parts": [{
            "script": {"turns": [
                {"speaker": "speaker3", "text": "Outside scope."},
                {"speaker": "speaker2", "text": "Shorter wording."},
            ]},
        }],
    }

    projected = project_local_candidate(base, candidate, 1)

    assert material_turns(projected) == [
        {"speaker": "speaker2", "text": "One"},
        {"speaker": "speaker3", "text": "Shorter wording."},
    ]


def test_blueprint_immutable_surface_allows_only_derived_evidence_fields():
    base = {
        "items": [{
            "number": 1,
            "target": "Tuesday",
            "evidence": "Tuesday works.",
            "confirmed": True,
        }],
    }
    derived_only = {
        "items": [{
            "number": 1,
            "target": "Tuesday",
            "evidence": "Tuesday is best.",
            "confirmed": False,
        }],
    }
    changed_target = {
        "items": [{
            "number": 1,
            "target": "Thursday",
            "evidence": "Thursday is best.",
            "confirmed": False,
        }],
    }

    assert _blueprint_immutable_surface(base) == _blueprint_immutable_surface(derived_only)
    assert _blueprint_immutable_surface(base) != _blueprint_immutable_surface(changed_target)


def test_confirmed_update_allows_removing_one_redundant_confirmation():
    blueprint = {
        "items": [
            {"number": 8, "target": "breakfast", "confirmed": False},
            {"number": 9, "target": "456982", "confirmed": True},
            {"number": 10, "target": "HV62K", "confirmed": True},
        ],
    }

    revised, changed = _apply_confirmed_updates(
        blueprint, [{"number": 9, "confirmed": False}])

    assert changed == [9]
    assert blueprint["items"][1]["confirmed"] is True
    assert revised["items"][1]["confirmed"] is False
    assert revised["items"][2]["confirmed"] is True


@pytest.mark.parametrize(
    "updates",
    [
        [{"number": 9, "confirmed": True}],
        [{"number": 8, "confirmed": False}],
        [{"number": 99, "confirmed": False}],
        [{"number": 9, "confirmed": False}, {"number": 9, "confirmed": False}],
    ],
)
def test_confirmed_update_rejects_expansion_or_invalid_targets(updates):
    blueprint = {
        "items": [
            {"number": 8, "target": "breakfast", "confirmed": False},
            {"number": 9, "target": "456982", "confirmed": True},
        ],
    }

    with pytest.raises(ValueError, match="true to false"):
        _apply_confirmed_updates(blueprint, updates)


def test_local_revision_downgrades_only_confirmation_density_to_warning():
    validation = ValidationResult(
        errors=[
            "blueprint must mark at least 3 confirmed items; found 2",
            "at least one spelled-name item must be confirmed; "
            "these are the easiest to mishear under once-only listening",
        ],
        warnings=[],
        metrics={},
    )

    _relax_local_confirmation_density(validation)

    assert validation.errors == [
        "at least one spelled-name item must be confirmed; "
        "these are the easiest to mishear under once-only listening",
    ]
    assert validation.warnings == [
        "manual local edit accepted with advisory: "
        "blueprint must mark at least 3 confirmed items; found 2",
    ]


def test_local_revision_can_pass_with_two_confirmed_items():
    validation = ValidationResult(
        errors=["blueprint must mark at least 3 confirmed items; found 2"],
        warnings=["dialogue words outside preferred 600-650: 469"],
        metrics={},
    )

    _relax_local_confirmation_density(validation)

    assert validation.ok is True
    assert validation.errors == []
    assert len(validation.warnings) == 2


def test_local_question_gate_downgrades_unrelated_audit_variance():
    class Candidate:
        pass

    candidate = Candidate()
    candidate.counts = {}
    candidate.validation = ValidationResult([], [], {})
    candidate.cross_check = type("Cross", (), {
        "hard_defects": [{"outcome": "anchor_divergence", "number": 4}],
        "leakage": [],
        "equally_supported_rivals": [],
        "needs_review": [],
        "consistency": {"computed": {"reviewed_question_ids": list(range(1, 11))}},
        "compared": 10,
        "agreed": 9,
    })()

    blockers, advisories = _local_question_gate(candidate, {10})

    assert blockers == []
    assert advisories == [
        "基础版本未受本次 Turn 修改影响的 Q4 在复审中出现波动："
        "cross-check anchor_divergence on Q4"
    ]


def test_local_question_gate_keeps_edited_question_defect_blocking():
    class Candidate:
        pass

    candidate = Candidate()
    candidate.counts = {}
    candidate.validation = ValidationResult([], [], {})
    candidate.cross_check = type("Cross", (), {
        "hard_defects": [{"outcome": "answer_divergence", "number": 10}],
        "leakage": [],
        "equally_supported_rivals": [],
        "needs_review": [],
        "consistency": {"computed": {"reviewed_question_ids": list(range(1, 11))}},
        "compared": 10,
        "agreed": 9,
    })()

    blockers, advisories = _local_question_gate(candidate, {10})

    assert blockers == ["cross-check answer_divergence on Q10"]
    assert advisories == []
