"""A Runtime that only waits, so that the platform's own time limits can be observed.

Answers one question that no local test can, and that the repository currently answers three
different ways: **how long may a single ``InvokeAgentRuntime`` call hang before the platform ends
it, and does the answer differ between a plain response and a streamed one?**

The account's quotas say it does (``aws service-quotas list-service-quotas --service-code
bedrock-agentcore``, and the Runtime "Invocation limits" table in the official quota page):

    L-3ED45A13  Request timeout             15 min  not adjustable  "synchronous requests"
    L-C91AC63F  Streaming maximum duration  60 min  not adjustable  "streaming connections"

Two numbers mean two experiments. ``probe_sync`` sleeps past the 15-minute figure on the
non-streaming path and is expected to be cut off; ``probe_stream`` heartbeats past it on the
streaming path and is expected to survive. Neither result is assumed -- the whole point is that
`material/part1-question-stage-analysis.md` §7.1 asserts no such synchronous limit exists, which
the quota above contradicts, and an assertion answered by another assertion is not progress.

**This is not the production app.** It runs on a throwaway Runtime built from
``backend/probe.Dockerfile`` (``deploy/probe-runtime.sh create``) and deleted afterwards, because
``deploy/runtime.sh`` updates the production Runtime in place and moves its DEFAULT endpoint --
probing through it would put debug code in front of live traffic for the duration.

It makes no model calls, reads no S3, and loads no scenarios: it imports nothing from this
repository at all. That is a property of the file, checked by ``backend/tests/test_probe_app.py``
rather than promised here, so that "the probe cannot call a model" is verifiable instead of
asserted.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator, Dict

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

# 1000s, i.e. 100s past the 15-minute quota. The margin is deliberate in both directions: too small
# (say 920s) and a cut-off cannot be told apart from a container that started slowly, too large and
# every run costs pointless minutes. Read from the environment so that an unexpected result -- A not
# being cut off at all -- can be re-tested at 1900s without rebuilding and re-pushing an image.
SYNC_SECONDS = float(os.environ.get("PROBE_SYNC_SECONDS", "1000"))

# 1200s sits between the two quotas and far from both, so a normal return rules out "900s also ends
# streams" while a cut-off rules out "the 3600s figure applies here".
STREAM_SECONDS = float(os.environ.get("PROBE_STREAM_SECONDS", "1200"))

# The interval production actually uses (`web/fanout.py` HEARTBEAT_SECONDS), not a value invented for
# this probe -- so the run also exercises the real heartbeat cadence against the real platform.
HEARTBEAT_SECONDS = float(os.environ.get("PROBE_HEARTBEAT_SECONDS", "15"))

# How often the SYNCHRONOUS path prints that it is still alive. The synchronous path cannot send the
# client anything mid-call by definition, so stdout is the only channel it has, and without it a
# handler terminated by the platform at 900s and a handler never dispatched produce the same
# evidence: nothing.
PROGRESS_SECONDS = float(os.environ.get("PROBE_PROGRESS_SECONDS", "60"))


@app.entrypoint
async def invoke(payload: Dict[str, Any]):
    """``probe_sync`` returns a dict (JSON); ``probe_stream`` yields events (SSE).

    The two paths CANNOT share one function body. ``BedrockAgentCoreApp`` picks the response type
    by testing whether the returned object is an async generator
    (``inspect.isasyncgen`` in ``bedrock_agentcore/runtime/app.py``), and a function containing a
    single ``yield`` anywhere is a generator function in its entirety -- so an
    ``if ...: return {...} else: yield ...`` version would serve *both* actions as
    ``text/event-stream`` and the synchronous measurement would silently be a second streaming
    measurement. Same reason ``backend/app.py`` delegates to ``_generate``.
    """
    action = (payload or {}).get("action", "probe_sync")

    if action == "probe_sync":
        # From the payload, falling back to the environment. The first run of this probe produced a
        # result neither expectation covered -- the client hung to its own read timeout while the
        # container logged nothing at all -- and telling "the handler was cancelled" from "the handler
        # never ran" needs a SHORT run to compare against. Rebuilding and re-pushing an image to
        # change one number would cost more than the measurement.
        seconds = float((payload or {}).get("seconds", SYNC_SECONDS))
        started = time.monotonic()
        # Logged before and during, not only on return. `BedrockAgentCoreApp` logs when the handler
        # RETURNS, so a handler terminated mid-sleep leaves an empty log -- indistinguishable from
        # one that was never dispatched. These lines are what make those two cases different.
        print("probe_sync ENTERED, sleeping %.1fs" % seconds, flush=True)
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                break
            await asyncio.sleep(min(PROGRESS_SECONDS, seconds - elapsed))
            print("probe_sync alive at %.1fs of %.1fs" % (time.monotonic() - started, seconds),
                  flush=True)
        print("probe_sync FINISHED sleeping %.1fs" % (time.monotonic() - started), flush=True)
        return {
            "action": action,
            "requested_seconds": seconds,
            # Measured inside the container as well as by the client. If the client sees an error at
            # ~900s while this value never gets produced, the platform ended the request; if the
            # client errors and a later log line shows the sleep completing, the request was severed
            # while the handler kept running -- a different fact, and one only two clocks can tell
            # apart.
            "slept_seconds": round(time.monotonic() - started, 3),
        }

    if action == "probe_stream":
        # Returned, not awaited: this hands back the async generator object itself, which is what
        # makes the SDK choose SSE.
        return _stream()

    return {
        "error": "unknown action %r; expected probe_sync or probe_stream" % action,
        "note": "this is the timing probe, not the generation backend",
    }


async def _stream() -> AsyncIterator[Dict[str, Any]]:
    """Heartbeat every 15s for 1200s, then a closing event.

    The closing event is the only thing that distinguishes "the stream survived" from "the stream
    was cut off after the last heartbeat the client happened to receive". Without it, a connection
    severed at 900s and one that ran to completion both look like a series of heartbeats that
    stopped.
    """
    started = time.monotonic()
    index = 0
    yield {"type": "probe_started", "planned_seconds": STREAM_SECONDS,
           "heartbeat_seconds": HEARTBEAT_SECONDS}
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= STREAM_SECONDS:
            break
        # Never overshoot the total: the last interval is trimmed so the run ends at 1200s rather
        # than at the next multiple of 15 after it.
        await asyncio.sleep(min(HEARTBEAT_SECONDS, STREAM_SECONDS - elapsed))
        index += 1
        yield {"type": "probe_heartbeat", "index": index,
               "elapsed_seconds": round(time.monotonic() - started, 3)}
    yield {"type": "probe_completed", "heartbeats": index,
           "elapsed_seconds": round(time.monotonic() - started, 3)}


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    app.run(port=int(os.environ.get("PORT", "8080")))
