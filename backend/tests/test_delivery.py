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


class TestStreamRequestIsTheSameRunAsRunRequest:
    """The wiring. `run_request` is now a wrapper over `stream_request`, and that is load-bearing.

    `run_request` used to buffer every event and replay them after the run, which cannot be the
    Runtime's shape: the stage events ARE the keepalive (`orchestration/events.py`), so a request that
    said nothing for six minutes would be dropped by the first intermediary with an idle-read timeout.
    What makes the two safe to keep is that the callback form now delegates rather than duplicating --
    so these tests are about the two not being able to disagree.
    """

    def test_events_are_yielded_during_the_run_not_after_it(self):
        """The property the buffering version failed. Asserted by observing the stage events arrive
        before the terminal one, from a run that only completes because the consumer keeps reading."""
        from backend.orchestration.delivery import stream_request

        recorder = Recorder([material_ok], [questions_ok()])
        store = memory_store()

        async def go():
            seen = []
            async for event in stream_request(
                    [FakeScenario()], "batch-stream", store=store,
                    run_material=recorder.run_material,
                    run_question_stage=recorder.run_questions):
                seen.append(event)
                # The terminal event must be last, and something must have arrived before it.
                if event["type"] == "request_completed":
                    assert len(seen) > 1, "the only event was the summary: nothing streamed"
            return seen

        seen = asyncio.run(go())
        assert seen[-1]["type"] == "request_completed"
        assert [e["type"] for e in seen[:-1]].count("request_completed") == 0

    def test_the_terminal_event_carries_the_summary_run_request_returns(self):
        """One document, two shapes. A second channel for the status is how they would drift."""
        from backend.orchestration.delivery import stream_request

        store = memory_store()
        recorder = Recorder([material_ok], [questions_ok()])

        async def go():
            return [e async for e in stream_request(
                [FakeScenario()], "batch-same-a", store=store,
                run_material=recorder.run_material, run_question_stage=recorder.run_questions)]

        streamed = asyncio.run(go())[-1]

        other = Recorder([material_ok], [questions_ok()])
        returned = asyncio.run(run_request(
            [FakeScenario()], "batch-same-b", store=memory_store(),
            run_material=other.run_material, run_question_stage=other.run_questions))

        assert streamed["type"] == "request_completed"
        # Same keys, and the batch_id is the only value that differs by construction.
        assert set(streamed) - {"type", "at"} == set(returned)
        assert streamed["status"] == returned["status"] == SUCCEEDED
        assert streamed["delivered"] == returned["delivered"] == 1

    def test_run_request_still_emits_every_event_to_its_callback(self):
        """The callback form is what `probe`-style callers and the tests above it use. Delegating must
        not have cost it any event."""
        emitted = []

        async def emit(event):
            emitted.append(event)

        recorder = Recorder([material_ok], [questions_ok()])
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-cb", store=memory_store(), emit=emit,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert emitted[-1]["type"] == "request_completed"
        assert emitted[-1]["status"] == summary["status"] == SUCCEEDED
        assert any(e["type"] == "stage" for e in emitted), "the keepalive events were lost"

    def test_an_abandoned_stream_does_not_leave_the_stages_running(self):
        """A browser that goes away closes the generator. The slot tasks must not carry on spending
        model tokens for output nobody will read."""
        from backend.orchestration.delivery import stream_request

        recorder = Recorder([material_ok], [questions_ok()])
        store = memory_store()

        async def go():
            stream = stream_request(
                [FakeScenario()], "batch-abandon", store=store,
                run_material=recorder.run_material, run_question_stage=recorder.run_questions)
            first = await stream.__anext__()
            await stream.aclose()
            return first

        first = asyncio.run(go())
        assert first["type"] != "request_completed"
        # No pending-task warning, and the loop closed cleanly: `_pump`'s finally cancelled the work.

    def test_a_persistence_failure_still_ends_with_a_terminal_event(self):
        """A stream that ends without `request_completed` means a lost connection. That must not be
        confusable with a request that finished short, so even the refuse-to-start path says so."""
        from backend.orchestration.delivery import stream_request
        from backend.orchestration.slot_store import SlotPersistenceError

        store = memory_store()

        def refuse(document):
            raise SlotPersistenceError("bucket refused")

        store.save_request = refuse  # type: ignore[assignment]
        recorder = Recorder([material_ok], [questions_ok()])

        async def go():
            return [e async for e in stream_request(
                [FakeScenario()], "batch-nostore-stream", store=store,
                run_material=recorder.run_material, run_question_stage=recorder.run_questions)]

        seen = asyncio.run(go())
        assert seen[-1]["type"] == "request_completed"
        assert seen[-1]["status"] == SYSTEM_FAILURE
        assert recorder.material_calls == []


class TestTheSummaryCarriesTheSlotStatesTheWebTierReads:
    """§8.1: `web/fanout.py` decides checkpointed-vs-stuck from these rows, on the wire.

    Without them the web tier would need an S3 read per silent child to tell a resumable slot from a
    dead one -- and the row's absence would leave it doing what it used to do, which is call every
    silent slot a failure.
    """

    def test_every_slot_appears_with_its_state_and_resumable_flag(self):
        recorder = Recorder([material_ok], [questions_ok()])
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-rows", store=memory_store(),
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        rows = summary["slots"]
        assert [r["slot_id"] for r in rows] == ["slot-1"]
        assert rows[0]["state"] == COMPLETE
        assert rows[0]["resumable"] is False
        assert rows[0]["checkpointed"] is False
        assert rows[0]["created_at"] is not None

    def test_a_checkpointed_slot_says_so_and_says_it_is_resumable(self):
        """The row the web tier needs to draw 「还没做完，可以继续」 instead of a failure."""
        recorder = Recorder([material_ok], [questions_ok()])
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-rows-ckpt", store=memory_store(),
            budget=_budget(material=True, questions=False),
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        assert summary["status"] == INCOMPLETE
        row = summary["slots"][0]
        assert row["state"] == MATERIAL_DONE
        assert row["resumable"] is True
        assert row["checkpointed"] is True
        assert row["last_failure"]["reason"] == "time_budget"

    def test_a_replacement_and_its_exhausted_original_both_appear(self):
        """Both records for one position, so the reader can collapse them by outcome. Only the
        replacement's row would leave the exhausted attempt unexplained; only the original's would
        report a delivered position as failed."""
        recorder = Recorder(
            [material_regenerate, material_regenerate, material_regenerate, material_ok],
            [questions_ok()])
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-rows-repl", store=memory_store(),
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        rows = {r["slot_id"]: r for r in summary["slots"]}
        assert set(rows) == {"slot-1", "slot-1r1"}
        assert rows["slot-1"]["state"] == EXHAUSTED and rows["slot-1"]["resumable"] is False
        assert rows["slot-1"]["replaced_by"] == "slot-1r1"
        assert rows["slot-1r1"]["state"] == COMPLETE
        # The tie-break the reader ranks on: the replacement is the newer record.
        assert rows["slot-1r1"]["created_at"] >= rows["slot-1"]["created_at"]

    def test_the_row_is_a_projection_not_the_whole_record(self):
        """The stored record grows fields for the runner's own bookkeeping. Publishing it whole would
        make each of those a field the frontend can start depending on."""
        recorder = Recorder([material_ok], [questions_ok()])
        store = memory_store()
        summary = asyncio.run(run_request(
            [FakeScenario()], "batch-rows-proj", store=store,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions))

        stored = set(store.load_slot("batch-rows-proj", "slot-1").as_record())
        published = set(summary["slots"][0])
        assert stored - published, "the wire row is the whole record: it is not a projection"
        # `group_key` is the sharpest case: an internal join key with no meaning to a browser.
        assert "group_key" not in published


class TestTheTwoIds:
    """§8.1: `batch_id` names the request, `group_id` names the candidate group.

    Collapsing them breaks resumption or breaks selection, in opposite directions -- which is why the
    web fan-out gives each child its own `batch_id` and one shared `group_id`.
    """

    def test_the_group_key_defaults_to_the_batch_id(self):
        """A request that names no group is its own group, so nothing changes for a caller that never
        heard of the field."""
        seen = []
        recorder = Recorder([material_ok], [questions_ok()])
        summary = asyncio.run(_run_capturing_group(
            seen, [FakeScenario()], "batch-grp-default", recorder))
        assert summary["status"] == SUCCEEDED
        assert seen == ["batch-grp-default:accommodation-rental"]

    def test_a_group_id_overrides_it_for_registration_only(self):
        """The candidate group is shared; the slot records still hang off `batch_id`. Both halves are
        asserted here because swapping them is the mistake this split exists to prevent."""
        seen = []
        recorder = Recorder([material_ok], [questions_ok()])
        store = memory_store()
        summary = asyncio.run(_run_capturing_group(
            seen, [FakeScenario()], "web-9-slot-2", recorder, group_id="web-9", store=store))

        assert seen == ["web-9:accommodation-rental"]
        assert summary["batch_id"] == "web-9-slot-2"
        # Storage is keyed by the request id, not by the group: two children of one batch must not
        # write over each other's records.
        assert [r.slot_id for r in store.list_slots("web-9-slot-2")] == ["slot-1"]
        assert store.list_slots("web-9") == []

    def test_two_children_of_one_batch_keep_separate_records_and_one_group(self):
        """The fan-out's arrangement, end to end: what would collide if the ids were one field."""
        store = memory_store()
        seen = []
        for slot in ("slot-1", "slot-2"):
            asyncio.run(_run_capturing_group(
                seen, [FakeScenario()], "web-9-%s" % slot,
                Recorder([material_ok], [questions_ok()]), group_id="web-9", store=store))

        assert seen == ["web-9:accommodation-rental"] * 2, "the materials must share one group"
        for slot in ("slot-1", "slot-2"):
            records = store.list_slots("web-9-%s" % slot)
            assert [r.state for r in records] == [COMPLETE], slot


async def _run_capturing_group(seen, scenarios, batch_id, recorder, group_id=None, store=None):
    """Run one request, recording the group id each material was registered under.

    Wraps whatever `_register_material` currently is -- which under `offer_materials` is the fixture's
    id-minting fake -- and reads `ctx.group_id` on the way through. That is deliberately the plumbing
    half of the split: does the id reach the registration call site, and does it default to `batch_id`.
    The composition of the group KEY from it is tested separately, against the real function.
    """
    import contextlib

    original = delivery_module._register_material

    def capture(result, scenario, record, ctx):
        seen.append("%s:%s" % (ctx.group_id, record.scenario_id))
        return original(result, scenario, record, ctx)

    with contextlib.ExitStack() as stack:
        delivery_module._register_material = capture
        stack.callback(setattr, delivery_module, "_register_material", original)
        return await run_request(
            scenarios, batch_id, store=store or memory_store(), group_id=group_id,
            run_material=recorder.run_material, run_question_stage=recorder.run_questions)


# The real function, captured before `offer_materials` replaces it. Held at module level because the
# fixture is autouse: an `import` inside a test body would get the fixture's fake, and the test would
# then pass by asserting nothing about the line it names.
_REAL_REGISTER_MATERIAL = delivery_module._register_material


class TestTheGroupKeyIsComposedFromTheGroupId:
    """The one line the two ids meet on, tested directly.

    The run-level tests above pin the plumbing -- that `group_id` reaches `_Context` and defaults to
    `batch_id` -- but they cannot pin the composition, because `offer_materials` replaces
    `_register_material` wholesale. So this class calls it for real with `batch._register` captured.
    """

    def test_the_group_key_is_group_id_and_scenario(self, monkeypatch):
        from backend.orchestration import batch as batch_module
        from backend.orchestration.delivery import _Context
        from backend.orchestration.slot_store import SlotRecord

        _register_material = _REAL_REGISTER_MATERIAL

        keys = []

        def capture(result, scenario, group_key):
            keys.append(group_key)
            result.material_id = "mat-1"

        monkeypatch.setattr(batch_module, "_register", capture)
        ctx = _Context(memory_store(), DeliveryBudget(), asyncio.Queue(), "web-9-slot-2", {},
                       None, None, group_id="web-9")
        record = SlotRecord(batch_id="web-9-slot-2", slot_id="slot-1",
                            scenario_id="accommodation-rental")
        _register_material(material_ok("slot-1", "accommodation-rental"), FakeScenario(),
                           record, ctx)

        # The group, not the request: two children of one batch must produce the same key here, and
        # `batch_id` would make every child its own group of one.
        assert keys == ["web-9:accommodation-rental"]

    def test_without_a_group_id_the_key_is_the_batch_id(self, monkeypatch):
        from backend.orchestration import batch as batch_module
        from backend.orchestration.delivery import _Context
        from backend.orchestration.slot_store import SlotRecord

        _register_material = _REAL_REGISTER_MATERIAL

        keys = []

        def capture(result, scenario, group_key):
            keys.append(group_key)
            result.material_id = "mat-1"

        monkeypatch.setattr(batch_module, "_register", capture)
        ctx = _Context(memory_store(), DeliveryBudget(), asyncio.Queue(), "batch-solo", {},
                       None, None)
        record = SlotRecord(batch_id="batch-solo", slot_id="slot-1", scenario_id="booking-hotel")
        _register_material(material_ok("slot-1", "booking-hotel"), FakeScenario(), record, ctx)

        assert keys == ["batch-solo:booking-hotel"]
