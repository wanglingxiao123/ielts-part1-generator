from __future__ import annotations

import pytest

from audio_storage.object_store import InMemoryObjectStore
from backend.orchestration import manual_question_revision as subject
from backend.orchestration.slot_store import SlotPersistenceError, SlotStore


async def collect(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_material_required_outcome_stores_no_version(monkeypatch):
    async def revise(*_args):
        return {
            "outcome": "needs_material_revision",
            "reasons": [{"comment_id": "c1", "question_number": 2,
                         "reason": "the recording states two equal answers"}],
        }

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    backing = InMemoryObjectStore()
    store = SlotStore(backing)
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert events[-1]["type"] == "question_revision_needs_material"
    assert store.load_question_version("mat-1", "req-1") is None
    request = store._read("_question_revisions/mat-1/req-1.json")
    assert request["status"] == "needs_material_revision"


@pytest.mark.asyncio
async def test_agent_failure_stores_failed_request_without_version(monkeypatch):
    async def revise(*_args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert events[-1]["type"] == "question_revision_failed"
    assert store.load_question_version("mat-1", "req-1") is None
    assert store._read("_question_revisions/mat-1/req-1.json")["status"] == "failed"


@pytest.mark.asyncio
async def test_only_a_fully_checked_package_becomes_an_immutable_version(monkeypatch):
    package = {"material_id": "mat-1", "question_face": {"questions": []},
               "answer_key": [], "evidence": []}

    async def revise(*_args):
        return {"outcome": "revised", "package": package}

    class Validation:
        errors = []
        warnings = []

        def as_dict(self):
            return {"ok": True, "errors": [], "warnings": [], "metrics": {}}

    class Cross:
        consistency = {"computed": {"question_qc_status": "PASS", "counts": {
            "CRITICAL": 0, "MAJOR": 0, "MINOR": 0,
        }}}
        hard_defects = []
        needs_review = []
        leakage = []
        equally_supported_rivals = []
        compared = agreed = 10

        def as_dict(self):
            return {"ok": True, "compared": 10, "agreed": 10}

    async def validate(*_args):
        return Validation()

    async def audit(*_args):
        return {"question_qc_status": "PASS"}

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    monkeypatch.setattr(subject, "validate_questions", validate)
    monkeypatch.setattr(subject, "question_metrics", lambda *_args: {})
    monkeypatch.setattr(subject.agent_steps, "audit_questions_blind", audit)
    monkeypatch.setattr(subject, "crosscheck_questions", lambda *_args: Cross())
    monkeypatch.setattr(subject, "hard_blockers", lambda _candidate: [])

    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert [event["type"] for event in events] == [
        "question_revision_started",
        "question_revision_validating",
        "question_revision_auditing",
        "question_revision_completed",
    ]
    version = store.load_question_version("mat-1", "req-1")
    assert version["package"] == package
    assert version["based_on_version_id"] == "original"
    assert version["source_comment_ids"] == ["c1"]


def test_question_version_is_create_only_and_same_payload_is_idempotent():
    store = SlotStore(InMemoryObjectStore())
    first = {"id": "req-1", "package": {"question_face": {"title": "first"}}}
    store.save_question_version("mat-1", "req-1", first)
    store.save_question_version("mat-1", "req-1", first)

    with pytest.raises(SlotPersistenceError):
        store.save_question_version(
            "mat-1", "req-1",
            {"id": "req-1", "package": {"question_face": {"title": "changed"}}},
        )
    assert store.load_question_version("mat-1", "req-1") == first


@pytest.mark.asyncio
async def test_completed_request_replay_does_not_call_agent(monkeypatch):
    calls = 0

    async def revise(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    store.save_question_version("mat-1", "req-1", {
        "id": "req-1", "package": {"question_face": {}},
    })
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert calls == 0
    assert events[-1]["type"] == "question_revision_completed"


@pytest.mark.asyncio
async def test_non_durable_runtime_refuses_before_agent_call(monkeypatch):
    calls = 0

    async def revise(*_args):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore(), persistent=False)
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert calls == 0
    assert events[-1]["type"] == "question_revision_failed"
    assert "存储未配置" in events[-1]["message"]
