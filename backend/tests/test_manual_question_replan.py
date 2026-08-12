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
async def test_invalid_blueprint_exhaustion_escalates_to_material_revision(monkeypatch):
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

    assert events[-1]["type"] == "question_revision_needs_material"
    assert "ten valid points" in events[-1]["reasons"][0]["reason"]
    assert store.load_question_version("mat-1", "replan-1") is None
    assert store.load_question_revision("mat-1", "replan-1")["status"] == \
        "needs_material_revision"


@pytest.mark.asyncio
async def test_undeliverable_full_question_set_escalates_to_material_revision(monkeypatch):
    async def replan(*_args):
        return GenOutput(
            {"turns": []},
            {"blueprint_schema_version": 2, "items": [{"number": 2}]},
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
            blockers=["Q4 still has two defensible answers"],
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
        blueprint={"blueprint_schema_version": 2, "items": [{"number": 1}]},
        package={"question_face": {}},
        comments=[comment()],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_needs_material"
    assert "two defensible answers" in events[-1]["reasons"][0]["reason"]
