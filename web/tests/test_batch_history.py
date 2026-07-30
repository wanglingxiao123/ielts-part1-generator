"""Batch history: the record, its survival across a restart, the three statuses, and read-only.

Four properties here, each with a failure mode that would otherwise reach the client:

* **Survival across a web-task restart.** This is the whole feature. The batch used to live in a
  browser-side `Map`, and the reason history did not exist is that a reload lost it. The test
  substitutes a second `WebTier` over the same store, which is what a Fargate redeploy is: a new
  task, the same bucket. A record written only at `batch_completed` would pass a naive "the batch is
  listed" test and still lose every material of an interrupted batch, so the interrupted case is
  asserted separately.

* **The status derivation.** 待选稿 / 已提交 / 已归档 have to come from facts the backend can
  substantiate. Two are derived (from candidate expiry) and one had to be added (`submit`), and the
  tests pin which is which -- including that a submitted batch does not become 已归档 with age,
  because "someone submitted this" is a recorded fact that time cannot unmake.

* **Read-only.** `read_only` is returned by the backend rather than re-derived in the UI, so a
  submitted or archived batch cannot be made mutable by a frontend mistake. `test_a_read_only_batch`
  asserts the flag AND that `submit` on an archived batch does not resurrect it.

* **Writes never block the stream.** The recorder hands snapshots to its own thread. A store that
  sleeps on every write is used to prove the events still flow -- an inline `put` would serialise
  the batch behind S3 latency, and no functional assertion would notice.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from web.app import WebTier, _scenario_shape
from web.batch_history import (
    ARCHIVED,
    CANDIDATE_TTL_SECONDS,
    PENDING_SELECTION,
    STALE_RUNNING_SECONDS,
    SUBMITTED,
    BatchHistory,
    derive,
    new_batch_id,
)
from web.batch_store import BATCH_PREFIX, InMemoryBatchStore, S3BatchStore
from web.fanout import plan_children

from .conftest import FanOutRuntimeClient, collect, register


def material_event(slot_id: str, scenario: str, material_id: str, *, verdict: str = "PASS"):
    """A `material_completed` frame shaped like the backend's, trimmed to what history reads."""
    return {
        "type": "material_completed",
        "slot_id": slot_id,
        "scenario": scenario,
        "scenario_key": scenario,
        "ok": True,
        "material_id": material_id,
        "material": {"listening_material_parts": [{"script": {"turns": [{"text": "hi"}]}}]},
        "blueprint": {"items": []},
        "audit": {"verdict": verdict, "findings": []},
        "cross_check": {"ok": True},
        "route": "pending",
        "degraded": False,
        "at": time.time(),
    }


def _await_materials(store, batch_id: str, count: int, *, timeout: float = 5.0) -> None:
    """Block until the stored record lists `count` materials.

    The recorder writes on its own thread, so a test that read the store immediately would be
    asserting on the scheduler. Polling with a generous ceiling keeps it deterministic in both
    directions: it cannot pass early, and a genuinely absent write fails rather than hangs.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = store.load_index(batch_id)
        if stored is not None and len(stored.get("materials") or []) >= count:
            return
        time.sleep(0.01)
    stored = store.load_index(batch_id)
    raise AssertionError(
        "expected %d material(s) in the stored record for %s, found %r -- the per-material write "
        "did not happen, so an interrupted batch would lose what it had already delivered"
        % (count, batch_id, (stored or {}).get("materials"))
    )


def record(**overrides):
    """A stored batch record with sane defaults, for the pure `derive` tests."""
    base = {
        "batch_id": "web-1-1",
        "created_at": time.time(),
        "requested_total": 2,
        "scenarios": [{"scenario_key": "booking-hotel", "count": 2}],
        "materials": [{"material_id": "m1"}, {"material_id": "m2"}],
        "state": "complete",
        "completed_at": time.time(),
    }
    base.update(overrides)
    return base


# ── the status derivation ────────────────────────────────────────────────────


class TestStatusDerivation:
    def test_a_fresh_unsubmitted_batch_is_pending_selection(self):
        """待选稿: nothing submitted, and its candidates are still resolvable."""
        view = derive(record())
        assert view["status"] == PENDING_SELECTION
        assert view["read_only"] is False

    def test_an_unsubmitted_batch_past_the_candidate_ttl_is_archived(self):
        """已归档 is the candidate expiry, not an age threshold picked to fill the third chip.

        Once `_candidates/` has aged out, `list_candidates` no longer offers the batch's materials
        and no selection can be made against it -- so there is genuinely no decision left.
        """
        now = time.time()
        view = derive(record(created_at=now - CANDIDATE_TTL_SECONDS - 1), now=now)
        assert view["status"] == ARCHIVED
        assert view["read_only"] is True

    def test_the_boundary_is_the_candidate_ttl_exactly(self):
        now = time.time()
        inside = derive(record(created_at=now - CANDIDATE_TTL_SECONDS + 60), now=now)
        outside = derive(record(created_at=now - CANDIDATE_TTL_SECONDS), now=now)
        assert inside["status"] == PENDING_SELECTION
        assert outside["status"] == ARCHIVED

    def test_a_submitted_batch_is_submitted(self):
        now = time.time()
        view = derive(record(submitted_at=now - 60, submitted_by="a@b.c"), now=now)
        assert view["status"] == SUBMITTED
        assert view["read_only"] is True

    def test_submission_outranks_age(self):
        """已提交 stays 已提交 however old it gets.

        The recorded fact is "someone submitted this", and age does not unmake it. Falling through
        to 已归档 would make the panel forget a decision that was actually taken.
        """
        now = time.time()
        view = derive(
            record(created_at=now - 30 * 24 * 3600, submitted_at=now - 29 * 24 * 3600), now=now,
        )
        assert view["status"] == SUBMITTED

    def test_the_ttl_matches_the_backend_constant(self):
        """`web/` ships without `backend/` (see web/Dockerfile), so the constant is duplicated.

        This is what keeps the duplicate honest: if the backend's TTL moves and this one does not,
        the panel would call a batch 已归档 while its candidates were still selectable.
        """
        from backend.orchestration.candidate_store import (
            CANDIDATE_TTL_SECONDS as backend_ttl,
        )

        assert CANDIDATE_TTL_SECONDS == backend_ttl


class TestInterrupted:
    def test_a_batch_still_running_is_not_reported_interrupted(self):
        """A long batch is normal: the per-material wall is 900s and a large batch runs waves."""
        now = time.time()
        view = derive(record(state="running", completed_at=None, created_at=now - 1800), now=now)
        assert view["interrupted"] is False
        assert view["state"] == "running"

    def test_a_stale_running_record_is_reported_interrupted(self):
        """The web-task-died case. Nothing will ever come back to finalise this record.

        Reported rather than smoothed over: inferring completion would say a batch finished when the
        only thing known is that nobody is working on it.
        """
        now = time.time()
        view = derive(
            record(state="running", completed_at=None,
                   created_at=now - STALE_RUNNING_SECONDS - 1),
            now=now,
        )
        assert view["interrupted"] is True

    def test_an_interrupted_batch_still_lists_the_materials_it_delivered(self):
        now = time.time()
        view = derive(
            record(state="running", completed_at=None, requested_total=6,
                   materials=[{"material_id": "m%d" % n} for n in range(5)],
                   created_at=now - STALE_RUNNING_SECONDS - 1),
            now=now,
        )
        assert view["interrupted"] is True
        assert view["arrived"] == 5 and view["requested_total"] == 6


# ── the record, written from a real fanned-out stream ────────────────────────


class TestRecording:
    async def test_a_batch_is_recorded_as_its_materials_arrive(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient, history: BatchHistory,
    ):
        tier = WebTier(auth, fanout_runtime, str(static_dir), history=history)
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])

        for slot, scenario, mid in (("slot-1", "booking-hotel", "20260730-booking-hotel-aaaaaaaa"),
                                    ("slot-2", "booking-hotel", "20260730-booking-hotel-bbbbbbbb")):
            body = fanout_runtime.body_for(slot)
            body.push_event(material_event(slot, scenario, mid))
            body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0,
                             "skipped": 0, "degraded": 0, "at": time.time()})
            body.finish()

        await collect(tier, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 2})

        batches = history.list_batches()
        assert len(batches) == 1
        batch = batches[0]
        assert batch["arrived"] == 2
        assert batch["requested_total"] == 2
        assert batch["scenarios"] == [{"scenario_key": "booking-hotel", "count": 2}]
        assert batch["state"] == "complete"
        assert batch["status"] == PENDING_SELECTION
        assert {m["material_id"] for m in batch["materials"]} == {
            "20260730-booking-hotel-aaaaaaaa", "20260730-booking-hotel-bbbbbbbb",
        }
        # Two materials of ONE scenario must carry distinct `index` values. This assertion is the
        # one this test was missing, and its absence let a real defect through: `_on_material` never
        # recorded `index` while `get_batch` read it, so every historical material came back with
        # `index: None`. The frontend seats a card at `(scenario_key, index)` and defaults a missing
        # index to 0, so both materials claimed slot 0 and the second overwrote the first. A 3x2
        # batch rendered three cards and reported 「已完成 3/6，其余未能生成」 while all six
        # materials, and all six sidecars, were present in S3.
        by_id = {m["material_id"]: m for m in batch["materials"]}
        assert sorted(m["index"] for m in batch["materials"]) == [0, 1]
        assert by_id["20260730-booking-hotel-aaaaaaaa"]["index"] == 0
        assert by_id["20260730-booking-hotel-bbbbbbbb"]["index"] == 1
        # And the seating key itself must be unique, which is what the grid actually collides on.
        seats = {(m["scenario_key"], m["index"]) for m in batch["materials"]}
        assert len(seats) == len(batch["materials"])

    async def test_the_final_state_lands_even_when_a_write_is_slow(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient,
    ):
        """`state: "complete"` must survive a store slower than the gap between the last two events.

        This is the defect every batch in the deployed bucket had (2026-07-30): all three records read
        `state: "running"`, `completed_at: null`, `counts: {}` for batches that finished cleanly. The
        consequence is not cosmetic -- `derive` turns `interrupted` true after `STALE_RUNNING_SECONDS`,
        so a complete batch grows a 已中断 badge and a 「缺的部分不会再补齐」 banner two hours later.

        The race: `material_completed` and `batch_completed` arrive milliseconds apart, the worker is
        inside the PUT for the former when the latter sets `_dirty`, and then `close()` sets `_stop`
        and `_dirty` together. The old loop flushed once more, saw `_stop`, and returned -- dropping
        the write that carried `state: "complete"`.

        A latency the in-memory store does not have is what makes it deterministic here: with a fast
        store the two writes coalesce and the bug is invisible, which is exactly why every existing
        test passed while production was wrong.
        """
        class SlowStore(InMemoryBatchStore):
            def save_index(self, batch_id, record):
                time.sleep(0.15)
                super().save_index(batch_id, record)

        store = SlowStore()
        tier = WebTier(auth, fanout_runtime, str(static_dir), history=BatchHistory(store))
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        body = fanout_runtime.body_for("slot-1")
        body.push_event(material_event("slot-1", "booking-hotel", "m-1"))
        body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0,
                         "skipped": 0, "degraded": 0, "at": time.time()})
        body.finish()

        await collect(tier, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 1})

        # The web tier minted the id; this store holds exactly this one batch.
        stored = store.load_all_indexes()[0]
        assert stored["state"] == "complete", (
            "the batch_completed write was dropped; this record would grow a 已中断 badge"
        )
        assert stored["completed_at"], "a complete batch must carry completed_at"
        assert stored["counts"]["succeeded"] == 1

    async def test_the_list_does_not_carry_the_scripts(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient, history: BatchHistory,
    ):
        """The panel renders a scenario tag and a set count. Shipping ~20KB of script per material
        to draw that would make a 12-batch history a multi-megabyte response."""
        tier = WebTier(auth, fanout_runtime, str(static_dir), history=history)
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        body = fanout_runtime.body_for("slot-1")
        body.push_event(material_event("slot-1", "booking-hotel", "m-1"))
        body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0, "skipped": 0,
                         "degraded": 0, "at": time.time()})
        body.finish()
        await collect(tier, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 1})

        summary = history.list_batches()[0]
        listed = summary["materials"][0]
        assert "material" not in listed and "blueprint" not in listed
        # And the detail route does carry them: a list that is cheap because the artifacts are
        # nowhere would be a different, useless kind of cheap.
        full = history.get_batch(summary["batch_id"])
        assert full is not None
        assert full["materials"][0]["material"]["listening_material_parts"]

    async def test_a_material_with_no_candidate_is_not_a_history_row(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient, history: BatchHistory,
    ):
        """`material_id: null` means candidate registration failed server-side.

        Nothing later could resolve it -- no 试听, no select -- so it cannot be a row a reviewer can
        click. It still counts toward the batch totals through `batch_completed`.
        """
        tier = WebTier(auth, fanout_runtime, str(static_dir), history=history)
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        event = material_event("slot-1", "booking-hotel", "unused")
        event["material_id"] = None
        body = fanout_runtime.body_for("slot-1")
        body.push_event(event)
        body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0, "skipped": 0,
                         "degraded": 0, "at": time.time()})
        body.finish()
        await collect(tier, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 1})

        batch = history.list_batches()[0]
        assert batch["materials"] == []
        assert batch["counts"]["succeeded"] == 1

    async def test_a_batch_that_produced_nothing_is_still_recorded(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient, history: BatchHistory,
    ):
        """Written at batch START, so evidence exists that the batch was asked for at all."""
        tier = WebTier(auth, fanout_runtime, str(static_dir), history=history)
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        fanout_runtime.fail_slots["slot-1"] = RuntimeError("no capacity")
        await collect(tier, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 1})

        batch = history.list_batches()[0]
        assert batch["arrived"] == 0
        assert batch["requested_total"] == 1
        assert batch["scenarios"] == [{"scenario_key": "booking-hotel", "count": 1}]

    async def test_a_history_store_failure_does_not_break_the_batch(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient,
    ):
        """A batch that generated six materials must not fail because S3 refused 300 bytes."""

        class ExplodingStore(InMemoryBatchStore):
            def save_index(self, batch_id, record):
                raise RuntimeError("bucket on fire")

            def save_material(self, batch_id, material_id, record):
                raise RuntimeError("bucket on fire")

        tier = WebTier(auth, fanout_runtime, str(static_dir),
                       history=BatchHistory(ExplodingStore()))
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        body = fanout_runtime.body_for("slot-1")
        body.push_event(material_event("slot-1", "booking-hotel", "m-1"))
        body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0, "skipped": 0,
                         "degraded": 0, "at": time.time()})
        body.finish()

        recorder = await collect(tier, cookie,
                                 {"action": "generate", "scenarios": ["booking-hotel"],
                                  "count": 1})
        kinds = [e["type"] for e in recorder.events()]
        assert "material_completed" in kinds and "batch_completed" in kinds

    async def test_history_writes_do_not_block_the_stream(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient,
    ):
        """The recorder writes on its own thread, so a slow bucket cannot pace the cards.

        The store sleeps longer than the whole batch takes. If any write were inline the events
        would arrive after it, and this assertion on the elapsed time would fail.
        """

        class SlowStore(InMemoryBatchStore):
            def save_index(self, batch_id, record):
                time.sleep(0.4)
                super().save_index(batch_id, record)

        tier = WebTier(auth, fanout_runtime, str(static_dir), history=BatchHistory(SlowStore()))
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        for slot in ("slot-1", "slot-2", "slot-3"):
            body = fanout_runtime.body_for(slot)
            body.push_event(material_event(slot, "booking-hotel", "m-%s" % slot))
            body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0, "skipped": 0,
                             "degraded": 0, "at": time.time()})
            body.finish()

        started = time.monotonic()
        recorder = await collect(tier, cookie,
                                 {"action": "generate", "scenarios": ["booking-hotel"],
                                  "count": 3})
        # `close()` joins the worker for up to 5s, so the ceiling covers one final in-flight write
        # plus the flush -- not one per material, which is the difference being asserted.
        assert time.monotonic() - started < 2.0
        assert sum(1 for e in recorder.events() if e["type"] == "material_completed") == 3


# ── survival across a web-task restart ───────────────────────────────────────


class TestSurvivesRestart:
    async def test_a_batch_survives_a_web_task_restart(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient,
        batch_store: InMemoryBatchStore,
    ):
        """A Fargate redeploy is a new task over the same bucket. Substituted here exactly so.

        This is the property the whole feature rests on. The old batch store was a browser-side
        `Map`, so nothing outlived the request that created it.
        """
        first = WebTier(auth, fanout_runtime, str(static_dir),
                        history=BatchHistory(batch_store))
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        body = fanout_runtime.body_for("slot-1")
        body.push_event(material_event("slot-1", "booking-hotel", "20260730-booking-hotel-cccccccc"))
        body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0, "skipped": 0,
                         "degraded": 0, "at": time.time()})
        body.finish()
        await collect(first, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 1})
        batch_id = first.history.list_batches()[0]["batch_id"]

        # The task is replaced: a brand-new tier, a brand-new in-process state, the same bucket.
        second = WebTier(auth, FanOutRuntimeClient(), str(static_dir),
                         history=BatchHistory(batch_store))
        listed = second.history.list_batches()
        assert [b["batch_id"] for b in listed] == [batch_id]
        # And the artifacts too, not just the row: a batch you can see but not open is not history.
        full = second.history.get_batch(batch_id)
        assert full is not None
        assert full["materials"][0]["material_id"] == "20260730-booking-hotel-cccccccc"
        assert full["materials"][0]["material"]["listening_material_parts"]

    async def test_a_batch_interrupted_mid_stream_keeps_what_arrived(
        self, batch_store: InMemoryBatchStore,
    ):
        """The incremental-write property, which is why the record is not written once at the end.

        Two of three materials arrive and the task dies. A record written only at `batch_completed`
        would leave nothing -- the two delivered materials would be as lost as the third.

        Each material is awaited in the store BEFORE the next is fed, and that ordering is what makes
        the test load-bearing rather than lucky. Asserting only the end state passes even with the
        per-material write removed: `start()`'s own flush races the two `on_event` calls and usually
        catches both. Verified by mutation -- deleting the `_touch()` in `_on_material` passes the
        end-state version of this test and fails this one.
        """
        history = BatchHistory(batch_store)
        recorder = history.recorder("web-9-9", owner="a@amazon.com", requested_total=3,
                                    scenarios=[{"scenario_key": "booking-hotel", "count": 3}])
        recorder.start()
        try:
            for arrival, (slot, material_id) in enumerate(
                (("slot-1", "m-1"), ("slot-2", "m-2")), start=1
            ):
                recorder.on_event(material_event(slot, "booking-hotel", material_id))
                _await_materials(batch_store, "web-9-9", arrival)
        finally:
            # No `batch_completed` was ever fed: the task was killed, so nothing finalised the
            # record. `close()` here is the test tidying up its thread, not the tier finishing a
            # batch -- which is why `state` is still asserted to be "running" below.
            recorder.close()

        after_restart = BatchHistory(batch_store)
        view = after_restart.get_batch("web-9-9")
        assert view is not None
        assert view["arrived"] == 2 and view["requested_total"] == 3
        assert view["state"] == "running"
        assert view["completed_at"] is None
        assert {m["material_id"] for m in view["materials"]} == {"m-1", "m-2"}


# ── the added transition, and read-only ──────────────────────────────────────


class TestSubmit:
    def test_submit_records_the_status(self, history: BatchHistory,
                                       batch_store: InMemoryBatchStore):
        batch_store.save_index("b1", record(batch_id="b1"))
        view = history.submit("b1", ["m1"], actor="a@b.c")
        assert view["status"] == SUBMITTED
        assert view["read_only"] is True
        assert view["submitted_by"] == "a@b.c"
        assert view["submitted_material_ids"] == ["m1"]

    def test_submit_is_idempotent_and_keeps_the_first_time(
        self, history: BatchHistory, batch_store: InMemoryBatchStore,
    ):
        """`submitted_at` is when the batch stopped awaiting a decision; a revision does not move it.

        The id list, by contrast, is last-write-wins: the second submission is the reviewer's
        current opinion, not an addition to the first.
        """
        batch_store.save_index("b1", record(batch_id="b1"))
        first = history.submit("b1", ["m1"], actor="a@b.c")
        time.sleep(0.01)
        second = history.submit("b1", ["m2"], actor="a@b.c")
        assert second["submitted_at"] == first["submitted_at"]
        assert second["submitted_material_ids"] == ["m2"]

    def test_submit_de_duplicates_while_keeping_order(
        self, history: BatchHistory, batch_store: InMemoryBatchStore,
    ):
        batch_store.save_index("b1", record(batch_id="b1"))
        view = history.submit("b1", ["m2", "m1", "m2"], actor="a@b.c")
        assert view["submitted_material_ids"] == ["m2", "m1"]

    def test_submit_on_an_unknown_batch_raises(self, history: BatchHistory):
        """So the route 404s instead of silently creating a record for a batch that never ran."""
        with pytest.raises(KeyError):
            history.submit("nope", [], actor="a@b.c")


class TestReadOnly:
    def test_a_read_only_batch_is_flagged_by_the_backend(self, history: BatchHistory,
                                                          batch_store: InMemoryBatchStore):
        """`read_only` comes from the backend so a frontend mistake cannot make it mutable."""
        now = time.time()
        batch_store.save_index("fresh", record(batch_id="fresh", created_at=now))
        batch_store.save_index("old", record(batch_id="old",
                                             created_at=now - CANDIDATE_TTL_SECONDS - 1))
        by_id = {b["batch_id"]: b for b in history.list_batches(now=now)}
        assert by_id["fresh"]["read_only"] is False
        assert by_id["old"]["read_only"] is True

    def test_submitting_an_archived_batch_does_not_make_it_selectable_again(
        self, history: BatchHistory, batch_store: InMemoryBatchStore,
    ):
        """A submission records a decision; it cannot un-expire the candidates.

        The batch reads 已提交 afterwards -- which is honest, someone did submit it -- and stays
        read-only either way, so no path leads back to a selectable archived batch.
        """
        now = time.time()
        batch_store.save_index("old", record(batch_id="old",
                                             created_at=now - CANDIDATE_TTL_SECONDS - 1))
        view = history.submit("old", ["m1"], actor="a@b.c", now=now)
        assert view["status"] == SUBMITTED
        assert view["read_only"] is True


# ── routes ───────────────────────────────────────────────────────────────────


class TestRoutes:
    def test_history_routes_need_a_session(self, client):
        """Not on `PUBLIC_API_PATHS`, so the allowlist closes them by default."""
        assert client.get("/api/batch-history").status_code == 401
        assert client.get("/api/batch-history/b1").status_code == 401
        assert client.post("/api/batch-history/b1/submit", json={"material_ids": []}).status_code == 401

    def test_list_is_newest_first(self, client, batch_store: InMemoryBatchStore):
        """「数据按时间倒序」. Ordered by the store so every caller agrees."""
        register(client)
        now = time.time()
        for index, age in enumerate((300, 100, 200)):
            batch_store.save_index("b%d" % index,
                                   record(batch_id="b%d" % index, created_at=now - age))
        body = client.get("/api/batch-history").json()
        assert [b["batch_id"] for b in body["batches"]] == ["b1", "b2", "b0"]

    def test_a_batch_is_scoped_to_its_owner(self, client, batch_store: InMemoryBatchStore):
        """Two reviewers must not see each other's batches."""
        register(client, email="a@amazon.com")
        batch_store.save_index("mine", dict(record(batch_id="mine"), owner="a@amazon.com"))
        batch_store.save_index("theirs", dict(record(batch_id="theirs"), owner="b@amazon.com"))
        body = client.get("/api/batch-history").json()
        assert [b["batch_id"] for b in body["batches"]] == ["mine"]

    def test_a_record_with_no_owner_is_visible(self, client, batch_store: InMemoryBatchStore):
        """It predates the field. Hiding a batch somebody generated is the worse answer."""
        register(client, email="a@amazon.com")
        batch_store.save_index("legacy", record(batch_id="legacy"))
        body = client.get("/api/batch-history").json()
        assert [b["batch_id"] for b in body["batches"]] == ["legacy"]

    def test_a_material_route_answers_by_id_alone(self, client,
                                                   batch_store: InMemoryBatchStore):
        register(client)
        batch_store.save_material("b1", "m1", {"material": {"x": 1},
                                               "scenario_key": "booking-hotel"})
        body = client.get("/api/batch-history-material/m1").json()
        assert body["material_id"] == "m1"
        assert body["scenario_key"] == "booking-hotel"

    def test_the_material_route_needs_a_session(self, client):
        assert client.get("/api/batch-history-material/m1").status_code == 401

    def test_an_unknown_material_is_a_404(self, client):
        register(client)
        response = client.get("/api/batch-history-material/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "MATERIAL_NOT_FOUND"

    def test_getting_an_unknown_batch_is_a_404_naming_it(self, client):
        register(client)
        response = client.get("/api/batch-history/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "BATCH_NOT_FOUND"

    def test_submit_through_the_route(self, client, batch_store: InMemoryBatchStore):
        register(client, email="a@amazon.com")
        batch_store.save_index("b1", record(batch_id="b1"))
        body = client.post("/api/batch-history/b1/submit", json={"material_ids": ["m1"]}).json()
        assert body["status"] == SUBMITTED
        assert body["submitted_by"] == "a@amazon.com"

    def test_submit_rejects_a_bad_body(self, client, batch_store: InMemoryBatchStore):
        register(client)
        batch_store.save_index("b1", record(batch_id="b1"))
        assert client.post("/api/batch-history/b1/submit", json={}).status_code == 400

    def test_submit_on_an_unknown_batch_is_a_404(self, client):
        register(client)
        response = client.post("/api/batch-history/nope/submit", json={"material_ids": []})
        assert response.status_code == 404

    def test_a_store_failure_is_a_502_not_a_500(self, auth, static_dir, runtime):
        """The frontend reads `error.code`; an unhandled exception would give it a bare 500."""
        from fastapi.testclient import TestClient

        class ExplodingStore(InMemoryBatchStore):
            def load_all_indexes(self):
                raise RuntimeError("bucket unreachable")

        tier = WebTier(auth, runtime, str(static_dir), history=BatchHistory(ExplodingStore()))
        with TestClient(tier.app) as client:
            register(client)
            response = client.get("/api/batch-history")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "BATCH_HISTORY_UNAVAILABLE"


# ── storage ──────────────────────────────────────────────────────────────────


class TestStore:
    def test_the_s3_backend_round_trips_through_the_object_store(self):
        from audio_storage.object_store import InMemoryObjectStore

        backing = InMemoryObjectStore()
        store = S3BatchStore(backing)
        store.save_index("b1", {"batch_id": "b1", "created_at": 1.0})
        store.save_material("b1", "m1", {"material": {"x": 1}})
        assert store.load_index("b1")["batch_id"] == "b1"
        assert store.load_material("b1", "m1") == {"material": {"x": 1}}
        # Under `_batches/`, alongside `_candidates/` and `_history/` -- never inside a state
        # directory, where a reviewer's material listing would pick it up.
        assert all(k.startswith(BATCH_PREFIX) for k in backing.list_keys(""))

    def test_a_missing_record_is_none_not_an_error(self):
        from audio_storage.object_store import InMemoryObjectStore

        store = S3BatchStore(InMemoryObjectStore())
        assert store.load_index("nope") is None
        assert store.load_material("nope", "m") is None

    def test_listing_ignores_the_material_sidecars(self):
        """`list_keys` returns them too; a sidecar read as a batch would be a phantom row."""
        from audio_storage.object_store import InMemoryObjectStore

        store = S3BatchStore(InMemoryObjectStore())
        store.save_index("b1", {"batch_id": "b1", "created_at": 1.0})
        store.save_material("b1", "m1", {"material": {}})
        assert [r["batch_id"] for r in store.load_all_indexes()] == ["b1"]

    def test_one_unreadable_record_does_not_empty_the_panel(self):
        from audio_storage.object_store import InMemoryObjectStore

        backing = InMemoryObjectStore()
        store = S3BatchStore(backing)
        store.save_index("good", {"batch_id": "good", "created_at": 1.0})
        backing.put("_batches/bad/index.json", b"{ truncated")
        assert [r["batch_id"] for r in store.load_all_indexes()] == ["good"]

    def test_batch_records_have_no_ttl(self):
        """`_candidates/` expires on read; a batch record must not, because 已归档 is long-lived."""
        from audio_storage.object_store import InMemoryObjectStore

        store = S3BatchStore(InMemoryObjectStore())
        store.save_index("ancient", {"batch_id": "ancient",
                                     "created_at": time.time() - 365 * 24 * 3600})
        assert [r["batch_id"] for r in store.load_all_indexes()] == ["ancient"]

    def test_the_in_memory_store_does_not_alias_its_records(self):
        """Round-trips through JSON like the S3 backend, so a test cannot pass by mutating."""
        store = InMemoryBatchStore()
        original = {"batch_id": "b1", "created_at": 1.0, "materials": []}
        store.save_index("b1", original)
        original["materials"].append({"material_id": "sneaky"})
        assert store.load_index("b1")["materials"] == []


class TestMaterialByIdAlone:
    """`get_material` -- the reader page for a historical batch.

    The reader-page URL is `/materials/{id}` and names no batch, so this cannot go through
    `get_batch`. Found in the browser rather than by reading code: 阅读全文 on a historical batch led
    to "材料不存在", because the frontend only ever resolved a material from its in-session cache.
    That directly contradicts the client's rule for a read-only batch -- 可看材料、可试听 -- since
    both of those begin on the reader page.
    """

    def test_a_material_resolves_by_id_without_its_batch(
        self, history: BatchHistory, batch_store: InMemoryBatchStore,
    ):
        batch_store.save_material("b1", "m1", {"material": {"x": 1}, "scenario_key": "booking-hotel"})
        found = history.get_material("m1")
        assert found is not None
        assert found["material"] == {"x": 1}
        # The id is echoed back, because the caller looked it up by id and the record is keyed on it.
        assert found["material_id"] == "m1"

    async def test_the_scenario_is_carried_on_the_sidecar_itself(
        self, auth, static_dir, fanout_runtime: FanOutRuntimeClient, history: BatchHistory,
    ):
        """`get_material` never reads the index, so the sidecar has to know its own scenario.

        Otherwise the reader page gets artifacts with no idea which scenario they belong to -- and
        the summary fields being duplicated into the sidecar looks like redundancy until this breaks.
        Driven through a real fanned-out stream, so what is asserted is what the recorder actually
        writes rather than a hand-built sidecar.
        """
        tier = WebTier(auth, fanout_runtime, str(static_dir), history=history)
        cookie = auth.issue_token(auth.register("a@amazon.com", "hunter2hunter2")["email"])
        body = fanout_runtime.body_for("slot-1")
        body.push_event(material_event("slot-1", "booking-hotel", "m-real"))
        body.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0, "skipped": 0,
                         "degraded": 0, "at": time.time()})
        body.finish()
        await collect(tier, cookie,
                      {"action": "generate", "scenarios": ["booking-hotel"], "count": 1})

        found = history.get_material("m-real")
        assert found is not None
        assert found["scenario_key"] == "booking-hotel"
        assert found["material"]["listening_material_parts"]
        # And the batch it came from, so the reader page can label it.
        assert found["batch_id"].startswith("web-")

    def test_an_unknown_material_is_none(self, history: BatchHistory):
        assert history.get_material("nope") is None

    def test_the_s3_backend_finds_a_material_across_batches(self):
        """One LIST plus one GET, and it must not match a different batch's index object."""
        from audio_storage.object_store import InMemoryObjectStore

        store = S3BatchStore(InMemoryObjectStore())
        store.save_index("b1", {"batch_id": "b1", "created_at": 1.0})
        store.save_index("b2", {"batch_id": "b2", "created_at": 2.0})
        store.save_material("b2", "wanted", {"material": {"y": 2}})
        found = store.find_material("wanted")
        assert found == {"material": {"y": 2}}
        assert store.find_material("missing") is None

    def test_a_material_id_that_is_a_suffix_of_another_does_not_collide(self):
        """The lookup is a suffix match on the key, so `m1` must not resolve `xm1`."""
        from audio_storage.object_store import InMemoryObjectStore

        for store in (S3BatchStore(InMemoryObjectStore()), InMemoryBatchStore()):
            store.save_material("b1", "xm1", {"material": {"wrong": True}})
            assert store.find_material("m1") is None
            store.save_material("b1", "m1", {"material": {"right": True}})
            assert store.find_material("m1") == {"material": {"right": True}}


class TestScenarioShape:
    def test_scenarios_are_collapsed_to_counts_in_plan_order(self):
        """The panel renders 「🏨 酒店预订 × 2」, and its order must match the cards' order."""
        children, _ = plan_children(
            {"scenarios": ["booking-hotel", "employment-vacancy"],
             "counts": {"booking-hotel": 2, "employment-vacancy": 1}},
            batch_id="b1",
        )
        assert _scenario_shape(children) == [
            {"scenario_key": "booking-hotel", "count": 2},
            {"scenario_key": "employment-vacancy", "count": 1},
        ]

    def test_the_custom_scenario_appears_last(self):
        children, _ = plan_children(
            {"scenarios": ["booking-hotel"], "count": 1,
             "custom_scenario": {"prompt_hint": "x", "count": 2}},
            batch_id="b1",
        )
        assert _scenario_shape(children) == [
            {"scenario_key": "booking-hotel", "count": 1},
            {"scenario_key": "custom", "count": 2},
        ]


class TestBatchId:
    def test_the_id_format_is_unchanged(self):
        """It namespaces the backend's candidate groups, so it cannot be prettified freely."""
        assert new_batch_id(3, now=1_700_000_000.0) == "web-1700000000000-3"
