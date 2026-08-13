import threading
import time

from web.execution_manager import ExecutionManager


def test_observer_disconnect_does_not_stop_producer():
    release = threading.Event()
    finished = threading.Event()
    manager = ExecutionManager()

    def producer(publish):
        publish(b"data: started\n\n")
        release.wait(timeout=1)
        publish(b"data: completed\n\n")
        finished.set()

    execution, started = manager.start("revision:r1", producer)
    assert started is True
    observer = execution.observe(heartbeat_seconds=0.01)
    assert next(observer) == b"data: started\n\n"
    observer.close()

    release.set()
    assert finished.wait(timeout=1)
    replay = list(execution.observe(heartbeat_seconds=0.01))
    assert replay == [b"data: started\n\n", b"data: completed\n\n"]


def test_duplicate_start_reuses_execution_without_calling_second_producer():
    release = threading.Event()
    calls = []
    manager = ExecutionManager()

    def first(publish):
        calls.append("first")
        release.wait(timeout=1)

    def second(publish):
        calls.append("second")

    original, started = manager.start("batch:b1", first)
    duplicate, duplicate_started = manager.start("batch:b1", second)
    assert started is True
    assert duplicate_started is False
    assert duplicate is original
    release.set()

    deadline = time.monotonic() + 1
    while not original.done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["first"]


def test_different_execution_ids_run_independently():
    revision_release = threading.Event()
    batch_finished = threading.Event()
    manager = ExecutionManager()

    def revision(publish):
        revision_release.wait(timeout=1)
        publish(b"revision complete")

    def batch(publish):
        publish(b"batch complete")
        batch_finished.set()

    revision_execution, _ = manager.start("revision:r1", revision)
    batch_execution, _ = manager.start("batch:b1", batch)

    assert batch_finished.wait(timeout=1)
    assert batch_execution.done
    assert not revision_execution.done
    revision_release.set()
