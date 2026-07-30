"""The SSE response must deliver events as they arrive, not buffered to the end of the batch.

This is the property most likely to regress silently: an implementation that collects events into a
list and returns them at the end passes every "did the browser get the right events" test, and
breaks the only thing the endpoint is for -- progress visible during a multi-minute batch instead of
after it.

`TestClient` cannot prove it. Verified by reading `starlette/testclient.py` and by experiment: the
transport does `raw_kwargs["stream"] = httpx.ByteStream(raw_kwargs["stream"].read())`, i.e. the
whole body is drained before `Response` is constructed, so even `client.stream()` hands the test a
complete buffer. A test written against it would pass under a buffered implementation.

So these tests drive the ASGI app directly and watch the `http.response.body` messages. The key
test is `test_the_stream_is_incremental_not_buffered`: the producer refuses to emit event N+1 until
the transport has *seen* event N. Under a buffering implementation the two sides deadlock and the
test fails on its timeout rather than passing quietly.

There are now TWO paths, and both are covered here:

* `action: generate` is fanned out over N invocations and merged (`web/fanout.py`, `_frames`). This
  is the path every real batch takes, so it is the one the incremental-delivery tests drive.
* every other action is a single invocation relayed by `_relay`, a sync generator run through
  `iterate_in_threadpool`. No action answers `generate`-style SSE today, so this is a generic-proxy
  guarantee rather than a live one -- kept, and tested, because the alternative is silently
  buffering a streaming body the day a second streaming action appears.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from web.auth import SESSION_COOKIE

from .conftest import FakeStreamingBody


class BodyRecorder:
    """A minimal ASGI `send` that records each response-body message as the app emits it."""

    def __init__(self) -> None:
        self.status: Optional[int] = None
        self.headers: Dict[str, str] = {}
        self.chunks: List[bytes] = []
        self.complete = False
        self.first_chunk = asyncio.Event()
        # One event per chunk index, so a producer can await "chunk k has been sent".
        self.chunk_arrived = asyncio.Event()

    async def __call__(self, message: Dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                self.chunks.append(body)
                self.first_chunk.set()
                self.chunk_arrived.set()
            if not message.get("more_body", False):
                self.complete = True

    def events(self) -> List[Dict[str, Any]]:
        text = b"".join(self.chunks).decode("utf-8")
        return [
            json.loads(frame[len("data: "):])
            for frame in text.split("\n\n")
            if frame.startswith("data: ")
        ]


def _scope(cookie: str, path: str = "/api/invocations") -> Dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"cookie", ("%s=%s" % (SESSION_COOKIE, cookie)).encode()),
        ],
    }


async def _drive(tier, cookie: str, payload: Dict[str, Any]) -> Tuple[BodyRecorder, asyncio.Task]:
    """Start the app on a scope and return the recorder plus the still-running task."""
    body = json.dumps(payload).encode()
    sent = False

    async def receive() -> Dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)  # no disconnect during the test
        return {"type": "http.disconnect"}  # pragma: no cover

    recorder = BodyRecorder()
    task = asyncio.ensure_future(tier.app(_scope(cookie), receive, recorder))
    return recorder, task


@pytest.fixture
def logged_in(tier):
    tier.auth.register("a@amazon.com", "hunter2hunter2")
    return tier.auth.issue_token("a@amazon.com")


ONE_SET = {"action": "generate", "scenarios": ["accommodation-rental"], "count": 1}


@pytest.fixture
def fan_tier(auth, fanout_runtime, static_dir):
    """A tier whose runtime hands each child invocation its own body."""
    from web.app import WebTier

    return WebTier(auth, fanout_runtime, str(static_dir))


@pytest.fixture
def fan_logged_in(fan_tier):
    fan_tier.auth.register("a@amazon.com", "hunter2hunter2")
    return fan_tier.auth.issue_token("a@amazon.com")


async def _await_calls(runtime, count: int, timeout: float = 5.0) -> None:
    """Wait until `count` child invocations have returned headers."""
    for _ in range(int(timeout / 0.005)):
        if len(runtime.calls) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(
        "only %d of %d children were invoked within %.1fs" % (len(runtime.calls), count, timeout)
    )


@pytest.mark.asyncio
async def test_the_stream_is_incremental_not_buffered(fan_tier, fanout_runtime, fan_logged_in):
    """Each event must reach the transport before the next one is produced.

    The producer awaits `chunk_arrived` between events, so a buffered implementation would never let
    the producer advance, nothing would ever be sent, and this test would time out.
    """
    stream = fanout_runtime.body_for("slot-1")
    recorder, task = await _drive(fan_tier, fan_logged_in, ONE_SET)
    await _await_calls(fanout_runtime, 1)

    # The merged `batch_started` is emitted by the web tier before any child answers, so it is
    # already on the wire. Everything after it comes from a child.
    await asyncio.wait_for(recorder.chunk_arrived.wait(), timeout=5)
    assert [e["type"] for e in recorder.events()] == ["batch_started"]

    delivered: List[int] = []
    for index in range(4):
        recorder.chunk_arrived.clear()
        stream.push_event({"type": "stage", "slot_id": "slot-1", "stage": "step-%d" % index})
        await asyncio.wait_for(recorder.chunk_arrived.wait(), timeout=5)
        # The decisive assertion: the browser has bytes while the Runtime stream is still open.
        assert recorder.complete is False, "response closed before the batch finished"
        delivered.append(len(recorder.chunks))

    assert delivered == [2, 3, 4, 5], (
        "events must arrive one at a time; got cumulative counts %r" % delivered
    )

    stream.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0})
    stream.finish()
    await asyncio.wait_for(task, timeout=5)

    assert recorder.complete is True
    assert [e["type"] for e in recorder.events()] == (
        ["batch_started"] + ["stage"] * 4 + ["batch_completed"]
    )


@pytest.mark.asyncio
async def test_headers_arrive_before_any_invoke(fan_tier, fanout_runtime, fan_logged_in):
    """Status and content-type must be sent up front, or the browser cannot start parsing.

    Stronger than it used to be: the headers now precede the first `invoke_agent_runtime` call
    entirely, because nothing is awaited before the StreamingResponse is returned. A user who
    submits ten sets sees the skeleton grid immediately rather than after the first cold start.
    """
    recorder, task = await _drive(fan_tier, fan_logged_in, ONE_SET)

    for _ in range(400):
        if recorder.status is not None:
            break
        await asyncio.sleep(0.005)
    assert recorder.status == 200
    assert "text/event-stream" in recorder.headers.get("content-type", "")
    # `no-transform` and `X-Accel-Buffering: no` keep an intermediary from re-buffering the stream.
    assert "no-cache" in recorder.headers.get("cache-control", "")
    assert recorder.headers.get("x-accel-buffering") == "no"
    # Content-Length would force the whole body to be known up front.
    assert "content-length" not in recorder.headers

    await _await_calls(fanout_runtime, 1)
    fanout_runtime.finish_all()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_frames_are_reframed_one_per_message(fan_tier, fanout_runtime, fan_logged_in):
    """Each chunk is a complete `data: ...\\n\\n` frame the browser can parse alone."""
    recorder, task = await _drive(fan_tier, fan_logged_in, ONE_SET)
    await asyncio.wait_for(recorder.chunk_arrived.wait(), timeout=5)

    frame = recorder.chunks[0].decode()
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame[6:].strip())["type"] == "batch_started"

    await _await_calls(fanout_runtime, 1)
    fanout_runtime.finish_all()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_child_dying_mid_stream_fails_only_its_own_slot(
    fan_tier, fanout_runtime, fan_logged_in
):
    """A child stream that dies must report its slot and leave the batch running.

    This replaces the old whole-response `batch_failed`: with one invocation per material, a broken
    connection is one card's problem. Turning it into a terminal `batch_failed` would throw away the
    other children -- which is the entire reason the fan-out exists.
    """

    class ExplodingBody(FakeStreamingBody):
        def iter_lines(self, chunk_size: int = 1024):
            yield b'data: {"type":"stage","slot_id":"slot-1","stage":"generating"}'
            raise ConnectionResetError("runtime went away")

    fanout_runtime.set_body("slot-1", ExplodingBody())
    # Armed before the request, so which thread wins the race to `invoke` cannot matter.
    healthy = fanout_runtime.body_for("slot-2")
    healthy.push_event(
        {"type": "material_completed", "slot_id": "slot-1", "scenario": "b", "ok": True}
    )
    healthy.push_event({"type": "batch_completed", "succeeded": 1, "failed": 0})

    recorder, task = await _drive(
        fan_tier, fan_logged_in,
        {"action": "generate", "scenarios": ["a", "b"], "count": 1},
    )
    await _await_calls(fanout_runtime, 2)
    fanout_runtime.finish_all()
    await asyncio.wait_for(task, timeout=10)

    events = recorder.events()
    assert events[0]["type"] == "batch_started"
    assert events[-1]["type"] == "batch_completed"
    failed = [e for e in events if e["type"] == "material_failed"]
    assert len(failed) == 1
    assert failed[0]["slot_id"] == "slot-1"
    assert "ConnectionResetError" in str(failed[0]["detail"])
    # The other child's material still arrived, on its own slot.
    completed = [e for e in events if e["type"] == "material_completed"]
    assert [e["slot_id"] for e in completed] == ["slot-2"]
    assert events[-1]["succeeded"] == 1 and events[-1]["failed"] == 1
    assert recorder.status == 200, "headers were already sent; the error rides in the body"


@pytest.mark.asyncio
async def test_every_child_body_is_closed(fan_tier, fanout_runtime, fan_logged_in):
    recorder, task = await _drive(
        fan_tier, fan_logged_in,
        {"action": "generate", "scenarios": ["a"], "count": 3},
    )
    await _await_calls(fanout_runtime, 3)
    fanout_runtime.finish_all()
    await asyncio.wait_for(task, timeout=10)
    assert sorted(fanout_runtime.by_slot) == ["slot-1", "slot-2", "slot-3"]
    assert all(b.closed for b in fanout_runtime.by_slot.values())


@pytest.mark.asyncio
async def test_the_invoke_calls_do_not_block_the_event_loop(
    fan_tier, fanout_runtime, fan_logged_in
):
    """boto3 is synchronous, so it has to run off the loop or /healthz stalls under load.

    A ticker counts event-loop iterations while three slow invokes are in flight. If any of them ran
    inline the loop would be pinned and the ticker would not advance -- and an instance whose health
    check times out is killed by AgentCore, taking the batch with it.
    """
    import time as clock

    real_invoke = fanout_runtime.invoke

    def slow_invoke(payload, *, session_id=None):
        clock.sleep(0.2)  # blocking, exactly like a real boto3 call
        return real_invoke(payload, session_id=session_id)

    fanout_runtime.invoke = slow_invoke

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    spinner = asyncio.ensure_future(ticker())
    recorder, task = await _drive(
        fan_tier, fan_logged_in,
        {"action": "generate", "scenarios": ["a"], "count": 3},
    )
    await _await_calls(fanout_runtime, 3)
    fanout_runtime.finish_all()
    await asyncio.wait_for(task, timeout=10)
    spinner.cancel()

    assert ticks > 5, "event loop was blocked during invoke (only %d ticks)" % ticks


# ── the generic single-invocation relay (every action that is not `generate`) ─


@pytest.mark.asyncio
async def test_a_non_generate_streaming_answer_is_relayed_incrementally(tier, runtime, logged_in):
    """`_relay` is the sync-generator path, and it must not buffer either.

    No action answers `generate`-style SSE today, so this exercises the generic proxy rather than a
    live route. Kept because the failure mode is invisible: a `_relay` rewritten to collect lines
    into a list would pass every functional test and silently buffer the first streaming action
    someone adds.
    """
    stream = FakeStreamingBody()
    runtime.stream = stream
    recorder, task = await _drive(tier, logged_in, {"action": "some_future_stream"})

    delivered: List[int] = []
    for index in range(3):
        recorder.chunk_arrived.clear()
        stream.push_event({"type": "tick", "n": index})
        await asyncio.wait_for(recorder.chunk_arrived.wait(), timeout=5)
        assert recorder.complete is False
        delivered.append(len(recorder.chunks))
    assert delivered == [1, 2, 3]

    stream.finish()
    await asyncio.wait_for(task, timeout=5)
    assert recorder.complete is True
    assert stream.closed is True
    assert [e["type"] for e in recorder.events()] == ["tick"] * 3


@pytest.mark.asyncio
async def test_the_relay_reports_a_mid_stream_failure(tier, runtime, logged_in):
    """A single relayed stream that dies has no per-slot identity to blame, so it says so."""

    class ExplodingBody(FakeStreamingBody):
        def iter_lines(self, chunk_size: int = 1024):
            yield b'data: {"type":"tick"}'
            raise ConnectionResetError("runtime went away")

    runtime.stream = ExplodingBody()
    recorder, task = await _drive(tier, logged_in, {"action": "some_future_stream"})
    await asyncio.wait_for(task, timeout=5)

    assert [e["type"] for e in recorder.events()] == ["tick", "batch_failed"]
    failure = recorder.events()[-1]
    assert failure["reason"] == "stream_error"
    assert "ConnectionResetError" in failure["detail"]
    assert recorder.status == 200


@pytest.mark.asyncio
async def test_the_relay_call_does_not_block_the_event_loop(tier, runtime, logged_in):
    """The unary/relay path still hands boto3 to a threadpool rather than the loop."""
    import time as clock

    stream = FakeStreamingBody()

    def slow_invoke(payload, *, session_id=None):
        clock.sleep(0.3)  # blocking, exactly like a real boto3 call
        return "text/event-stream", stream, session_id or ""

    runtime.invoke = slow_invoke

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    spinner = asyncio.ensure_future(ticker())
    recorder, task = await _drive(tier, logged_in, {"action": "some_future_stream"})
    stream.push_event({"type": "tick"})
    stream.finish()
    await asyncio.wait_for(task, timeout=5)
    spinner.cancel()

    assert ticks > 5, "event loop was blocked during invoke (only %d ticks)" % ticks
