"""Fixtures and the runtime stub. Nothing here touches AWS.

The stub is the interesting part. `FakeStreamingBody` imitates botocore's `StreamingBody` closely
enough that the relay cannot tell the difference -- specifically it implements
`iter_lines(chunk_size=...)` as a *blocking* generator whose next line may not be available yet.
That is what makes the incremental-delivery test meaningful: a buffered relay implementation would
deadlock or fail it rather than quietly pass.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

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
        while True:
            item = self._next()
            if item is SENTINEL:
                return
            yield item

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
    "StubRuntimeClient",
    "register",
    "threading",
]
