"""Process-owned background executions with detachable SSE observers."""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable, Deque, Dict, Iterator, Optional, Tuple

LOG = logging.getLogger(__name__)


class Execution:
    def __init__(self, execution_id: str, *, buffer_size: int) -> None:
        self.execution_id = execution_id
        self._frames: Deque[Tuple[int, bytes]] = collections.deque(
            maxlen=max(1, buffer_size))
        self._next_sequence = 0
        self._condition = threading.Condition()
        self._done = False

    def publish(self, frame: bytes) -> None:
        with self._condition:
            self._next_sequence += 1
            self._frames.append((self._next_sequence, frame))
            self._condition.notify_all()

    def finish(self) -> None:
        with self._condition:
            self._done = True
            self._condition.notify_all()

    @property
    def done(self) -> bool:
        with self._condition:
            return self._done

    def observe(self, *, heartbeat_seconds: float = 15.0) -> Iterator[bytes]:
        cursor = 0
        while True:
            with self._condition:
                available = next(
                    ((sequence, frame) for sequence, frame in self._frames
                     if sequence > cursor),
                    None,
                )
                if available is not None:
                    cursor, frame = available
                elif self._done:
                    return
                else:
                    notified = self._condition.wait(timeout=heartbeat_seconds)
                    if not notified:
                        frame = b": hb\n\n"
                    else:
                        continue
            yield frame


Producer = Callable[[Callable[[bytes], None]], None]


class ExecutionManager:
    """Own producers independently from any one HTTP response."""

    def __init__(self, *, buffer_size: int = 512, retention_seconds: float = 3600) -> None:
        self._buffer_size = buffer_size
        self._retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._executions: Dict[str, Execution] = {}
        self._finished_at: Dict[str, float] = {}

    def start(self, execution_id: str, producer: Producer) -> tuple[Execution, bool]:
        with self._lock:
            self._prune_locked()
            existing = self._executions.get(execution_id)
            if existing is not None:
                return existing, False
            execution = Execution(execution_id, buffer_size=self._buffer_size)
            self._executions[execution_id] = execution

        def run() -> None:
            try:
                producer(execution.publish)
            except Exception:
                LOG.exception("background execution %s failed", execution_id)
            finally:
                execution.finish()
                with self._lock:
                    self._finished_at[execution_id] = time.monotonic()

        threading.Thread(
            target=run,
            name="web-execution-%s" % execution_id[:12],
            daemon=True,
        ).start()
        return execution, True

    def get(self, execution_id: str) -> Optional[Execution]:
        with self._lock:
            self._prune_locked()
            return self._executions.get(execution_id)

    def is_running(self, execution_id: str) -> bool:
        found = self.get(execution_id)
        return found is not None and not found.done

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            execution_id
            for execution_id, finished_at in self._finished_at.items()
            if now - finished_at >= self._retention_seconds
        ]
        for execution_id in expired:
            self._finished_at.pop(execution_id, None)
            self._executions.pop(execution_id, None)
