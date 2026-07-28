"""The SSE relay must deliver events as they arrive, not buffered to the end of the batch.

This is the property most likely to regress silently: a relay that collects lines into a list and
returns them at the end passes every "did the browser get the right events" test, and breaks the
only thing the endpoint is for -- progress visible during an ~8-minute batch instead of after it.

`TestClient` cannot prove it. Verified by reading `starlette/testclient.py` and by experiment: the
transport does `raw_kwargs["stream"] = httpx.ByteStream(raw_kwargs["stream"].read())`, i.e. the
whole body is drained before `Response` is constructed, so even `client.stream()` hands the test a
complete buffer. A test written against it would pass under a buffered relay.

So these tests drive the ASGI app directly and watch the `http.response.body` messages. The key
test is `test_relay_is_incremental_not_buffered`: the producer refuses to emit event N+1 until the
transport has *seen* event N. Under a buffering relay the two sides deadlock and the test fails on
its timeout rather than passing quietly.
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


@pytest.mark.asyncio
async def test_relay_is_incremental_not_buffered(tier, runtime, logged_in):
    """Each event must reach the transport before the next one is produced.

    The producer awaits `chunk_arrived` between events, so if the relay buffered the whole stream
    the producer would never advance, nothing would ever be sent, and this test would time out.
    """
    stream = FakeStreamingBody()
    runtime.stream = stream
    recorder, task = await _drive(tier, logged_in, {"action": "generate"})

    delivered_before_close: List[int] = []
    for index in range(4):
        recorder.chunk_arrived.clear()
        # `push_event` is a plain queue put; the blocking read happens in Starlette's threadpool.
        stream.push_event({"type": "stage", "slot_id": "slot-%d" % (index + 1)})
        await asyncio.wait_for(recorder.chunk_arrived.wait(), timeout=5)
        # The decisive assertion: the browser has bytes while the Runtime stream is still open.
        assert recorder.complete is False, "response closed before the batch finished"
        delivered_before_close.append(len(recorder.chunks))

    assert delivered_before_close == [1, 2, 3, 4], (
        "events must arrive one at a time; got cumulative counts %r" % delivered_before_close
    )

    stream.push_event({"type": "batch_completed", "succeeded": 4, "failed": 0})
    stream.finish()
    await asyncio.wait_for(task, timeout=5)

    assert recorder.complete is True
    assert [e["type"] for e in recorder.events()] == ["stage"] * 4 + ["batch_completed"]


@pytest.mark.asyncio
async def test_headers_arrive_before_the_first_event(tier, runtime, logged_in):
    """Status and content-type must be sent up front, or the browser cannot start parsing."""
    stream = FakeStreamingBody()
    runtime.stream = stream
    recorder, task = await _drive(tier, logged_in, {"action": "generate"})

    for _ in range(100):
        if recorder.status is not None:
            break
        await asyncio.sleep(0.01)
    assert recorder.status == 200
    assert "text/event-stream" in recorder.headers.get("content-type", "")
    assert recorder.chunks == [], "headers should precede any body"
    # `no-transform` and `X-Accel-Buffering: no` keep an intermediary from re-buffering the stream.
    assert "no-cache" in recorder.headers.get("cache-control", "")
    assert recorder.headers.get("x-accel-buffering") == "no"
    # Content-Length would force the whole body to be known up front.
    assert "content-length" not in recorder.headers

    stream.finish()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_frames_are_reframed_one_per_message(tier, runtime, logged_in):
    """Each relayed chunk is a complete `data: ...\\n\\n` frame the browser can parse alone."""
    stream = FakeStreamingBody()
    runtime.stream = stream
    recorder, task = await _drive(tier, logged_in, {"action": "generate"})

    recorder.chunk_arrived.clear()
    stream.push_event({"type": "batch_started", "total": 1})
    await asyncio.wait_for(recorder.chunk_arrived.wait(), timeout=5)
    frame = recorder.chunks[0].decode()
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame[6:].strip())["type"] == "batch_started"

    stream.finish()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_mid_stream_failure_becomes_a_batch_failed_frame(tier, runtime, logged_in):
    """A stream that dies mid-batch must say so; the frontend cannot reconnect to a lost batch."""

    class ExplodingBody(FakeStreamingBody):
        def iter_lines(self, chunk_size: int = 1024):
            yield b'data: {"type":"stage","slot_id":"slot-1"}'
            raise ConnectionResetError("runtime went away")

    stream = ExplodingBody()
    runtime.stream = stream
    recorder, task = await _drive(tier, logged_in, {"action": "generate"})
    await asyncio.wait_for(task, timeout=5)

    types = [e["type"] for e in recorder.events()]
    assert types == ["stage", "batch_failed"]
    failure = recorder.events()[-1]
    assert failure["reason"] == "stream_error"
    assert "ConnectionResetError" in failure["detail"]
    assert recorder.status == 200, "headers were already sent; the error rides in the body"


@pytest.mark.asyncio
async def test_the_runtime_body_is_closed(tier, runtime, logged_in):
    stream = FakeStreamingBody()
    runtime.stream = stream
    recorder, task = await _drive(tier, logged_in, {"action": "generate"})
    stream.finish()
    await asyncio.wait_for(task, timeout=5)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_the_invoke_call_does_not_block_the_event_loop(tier, runtime, logged_in):
    """boto3 is synchronous, so it has to run in a threadpool or /healthz stalls under load.

    A ticker counts event-loop iterations while a slow invoke is in flight. If the call ran inline
    the loop would be pinned and the ticker would not advance.
    """
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
    recorder, task = await _drive(tier, logged_in, {"action": "generate"})
    stream.push_event({"type": "batch_completed"})
    stream.finish()
    await asyncio.wait_for(task, timeout=5)
    spinner.cancel()

    assert ticks > 5, "event loop was blocked during invoke (only %d ticks)" % ticks
