from __future__ import annotations

import copy
from types import SimpleNamespace

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
        base_version={"package": {}},
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
        base_version={"package": {}},
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
        consistency = {"computed": {
            "question_qc_status": "PASS",
            "counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0},
            "reviewed_question_ids": list(range(1, 11)),
        }}
        hard_defects = []
        needs_review = []
        leakage = []
        equally_supported_rivals = []
        items = []
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
        base_version={"package": {}},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert [event["type"] for event in events] == [
        "question_revision_started",
        "question_revision_validating",
        "question_revision_auditing",
        "question_revision_storing",
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


def _package():
    return {
        "question_face": {
            "instructions": [
                {"group_id": "A", "question_range": "1-5"},
                {"group_id": "B", "question_range": "6-10"},
            ],
            "groups": [
                {"group_id": "A", "layout": "form"},
                {"group_id": "B", "layout": "note"},
            ],
            "questions": [
                {"number": number, "group_id": "A" if number <= 5 else "B",
                 "carrier_before": "£" if number == 5 else ""}
                for number in range(1, 11)
            ],
        },
        "answer_key": [
            {"number": number, "canonical": "£128" if number == 5 else "answer-%d" % number}
            for number in range(1, 11)
        ],
        "evidence": [
            {"number": number, "turn_index": number}
            for number in range(1, 11)
        ],
    }


def _candidate(package, *, rival_number=9, validator_errors=None):
    rival = {"number": rival_number, "text": "passport"}
    cross = SimpleNamespace(
        hard_defects=[],
        leakage=[],
        equally_supported_rivals=[rival],
        needs_review=[],
        compared=10,
        agreed=10,
        consistency={"computed": {"reviewed_question_ids": list(range(1, 11))},
                     "errors": []},
        items=[],
    )
    review = {
        "per_question_findings": [
            {"number": rival_number, "rule_id": "AR-012", "severity": "MAJOR",
             "state": "open"}
        ],
        "group_findings": [],
    }
    return SimpleNamespace(
        package=package,
        review=review,
        cross_check=cross,
        validation=SimpleNamespace(errors=validator_errors or []),
    )


def _base_version(package):
    return {
        "package": package,
        "quality": {
            "review": {
                "per_question_findings": [],
                "group_findings": [],
                "question_qc_status": "PASS",
            },
            "cross_check": {"compared": 10, "agreed": 10},
            "validation": {"ok": True, "errors": []},
        },
    }


def test_unchanged_q9_audit_variance_does_not_block_a_q5_only_revision():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][4]["carrier_before"] = ""

    blockers, advisories, changed = subject._revision_gate(
        _candidate(revised), _base_version(base))

    assert blockers == []
    assert changed == [1, 2, 3, 4, 5]
    assert any("Q9" in advisory and "passport" in advisory for advisory in advisories)


def test_audit_issue_on_an_actually_changed_question_still_blocks():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][8]["carrier_before"] = "Required ID: "

    blockers, advisories, changed = subject._revision_gate(
        _candidate(revised), _base_version(base))

    assert changed == [6, 7, 8, 9, 10]
    assert advisories == []
    assert any("Q9" in blocker and "passport" in blocker for blocker in blockers)


def test_validator_errors_always_block_even_when_the_question_is_unchanged():
    base = _package()
    blockers, _, changed = subject._revision_gate(
        _candidate(copy.deepcopy(base), validator_errors=["bad rectangle"]),
        _base_version(base),
    )

    assert changed == []
    assert blockers == ["validator error: bad rectangle"]


def test_missing_baseline_quality_uses_the_existing_strict_gate(monkeypatch):
    monkeypatch.setattr(subject, "hard_blockers", lambda _candidate: ["strict blocker"])

    blockers, advisories, changed = subject._revision_gate(
        _candidate(_package()), {"package": _package()})

    assert blockers == ["strict blocker"]
    assert advisories == []
    assert changed == []


def test_crosscheck_hard_defect_never_downgrades_on_an_unchanged_question():
    base = _package()
    candidate = _candidate(copy.deepcopy(base))
    candidate.cross_check.hard_defects = [{"number": 9, "outcome": "answer_mismatch"}]

    blockers, advisories, _ = subject._revision_gate(
        candidate, _base_version(base))

    assert any("answer_mismatch" in blocker for blocker in blockers)
    assert all("answer_mismatch" not in advisory for advisory in advisories)


def test_unexplained_crosscheck_shortfall_always_blocks():
    base = _package()
    candidate = _candidate(copy.deepcopy(base))
    candidate.cross_check.agreed = 9

    blockers, _, _ = subject._revision_gate(candidate, _base_version(base))

    assert any("agrees on 9 of 10" in blocker for blocker in blockers)


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
        base_version={"package": {}},
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
        base_version={"package": {}},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert calls == 0
    assert events[-1]["type"] == "question_revision_failed"
    assert "存储未配置" in events[-1]["message"]
