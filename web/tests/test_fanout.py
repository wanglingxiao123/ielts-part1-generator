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

from web import fanout
from web.fanout import (FANOUT_CONCURRENCY, FanOut, build_executor, launch_order,
                        plan_children)

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


class TestLaunchOrder:
    """Which children get a worker first. Separate from the slot plan, and it has to stay separate:
    slot ids are the frontend's card grid, start order is a scheduling choice."""

    def test_every_scenario_gets_a_material_in_the_first_wave(self):
        """The user's report: 「前面几个类别都跑完了，自定义类别才开始跑」.

        Plan order groups a scenario's materials together, so with concurrency 6 a 7x3 batch put the
        custom scenario in slots 19-21 -- three waves in. Nothing was wrong except the order.
        """
        children, _ = plan_children(
            {"scenarios": ["a", "b", "c", "d", "e", "f"], "count": 3,
             "custom_scenario": {"prompt_hint": "餐厅点餐", "count": 3}},
            batch_id="b1",
        )
        order = launch_order(children)
        assert len(order) == len(children) == 21
        first_wave = [c.scenario for c in order[:7]]
        assert sorted(first_wave) == ["a", "b", "c", "custom", "d", "e", "f"]

    def test_the_slot_ids_are_untouched(self):
        """Reordering the launch must not renumber anything: the frontend matches cards by exact id,
        and a moved slot attaches a material to the wrong card."""
        children, slots = plan_children(
            {"scenarios": ["a", "b"], "counts": {"a": 2, "b": 2}}, batch_id="b1",
        )
        order = launch_order(children)
        assert sorted(c.slot_ids[0] for c in order) == slots
        for child in order:
            assert child.slot_ids == children[child.index].slot_ids
            assert child.seats == children[child.index].seats

    def test_a_scenario_keeps_its_own_materials_in_order(self):
        """Within one scenario, seat 0 starts before seat 1 -- so the first card of a group fills
        first and 「第 1 套」 is not perpetually behind 「第 2 套」."""
        children, _ = plan_children({"scenarios": ["a"], "count": 4}, batch_id="b1")
        order = launch_order(children)
        assert [c.slot_ids[0] for c in order] == ["slot-1", "slot-2", "slot-3", "slot-4"]

    def test_every_child_is_launched_exactly_once(self):
        children, _ = plan_children(
            {"scenarios": ["a", "b", "c"], "counts": {"a": 1, "b": 5, "c": 2}}, batch_id="b1",
        )
        order = launch_order(children)
        assert sorted(c.index for c in order) == list(range(len(children)))

    def test_nothing_to_launch_is_not_an_error(self):
        assert launch_order([]) == []


# ── the merged stream ────────────────────────────────────────────────────────


@pytest.fixture
def executor():
    pool = build_executor(6)
    yield pool
    pool.shutdown(wait=False)


def build(runtime, payload, executor, *, concurrency: int = 6, batch_id: str = "b1") -> FanOut:
    children, slots = plan_children(payload, batch_id=batch_id)
    return FanOut(runtime, children, slots, executor=executor, concurrency=concurrency,
                  batch_id=batch_id)


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

    async def test_batch_started_names_the_batch_id_the_web_tier_minted(self, executor):
        """The one frame that ties the browser's URL to the S3 record.

        Without it `frontend/src/api/agentcore.ts` minted its own id, so `/batches/:batchId` and
        `_batches/<id>/index.json` were two different id spaces and the history panel reported
        「没有找到批次 … 的历史记录」 for a batch it had just generated. A child's own `batch_started`
        is still swallowed, so this is the only place the id can come from.
        """
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_batch(slot))
        events = await drain(build(
            runtime, {"scenarios": ["a"], "count": 2}, executor, batch_id="web-1785228044000-7",
        ))
        assert events[0]["type"] == "batch_started"
        assert events[0]["batch_id"] == "web-1785228044000-7"
        # And the children's ids never leak: each child was invoked with the same batch id, so a
        # child echoing its own would be indistinguishable -- there is exactly one on the wire.
        assert [e for e in events if "batch_id" in e] == [events[0]]

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


# ── 心跳 ─────────────────────────────────────────────────────────────────────


class TestHeartbeat:
    """静默的合流必须仍然往线上写字节。

    实测：一个 8 套的真实批次在两个相邻事件之间静默了 96 秒（216.6s → 313.0s）——一个 child 从
    `generating` 到它的终态帧之间什么都不说，而并发 6 时后面那一波还没开始。96 秒长于所有中间层的
    空闲读取容忍度（CloudFront 源站读取上限 60s，ALB 空闲默认 60s），所以不发心跳的话，一个跑得
    好好的批次会在中途被掐断，浏览器看到的是「连接丢失」。

    心跳是 `HEARTBEAT` 哨兵，由 `web/app.py` 成帧为 SSE **注释**（`: hb`）。它不进事件流：不进
    reducer、不占 seq、不被 recorder 记录。测的就是这两件事——它出现，且它不是事件。
    """

    async def test_silence_yields_a_heartbeat(self, executor, monkeypatch):
        """child 迟迟不说话时，合流自己发心跳，而不是干等。"""
        monkeypatch.setattr(fanout, "HEARTBEAT_SECONDS", 0.05)
        runtime = FanOutRuntimeClient()
        body = runtime.body_for("slot-1")
        fan = build(runtime, {"scenarios": ["a"], "count": 1}, executor)

        seen = []

        async def consume():
            async for item in fan.events():
                seen.append(item)
                # 收到两个心跳就说明机制在转；然后放行让批次正常收尾。
                if sum(1 for x in seen if x is fanout.HEARTBEAT) >= 2:
                    for event in child_batch("a"):
                        body.push_event(event)
                    body.finish()

        await asyncio.wait_for(consume(), timeout=10.0)

        hearts = [x for x in seen if x is fanout.HEARTBEAT]
        assert len(hearts) >= 2, "静默期没有发出心跳：%r" % (seen,)

    async def test_heartbeat_is_not_an_event(self, executor, monkeypatch):
        """心跳不能污染事件序列。

        这是哨兵而不是 `{"type": "ping"}` 的全部理由。一个占了 seq 的 keepalive 会让重连的游标停在
        一个不携带任何状态的帧上——6 分钟的批次有约 24 个这样的帧。
        """
        monkeypatch.setattr(fanout, "HEARTBEAT_SECONDS", 0.05)
        runtime = FanOutRuntimeClient()
        body = runtime.body_for("slot-1")
        fan = build(runtime, {"scenarios": ["a"], "count": 1}, executor)

        seen = []

        async def consume():
            async for item in fan.events():
                seen.append(item)
                if sum(1 for x in seen if x is fanout.HEARTBEAT) >= 1 and not body.closed:
                    for event in child_batch("a"):
                        body.push_event(event)
                    body.finish()

        await asyncio.wait_for(consume(), timeout=10.0)

        events = [x for x in seen if x is not fanout.HEARTBEAT]
        # 心跳不是 dict，所以任何按 `event["type"]` 分发的代码都碰不到它。
        for heart in [x for x in seen if x is fanout.HEARTBEAT]:
            assert not isinstance(heart, dict)
        # 事件序列本身完好：一个 batch_started、一个终态、一个 batch_completed。
        types = [e["type"] for e in events]
        assert types[0] == "batch_started"
        assert types[-1] == "batch_completed"
        assert "material_completed" in types

    async def test_a_busy_stream_needs_no_heartbeat(self, executor, monkeypatch):
        """事件不断时不该额外插心跳——那只是噪音。"""
        monkeypatch.setattr(fanout, "HEARTBEAT_SECONDS", 30.0)
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            body = runtime.body_for(slot)
            for event in child_batch(slot):
                body.push_event(event)
            body.finish()
        fan = build(runtime, {"scenarios": ["a"], "count": 2}, executor)

        seen = await drain(fan)
        assert not any(x is fanout.HEARTBEAT for x in seen)


# ── generate_sets: exact-count delivery through the fan-out ───────────────────


def child_request(*rows, status: str = "succeeded", faults=None, delivered=None):
    """One `generate_sets` child's whole stream: its own batch_started, then `request_completed`.

    `rows` are `delivery._slot_row` projections. The child names its slots `slot-1`, `slot-1r1` and so
    on, in ITS OWN id space -- which is the whole reason the merge has to translate them.
    """
    rows = list(rows)
    return [
        {"type": "batch_started", "total": 1, "deadline_at": 1.0, "config": {"model_id": "gpt"}},
        {"type": "request_completed",
         "batch_id": "child",
         "status": status,
         "requested": 1,
         "delivered": len([r for r in rows if r.get("state") == "complete"])
                      if delivered is None else delivered,
         "sets": [],
         "system_faults": faults if faults is not None else [],
         "paused": False,
         "slots": rows},
    ]


def slot_row(slot_id="slot-1", state="complete", **kwargs):
    """A `_slot_row`-shaped projection, defaulted to a delivered slot."""
    row = {
        "slot_id": slot_id,
        "scenario": kwargs.pop("scenario", "a"),
        "state": state,
        "material_id": kwargs.pop("material_id", "mat-1"),
        "created_at": kwargs.pop("created_at", 1000.0),
        "resumable": state not in ("complete", "exhausted"),
        "checkpointed": False,
        "system_fault": None,
        "last_failure": None,
        "attempts": {},
        "replaces": None,
        "replaced_by": None,
    }
    row.update(kwargs)
    return row


class FakeSlotState:
    """A `SlotStateReader` whose answers a test writes directly. Never touches S3."""

    def __init__(self, by_batch=None, available: bool = True, raises=None):
        self.by_batch = by_batch or {}
        self.available = available
        self.raises = raises
        self.asked = []

    def load_slots(self, batch_id: str):
        self.asked.append(batch_id)
        if self.raises is not None:
            raise self.raises
        return self.by_batch.get(batch_id)


def build_sets(runtime, payload, executor, *, slot_state=None, concurrency: int = 6,
               batch_id: str = "b1") -> FanOut:
    children, slots = plan_children(dict(payload, action="generate_sets"), batch_id=batch_id)
    return FanOut(runtime, children, slots, executor=executor, concurrency=concurrency,
                  batch_id=batch_id,
                  slot_state=slot_state if slot_state is not None else FakeSlotState(available=False))


class TestPlanChildrenForGenerateSets:
    """The id split. Getting it wrong breaks resumption or selection, in opposite directions."""

    def test_each_child_gets_its_own_request_id(self):
        """Every child calls its own slot `slot-1` and writes it under `_slots/{batch_id}/`. One
        shared id would have N children overwriting one record, and a resumption would find one slot
        where the user asked for N."""
        children, _ = plan_children(
            {"action": "generate_sets", "scenarios": ["a"], "count": 3}, batch_id="web-9")
        assert [c.payload["batch_id"] for c in children] == [
            "web-9-slot-1", "web-9-slot-2", "web-9-slot-3"]
        assert len({c.payload["batch_id"] for c in children}) == 3

    def test_every_child_shares_one_group_id(self):
        """The counterweight: candidates compete for one user choice within a group, so two materials
        of one submission must share it. Distinct group ids would stop them competing at all."""
        children, _ = plan_children(
            {"action": "generate_sets", "scenarios": ["a"], "count": 3}, batch_id="web-9")
        assert {c.payload["group_id"] for c in children} == {"web-9"}

    def test_a_plain_generate_batch_is_untouched(self):
        """`generate` has no slot records to collide, mints its own group key per invocation, and is
        what the deployed frontend sends. Splitting its ids would change a shipped path for nothing."""
        children, _ = plan_children({"scenarios": ["a"], "count": 2}, batch_id="web-9")
        assert {c.payload["batch_id"] for c in children} == {"web-9"}
        assert all("group_id" not in c.payload for c in children)

    def test_the_action_reaches_every_child(self):
        children, _ = plan_children(
            {"action": "generate_sets", "scenarios": ["a"], "count": 2}, batch_id="b1")
        assert {c.payload["action"] for c in children} == {"generate_sets"}


class TestOutcomeForState:
    """The three-way reading of one recorded slot state, in isolation."""

    def test_complete_is_ok(self):
        assert fanout.outcome_for_state(slot_row(state="complete")) == "ok"

    def test_exhausted_is_failed(self):
        assert fanout.outcome_for_state(slot_row(state="exhausted")) == "failed"

    @pytest.mark.parametrize("state", ["material_pending", "material_done", "questions_pending"])
    def test_every_non_terminal_state_is_pending(self, state):
        assert fanout.outcome_for_state(slot_row(state=state)) == "pending"

    def test_a_stored_row_without_the_resumable_flag_falls_back_to_state(self):
        """Not a legacy path: rows read from `_slots/` come from `SlotRecord.as_record()`, which has
        `state` and no `resumable`. So this is the branch every silent slot takes."""
        stored = {"slot_id": "slot-1", "state": "material_done", "created_at": 1.0}
        assert fanout.outcome_for_state(stored) == "pending"
        assert fanout.outcome_for_state(dict(stored, state="exhausted")) == "failed"
        assert fanout.outcome_for_state(dict(stored, state="complete")) == "ok"

    def test_a_row_with_no_state_at_all_is_failed(self):
        """Unreadable is not resumable: promising a retry that will not happen is worse than saying
        the slot failed."""
        assert fanout.outcome_for_state({"slot_id": "slot-1"}) == "failed"

    def test_the_runtimes_flag_wins_over_the_local_reading(self):
        """Which states are terminal is the Runtime's rule. A row that says a state this module has
        never heard of is resumable is taken at its word."""
        row = {"slot_id": "slot-1", "state": "some_future_state", "resumable": False}
        assert fanout.outcome_for_state(row) == "failed"


class TestBestRow:
    """A position accumulates records; the card shows the position."""

    def test_a_completed_replacement_beats_the_exhausted_original(self):
        """The misreport this exists to remove: reporting the abandoned original would call a
        delivered set a failure."""
        rows = [slot_row("slot-1", state="exhausted", created_at=1.0),
                slot_row("slot-1r1", state="complete", created_at=2.0)]
        assert fanout.best_row(rows)["slot_id"] == "slot-1r1"
        # And in either arrival order -- ranking, not list position.
        assert fanout.best_row(list(reversed(rows)))["slot_id"] == "slot-1r1"

    def test_a_still_working_replacement_beats_the_exhausted_original(self):
        rows = [slot_row("slot-1", state="exhausted", created_at=1.0),
                slot_row("slot-1r1", state="material_done", created_at=2.0)]
        assert fanout.best_row(rows)["slot_id"] == "slot-1r1"

    def test_the_newest_wins_a_tie(self):
        rows = [slot_row("slot-1", state="exhausted", created_at=1.0),
                slot_row("slot-1r1", state="exhausted", created_at=2.0)]
        assert fanout.best_row(rows)["slot_id"] == "slot-1r1"

    def test_a_missing_created_at_does_not_raise(self):
        rows = [slot_row("slot-1", state="exhausted", created_at=None),
                slot_row("slot-1r1", state="exhausted", created_at=None)]
        assert fanout.best_row(rows)["slot_id"] in ("slot-1", "slot-1r1")


class TestRequestStatusOnTheWire:
    """What the merged `batch_completed` says about an exact-count request."""

    async def test_exactly_n_delivered_is_succeeded(self, executor):
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_request(slot_row(state="complete")))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 2}, executor))

        summary = events[-1]
        assert summary["request_status"] == "succeeded"
        assert summary["requested"] == 2 and summary["delivered"] == 2
        assert summary["succeeded"] == 2 and summary["failed"] == 0
        assert summary["resumable_slots"] == []

    async def test_one_short_of_n_is_never_succeeded(self, executor):
        """§8.2(5): there is no partial-success exit. N-1 delivered sets is not a delivered request,
        however healthy every event looked."""
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_request(slot_row(state="complete")))
        arm(runtime, "slot-2", child_request(slot_row(state="exhausted"), status="incomplete"))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 2}, executor))

        summary = events[-1]
        assert summary["request_status"] == "incomplete"
        assert summary["delivered"] == 1 and summary["requested"] == 2
        # Not resumable: the slot gave up, it did not run out of clock.
        assert summary["resumable_slots"] == []

    async def test_a_checkpointed_slot_is_incomplete_and_resumable_not_failed(self, executor):
        """The checkpoint case. The slot has a qualified material on disk and stopped on the clock;
        calling it failed tells the user their material is gone when the next invocation would have
        finished it."""
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_request(slot_row(state="complete")))
        arm(runtime, "slot-2", child_request(
            slot_row(state="material_done", checkpointed=True,
                     last_failure={"reason": "time_budget"}),
            status="incomplete"))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 2}, executor))

        summary = events[-1]
        assert summary["request_status"] == "incomplete"
        assert summary["resumable_slots"] == ["slot-2"]
        assert summary["failed"] == 0, "a resumable slot must not be counted as a failure"
        row = [r for r in summary["slots"] if r["slot_id"] == "slot-2"][0]
        assert row["pending"] is True and row["ok"] is False
        # `last_failure` says why the last attempt stopped; promoting it to `reason` would draw a slot
        # mid-retry as a finished failure.
        assert row["reason"] is None
        assert row["last_failure"] == {"reason": "time_budget"}

    async def test_a_system_fault_surfaces_as_system_failure(self, executor):
        """Not merely incomplete: a system fault is not resumable without a human, and reporting it
        as a shortfall would send the user back to retry a request that cannot succeed."""
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_request(
            slot_row(state="exhausted", system_fault="validator_unavailable"),
            status="system_failure",
            faults=[{"slot_id": "slot-1", "reason": "validator_unavailable"}]))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor))

        summary = events[-1]
        assert summary["request_status"] == "system_failure"
        assert summary["system_faults"] == [
            {"slot_id": "slot-1", "reason": "validator_unavailable"}]

    async def test_a_stated_system_failure_counts_even_with_no_fault_list(self, executor):
        """A child reports `system_failure` for causes the web tier cannot see -- storage refusing a
        write, its own status document unpersistable -- and those arrive as a status, not a fault
        row. Re-deriving the status from slot states alone would report them as merely incomplete."""
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_request(
            slot_row(state="material_pending"), status="system_failure", faults=[]))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor))

        assert events[-1]["request_status"] == "system_failure"

    async def test_reaching_n_is_not_demoted_by_a_fault_on_a_refilled_position(self, executor):
        """`delivery._status` checks the count first, and this has to agree: a position that
        exhausted and was refilled delivered its set, and the request delivered N."""
        runtime = FanOutRuntimeClient()
        arm(runtime, "slot-1", child_request(
            slot_row("slot-1", state="exhausted", created_at=1.0,
                     system_fault="candidate_swaps_exhausted"),
            slot_row("slot-1r1", state="complete", created_at=2.0),
            status="succeeded",
            faults=[{"slot_id": "slot-1", "reason": "candidate_swaps_exhausted"}]))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor))

        summary = events[-1]
        assert summary["request_status"] == "succeeded"
        assert summary["delivered"] == 1 and summary["succeeded"] == 1
        # The position, not the abandoned record: the card shows the replacement's material.
        assert [r["material_id"] for r in summary["slots"]] == ["mat-1"]

    async def test_the_child_request_ids_are_reported(self, executor):
        """So an operator, or a resume, can address the requests this batch was actually made of.
        The browser batch id is not one of them."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_request(slot_row(state="complete")))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 2}, executor,
                                        batch_id="web-9"))
        assert events[-1]["request_ids"] == ["web-9-slot-1", "web-9-slot-2"]

    async def test_request_completed_is_not_relayed_to_the_browser(self, executor):
        """One merged batch has one terminal event. N children each announcing a finished request
        would tell the frontend the batch ended N times."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_request(slot_row(state="complete")))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 2}, executor))

        types = [e["type"] for e in events if isinstance(e, dict)]
        assert types.count("request_completed") == 0
        assert types.count("batch_completed") == 1
        assert types.count("batch_started") == 1

    async def test_a_plain_generate_batch_carries_no_request_status(self, executor):
        """`generate` may legitimately deliver fewer materials than asked, so `incomplete` on one of
        its batches would report that path's normal outcome as a shortfall."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_batch(slot))
        events = await drain(build(runtime, {"scenarios": ["a"], "count": 2}, executor))

        summary = events[-1]
        for field in ("request_status", "requested", "delivered", "resumable_slots",
                      "request_ids"):
            assert field not in summary


class TestRecordedStateForASilentChild:
    """The fallback: a child that never answered, whose state is only on disk."""

    async def test_a_recorded_complete_slot_counts_as_delivered(self, executor):
        """A lost frame does not unmake a delivery. The set is on disk and the browser already has
        the material; counting it failed would draw a failure over a finished card."""
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()  # opens and closes, saying nothing
        state = FakeSlotState({"b1-slot-1": [
            {"slot_id": "slot-1", "state": "complete", "material_id": "mat-7",
             "created_at": 1.0}]})
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=state))

        summary = events[-1]
        assert summary["succeeded"] == 1 and summary["failed"] == 0
        assert summary["request_status"] == "succeeded"
        assert summary["slots"][0]["material_id"] == "mat-7"
        assert state.asked == ["b1-slot-1"]

    async def test_a_recorded_resumable_slot_is_pending_not_failed(self, executor):
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()
        state = FakeSlotState({"b1-slot-1": [
            {"slot_id": "slot-1", "state": "material_done", "material_id": "mat-7",
             "created_at": 1.0}]})
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=state))

        summary = events[-1]
        assert summary["request_status"] == "incomplete"
        assert summary["resumable_slots"] == ["slot-1"]
        assert summary["failed"] == 0

    async def test_a_completed_replacement_is_read_over_its_exhausted_original(self, executor):
        """The records are collapsed by outcome, not by list position. Mapping positionally would
        report the exhausted original and ignore the replacement that delivered."""
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()
        state = FakeSlotState({"b1-slot-1": [
            {"slot_id": "slot-1", "state": "exhausted", "created_at": 1.0},
            {"slot_id": "slot-1r1", "state": "complete", "material_id": "mat-9",
             "created_at": 2.0}]})
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=state))

        summary = events[-1]
        assert summary["succeeded"] == 1
        assert summary["slots"][0]["material_id"] == "mat-9"

    async def test_a_slot_that_produced_nothing_and_recorded_nothing_is_still_failed(self, executor):
        """The original case, unchanged. `None` from the reader means the child never got as far as
        its first write, and a clean batch drawn over a spinning card is worse than a failure."""
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=FakeSlotState({})))

        summary = events[-1]
        assert summary["failed"] == 1 and summary["succeeded"] == 0
        assert summary["request_status"] == "incomplete"
        assert summary["resumable_slots"] == []

    async def test_an_unreadable_store_leaves_the_slot_failed_rather_than_breaking_the_stream(
            self, executor):
        """This runs while the browser waits for the last frame of a batch it has already seen the
        materials of. An exception here would replace the summary with a stream error."""
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()
        state = FakeSlotState(raises=RuntimeError("AccessDenied"))
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=state))

        summary = events[-1]
        assert summary["type"] == "batch_completed"
        assert summary["failed"] == 1

    async def test_nothing_is_read_when_every_child_answered(self, executor):
        """The ordinary path costs no S3 read at all: the child that owns the state reported it."""
        runtime = FanOutRuntimeClient()
        for slot in ("slot-1", "slot-2"):
            arm(runtime, slot, child_request(slot_row(state="complete")))
        state = FakeSlotState({})
        await drain(build_sets(runtime, {"scenarios": ["a"], "count": 2}, executor,
                              slot_state=state))
        assert state.asked == []

    async def test_a_generate_batch_never_reads_slot_state(self, executor):
        """It has no `_slots/` records to find, and a read per silent child would be pure latency on
        the path that is already deployed."""
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()
        state = FakeSlotState({})
        children, slots = plan_children({"scenarios": ["a"], "count": 1}, batch_id="b1")
        fan = FanOut(runtime, children, slots, executor=executor, concurrency=6, batch_id="b1",
                     slot_state=state)
        events = await drain(fan)
        assert state.asked == []
        assert events[-1]["failed"] == 1

    async def test_an_unavailable_reader_reads_nothing(self, executor):
        """No bucket configured is a local run, and the pre-existing behaviour is the right default:
        a missing bucket must not make an unknown slot look complete."""
        runtime = FanOutRuntimeClient()
        runtime.body_for("slot-1").finish()
        state = FakeSlotState({"b1-slot-1": [{"slot_id": "slot-1", "state": "complete"}]},
                              available=False)
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=state))
        assert state.asked == []
        assert events[-1]["failed"] == 1

    async def test_a_child_that_failed_outright_is_not_looked_up(self, executor):
        """It already has an outcome from the merged stream. Reading storage for it would risk
        overriding a stated failure with a stale record."""
        runtime = FanOutRuntimeClient()
        runtime.fail_slots["slot-1"] = RuntimeError("invoke refused")
        state = FakeSlotState({})
        events = await drain(build_sets(runtime, {"scenarios": ["a"], "count": 1}, executor,
                                        slot_state=state))
        assert state.asked == []
        assert events[-1]["failed"] == 1
