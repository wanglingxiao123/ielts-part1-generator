from __future__ import annotations

import asyncio
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
            "verdict": "PASS_WITH_MINOR_EDITS",
            "score": {"total": 90},
            "findings": [{"severity": "minor", "rule": "style"}],
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
    calls = []

    async def revise(*_args):
        calls.append(True)
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
    assert len(calls) == 1
    assert store.load_question_version("mat-1", "revision-failed") is None
    record = store.load_question_revision("mat-1", "revision-failed")
    assert record["status"] == "failed"
    assert record["failure_code"] == "RuntimeError"
    assert record["attempt_count"] == 1


@pytest.mark.asyncio
async def test_cross_check_failure_is_fed_to_second_attempt(monkeypatch):
    base = {"material_id": "mat-1", "turns": [{"text": "old"}]}
    candidates = [
        GenOutput({"material_id": "mat-1", "turns": [{"text": "bad"}]}, {"items": []}),
        GenOutput({"material_id": "mat-1", "turns": [{"text": "good"}]}, {"items": []}),
    ]
    feedback_seen = []

    async def revise(_material, _blueprint, _comments, feedback=None):
        feedback_seen.append(feedback)
        return candidates[len(feedback_seen) - 1]

    class DefectiveCrossCheck(CrossCheck):
        hard_defects = [{"number": 1, "outcome": "unrecoverable"}]
        unrecoverable = [{
            "number": 1, "type": "duration", "turn_index": 2, "evidence": "bad",
        }]

    checks = iter([DefectiveCrossCheck(), CrossCheck()])
    runner = MetricsRunner()
    monkeypatch.setattr(subject.agent_steps, "revise_material_from_comments", revise)
    monkeypatch.setattr(subject, "validate", lambda *_args: _async(Validation()))
    monkeypatch.setattr(subject, "run_metrics_remote", lambda *_args: _async(Metrics()))
    monkeypatch.setattr(subject.agent_steps, "audit_blind", lambda *_args: _async({
        "verdict": "PASS", "score": {"total": 90}, "findings": [], "warnings": [],
    }))
    monkeypatch.setattr(subject, "crosscheck", lambda *_args: next(checks))
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", lambda *_args: _async({
        "feasible": True, "category_semantics_ok": True, "reasons": [],
    }))
    monkeypatch.setattr(subject, "run_questions", lambda *_args, **_kwargs: _async(
        SimpleNamespace(
            ok=True, candidate=QuestionCandidate({"question_face": {"questions": []}}),
            advisories=[], blockers=[], detail=None,
        )))
    monkeypatch.setattr(subject, "_build_metrics_runner", lambda *_args: runner)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.revise_material_from_comments(
        store=store, material_id="mat-1", request_id="retry-1",
        source_request_id="source", base_version_id="original",
        material=base, blueprint={"items": []}, package={"question_face": {}},
        comments=[{"id": "c1", "anchor": {"type": "question", "index": 1}}],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_completed"
    assert feedback_seen[0] is None
    assert "unrecoverable" in feedback_seen[1]["cross_check"][0]
    version = store.load_question_version("mat-1", "retry-1")
    assert version["material"] == candidates[1].material
    assert version["attempt_count"] == 2
    assert store.load_question_revision("mat-1", "retry-1")["attempt_count"] == 2
    assert runner.closed is True


@pytest.mark.asyncio
async def test_validation_exhaustion_persists_all_blockers(monkeypatch):
    calls = []

    async def revise(_material, _blueprint, _comments, feedback=None):
        calls.append(feedback)
        number = len(calls)
        return GenOutput(
            {"material_id": "mat-1", "turns": [{"text": "candidate-%d" % number}]},
            {"items": [{"number": number}]},
        )

    class InvalidValidation(Validation):
        ok = False
        errors = ["V-1", "V-2"]

        def as_dict(self):
            return {"ok": False, "errors": self.errors, "metrics": self.metrics}

    monkeypatch.setattr(subject.agent_steps, "revise_material_from_comments", revise)
    monkeypatch.setattr(subject, "validate", lambda *_args: _async(InvalidValidation()))
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_material_from_comments(
        store=store, material_id="mat-1", request_id="retry-exhausted",
        source_request_id="source", base_version_id="original",
        material={"material_id": "mat-1", "turns": []},
        blueprint={"items": []}, package={"question_face": {}},
        comments=[{"id": "c1", "anchor": {"type": "question", "index": 1}}],
        actor="reviewer",
    ))

    assert len(calls) == 3
    assert calls[1]["validation"] == ["V-1", "V-2"]
    assert calls[2]["validation"] == ["V-1", "V-2"]
    record = store.load_question_revision("mat-1", "retry-exhausted")
    assert record["failure_phase"] == "validating_material"
    assert record["failure_code"] == "MATERIAL_VALIDATION_EXHAUSTED"
    assert record["blockers"] == ["V-1", "V-2"]
    assert record["attempt_count"] == 3
    assert len(record["attempts"]) == 3
    assert events[-1]["blockers"] == ["V-1", "V-2"]
    assert store.load_question_version("mat-1", "retry-exhausted") is None


@pytest.mark.asyncio
async def test_changed_material_id_is_not_retried(monkeypatch):
    calls = []

    async def revise(*_args):
        calls.append(True)
        raise subject.agent_steps.ModelCallError(
            "material revision changed the logical material_id")

    monkeypatch.setattr(subject.agent_steps, "revise_material_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())

    events = await collect(subject.revise_material_from_comments(
        store=store, material_id="mat-1", request_id="changed-id",
        source_request_id="source", base_version_id="original",
        material={"material_id": "mat-1", "turns": []},
        blueprint={"items": []}, package={"question_face": {}},
        comments=[{"id": "c1", "anchor": {"type": "question", "index": 1}}],
        actor="reviewer",
    ))

    assert len(calls) == 1
    assert events[-1]["type"] == "question_revision_failed"
    record = store.load_question_revision("mat-1", "changed-id")
    assert record["failure_code"] == "ModelCallError"
    assert record["attempt_count"] == 1
    assert store.load_question_version("mat-1", "changed-id") is None


@pytest.mark.asyncio
async def test_question_failure_without_regenerate_outcome_is_not_retried(monkeypatch):
    calls = []
    runner = MetricsRunner()

    async def revise(*_args):
        calls.append(True)
        return GenOutput(
            {"material_id": "mat-1", "turns": [{"text": "candidate"}]},
            {"items": []},
        )

    monkeypatch.setattr(subject.agent_steps, "revise_material_from_comments", revise)
    monkeypatch.setattr(subject, "validate", lambda *_args: _async(Validation()))
    monkeypatch.setattr(subject, "run_metrics_remote", lambda *_args: _async(Metrics()))
    monkeypatch.setattr(subject.agent_steps, "audit_blind", lambda *_args: _async({
        "verdict": "PASS", "score": {"total": 90}, "findings": [], "warnings": [],
    }))
    monkeypatch.setattr(subject, "crosscheck", lambda *_args: CrossCheck())
    monkeypatch.setattr(subject.agent_steps, "feasibility_audit", lambda *_args: _async({
        "feasible": True, "category_semantics_ok": True, "reasons": [],
    }))
    monkeypatch.setattr(subject, "run_questions", lambda *_args, **_kwargs: _async(
        SimpleNamespace(
            ok=False, candidate=None, advisories=[],
            blockers=["question service failed"], detail=None, outcome=None,
        )))
    monkeypatch.setattr(subject, "_build_metrics_runner", lambda *_args: runner)
    store = SlotStore(InMemoryObjectStore())

    await collect(subject.revise_material_from_comments(
        store=store, material_id="mat-1", request_id="question-system-failure",
        source_request_id="source", base_version_id="original",
        material={"material_id": "mat-1", "turns": []},
        blueprint={"items": []}, package={"question_face": {}},
        comments=[{"id": "c1", "anchor": {"type": "question", "index": 1}}],
        actor="reviewer",
    ))

    assert len(calls) == 1
    record = store.load_question_revision("mat-1", "question-system-failure")
    assert record["failure_code"] == "MATERIAL_QUESTION_QUALITY_EXHAUSTED"
    assert record["attempt_count"] == 1
    assert runner.closed is True


@pytest.mark.asyncio
async def test_cancellation_closes_metrics_without_writing_false_failure(monkeypatch):
    runner = MetricsRunner()
    audit_started = asyncio.Event()

    async def audit(*_args):
        audit_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        subject.agent_steps,
        "revise_material_from_comments",
        lambda *_args: _async(GenOutput(
            {"material_id": "mat-1", "turns": [{"text": "candidate"}]},
            {"items": []},
        )),
    )
    monkeypatch.setattr(subject, "validate", lambda *_args: _async(Validation()))
    monkeypatch.setattr(subject, "run_metrics_remote", lambda *_args: _async(Metrics()))
    monkeypatch.setattr(subject.agent_steps, "audit_blind", audit)
    monkeypatch.setattr(subject, "_build_metrics_runner", lambda *_args: runner)
    store = SlotStore(InMemoryObjectStore())
    stream = subject.revise_material_from_comments(
        store=store, material_id="mat-1", request_id="cancelled",
        source_request_id="source", base_version_id="original",
        material={"material_id": "mat-1", "turns": []},
        blueprint={"items": []}, package={"question_face": {}},
        comments=[{"id": "c1", "anchor": {"type": "question", "index": 1}}],
        actor="reviewer",
    )

    task = asyncio.create_task(collect(stream))
    await audit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.closed is True
    record = store.load_question_revision("mat-1", "cancelled")
    assert record["status"] == "running"
    assert record.get("failure_code") is None
    assert store.load_question_version("mat-1", "cancelled") is None


async def _async(value):
    return value
