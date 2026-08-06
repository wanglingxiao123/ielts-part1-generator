#!/usr/bin/env python
"""Time a long ``InvokeAgentRuntime`` call from the client side and record what ended it.

    python backend/scripts/probe_runtime_timing.py --action probe_sync   --arn <probe-runtime-arn>
    python backend/scripts/probe_runtime_timing.py --action probe_stream --arn <probe-runtime-arn>

Point it at the throwaway probe Runtime (``deploy/probe-runtime.sh create``), never at production.

What it is measuring, and why the setup below is not incidental:

* **``read_timeout=3600``.** botocore's default is 60s. At the default, every run would end after a
  minute and the number produced would describe botocore, not the platform. 3600 is above both
  quotas so that the platform is always the first thing to speak.
* **No retries.** A retried invoke re-POSTs after the timeout, so one 900s observation becomes 1800s
  of wall clock and the FIRST error -- the only one that says what the platform did -- is replaced by
  the last. Same reasoning as `web/runtime_client.py`, for a different reason: there it prevents
  double-billing, here it prevents a corrupted measurement.
* **Direct to the ``bedrock-agentcore`` endpoint.** Production reaches the Runtime through
  CloudFront (60s origin read timeout) and an ALB (120s idle). Either would end a long call first and
  the run would measure the edge instead of the Runtime.
* **``iter_lines`` on the streaming path.** Reading the body in one call yields only a total
  duration, which cannot distinguish "heartbeats arrived across the whole 1200s" from "data flowed
  for 900s, then the socket sat idle and flushed at the end". The arrival time of each line can.

Results are written as JSON (``--out``) as well as printed: each run costs 15-20 minutes, and a
closed terminal should not mean measuring it again.

**What the 2026-08-06 runs found, since it changes how to read a result here.** The synchronous
quota is real: the platform terminates the synchronous invocation's handler just after 900s. (What
was observed is the handler ceasing to make progress and never returning -- whether the microVM
itself was destroyed is not something this probe can see, and it makes no difference to a caller.)
But the platform sends the client *nothing* -- no 504, no error code, no closed socket -- so this
script's wall-clock number on the synchronous path is its OWN ``read_timeout``, not the platform's
limit. Measured: 3600s of read timeout produced 3600.5s of waiting, 1500s produced 1500.7s, and the
handler stopped at ~900s in every case. Reading the platform limit off the client therefore does not
work on this path; the container log does it (see ``PROBE_PROGRESS_SECONDS`` in
``backend/probe_app.py``). Full write-up in ``backend/docs/timing.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Above the 60-minute streaming quota, so botocore never wins the race to end the call.
READ_TIMEOUT_SECONDS = 3600.0
CONNECT_TIMEOUT_SECONDS = 10.0

# `total_max_attempts: 1` == the initial request and nothing more. Spelled this way rather than
# `max_attempts: 0` because botocore's `max_attempts` counts RETRIES and is rewritten internally into
# this key (verified against botocore 1.43.57, which also drops the mode to `legacy` when doing so);
# one spelling in the repository is better than two that need explaining.
RETRIES = {"total_max_attempts": 1, "mode": "standard"}


def build_client(region: str) -> Any:
    """A boto3 ``bedrock-agentcore`` client configured for a call that lasts an hour.

    Separate from ``main`` so the test suite can assert the resolved configuration without
    credentials or a network. What it asserts is ``client.meta.config``, i.e. what botocore decided,
    not the dict passed in here -- those differ, which is precisely why the constants above carry a
    comment.
    """
    import boto3  # noqa: PLC0415 - lazy so importing this module needs no credentials
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            read_timeout=READ_TIMEOUT_SECONDS,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            retries=dict(RETRIES),
        ),
    )


def new_session_id() -> str:
    """33 characters minimum, per the API model. `uuid4().hex` is 32 and is rejected."""
    import uuid  # noqa: PLC0415

    return "probe-%s" % uuid.uuid4().hex


def error_detail(exc: BaseException) -> Dict[str, Any]:
    """Everything the exception carries, verbatim and untruncated.

    Catching ``Exception`` rather than a specific type, and recording the raw text, is the point of
    the probe: what a platform cut-off looks like is UNKNOWN going in. It could be a botocore
    ``ReadTimeoutError``, a modelled service exception, or a 5xx from the front end. Naming the type
    in advance would be assuming the answer, and a paraphrased message cannot be matched against
    CloudWatch -- which is why ``RequestId`` is kept even though it looks like noise.
    """
    detail: Dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "exception_module": type(exc).__module__,
        "message": str(exc),
    }
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        detail["error_code"] = response.get("Error", {}).get("Code")
        detail["error_message"] = response.get("Error", {}).get("Message")
        meta = response.get("ResponseMetadata", {})
        detail["http_status"] = meta.get("HTTPStatusCode")
        detail["request_id"] = meta.get("RequestId")
    return detail


def run_sync(client: Any, arn: str, seconds: float | None = None) -> Dict[str, Any]:
    """Probe A: a non-streaming invoke that sleeps past the 15-minute synchronous quota.

    The interesting outcome is the failure. A success would mean the quota does not describe actual
    behaviour -- worth recording, but not something to build a timeout on.

    ``seconds`` overrides the container's own default. A SHORT run is the control: the first long run
    ended with the client timing out and the container logging nothing, and "the platform cut the
    handler" only means something if a shorter sleep through the identical path does return.
    """
    session = new_session_id()
    started = time.monotonic()
    outcome: Dict[str, Any] = {"session_id": session, "requested_seconds": seconds}
    request: Dict[str, Any] = {"action": "probe_sync"}
    if seconds is not None:
        request["seconds"] = seconds
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session,
            payload=json.dumps(request).encode("utf-8"),
        )
        body = response.get("response")
        text = body.read().decode("utf-8", "replace") if body is not None else ""
        outcome["ended_by"] = "returned"
        outcome["content_type"] = response.get("contentType")
        outcome["body"] = text[:2000]
        # The handler reports its own sleep. Two clocks tell "the platform ended the REQUEST while
        # the handler ran on" apart from "the handler finished", which one clock cannot.
        try:
            outcome["container_slept_seconds"] = json.loads(text).get("slept_seconds")
        except Exception:  # noqa: BLE001 - a non-JSON body is itself the observation
            outcome["container_slept_seconds"] = None
    except Exception as exc:  # noqa: BLE001 - the error IS the measurement
        outcome["ended_by"] = "error"
        outcome["error"] = error_detail(exc)
    outcome["wall_seconds"] = round(time.monotonic() - started, 3)
    return outcome


def run_stream(client: Any, arn: str) -> Dict[str, Any]:
    """Probe B: an SSE invoke that heartbeats past 15 minutes, checking the 60-minute quota applies.

    Every line's arrival time is kept. ``probe_completed`` in the last frame is what separates "the
    stream survived 1200s" from "the stream was cut and the client saw the heartbeats up to that
    point" -- both of which otherwise look like heartbeats that simply stop.
    """
    session = new_session_id()
    started = time.monotonic()
    lines: List[Dict[str, Any]] = []
    outcome: Dict[str, Any] = {"session_id": session}
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session,
            payload=json.dumps({"action": "probe_stream"}).encode("utf-8"),
        )
        outcome["content_type"] = response.get("contentType")
        outcome["first_byte_seconds"] = round(time.monotonic() - started, 3)
        body = response.get("response")
        # chunk_size=10 for the same reason as `web/iter_sse_payloads`: iter_lines only emits a line
        # once a read completes it, so a big chunk makes the reader wait for bytes the Runtime has not
        # sent and turns progressive delivery into buffered delivery -- which would destroy exactly
        # the arrival times being measured.
        for raw in body.iter_lines(chunk_size=10):
            at = round(time.monotonic() - started, 3)
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if not text:
                continue
            lines.append({"at_seconds": at, "line": text[:400]})
            print("  %8.1fs  %s" % (at, text[:120]), flush=True)
        outcome["ended_by"] = "stream_ended"
    except Exception as exc:  # noqa: BLE001 - a mid-stream cut-off is a valid outcome to record
        outcome["ended_by"] = "error"
        outcome["error"] = error_detail(exc)

    outcome["wall_seconds"] = round(time.monotonic() - started, 3)
    outcome["lines"] = lines
    outcome["line_count"] = len(lines)
    payloads = [entry for entry in lines if entry["line"].startswith("data:")]
    outcome["data_frame_count"] = len(payloads)
    outcome["saw_probe_completed"] = any("probe_completed" in entry["line"] for entry in payloads)
    # The largest silence between frames. If the platform were ending the connection on an idle
    # timer, this is the number that would show it -- a total duration would not.
    gaps = [round(b["at_seconds"] - a["at_seconds"], 3)
            for a, b in zip(payloads, payloads[1:])]
    outcome["max_gap_seconds"] = max(gaps) if gaps else None
    return outcome


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--action", choices=["probe_sync", "probe_stream"], required=True)
    parser.add_argument("--arn", required=True,
                        help="the PROBE runtime's ARN; pointing this at production is the one "
                             "mistake this script cannot detect for you")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--seconds", type=float, default=None,
                        help="probe_sync only: override the container's sleep length. Use a short "
                             "value (e.g. 120) as the control run for a long one that hung")
    parser.add_argument("--out", default=None,
                        help="default /tmp/probe_runtime_timing_<action>.json")
    args = parser.parse_args(argv)

    out = Path(args.out or "/tmp/probe_runtime_timing_%s.json" % args.action)
    client = build_client(args.region)
    resolved = client.meta.config
    print("action=%s arn=%s" % (args.action, args.arn))
    print("read_timeout=%s connect_timeout=%s retries=%s"
          % (resolved.read_timeout, resolved.connect_timeout, resolved.retries))
    print("waiting -- probe_sync takes ~15-17 min, probe_stream ~20 min", flush=True)

    if args.action == "probe_sync":
        result = run_sync(client, args.arn, args.seconds)
    else:
        result = run_stream(client, args.arn)
    result["action"] = args.action
    result["region"] = args.region
    result["arn"] = args.arn
    result["client_config"] = {
        "read_timeout": resolved.read_timeout,
        "connect_timeout": resolved.connect_timeout,
        "retries": resolved.retries,
    }

    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print("ended_by      : %s" % result["ended_by"])
    print("wall_seconds  : %s" % result["wall_seconds"])
    if result.get("error"):
        print("error         :")
        print(json.dumps(result["error"], indent=2, ensure_ascii=False))
    if args.action == "probe_stream":
        print("data frames   : %s (probe_completed=%s)"
              % (result["data_frame_count"], result["saw_probe_completed"]))
        print("max gap       : %s s" % result["max_gap_seconds"])
    print("written to    : %s" % out)
    # Exit 0 either way: for probe A an error IS the expected result, so a non-zero exit would make
    # the successful measurement look like a broken script.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
