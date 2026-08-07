"""Exact-count delivery: the three properties §8.2 cannot be satisfied by inspection.

Every other guarantee in this stage is visible in the code -- the two counters, the `material_done`
write, the absent "partial success" state. These three are not, because each is a claim about a
*sequence* of stage answers:

1. **A replacement slot appears when a position exhausts its candidate swaps** (§8.2(3)) -- and it
   inherits the position rather than starting a new request.
2. **A `material_done` material is never regenerated across invocations** (§8.2(1), §8.2(4)) -- the
   second invoke must start at the question stage, on the same material.
3. **Fewer than N complete sets is never `succeeded`** (§8.2(5)) -- including when the shortfall is
   the request's own doing and every event looks healthy.

The stages are scripted rather than mocked at the model layer: what is under test is what the runner
does with an answer, so the answers have to be exact. Each script also records what it was *asked*,
because the strongest form of test 2 is not "the material was reused" but "generation was never
called a second time".
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.deterministic.crosscheck import CrossCheckResult
from backend.orchestration import delivery as delivery_module
from backend.orchestration.delivery import DeliveryBudget, run_request
from backend.orchestration.loop import Candidate, MaterialResult
from backend.orchestration.question_loop import QuestionCandidate, QuestionResult
from backend.orchestration.slot_store import (
    COMPLETE,
    EXHAUSTED,
    INCOMPLETE,
    MATERIAL_DONE,
    MATERIAL_PENDING,
    SUCCEEDED,
    SYSTEM_FAILURE,
    SlotStore,
)
from backend.steps.agent_steps import GenOutput


class FakeScenario:
    def __init__(self, id="accommodation-rental"):
        self.id = id
        self.category = "accommodation"
        self.title_zh = "租房咨询"
        self.prompt_hint = "hint"
        self.default_count = 1


def memory_store() -> SlotStore:
    from audio_storage.object_store import InMemoryObjectStore

    # persistent=True even though it is in memory: these tests are about the runner's behaviour when
    # writes SUCCEED, and the non-persistent mode's whole difference is that it swallows failures.
    return SlotStore(InMemoryObjectStore(), persistent=True)


def material_ok(slot_id: str, scenario_id: str, feasibility: str = "PASS") -> MaterialResult:
    result = MaterialResult(
        slot_id, scenario_id, True,
        Candidate(GenOutput({"listening_material_parts": []}, {"blueprint": True}),
                  {"verdict": "PASS", "score": {"total": 85, "dimensions": {}}, "findings": []},
                  CrossCheckResult({"ok": True, "matched": 10}), "initial"),
        "initial", "pending")
    result.feasibility = {"outcome": feasibility, "reasons": []}
    return result


def material_regenerate(slot_id: str, scenario_id: str) -> MaterialResult:
    """A material the preflight says cannot carry ten fair questions."""
    return material_ok(slot_id, scenario_id, feasibility="REGENERATE_MATERIAL")


def questions_ok(rounds: int = 0) -> QuestionResult:
    candidate = QuestionCandidate(
        {"question_face": {"questions": []}, "answer_key": []},
        {"question_qc_status": "PASS"},
        _FakeCross(), _FakeValidation(), "initial")
    return QuestionResult(True, candidate, "initial", rounds=rounds)


def questions_regenerate(rounds: int = 2) -> QuestionResult:
    return QuestionResult(
        False, reason="questions_not_deliverable", outcome="REGENERATE_MATERIAL",
        blockers=["the cross-check agrees on 9 of 10 items"], rounds=rounds)


class _FakeCross:
    agreed = 10
    compared = 10
    by_outcome = {"agree": list(range(1, 11))}
    hard_defects: list = []
    leakage: list = []
    equally_supported_rivals: list = []
    needs_review: list = []
    consistency = {"computed": {"reviewed_question_ids": list(range(1, 11))}, "errors": []}

    def as_dict(self):
        return {"agreed": self.agreed, "compared": self.compared}


class _FakeValidation:
    errors: list = []
    warnings: list = []

    def as_dict(self):
        return {"errors": [], "warnings": []}


class Recorder:
    """Scripted material and question stages that remember every call.

    ``materials`` / ``questions`` are consumed in order; the last entry repeats once exhausted, so a
    test states only the answers that differ from the steady state.
    """

    def __init__(self, materials, questions):
        self._materials = list(materials)
        self._questions = list(questions)
        self.material_calls = []
        self.question_calls = []

    async def run_material(self, scenario, slot_id, emit, may_revise):
        self.material_calls.append(slot_id)
        answer = self._materials[min(len(self.material_calls) - 1, len(self._materials) - 1)]
        return answer(slot_id, scenario.id) if callable(answer) else answer

    async def run_questions(self, material, blueprint, emit):
        self.question_calls.append(material)
        answer = self._questions[min(len(self.question_calls) - 1, len(self._questions) - 1)]
        return answer() if callable(answer) else answer


@pytest.fixture(autouse=True)
def offer_materials(monkeypatch):
    """Registration mints a ``material_id`` and stores the artifacts, without S3.

    Patched at ``_register`` and ``_load_material`` together, because the pair IS the checkpoint under
    test in §8.2(1): the runner writes ``material_done`` after registering and reads the material back
    through the registry on the next stage, possibly in another process. A fake that registers but
    cannot read back would make test 2 pass for the wrong reason.
    """
    offered = {}
    counter = {"n": 0}

    def register(result, scenario, group_key):
        counter["n"] += 1
        result.material_id = "mat-%d" % counter["n"]
        result.scenario_key = scenario.id
        result.group_key = group_key
        offered[result.material_id] = (result.candidate.gen.material,
                                       result.candidate.gen.blueprint)

    monkeypatch.setattr(delivery_module, "_register_material",
                        lambda result, scenario, record, ctx: register(result, scenario, ""))
    monkeypatch.setattr(delivery_module, "_load_material",
                        lambda record, ctx: offered.get(str(record.material_id), (None, None)))
    monkeypatch.setattr(delivery_module, "_withdraw_material", lambda record, ctx: None)
    return offered


class TestReplacementSlot:
    """§8.2(3): a position that exhausts its swaps is refilled, never quietly dropped."""

    def test_a_position_that_exhausts_its_swaps_opens_a_replacement(self):
        # Three REGENERATE_MATERIAL verdicts exhaust slot-1 (initial + 2 swaps), then the
        # replacement's first material qualifies.
        recorder = Recorder(
            [material_regenerate, material_regenerate, material_regenerate, material_ok],
            [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-repl", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        records = {r.slot_id: r for r in store.list_slots("batch-repl")}
        assert set(records) == {"slot-1", "slot-1r1"}
        assert records["slot-1"].state == EXHAUSTED
        assert records["slot-1"].attempts["candidate_swaps"] == 3
        assert records["slot-1"].replaced_by == "slot-1r1"
        assert records["slot-1r1"].replaces == "slot-1"
        # The replacement inherits the position's scenario. A replacement generating a different
        # scenario would answer a request the user never made.
        assert records["slot-1r1"].scenario_id == "accommodation-rental"
        assert records["slot-1r1"].state == COMPLETE
        assert summary["status"] == SUCCEEDED
        assert summary["delivered"] == 1

    def test_the_replacement_delivers_the_set_the_request_is_counted_on(self):
        """The count comes from complete slots, not from original slots."""
        recorder = Recorder(
            [material_regenerate, material_regenerate, material_regenerate, material_ok],
            [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-repl2", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))
        assert [s["slot_id"] for s in summary["sets"]] == ["slot-1r1"]
        # `mat-1`, not `mat-4`: a material rejected by the feasibility verdict never reaches
        # registration, so the three discarded attempts mint no id at all. That is the intended
        # ordering -- an id is the key `_questions/`, audio and selection all join on, and minting one
        # for a material this stage is about to discard would publish a join key to nothing.
        delivered_id = summary["sets"][0]["material_id"]
        assert delivered_id == "mat-1"
        assert store.load_questions(delivered_id) is not None

    def test_a_question_stage_regenerate_verdict_spends_the_outer_budget(self):
        """§8.2(2): REGENERATE_MATERIAL from the question loop draws a new material, not a 3rd round.

        The distinction that matters: the question stage is entered once per material, never twice on
        the same one after a verdict. Two revision rounds have already run inside it against a
        blueprint that cannot change.
        """
        recorder = Recorder([material_ok],
                            [questions_regenerate(), questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-qregen", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))
        assert summary["status"] == SUCCEEDED
        assert len(recorder.material_calls) == 2
        assert len(recorder.question_calls) == 2
        record = store.load_slot("batch-qregen", "slot-1")
        assert record.attempts["candidate_swaps"] == 1

    def test_replacement_slots_are_bounded_and_a_bounded_run_is_not_a_success(self):
        """The bound exists so the invocation terminates; it does not create a partial-success exit."""
        recorder = Recorder([material_regenerate], [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-bound", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))
        states = [r.state for r in store.list_slots("batch-bound")]
        assert states == [EXHAUSTED] * 3
        assert summary["status"] == INCOMPLETE
        assert summary["delivered"] == 0


class TestCrossInvokeCheckpoint:
    """§8.2(1) + §8.2(4): a qualified material survives the invocation that produced it."""

    def test_a_second_invoke_resumes_at_the_question_stage(self):
        """The first invoke's clock stops before questions; the second must not regenerate.

        `may_start_material` true, `may_start_questions` false: exactly the state a 900s wall produces
        after a 250s material, and the reason the two predicates are separate.
        """
        first = Recorder([material_ok], [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-ckpt", store=store,
            budget=_budget(material=True, questions=False),
            run_material=first.run_material, run_question_stage=first.run_questions))

        assert summary["status"] == INCOMPLETE
        assert summary["paused"] is True
        assert first.question_calls == []
        record = store.load_slot("batch-ckpt", "slot-1")
        assert record.state == MATERIAL_DONE
        assert record.material_id == "mat-1"
        assert record.checkpoint_at is not None
        assert record.last_failure["reason"] == "time_budget"

        # A new invocation: a fresh Recorder, so a regenerated material would be visible as a call.
        second = Recorder([material_ok], [questions_ok()])
        resumed = asyncio.run(run_request(
            [FakeScenario()], "batch-ckpt", store=store,
            run_material=second.run_material, run_question_stage=second.run_questions))

        assert second.material_calls == [], "a material_done material must never be regenerated"
        assert len(second.question_calls) == 1
        assert resumed["status"] == SUCCEEDED
        assert store.load_slot("batch-ckpt", "slot-1").state == COMPLETE

    def test_a_completed_slot_is_not_re_run_by_a_later_invoke(self):
        """Resumption counts what is done; it does not redo it to confirm."""
        first = Recorder([material_ok], [questions_ok()])
        store = memory_store()
        asyncio.run(run_request(
            [FakeScenario()], "batch-done", store=store,
            run_material=first.run_material, run_question_stage=first.run_questions))

        second = Recorder([material_ok], [questions_ok()])
        resumed = asyncio.run(run_request(
            [FakeScenario()], "batch-done", store=store,
            run_material=second.run_material, run_question_stage=second.run_questions))
        assert second.material_calls == []
        assert second.question_calls == []
        assert resumed["status"] == SUCCEEDED
        assert resumed["delivered"] == 1

    def test_a_question_stage_crash_re_enters_on_the_same_material(self):
        """§8.2(1): a crash is not a verdict, so it must not cost the material.

        The sharpest form of the requirement -- the one that would look reasonable done wrong, since
        "the question stage failed, draw another material" is a defensible-sounding sentence that
        throws away 250s of qualified work for a timeout.
        """
        def crash():
            raise RuntimeError("model call died")

        recorder = Recorder([material_ok], [crash, questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-crash", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert summary["status"] == SUCCEEDED
        assert len(recorder.material_calls) == 1, "the crash must not have drawn a new material"
        assert len(recorder.question_calls) == 2
        record = store.load_slot("batch-crash", "slot-1")
        assert record.attempts["question_restarts"] == 1
        assert record.attempts["candidate_swaps"] == 0

    def test_a_second_identical_crash_is_a_system_fault_not_a_new_material(self):
        def crash():
            raise RuntimeError("model call died")

        recorder = Recorder([material_ok], [crash])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-crash2", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert summary["status"] == SYSTEM_FAILURE
        assert len(recorder.material_calls) == 1
        record = store.load_slot("batch-crash2", "slot-1")
        assert record.system_fault is True
        # No replacement: a position whose failure is not about the material must not spend another.
        assert len(store.list_slots("batch-crash2")) == 1


class TestFewerThanNIsNeverSuccess:
    """§8.2(5): the count is the gate, and there is no state for "short but finished"."""

    def test_one_of_two_slots_short_is_not_succeeded(self):
        recorder = Recorder([material_ok], [questions_ok(), questions_regenerate()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario("accommodation-rental"), FakeScenario("library-enrolment")],
            "batch-short", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert summary["requested"] == 2
        assert summary["status"] != SUCCEEDED
        assert summary["delivered"] < 2
        # And the shortfall is visible in storage, not only in the returned summary: a resumption reads
        # the records, so a status that lived only in the response would be unrecoverable.
        states = [r.state for r in store.list_slots("batch-short")]
        assert COMPLETE in states and states.count(COMPLETE) < 2

    def test_a_permanently_failing_slot_leaves_a_non_complete_record(self):
        """The §9.2 integration row: an always-failing slot must not be reported as delivered."""
        recorder = Recorder([material_regenerate], [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-never", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert summary["status"] != SUCCEEDED
        assert summary["delivered"] == 0
        assert summary["sets"] == []
        assert all(r.state != COMPLETE for r in store.list_slots("batch-never"))
        request = store.load_request("batch-never")
        assert request["status"] != SUCCEEDED
        assert request["complete"] == 0

    def test_no_delivered_set_is_recorded_for_a_short_request(self):
        """A `_questions/` object for an undelivered slot would be shippable by a later reader."""
        recorder = Recorder([material_regenerate], [questions_ok()])
        store = memory_store()
        asyncio.run(run_request(
            [FakeScenario()], "batch-noqs", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))
        assert store.load_questions("mat-1") is None

    def test_unwritable_slot_state_is_a_system_failure_before_anything_generates(self):
        """§8.2(5): infrastructure failure stays incomplete or says system_failure -- never succeeded."""
        recorder = Recorder([material_ok], [questions_ok()])
        store = memory_store()

        def refuse(_record):
            raise_it()

        def raise_it():
            from backend.orchestration.slot_store import SlotPersistenceError

            raise SlotPersistenceError("bucket refused")

        store.save_request = refuse  # type: ignore[assignment]
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-nostore", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert summary["status"] == SYSTEM_FAILURE
        assert summary["delivered"] == 0
        assert recorder.material_calls == [], "no work may start when progress cannot be recorded"

    def test_a_paused_request_is_incomplete_not_a_system_failure(self):
        """The two non-success states are distinguished: one is resumable, one needs a human."""
        recorder = Recorder([material_ok], [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-pause", store=store,
            budget=_budget(material=False, questions=False),
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))
        assert summary["status"] == INCOMPLETE
        assert recorder.material_calls == []
        assert store.load_slot("batch-pause", "slot-1").state == MATERIAL_PENDING


class TestTheBudgetSeparatesTheTwoStages:
    def test_the_question_threshold_is_not_the_material_threshold(self):
        """A 240s material p95 must not authorise a 420s question stage."""
        budget = DeliveryBudget(hard_limit=900, margin=90, p95_material=240, p95_questions=420)
        assert budget.p95_questions > budget.p95_material
        original = time.monotonic
        try:
            # 300s left: enough for a material at its measured p95, not enough for a question stage.
            # This is the state a 900s wall reaches after one 250s material, and the reason one
            # predicate could not have served both.
            time.monotonic = lambda: budget.deadline - 300  # type: ignore[assignment]
            assert budget.may_start_material()
            assert not budget.may_start_questions()
        finally:
            time.monotonic = original  # type: ignore[assignment]


def _budget(material: bool, questions: bool) -> DeliveryBudget:
    """A budget with each predicate pinned, so a test states the clock it means.

    Pinned rather than computed from a fake ``monotonic``: what these tests need is "the question stage
    was not affordable", and expressing that as a deadline arithmetic makes the test depend on the two
    p95 constants it is not about.
    """
    class Pinned(DeliveryBudget):
        def may_start_material(self):
            return material

        def may_start_questions(self):
            return questions

        def may_revise(self):
            return True

    return Pinned()
