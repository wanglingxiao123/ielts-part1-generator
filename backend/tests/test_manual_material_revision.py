from __future__ import annotations

from types import SimpleNamespace

import pytest

from audio_storage.object_store import InMemoryObjectStore
from backend.orchestration import manual_material_revision as subject
from backend.orchestration.slot_store import SlotStore
from backend.steps.agent_steps import GenOutput


async def collect(stream):
    return [event async for event in stream]


class Validation:
    ok = True
    errors = []
    metrics = {
        "blueprint_schema_version": 2,
        "qr027_numeric_answers": 2,
        "qr027_spelled_answers": 8,
        "qr027_largest_category": 2,
    }

    def as_dict(self):
        return {"ok": True, "errors": [], "metrics": self.metrics}


class CrossCheck:
    hard_defects = []
    ambiguous = []

    def as_dict(self):
        return {"ok": True}


class QuestionCandidate:
    def __init__(self, package):
        self.package = package

    def as_dict(self):
        return {"package": self.package, "validation": {"ok": True}}


class Metrics:
    def audit_metrics(self):
        return {"word_count": 120}


class MetricsRunner:
    closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_material_revision_stores_one_complete_assessment_version(monkeypatch):
    material = {"material_id": "mat-1", "turns": [{"speaker": "A", "text": "Old"}]}
    revised_material = {
        "material_id": "mat-1",
        "turns": [{"speaker": "A", "text": "New"}],
    }
    blueprint = {"items": [{"number": 1, "target": "Old"}]}
    revised_blueprint = {"items": [{"number": 1, "target": "New"}]}
    package = {"material_id": "mat-1", "question_face": {"questions": []}}
    revised_package = {
        "material_id": "mat-1",
        "question_face": {"questions": [{"number": 1}]},
    }
    runner = MetricsRunner()

    async def revise(*_args):
        return GenOutput(revised_material, revised_blueprint)

    async def validate(*_args):
        return Validation()

    async def metrics(*_args):
        return Metrics()

    async def audit(*_args):
        return {
            "verdict": "PASS",
            "score": {"total": 90},
            "findings": [],
            "warnings": [],
        }

    async def feasibility(*_args):
        return {"feasible": True, "category_semantics_ok": True, "reasons": []}

    async def questions(*_args, **_kwargs):
        return SimpleNamespace(
            ok=True,
            candidate=QuestionCandidate(revised_package),
            advisories=[],
            blockers=[],
            detail=None,
        )

    monkeypatch.setattr(subject.agent_steps, "revise_material_from_comments", revise)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject, "run_metrics_remote", metrics)
    monkeypatch.setattr(subject.agent_steps, "audit_blind", audit)
    monkeypatch.setattr(subject, "crosscheck", lambda *_args: CrossCheck())
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", feasibility)
    monkeypatch.setattr(subject, "run_questions", questions)
    monkeypatch.setattr(subject, "_build_metrics_runner", lambda *_args: runner)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.revise_material_from_comments(
        store=store,
        material_id="mat-1",
        request_id="revision-1",
        source_request_id="classification-1",
        base_version_id="original",
        material=material,
        blueprint=blueprint,
        package=package,
        comments=[{
            "id": "comment-1",
            "anchor": {"type": "question", "index": 1},
            "text": "Change the listening material.",
        }],
        actor="reviewer",
    ))

    record = store.load_question_revision("mat-1", "revision-1")
    assert events[-1]["type"] == "question_revision_completed", record.get("message")
    version = store.load_question_version("mat-1", "revision-1")
    assert version["operation"] == "revise_material"
    assert version["material"] == revised_material
    assert version["blueprint"] == revised_blueprint
    assert version["package"] == revised_package
    assert version["audio"] == {
        "status": "needs_synthesis",
        "version_key": "mat-1/revision-1",
    }
    assert version["material_sha256"]
    assert runner.closed is True


@pytest.mark.asyncio
async def test_material_revision_failure_writes_no_version(monkeypatch):
    async def revise(*_args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(subject.agent_steps, "revise_material_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_material_from_comments(
        store=store,
        material_id="mat-1",
        request_id="revision-failed",
        source_request_id="classification-1",
        base_version_id="original",
        material={"material_id": "mat-1", "turns": []},
        blueprint={"items": []},
        package={"question_face": {}},
        comments=[{
            "id": "comment-1",
            "anchor": {"type": "question", "index": 1},
            "text": "Change the material.",
        }],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_failed"
    assert store.load_question_version("mat-1", "revision-failed") is None
    assert store.load_question_revision(
        "mat-1", "revision-failed")["status"] == "failed"
