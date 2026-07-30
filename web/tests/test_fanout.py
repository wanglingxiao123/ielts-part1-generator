"""The fan-out: planning, merged-event identity, partial failure, and the concurrency cap.

Three properties here are the reason the module exists, and each has a failure mode that no
functional test would otherwise notice:

* **Event identity.** N children each emit `batch_started`, `slot-1`, `batch_completed`. Merged
  naively the frontend sees N batches and N cards fighting over one id -- and it would *look* like
  it worked, because the last writer wins and one card does update. `test_slot_ids_are_distinct...`
  and the batch_started/completed tests pin the reconciliation.
* **Partial failure.** The old design lost a whole batch to one bad slot; the whole point of paying
  for N invocations is that it no longer can.
* **The concurrency cap.** Unenforced, this is not a slow batch but a dead task: every child holds
  a thread while it waits on the model, and a health check that cannot answer gets the instance
  killed by AgentCore mid-batch.

`no_batch_ceiling` is the fourth, and it is a product assertion rather than a mechanical one: the
client asked for the concept to be gone, so a test says 30 sets are accepted.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from web.fanout import FANOUT_CONCURRENCY, FanOut, build_executor, plan_children

from .conftest import FakeStreamingBody, FanOutRuntimeClient, slot_from_session


# ── planning ─────────────────────────────────────────────────────────────────


class TestPlanChildren:
    def test_one_child_per_material(self):
        children, slots = plan_children(
            {"action": "generate", "scenarios": ["a", "b"], "count": 5}, batch_id="b1",
        )
        assert len(children) == 10
        assert slots == ["slot-%d" % n for n in range(1, 11)]
        # Each child asks for exactly one set. A child carrying count=5 would re-create the very
        # problem this module removed -- five materials inside one 900s wall.
        assert all(c.payload["count"] == 1 for c in children)
        assert all(len(c.slot_ids) == 1 for c in children)

    def test_slot_order_matches_the_request_order(self):
        """The frontend pre-plans slot-1..slot-N in request order and matches wire events by exact
        id, so a different order here silently attaches materials to the wrong cards."""
        children, _ = plan_children(
            {"scenarios": ["a", "b"], "counts": {"a": 2, "b": 1}}, batch_id="b1",
        )
        assert [c.scenario for c in children] == ["a", "a", "b"]
        assert [c.slot_ids[0] for c in children] == ["slot-1", "slot-2", "slot-3"]

    def test_per_scenario_counts_win_over_the_default(self):
        children, _ = plan_children(
            {"scenarios": ["a", "b"], "count": 4, "counts": {"a": 1}}, batch_id="b1",
        )
        assert [c.scenario for c in children] == ["a", "b", "b", "b", "b"]

    def test_the_custom_scenario_comes_last_and_is_expanded(self):
        """Same position as `backend/request.py`'s `_expand` puts it, for the same reason."""
        children, slots = plan_children(
            {"scenarios": ["a"], "count": 1,
             "custom_scenario": {"prompt_hint": "a cyclist asks about repairs", "count": 2}},
            batch_id="b1",
        )
        assert [c.scenario for c in children] == ["a", "custom", "custom"]
        assert slots == ["slot-1", "slot-2", "slot-3"]
        # Each custom child carries count=1, and the prompt survives verbatim.
        for child in children[1:]:
            assert child.payload["custom_scenario"]["count"] == 1
            assert child.payload["custom_scenario"]["prompt_hint"].startswith("a cyclist")

    def test_unrelated_payload_fields_reach_every_child(self):
        """`hard_limit_seconds` is how a test shrinks the budget; dropping it silently would make
        the degradation path unreachable from the web tier."""
        children, _ = plan_children(
            {"scenarios": ["a"], "count": 2, "hard_limit_seconds": 120, "actor": "reviewer"},
            batch_id="b1",
        )
        assert all(c.payload["hard_limit_seconds"] == 120 for c in children)
        assert all(c.payload["actor"] == "reviewer" for c in children)

    def test_every_child_carries_the_batch_id(self):
        """It is what groups candidates competing for one user choice. Two materials for the same
        scenario in one submission must compete; two submissions must not."""
        children, _ = plan_children({"scenarios": ["a"], "count": 3}, batch_id="web-42")
        assert {c.payload["batch_id"] for c in children} == {"web-42"}

    def test_an_unknown_scenario_is_forwarded_for_the_backend_to_reject(self):
        """The web tier has no catalogue. Guessing here would give the user a second, different
        sentence for the same mistake."""
        children, _ = plan_children({"scenarios": ["no-such-scenario"]}, batch_id="b1")
        assert len(children) == 1
        assert children[0].payload["scenarios"] == ["no-such-scenario"]

    def test_a_non_integer_count_is_forwarded_rather_than_guessed(self):
        children, _ = plan_children({"scenarios": ["a"], "count": "many"}, batch_id="b1")
        assert len(children) == 1

    def test_an_empty_request_plans_nothing(self):
        children, slots = plan_children({"scenarios": []}, batch_id="b1")
        assert children == [] and slots == []

    def test_there_is_no_batch_ceiling(self):
        """The client's requirement: 用户想生成多少套就生成多少套. 30 sets plan 30 children."""
        children, slots = plan_children(
            {"scenarios": ["a", "b", "c"], "count": 10}, batch_id="b1",
        )
        assert len(children) == 30 and len(slots) == 30


# ── the merged stream ────────────────────────────────────────────────────────


@pytest.fixture
def executor():
    pool = build_executor(6)
    yield pool
    pool.shutdown(wait=False)


def build(runtime, payload, executor, *, concurrency: int = 6) -> FanOut:
    children, slots = plan_children(payload, batch_id="b1")
    return FanOut(runtime, children, slots, executor=executor, concurrency=concurrency)


async def drain(fan: FanOut, timeout: float = 10.0):
    async def go():
        return [event async for event in fan.events()]

    return await asyncio.wait_for(go(), timeout=timeout)


def child_batch(slot_label: str, verdict_ok: bool = True):
    """One child's whole event stream, exactly as the backend emits it: its own batch_started, its
    own `slot-1`, its own batch_completed."""
    return [
        {"type": "batch_started", "total": 1, "deadline_at": 1.0,
         "config": {"model_id": "gpt", "concurrency": 1}},
        {"type": "stage", "slot_id": "slot-1", "scenario": slot_label, "stage": "generating"},
        ({"type": "material_completed", "slot_id": "slot-1", "scenario": slot_label, "ok": True,
          "material_id": "mid-%s" % slot_label}
         if verdict_ok else
         {"type": "material_failed", "slot_id": "slot-1", "scenario": slot_label, "ok": False,
          "reason": "model_error"}),
        {"type": "batch_completed", "succeeded": 1 if verdict_ok else 0,
         "failed": 0 if verdict_ok else 1, "skipped": 0, "degraded": 0, "refilled": 0,
         "stage_timings": {"total": {"count": 1, "min": 100.0, "max": 100.0, "mean": 100.0}},
         "slots": [{"slot_id": "slot-1", "scenario": slot_label, "ok": verdict_ok}]},
    ]


def arm(runtime: FanOutRuntimeClient, slot_id: str, events) -> None:
    body = runtime.body_for(slot_id)
    for event in events:
        body.push_event(event)
    body.finish()


class TestEventIdentity:
    async def test_exactly_one_batch_started_whatever_the_children_say(self, executor):
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2", "slot-3"):
            arm(runtime, slot, child_batch(slot))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 3}, executor))

        starts = [e for e in events if e["type"] == "batch_started"]
        assert len(starts) == 1
        # And it reports the BATCH's total, not a child's 1 -- the frontend lays its skeleton grid
        # out from this number.
        assert starts[0]["total"] == 3
        assert starts[0] is events[0], "the total must precede every child event"

    async def test_exactly_one_batch_completed_after_every_child(self, executor):
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_batch(slot))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        completed = [e for e in events if e["type"] == "batch_completed"]
        assert len(completed) == 1
        assert completed[0] is events[-1]
        # Terminal means terminal: nothing about a material may follow it, or the frontend's
        # `isTerminal` would close the stream on an unfinished batch.
        assert events.index(completed[0]) == len(events) - 1

    async def test_slot_ids_are_distinct_though_every_child_says_slot_1(self, executor):
        """The collision that makes the naive merge look like it works: three materials all
        addressed as `slot-1` would each overwrite the previous card."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2", "slot-3"):
            arm(runtime, slot, child_batch(slot))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 3}, executor))

        materials = [e for e in events if e["type"] == "material_completed"]
        assert sorted(e["slot_id"] for e in materials) == ["slot-1", "slot-2", "slot-3"]
        stages = [e for e in events if e["type"] == "stage"]
        assert sorted(e["slot_id"] for e in stages) == ["slot-1", "slot-2", "slot-3"]

    async def test_a_slot_keeps_one_id_across_all_its_own_events(self, executor):
        """A stage and its material must land on the SAME card. Renaming per event rather than per
        slot would scatter one material's progress across three."""
        runtime = FanOutRuntimeClient()
        body = runtime.body_for("slot-1")
        for stage in ("generating", "auditing", "revising"):
            body.push_event({"type": "stage", "slot_id": "slot-1", "scenario": "a",
                             "stage": stage})
        body.push_event({"type": "material_completed", "slot_id": "slot-1", "scenario": "a",
                         "ok": True})
        body.push_event({"type": "batch_completed", "succeeded": 1})
        body.finish()
        arm(runtime, "slot-2", child_batch("slot-2"))

        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))
        first_child = [e for e in events
                       if e.get("scenario") == "a" and e["type"] in ("stage",
                                                                     "material_completed")]
        assert {e["slot_id"] for e in first_child} == {"slot-1"}
        assert len(first_child) == 4

    async def test_the_aggregate_counts_are_the_sum_of_the_children(self, executor):
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_batch("slot-1", verdict_ok=True))
        arm(runtime, "slot-2", child_batch("slot-2", verdict_ok=False))
        arm(runtime, "slot-3", child_batch("slot-3", verdict_ok=True))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 3}, executor))

        summary = events[-1]
        assert summary["succeeded"] == 2 and summary["failed"] == 1
        assert summary["skipped"] == 0
        # Every slot named once, with the batch-wide id rather than each child's slot-1.
        assert sorted(row["slot_id"] for row in summary["slots"]) == \
            ["slot-1", "slot-2", "slot-3"]

    async def test_the_counts_follow_the_cards_not_the_childrens_summaries(self, executor):
        """A child that delivers its material and then loses the connection.

        Its `batch_completed` never arrives, so summing the children's summaries would report
        `succeeded: 0` over a grid the browser has already drawn a finished card into -- and the
        frontend would render `partial / completed: 0`. The counts are therefore derived from the
        terminal events actually relayed, which makes the summary agree with the cards by
        construction rather than by luck.
        """

        class DiesAfterDelivering(FakeStreamingBody):
            def iter_lines(self, chunk_size: int = 1024):
                yield (
                    b'data: {"type":"material_completed","slot_id":"slot-1",'
                    b'"scenario":"a","ok":true}'
                )
                raise ConnectionResetError("connection dropped after delivery")

        runtime = FanOutRuntimeClient()
        runtime.set_body("slot-1", DiesAfterDelivering())
        arm(runtime, "slot-2", child_batch("slot-2"))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        summary = events[-1]
        assert summary["succeeded"] == 2, summary
        assert summary["failed"] == 0
        # And the delivered material was NOT replaced by a failure card for the same slot.
        assert not [e for e in events if e["type"] == "material_failed"]
        assert sorted(e["slot_id"] for e in events if e["type"] == "material_completed") == \
            ["slot-1", "slot-2"]

    async def test_a_slot_that_produced_nothing_at_all_is_counted_failed(self, executor):
        """A child whose stream simply ends: no material, no stated failure.

        Counting it as a success would report a clean batch over a card still spinning. The browser
        holds a skeleton for every planned slot, so silence has to resolve to something.
        """
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()  # opens and closes, saying nothing
        arm(runtime, "slot-2", child_batch("slot-2"))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        summary = events[-1]
        assert summary["succeeded"] == 1 and summary["failed"] == 1
        assert [row["slot_id"] for row in summary["slots"] if not row["ok"]] == ["slot-1"]

    async def test_a_time_budget_skip_is_counted_skipped_not_failed(self, executor):
        """`skipped` is a distinct outcome the frontend renders differently, so folding it into
        `failed` would misreport a material nothing was attempted for."""
        runtime = FanOutRuntimeClient()
        body = runtime.body_for("slot-1")
        body.push_event({"type": "material_failed", "slot_id": "slot-1", "scenario": "a",
                         "ok": False, "reason": "skipped_time_budget", "skipped": True})
        body.push_event({"type": "batch_completed", "succeeded": 0, "skipped": 1})
        body.finish()
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 1}, executor))

        summary = events[-1]
        assert summary["skipped"] == 1 and summary["failed"] == 0 and summary["succeeded"] == 0

    async def test_degraded_and_refilled_still_come_from_the_children(self, executor):
        """The counterweight to deriving counts locally: these two the web tier cannot observe, so
        they are still summed from the children's summaries."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            body = runtime.body_for(slot)
            body.push_event({"type": "material_completed", "slot_id": "slot-1", "scenario": "a",
                             "ok": True})
            body.push_event({"type": "batch_completed", "succeeded": 1, "degraded": 1,
                             "refilled": 2})
            body.finish()
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        assert events[-1]["degraded"] == 2
        assert events[-1]["refilled"] == 4

    async def test_stage_timings_are_combined_as_a_weighted_mean(self, executor):
        """A mean of means would be wrong the moment two children ran a different number of stages,
        and this is the number docs/timing.md calibrates the user-facing estimate against."""
        runtime = FanOutRuntimeClient()
        for slot, count, mean in (("slot-1", 1, 100.0), ("slot-2", 3, 200.0)):
            body = runtime.body_for(slot)
            body.push_event({
                "type": "batch_completed", "succeeded": 1,
                "stage_timings": {"generate": {"count": count, "min": mean, "max": mean,
                                               "mean": mean}},
            })
            body.finish()
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        generate = events[-1]["stage_timings"]["generate"]
        assert generate["count"] == 4
        assert generate["mean"] == 175.0  # (100*1 + 200*3) / 4, not (100+200)/2
        assert generate["min"] == 100.0 and generate["max"] == 200.0

    async def test_every_child_gets_its_own_session_id(self, executor):
        """A shared id routes every child to one warm microVM and serialises the batch."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2", "slot-3"):
            arm(runtime, slot, child_batch(slot))
        await drain(build(runtime, {"scenarios": ["a"], "count": 3}, executor))

        assert len(set(runtime.session_ids)) == 3
        # The API's documented floor. A uuid4 hex alone is 32 and is rejected at call time.
        assert all(len(s) >= 33 for s in runtime.session_ids)
        # And each names its card, which is the only way to get from a failed card to its logs.
        assert sorted(slot_from_session(s) for s in runtime.session_ids) == \
            ["slot-1", "slot-2", "slot-3"]

    async def test_an_empty_plan_still_produces_a_terminal_event(self, executor):
        """The frontend spins forever on a stream that ends without a terminal frame."""
        events = await drain(build(FanOutRuntimeClient(), {"scenarios": []}, executor))
        assert [e["type"] for e in events] == ["batch_started", "batch_failed"]
        assert events[0]["total"] == 0


class TestPartialFailure:
    async def test_one_refused_invoke_does_not_stop_the_others(self, executor):
        runtime = FanOutRuntimeClient()
        runtime.fail_slots["slot-2"] = RuntimeError("ThrottlingException: slow down")
        for slot in ("slot-1", "slot-3"):
            arm(runtime, slot, child_batch(slot))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 3}, executor))

        completed = [e for e in events if e["type"] == "material_completed"]
        assert sorted(e["slot_id"] for e in completed) == ["slot-1", "slot-3"]
        failed = [e for e in events if e["type"] == "material_failed"]
        assert [e["slot_id"] for e in failed] == ["slot-2"]
        assert "ThrottlingException" in failed[0]["detail"]
        assert events[-1]["type"] == "batch_completed"
        assert events[-1]["succeeded"] == 2 and events[-1]["failed"] == 1

    async def test_a_child_that_dies_mid_stream_fails_only_its_slot(self, executor):
        class ExplodingBody(FakeStreamingBody):
            def iter_lines(self, chunk_size: int = 1024):
                yield b'data: {"type":"stage","slot_id":"slot-1","stage":"generating"}'
                raise ConnectionResetError("runtime went away")

        runtime = FanOutRuntimeClient()
        runtime.set_body("slot-1", ExplodingBody())
        arm(runtime, "slot-2", child_batch("slot-2"))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        # The events it did emit before dying still arrived, under its own slot id.
        assert "slot-1" in [e["slot_id"] for e in events if e["type"] == "stage"]
        failed = [e for e in events if e["type"] == "material_failed"]
        assert [e["slot_id"] for e in failed] == ["slot-1"]
        assert "ConnectionResetError" in failed[0]["detail"]
        assert [e["slot_id"] for e in events if e["type"] == "material_completed"] == ["slot-2"]

    async def test_a_child_backend_error_becomes_that_slots_failure(self, executor):
        """The backend rejects a bad payload with `batch_failed`, which is terminal for that child
        and must not be terminal for the batch."""
        runtime = FanOutRuntimeClient()
        body = runtime.body_for("slot-1")
        body.push_event({"type": "batch_failed", "reason": "bad_request",
                         "detail": "unknown scenario 'made-up'"})
        body.finish()
        arm(runtime, "slot-2", child_batch("slot-2"))
        events = await drain(build(runtime, {"scenarios": ["made-up", "a"]}, executor))

        failed = [e for e in events if e["type"] == "material_failed"]
        assert [e["slot_id"] for e in failed] == ["slot-1"]
        assert failed[0]["reason"] == "bad_request"
        assert events[-1]["type"] == "batch_completed" and events[-1]["succeeded"] == 1

    async def test_a_unary_answer_to_generate_is_that_slots_failure(self, executor):
        """`app.py` answers an unknown action with a JSON dict. On a `generate` that means the
        payload never became a generator, and dropping it would leave one card spinning forever."""
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_batch("slot-1"))

        real_invoke = runtime.invoke

        def sometimes_unary(payload, *, session_id=None):
            if slot_from_session(session_id or "") == "slot-2":
                body = FakeStreamingBody()
                body.push_raw(json.dumps({"error": "unknown action"}).encode())
                body.finish()
                return "application/json", body, session_id or ""
            return real_invoke(payload, session_id=session_id)

        runtime.invoke = sometimes_unary
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        failed = [e for e in events if e["type"] == "material_failed"]
        assert [e["slot_id"] for e in failed] == ["slot-2"]
        assert "unknown action" in failed[0]["detail"]
        assert events[-1]["succeeded"] == 1 and events[-1]["failed"] == 1

    async def test_a_non_json_line_is_reported_not_swallowed(self, executor):
        """A Runtime that prints a plain-text 502 onto an event-stream response. Discarding it would
        leave the card spinning with no explanation anywhere."""
        runtime = FanOutRuntimeClient()
        body = runtime.body_for("slot-1")
        body.push_raw(b"Internal Server Error")
        body.push_raw(b"")
        body.finish()
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 1}, executor))

        failed = [e for e in events if e["type"] == "material_failed"]
        assert len(failed) == 1
        assert "Internal Server Error" in failed[0]["detail"]

    async def test_every_child_failing_still_completes_the_batch(self, executor):
        runtime = FanOutRuntimeClient()
        runtime.raise_on_invoke = RuntimeError("AccessDeniedException")
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 4}, executor))

        assert [e["type"] for e in events] == (
            ["batch_started"] + ["material_failed"] * 4 + ["batch_completed"]
        )
        assert events[-1]["failed"] == 4 and events[-1]["succeeded"] == 0


class TestConcurrency:
    async def test_no_more_than_the_cap_are_in_flight_at_once(self, executor):
        """Unenforced, this is not a slow batch but a dead task: each child holds a thread while it
        waits on the model, and AgentCore kills an instance whose /ping times out."""
        runtime = FanOutRuntimeClient()
        for n in range(1, 13):
            arm(runtime, "slot-%d" % n, child_batch("slot-%d" % n))

        fan = build(runtime, {"scenarios": ["a"], "count": 12}, executor, concurrency=3)
        events = await drain(fan)

        assert runtime.peak_in_flight <= 3, (
            "peak concurrency %d exceeded the cap of 3" % runtime.peak_in_flight
        )
        # And all twelve did run: a cap that drops work is not a cap.
        assert len(runtime.calls) == 12
        assert len([e for e in events if e["type"] == "material_completed"]) == 12

    async def test_the_cap_does_not_exceed_the_work_available(self, executor):
        fan = build(FanOutRuntimeClient(), {"scenarios": ["a"], "count": 2}, executor,
                    concurrency=6)
        assert fan.concurrency == 2
        assert fan.events  # not started; the assertion is about construction

    async def test_the_executor_is_no_larger_than_the_cap(self):
        """The reason it is a dedicated pool at all: a child parked for four minutes must not be
        able to hold a thread that /healthz needs. anyio's shared pool has 40 tokens; this has 6."""
        pool = build_executor(FANOUT_CONCURRENCY)
        try:
            assert pool._max_workers == FANOUT_CONCURRENCY
        finally:
            pool.shutdown(wait=False)

    async def test_a_slot_is_released_only_when_its_child_ends(self, executor):
        """The gate has to be held for the child's whole life, not just its invoke. Releasing after
        the call returned would let all N children stream at once -- the cap would measure nothing,
        because `invoke_agent_runtime` returns as soon as headers arrive."""
        runtime = FanOutRuntimeClient()
        started = threading.Event()
        hold = runtime.body_for("slot-1")
        hold.push_event({"type": "stage", "slot_id": "slot-1", "scenario": "a",
                         "stage": "generating"})
        for n in (2, 3):
            arm(runtime, "slot-%d" % n, child_batch("slot-%d" % n))

        fan = build(runtime, {"scenarios": ["a"], "count": 3}, executor, concurrency=1)

        async def run():
            return [e async for e in fan.events()]

        task = asyncio.ensure_future(run())
        # Let the held child start and emit, then confirm nobody else was invoked.
        for _ in range(400):
            if len(runtime.calls) >= 1:
                started.set()
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        await asyncio.sleep(0.15)
        assert len(runtime.calls) == 1, (
            "a second child started while the first was still streaming; the gate is released too "
            "early"
        )

        hold.push_event({"type": "batch_completed", "succeeded": 1})
        hold.finish()
        events = await asyncio.wait_for(task, timeout=10)
        assert len(runtime.calls) == 3
        assert events[-1]["type"] == "batch_completed"


class TestAbandonment:
    async def test_closing_stops_the_children(self, executor):
        """A browser that navigates away must not leave N invocations paying the model for output
        nobody will read. `close()` shuts the bodies, which is what unblocks the reader threads."""
        runtime = FanOutRuntimeClient()
        held = runtime.body_for("slot-1")
        held.push_event({"type": "stage", "slot_id": "slot-1", "scenario": "a",
                         "stage": "generating"})

        fan = build(runtime, {"scenarios": ["a"], "count": 1}, executor)
        stream = fan.events()
        assert (await stream.__anext__())["type"] == "batch_started"
        assert (await stream.__anext__())["type"] == "stage"

        # What Starlette does when the client disconnects mid-stream.
        await stream.aclose()

        for _ in range(400):
            if held.closed:
                break
            await asyncio.sleep(0.005)
        assert held.closed is True, "an abandoned child's body was left open"
