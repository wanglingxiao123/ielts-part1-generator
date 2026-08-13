from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from audio_storage.object_store import InMemoryObjectStore
from backend.orchestration import manual_question_replan as subject
from backend.orchestration.slot_store import SlotStore
from backend.steps.agent_steps import GenOutput


async def collect(stream):
    return [event async for event in stream]


def comment():
    return {
        "id": "comment-1",
        "anchor": {"type": "question", "index": 3},
        "text": "Change the form group to notes.",
    }


class Validation:
    def __init__(self, errors=None):
        self.errors = errors or []
        self.warnings = []
        self.metrics = {
            "blueprint_schema_version": 2,
            "qr027_numeric_answers": 2,
            "qr027_spelled_answers": 8,
            "qr027_largest_category": 2,
        }

    @property
    def ok(self):
        return not self.errors

    def as_dict(self):
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": [],
            "metrics": self.metrics,
        }


class Candidate:
    def __init__(self, package):
        self.package = package

    def as_dict(self):
        return {
            "package": self.package,
            "review": {"question_qc_status": "PASS"},
            "cross_check": {"agreed": 10},
            "validation": {"ok": True, "errors": []},
        }


@pytest.mark.asyncio
async def test_replan_stores_blueprint_snapshot_and_never_changes_material(monkeypatch):
    material = {"material_id": "mat-1", "turns": [{"speaker": "A", "text": "hello"}]}
    material_before = copy.deepcopy(material)
    old_blueprint = {"blueprint_schema_version": 2, "items": [{"number": 1}]}
    new_blueprint = {"blueprint_schema_version": 2, "items": [{"number": 2}]}
    old_package = {"material_id": "mat-1", "question_face": {"questions": []}}
    new_package = {"material_id": "mat-1", "question_face": {"questions": [{"number": 1}]}}

    async def replan(*_args):
        return GenOutput(copy.deepcopy(material), copy.deepcopy(new_blueprint))

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        return {
            "feasible": True,
            "reasons": ["all ten points are supported"],
            "category_semantics_ok": True,
        }

    async def run_questions(_material, blueprint, emit):
        assert blueprint == new_blueprint
        await emit("question_cross_check", {})
        return SimpleNamespace(
            ok=True,
            candidate=Candidate(new_package),
            advisories=[],
            blockers=[],
            detail=None,
        )

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    monkeypatch.setattr(subject, "run_questions", run_questions)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-1",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint=old_blueprint,
        package=old_package,
        comments=[comment()],
        actor="reviewer",
    ))

    assert material == material_before
    assert events[-1]["type"] == "question_revision_completed"
    assert "question_revision_validating" in [event["type"] for event in events]
    assert "question_revision_auditing" in [event["type"] for event in events]
    version = store.load_question_version("mat-1", "replan-1")
    assert version["blueprint"] == new_blueprint
    assert version["package"] == new_package
    assert version["based_on_version_id"] == "original"
    assert version["changed_questions"] == list(range(1, 11))
    record = store.load_question_revision("mat-1", "replan-1")
    assert record["operation"] == "replan_questions"
    assert record["source_request_id"] == "classify-1"
    assert record["status"] == "completed"


@pytest.mark.asyncio
async def test_category_semantics_rejection_replans_with_feedback(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "Take bus route 62."}]}
    old_blueprint = {"blueprint_schema_version": 2, "items": [{"number": 1}]}
    planned_blueprints = [
        {"blueprint_schema_version": 2, "items": [
            {"number": 2, "target_answer": "62", "answer_category": "quantity"},
        ]},
        {"blueprint_schema_version": 2, "items": [
            {"number": 3, "target_answer": "62", "answer_category": "service"},
        ]},
    ]
    planner_feedback = []
    feasibility_calls = 0

    async def replan(_material, _blueprint, _comments, feedback):
        planner_feedback.append(feedback)
        return GenOutput(
            copy.deepcopy(material),
            copy.deepcopy(planned_blueprints[len(planner_feedback) - 1]),
        )

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        nonlocal feasibility_calls
        feasibility_calls += 1
        if feasibility_calls == 1:
            return {
                "feasible": False,
                "reasons": [
                    "Item 8: `62` identifies the operating bus route, not a quantity."
                ],
                "category_semantics_ok": False,
            }
        return {
            "feasible": True,
            "reasons": ["all ten points are supported"],
            "category_semantics_ok": True,
        }

    async def run_questions(*_args, **_kwargs):
        return SimpleNamespace(
            ok=True,
            candidate=Candidate({"question_face": {"questions": []}}),
            advisories=[],
            blockers=[],
            detail=None,
        )

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    monkeypatch.setattr(subject, "run_questions", run_questions)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-semantic-retry",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint=old_blueprint,
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_completed"
    assert feasibility_calls == 2
    assert planner_feedback[0] is None
    assert "bus route" in " ".join(planner_feedback[1])
    assert [event["type"] for event in events].count(
        "question_revision_feasibility") == 2
    version = store.load_question_version("mat-1", "replan-semantic-retry")
    assert version["blueprint"] == planned_blueprints[1]


@pytest.mark.asyncio
async def test_layout_only_replans_when_an_information_point_changes(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "Take bus route 62."}]}
    old_blueprint = {
        "blueprint_schema_version": 2,
        "items": [{
            "number": 1,
            "target": "62",
            "evidence": "bus route 62",
            "turn_index": 0,
            "item_form": "form",
        }],
    }
    invalid = copy.deepcopy(old_blueprint)
    invalid["items"][0].update({"target": "Bus route 62", "item_form": "note"})
    corrected = copy.deepcopy(old_blueprint)
    corrected["items"][0]["item_form"] = "note"
    planner_feedback = []

    async def replan(_material, _blueprint, _comments, feedback):
        planner_feedback.append(feedback)
        blueprint = invalid if len(planner_feedback) == 1 else corrected
        return GenOutput(copy.deepcopy(material), copy.deepcopy(blueprint))

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        return {
            "feasible": True,
            "reasons": ["the unchanged point supports the Note layout"],
            "category_semantics_ok": True,
        }

    async def run_questions(*_args, **_kwargs):
        return SimpleNamespace(
            ok=True,
            candidate=Candidate({"question_face": {"questions": []}}),
            advisories=[],
            blockers=[],
            detail=None,
        )

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    monkeypatch.setattr(subject, "run_questions", run_questions)
    store = SlotStore(InMemoryObjectStore())
    scoped_comment = dict(comment(), replan_scope="layout_only")

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-layout-only",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint=old_blueprint,
        package={"question_face": {}},
        comments=[scoped_comment],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_completed"
    assert "changed Q1" in " ".join(planner_feedback[1])
    version = store.load_question_version("mat-1", "replan-layout-only")
    assert version["blueprint"]["items"][0]["target"] == "62"
    assert version["blueprint"]["items"][0]["evidence"] == "bus route 62"
    assert version["blueprint"]["items"][0]["turn_index"] == 0
    assert version["blueprint"]["items"][0]["item_form"] == "note"


@pytest.mark.asyncio
async def test_layout_only_scope_exhaustion_is_failed(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "Take bus route 62."}]}
    old_blueprint = {
        "blueprint_schema_version": 2,
        "items": [{
            "number": 1,
            "target": "62",
            "evidence": "bus route 62",
            "turn_index": 0,
        }],
    }

    async def replan(*_args):
        changed = copy.deepcopy(old_blueprint)
        changed["items"][0]["target"] = "Bus route 62"
        return GenOutput(copy.deepcopy(material), changed)

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-layout-scope-exhausted",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint=old_blueprint,
        package={"question_face": {}},
        comments=[dict(comment(), replan_scope="layout_only")],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_failed"
    assert not any(
        event["type"] == "question_revision_needs_material" for event in events
    )
    record = store.load_question_revision(
        "mat-1", "replan-layout-scope-exhausted")
    assert record["status"] == "failed"
    assert "layout-only boundary" in record["message"]


@pytest.mark.asyncio
async def test_category_semantics_exhaustion_is_failed_not_needs_material(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "Take bus route 62."}]}
    planner_feedback = []

    async def replan(_material, _blueprint, _comments, feedback):
        planner_feedback.append(feedback)
        return GenOutput(
            copy.deepcopy(material),
            {
                "blueprint_schema_version": 2,
                "items": [{"number": len(planner_feedback) + 1}],
            },
        )

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        return {
            "feasible": False,
            "reasons": [
                "Item 8: `62` identifies the operating bus route, not a quantity."
            ],
            "category_semantics_ok": False,
        }

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-semantic-exhausted",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert len(planner_feedback) == subject.MAX_PLAN_ATTEMPTS
    assert all(
        "bus route" in " ".join(feedback)
        for feedback in planner_feedback[1:]
    )
    assert events[-1]["type"] == "question_revision_failed"
    assert not any(
        event["type"] == "question_revision_needs_material" for event in events
    )
    assert store.load_question_version(
        "mat-1", "replan-semantic-exhausted") is None
    record = store.load_question_revision(
        "mat-1", "replan-semantic-exhausted")
    assert record["status"] == "failed"
    assert "category semantics could not be corrected" in record["message"]


@pytest.mark.asyncio
async def test_category_semantics_exhaustion_after_validation_failure_is_failed(
    monkeypatch,
):
    material = {"turns": [{"speaker": "A", "text": "Take bus route 62."}]}
    planner_feedback = []
    validation_calls = 0

    async def replan(_material, _blueprint, _comments, feedback):
        planner_feedback.append(feedback)
        return GenOutput(
            copy.deepcopy(material),
            {
                "blueprint_schema_version": 2,
                "items": [{"number": len(planner_feedback) + 1}],
            },
        )

    async def validate(*_args):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            return Validation(["The first replacement blueprint is malformed."])
        return Validation()

    async def feasibility(*_args):
        return {
            "feasible": True,
            "reasons": [
                "Item 8: `62` identifies the operating bus route, not a quantity."
            ],
            "category_semantics_ok": False,
        }

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-semantic-after-validation",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert len(planner_feedback) == subject.MAX_PLAN_ATTEMPTS
    assert "malformed" in " ".join(planner_feedback[1])
    assert "bus route" in " ".join(planner_feedback[2])
    assert events[-1]["type"] == "question_revision_failed"
    assert not any(
        event["type"] == "question_revision_needs_material" for event in events
    )
    record = store.load_question_revision(
        "mat-1", "replan-semantic-after-validation")
    assert record["status"] == "failed"
    assert "category semantics could not be corrected" in record["message"]


@pytest.mark.asyncio
async def test_material_infeasibility_replans_before_needs_material(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "Only nine usable details."}]}
    planner_feedback = []

    async def replan(_material, _blueprint, _comments, feedback):
        planner_feedback.append(feedback)
        return GenOutput(
            copy.deepcopy(material),
            {
                "blueprint_schema_version": 2,
                "items": [{"number": len(planner_feedback) + 1}],
            },
        )

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        return {
            "feasible": False,
            "reasons": ["The selected points do not support ten unique questions."],
            "category_semantics_ok": True,
        }

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-infeasible",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[dict(comment(), replan_scope="retarget")],
        actor="reviewer",
    ))

    assert len(planner_feedback) == subject.MAX_PLAN_ATTEMPTS
    assert all(
        "ten unique questions" in " ".join(feedback)
        for feedback in planner_feedback[1:]
    )
    assert events[-1]["type"] == "question_revision_needs_material"
    record = store.load_question_revision("mat-1", "replan-infeasible")
    assert record["status"] == "needs_material_revision"
    assert record["operation"] == "replan_questions"
    assert record["source_request_id"] == "classify-1"
    assert record["base_version_id"] == "original"
    assert record["source_comments"][0]["replan_scope"] == "retarget"
    assert record["failure_phase"] == "feasibility"
    assert record["failure_code"] == "MATERIAL_INFEASIBLE"
    assert record["comment_outcomes"][0]["outcome"] == "replan_questions"
    assert record["comment_outcomes"][0]["replan_scope"] == "retarget"


def test_material_escalation_preserves_mixed_comment_outcomes():
    outcomes = subject._replan_outcomes([
        dict(comment(), id="local-fix"),
        dict(comment(), id="retarget", replan_scope="retarget"),
    ], "material is infeasible")

    assert [(row["comment_id"], row["outcome"]) for row in outcomes] == [
        ("local-fix", "question_only"),
        ("retarget", "replan_questions"),
    ]
    assert outcomes[1]["replan_scope"] == "retarget"


@pytest.mark.asyncio
async def test_undecidable_feasibility_is_failed_not_needs_material(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "hello"}]}

    async def replan(*_args):
        return GenOutput(
            copy.deepcopy(material),
            {"blueprint_schema_version": 2, "items": [{"number": 2}]},
        )

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        return None

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-undecidable",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_failed"
    assert not any(
        event["type"] == "question_revision_needs_material" for event in events
    )
    record = store.load_question_revision("mat-1", "replan-undecidable")
    assert record["status"] == "failed"
    assert "SEMANTICS_MISSING" in record["message"]


@pytest.mark.asyncio
async def test_replan_rejects_a_model_returned_material_change(monkeypatch):
    material = {"turns": [{"speaker": "A", "text": "original"}]}

    async def replan(*_args):
        return GenOutput(
            {"turns": [{"speaker": "A", "text": "changed"}]},
            {"blueprint_schema_version": 2, "items": [{"number": 2}]},
        )

    async def validate(*_args):
        raise AssertionError("a changed material must be rejected before validation")

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-changed-material",
        source_request_id="classify-1",
        base_version_id="original",
        material=material,
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_failed"
    assert store.load_question_version("mat-1", "replan-changed-material") is None
    record = store.load_question_revision("mat-1", "replan-changed-material")
    assert record["status"] == "failed"
    assert "changed the listening material" in record["message"]


@pytest.mark.asyncio
async def test_invalid_blueprint_exhaustion_is_failed_not_material_revision(monkeypatch):
    async def replan(*_args):
        return GenOutput(
            {"turns": []},
            {"blueprint_schema_version": 2, "items": [{"number": 2}]},
        )

    async def validate(*_args):
        return Validation(["blueprint does not contain ten valid points"])

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-1",
        source_request_id="classify-1",
        base_version_id="original",
        material={"turns": []},
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_failed"
    assert "ten valid points" in events[-1]["blockers"][0]
    assert store.load_question_version("mat-1", "replan-1") is None
    record = store.load_question_revision("mat-1", "replan-1")
    assert record["status"] == "failed"
    assert record["operation"] == "replan_questions"
    assert record["source_request_id"] == "classify-1"
    assert record["failure_phase"] == "validation"
    assert record["failure_code"] == "REPLAN_VALIDATION_EXHAUSTED"


@pytest.mark.asyncio
async def test_layout_only_q9_quality_failure_is_failed_not_material_revision(
    monkeypatch,
):
    current_blueprint = {
        "blueprint_schema_version": 2,
        "items": [{
            "number": 6,
            "target": "3 hours",
            "evidence": "It takes three hours.",
            "turn_index": 18,
            "item_form": "form",
        }],
    }
    replanned_blueprint = copy.deepcopy(current_blueprint)
    replanned_blueprint["items"][0]["item_form"] = "note"

    async def replan(*_args):
        return GenOutput(
            {"turns": []},
            copy.deepcopy(replanned_blueprint),
        )

    async def validate(*_args):
        return Validation()

    async def feasibility(*_args):
        return {
            "feasible": True,
            "reasons": ["supported"],
            "category_semantics_ok": True,
        }

    async def run_questions(*_args, **_kwargs):
        return SimpleNamespace(
            ok=False,
            candidate=None,
            advisories=[],
            blockers=[
                "Q9 has an open MAJOR finding AR-012 in the blind audit",
                "Q9 has an equally-supported rival answer '14 May' (AR-012)",
                "Q9's evidence anchor is one turn from the writer's and unconfirmed",
            ],
            detail=None,
        )

    monkeypatch.setattr(subject.agent_steps, "replan_blueprint", replan)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    monkeypatch.setattr(subject, "run_questions", run_questions)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.replan_from_comments(
        store=store,
        material_id="mat-1",
        request_id="replan-1",
        source_request_id="classify-1",
        base_version_id="original",
        material={"turns": []},
        blueprint=current_blueprint,
        package={"question_face": {}},
        comments=[dict(
            comment(),
            replan_scope="layout_only",
            text="Change Q6-10 from Form to Note without changing the material.",
        )],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_failed"
    assert not any(
        event["type"] == "question_revision_needs_material" for event in events
    )
    assert any("AR-012" in blocker for blocker in events[-1]["blockers"])
    record = store.load_question_revision("mat-1", "replan-1")
    assert record["status"] == "failed"
    assert record["operation"] == "replan_questions"
    assert record["source_request_id"] == "classify-1"
    assert record["source_comments"][0]["replan_scope"] == "layout_only"
    assert record["failure_phase"] == "question_generation"
    assert record["failure_code"] == "QUESTION_GENERATION_QUALITY_FAILED"
