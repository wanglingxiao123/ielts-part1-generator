"""Fixtures and the runtime stubs. Nothing here touches AWS.

The stubs are the interesting part. `FakeStreamingBody` imitates botocore's `StreamingBody` closely
enough that the relay cannot tell the difference -- specifically it implements
`iter_lines(chunk_size=...)` as a *blocking* generator whose next line may not be available yet.
That is what makes the incremental-delivery test meaningful: a buffered relay implementation would
deadlock or fail it rather than quietly pass.

`FanOutRuntimeClient` is the same idea one level up. `action: generate` is no longer one invocation
but N, so a stub that answers every call with one shared body could not tell a working fan-out from
a broken one. This one hands each invocation its own body, records the payload and session id per
child, and lets a test hold a child open, fail it, or count how many were in flight at once --
which is how the concurrency cap is asserted at all.
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.auth import AuthService, MemoryUserStore, SessionSigner  # noqa: E402
from web.runtime_client import SSE_CONTENT_TYPE  # noqa: E402
from web.app import WebTier  # noqa: E402

SENTINEL = object()


class FakeStreamingBody:
    """A botocore-shaped streaming body fed from a queue.

    `iter_lines` blocks on the queue, so the reader genuinely waits for the producer. `close()`
    is recorded because the relay is supposed to call it.
    """

    # Every blocking read is bounded. A buffering relay would otherwise wedge the threadpool
    # thread forever and the suite would hang instead of failing -- verified by mutation-testing
    # the relay into a buffered one, which hung the run until this timeout was added.
    READ_TIMEOUT_SECONDS = 10.0

    def __init__(self) -> None:
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self.closed = False
        self.chunk_sizes: List[int] = []
        # Called once when `iter_lines` ends, by whichever way it ends (sentinel or close). Lets
        # FanOutRuntimeClient know a child released its concurrency slot without the child having
        # to report anything.
        self.on_exhausted: Optional[Callable[[], None]] = None
        self._exhausted = False

    # producer side ----------------------------------------------------------
    def push_event(self, event: Dict[str, Any]) -> None:
        self._queue.put(("data: %s" % json.dumps(event)).encode("utf-8"))
        self._queue.put(b"")  # the blank line that terminates an SSE frame

    def push_raw(self, line: bytes) -> None:
        self._queue.put(line)

    def finish(self) -> None:
        self._queue.put(SENTINEL)

    # consumer side ----------------------------------------------------------
    def _next(self) -> Any:
        try:
            return self._queue.get(timeout=self.READ_TIMEOUT_SECONDS)
        except queue.Empty:
            raise AssertionError(
                "no stream data for %.0fs: the relay is not consuming lines as they arrive "
                "(a buffered relay produces exactly this)" % self.READ_TIMEOUT_SECONDS
            )

    def iter_lines(self, chunk_size: int = 1024) -> Iterator[bytes]:
        self.chunk_sizes.append(chunk_size)
        try:
            while True:
                item = self._next()
                if item is SENTINEL:
                    return
                yield item
        finally:
            self._exhaust()

    def _exhaust(self) -> None:
        if self._exhausted:
            return
        self._exhausted = True
        if self.on_exhausted is not None:
            self.on_exhausted()

    def read(self) -> bytes:
        parts: List[bytes] = []
        while True:
            item = self._next()
            if item is SENTINEL:
                break
            parts.append(item)
        return b"".join(parts)

    def close(self) -> None:
        self.closed = True
        # A real StreamingBody's close() unblocks a reader parked on the socket. That property is
        # what `FanOut.close()` relies on to stop an abandoned child, so the fake has to have it:
        # without the sentinel the reader would sit here until READ_TIMEOUT_SECONDS and the test
        # would fail on a timeout rather than on the behaviour under test.
        self._queue.put(SENTINEL)


class StubRuntimeClient:
    """Stands in for `AgentCoreRuntimeClient`. Records every payload it is handed."""

    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.calls: List[Dict[str, Any]] = []
        self.session_ids: List[str] = []
        self.json_response: Dict[str, Any] = {"scenarios": {"version": 1}}
        self.stream: Optional[FakeStreamingBody] = None
        self.raise_on_invoke: Optional[Exception] = None

    def invoke(self, payload: Dict[str, Any], *, session_id: Optional[str] = None):
        self.calls.append(payload)
        self.session_ids.append(session_id or "")
        if self.raise_on_invoke is not None:
            raise self.raise_on_invoke
        if self.stream is not None:
            return SSE_CONTENT_TYPE, self.stream, session_id or ""
        body = FakeStreamingBody()
        body.push_raw(json.dumps(self.json_response).encode("utf-8"))
        body.finish()
        return "application/json", body, session_id or ""


def slot_from_session(session_id: str) -> str:
    """`ielts-slot-3-<hex>` -> `slot-3`. The inverse of fanout.py's session id prefix."""
    parts = session_id.split("-")
    if len(parts) >= 3 and parts[0] == "ielts" and parts[1] == "slot":
        return "slot-%s" % parts[2]
    return "slot-?"


class FanOutRuntimeClient:
    """A runtime whose every `invoke` gets its own body. For the fanned-out `generate` path.

    Bodies are addressed by the child's `slot_id`, NOT by call order. Children run on N executor
    threads, so which one reaches `invoke` first is genuinely a race -- an early version of this
    stub keyed on call order and the "one child fails, the others complete" test failed roughly half
    the time by blaming the wrong slot. `body_for("slot-2")` is deterministic because the slot id is
    fixed by `plan_children` before any thread starts.

    The slot id is recovered from the `runtimeSessionId`, which `fanout.py` prefixes with it for
    exactly this reason -- it is the only per-child field that reaches AWS, and therefore the only
    one a CloudWatch log stream can be matched back to a card by.

    `in_flight` / `peak_in_flight` count invocations that have returned headers but whose body has
    not been exhausted. That is the number the concurrency cap governs -- a material in flight, not
    a task queued -- so it is the number the cap test asserts on.
    """

    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.calls: List[Dict[str, Any]] = []
        self.session_ids: List[str] = []
        # slot_id -> body. Created on demand by `body_for`, so a test can arm a child before the
        # request even starts.
        self.by_slot: Dict[str, "FakeStreamingBody"] = {}
        # slot_id -> exception, so one named child can be refused at invoke time.
        self.fail_slots: Dict[str, Exception] = {}
        self.raise_on_invoke: Optional[Exception] = None
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = threading.Lock()

    def body_for(self, slot_id: str) -> "FakeStreamingBody":
        """The body handed to the child owning `slot_id`. Created on demand.

        Pre-creating it means a test can arm a child (`body_for("slot-1").push_event(...)`, or
        substituting an exploding body) BEFORE the request starts, which removes the last race from
        these tests.
        """
        with self._lock:
            body = self.by_slot.get(slot_id)
            if body is None:
                body = FakeStreamingBody()
                self.by_slot[slot_id] = body
            return body

    def set_body(self, slot_id: str, body: "FakeStreamingBody") -> None:
        with self._lock:
            self.by_slot[slot_id] = body

    def invoke(self, payload: Dict[str, Any], *, session_id: Optional[str] = None):
        slot_id = slot_from_session(session_id or "")
        with self._lock:
            self.calls.append(payload)
            self.session_ids.append(session_id or "")
        if self.raise_on_invoke is not None:
            raise self.raise_on_invoke
        if slot_id in self.fail_slots:
            raise self.fail_slots[slot_id]
        body = self.body_for(slot_id)
        body.on_exhausted = self._leave
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        return SSE_CONTENT_TYPE, body, session_id or ""

    def _leave(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def finish_all(self) -> None:
        with self._lock:
            bodies = list(self.by_slot.values())
        for body in bodies:
            body.finish()


async def collect(tier, cookie: str, payload: Dict[str, Any], *, timeout: float = 10.0):
    """Drive `/api/invocations` to completion and return the decoded events, in arrival order.

    Used by the fan-out tests instead of `TestClient`: TestClient drains the whole body before
    handing back a response, which is fine for whole-stream assertions and useless for anything
    about ordering. Driving the ASGI app directly keeps both available from one helper.
    """
    from .test_sse_relay import BodyRecorder, _scope

    body = json.dumps(payload).encode()
    sent = False

    async def receive() -> Dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover

    recorder = BodyRecorder()
    await asyncio.wait_for(tier.app(_scope(cookie), receive, recorder), timeout=timeout)
    return recorder


@pytest.fixture
def store() -> MemoryUserStore:
    return MemoryUserStore()


@pytest.fixture
def auth(store: MemoryUserStore) -> AuthService:
    return AuthService(store, SessionSigner(b"test-signing-key"), ["*"])


@pytest.fixture
def runtime() -> StubRuntimeClient:
    return StubRuntimeClient()


@pytest.fixture
def fanout_runtime() -> FanOutRuntimeClient:
    return FanOutRuntimeClient()


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    assets = directory / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    return directory


@pytest.fixture
def tier(auth: AuthService, runtime: StubRuntimeClient, static_dir: Path) -> WebTier:
    return WebTier(auth, runtime, str(static_dir))


@pytest.fixture
def client(tier: WebTier):
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as test_client:
        yield test_client


def register(test_client, email: str = "a@amazon.com", password: str = "hunter2hunter2"):
    return test_client.post("/api/auth/register", json={"email": email, "password": password})


__all__ = [
    "FakeStreamingBody",
    "FanOutRuntimeClient",
    "StubRuntimeClient",
    "collect",
    "register",
    "threading",
]
