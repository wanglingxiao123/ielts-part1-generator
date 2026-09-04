from __future__ import annotations

import pytest

from backend.orchestration.manual_material_local_revision import (
    _blueprint_immutable_surface,
    project_local_candidate,
)


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
