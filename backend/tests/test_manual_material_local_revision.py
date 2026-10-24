from __future__ import annotations

import pytest

from backend.orchestration.manual_material_local_revision import (
    _apply_confirmed_updates,
    _base_question_quality,
    _blueprint_immutable_surface,
    _local_question_gate,
    _local_material_gate,
    _material_audit_passed,
    _question_quality_for_storage,
    _relax_local_confirmation_density,
    material_turns,
    project_local_candidate,
)
from backend.deterministic.validate import ValidationResult
from backend.deterministic.crosscheck import CrossCheckResult


@pytest.mark.parametrize("patch", [
    {"verdict": "FAIL"}, {"assessable": False},
    {"findings": [{"severity": "major"}]},
    {"findings": [{"severity": "critical"}]},
    {"findings": [{"severity": "unknown"}]},
])
def test_local_baseline_comparison_never_overrides_a_failed_material_audit(patch):
    audit = {"assessable": True, "verdict": "PASS", "findings": []}
    assert _material_audit_passed(audit)
    assert not _material_audit_passed({**audit, **patch})


def test_local_material_reconciles_only_proven_unchanged_baseline_details():
    original = {"turns": [
        {"text": "The kettle cost fifty-six pounds."},
        {"text": "It goes back to the card you paid with."},
        {"text": "No, that's all very clear. Thank you so much for your help."},
    ]}
    projected = {"turns": [
        *original["turns"][:2], {"text": "That's clear. Thanks for your help."},
    ]}
    extras = [
        {"type": "price", "turn_index": 0, "evidence": "The kettle cost fifty-six pounds."},
        {"type": "option", "turn_index": 1, "evidence": "It goes back to the card you paid with"},
    ]
    check = CrossCheckResult({"unintended_target": extras})
    baseline = CrossCheckResult({"planned": 10, "matched": 10, "unintended_target": []})
    blockers, advisories = _local_material_gate(check, baseline, original, projected, 2)
    assert blockers == []
    assert len(advisories) == 2
    assert check.hard_defects == extras  # Preserve the original report, never rewrite it to PASS.

    # No baseline evidence, or a missing target, must still fail.
    assert len(_local_material_gate(check, None, original, projected, 2)[0]) == 2
    check.unrecoverable = [{"number": 4, "turn_index": 0}]
    assert "Q4" in _local_material_gate(check, baseline, original, projected, 2)[0][0]


@pytest.mark.parametrize("change", ["edited_turn", "changed_source", "unsupported_quote",
                                   "short_quote", "unknown_anchor", "incomplete_baseline"])
def test_local_material_does_not_hide_new_or_unproven_defects(change):
    import copy
    original = {"turns": [{"text": "The price is fifty pounds. Delivery is four pounds."},
                           {"text": "Thank you."}]}
    projected = copy.deepcopy(original)
    projected["turns"][1]["text"] = "Thanks."
    old = {"type": "price", "turn_index": 0, "evidence": "The price is fifty pounds."}
    new = dict(old)
    turn_index = 1
    if change == "edited_turn":
        turn_index = 0
    elif change == "changed_source":
        projected["turns"][0]["text"] += " It is free today."
    elif change == "unsupported_quote":
        new["evidence"] = "The price is nine pounds."
    elif change == "short_quote":
        new["evidence"] = "price"
    elif change == "unknown_anchor":
        new["turn_index"] = None
    blockers, advisories = _local_material_gate(
        CrossCheckResult({"unintended_target": [new]}),
        CrossCheckResult({"planned": 10, "matched": 9 if change == "incomplete_baseline" else 10,
                          "unintended_target": [old]}),
        original, projected, turn_index)
    assert len(blockers) == 1
    assert advisories == []


@pytest.mark.asyncio
async def test_failed_local_audit_persists_diagnostics_without_creating_version(monkeypatch):
    from audio_storage.object_store import InMemoryObjectStore
    from backend.orchestration.slot_store import SlotStore
    from backend.orchestration import manual_material_local_revision as subject
    base = {"turns": [{"speaker": "speaker2", "text": "Thank you."}]}
    changed = {"turns": [{"speaker": "speaker2", "text": "Thanks."}]}
    async def revise(*args):
        return {"outcome": "local_material_edit", "material": changed, "reason": "Shorten."}
    async def validate(*args):
        return ValidationResult([], [], {})
    async def audit(*args):
        return {"request_satisfied": False, "scope_preserved": True,
                "dependencies_consistent": False,
                "blocking_findings": [{"edit_index": 0, "message": "new contradiction"}], "notes": []}
    monkeypatch.setattr(subject.agent_steps, "revise_material_turn", revise)
    from backend.steps import assessment_edit
    monkeypatch.setattr(assessment_edit, "review", audit)
    monkeypatch.setattr(subject, "validate_questions", validate)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject, "crosscheck", lambda *args: CrossCheckResult({
        "planned": 1, "matched": 0,
        "unrecoverable": [{"number": 1, "turn_index": 0}],
    }))
    store = SlotStore(InMemoryObjectStore())
    events = [event async for event in subject.revise_material_local(
        store=store, material_id="fixture", request_id="request",
        base_version_id="original", material=base, blueprint={"items": []},
        package={}, base_version={},
        comments=[{"id": "c", "anchor": {"index": 0}}], actor="test",
    )]
    assert events[-1]["type"] == "question_revision_failed"
    request = store.load_question_revision("fixture", "request")
    assert request["material_diagnostics"]["impact_review"]["blocking_findings"][0]["message"] == "new contradiction"
    assert request["local_patch"]["before"] == "Thank you."
    assert request["local_patch"]["after"] == "Thanks."
    assert store.load_question_version("fixture", "request") is None


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


def test_base_question_quality_reads_nested_assessment_quality():
    questions = {"status": "PASS", "cross_check": {"ok": True}}

    assert _base_question_quality({
        "quality": {"material": {"validation": {"ok": True}}, "questions": questions}
    }) == questions


def test_base_question_quality_supports_legacy_flat_quality():
    quality = {"status": "PASS", "cross_check": {"ok": True}}

    assert _base_question_quality({"quality": quality}) == quality


def test_unchanged_package_uses_clean_base_quality_after_unrelated_audit_variance():
    candidate = {"status": "FAIL", "cross_check": {"ok": False}}
    baseline = {"status": "PASS", "cross_check": {"ok": True}}

    stored = _question_quality_for_storage(
        candidate_quality=candidate,
        base_version={"quality": {"questions": baseline}},
        package_changed=False,
        baseline_advisories=["Q4 audit variance"],
    )

    assert stored == baseline
    assert stored is not baseline


def test_changed_package_never_falls_back_to_base_quality():
    candidate = {"status": "PASS", "cross_check": {"ok": True, "agreed": 10}}

    assert _question_quality_for_storage(
        candidate_quality=candidate,
        base_version={"quality": {"questions": {"status": "PASS"}}},
        package_changed=True,
        baseline_advisories=["Q4 audit variance"],
    ) is candidate
