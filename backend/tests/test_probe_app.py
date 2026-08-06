"""Guards on the timing probe: the parts of it that would fail silently if wrong.

The probe's whole value is that its two runs answer two different questions. Three things can break
that without anything looking broken:

* the two actions could both end up on the streaming path, so "the synchronous measurement" is a
  second streaming measurement;
* the probe image could acquire the model SDKs, so "the probe cannot call a model" becomes an
  argument from reading source rather than a fact about the container;
* the timing client could keep botocore's retries, so a 900s observation silently becomes 1800s of
  wall clock with the first -- and only informative -- error discarded.

None of those produce an error at run time. They produce a plausible number that means something
else, which is the failure mode this file exists for.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend import probe_app
from backend.scripts import probe_runtime_timing

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_DOCKERFILE = REPO_ROOT / "backend" / "probe.Dockerfile"


class TestTheTwoPathsStayTwoPaths:
    """``BedrockAgentCoreApp`` chooses JSON vs SSE by inspecting the returned object."""

    def test_the_entrypoint_is_not_itself_a_generator_function(self) -> None:
        """A single `yield` anywhere in ``invoke`` would make BOTH actions stream.

        The SDK tests ``inspect.isasyncgen(result)`` on the handler's return value
        (``bedrock_agentcore/runtime/app.py``), and Python makes a function containing any `yield` a
        generator function in its entirety -- so an ``if ...: return {...} else: yield ...`` rewrite
        would serve `probe_sync` as ``text/event-stream`` too. The probe would still run, still take
        1000 seconds, and still report a number; the number would just be about the wrong quota.
        """
        assert not inspect.isasyncgenfunction(probe_app.invoke)

    @pytest.mark.asyncio
    async def test_probe_stream_returns_an_async_generator(self) -> None:
        """And the streaming action must actually reach the streaming branch."""
        result = await probe_app.invoke({"action": "probe_stream"})
        assert inspect.isasyncgen(result)
        await result.aclose()

    @pytest.mark.asyncio
    async def test_an_unknown_action_answers_rather_than_hanging(self) -> None:
        # Not cosmetic: a typo in the payload would otherwise sleep for 1000 seconds before the
        # mistake surfaced, since `probe_sync` is the default action.
        result = await probe_app.invoke({"action": "nope"})
        assert not inspect.isasyncgen(result)
        assert "unknown action" in result["error"]

    @pytest.mark.asyncio
    async def test_the_stream_heartbeats_and_reports_completion(self, monkeypatch) -> None:
        """Run the real generator over a compressed clock.

        Times are patched, not the logic: the closing ``probe_completed`` frame is what distinguishes
        a stream that survived from one that was severed after the last heartbeat the client saw, and
        the trimmed final interval is what keeps a 1200s run from overshooting to 1205s.
        """
        monkeypatch.setattr(probe_app, "STREAM_SECONDS", 0.05)
        monkeypatch.setattr(probe_app, "HEARTBEAT_SECONDS", 0.01)
        events = [event async for event in probe_app._stream()]

        assert events[0]["type"] == "probe_started"
        assert events[-1]["type"] == "probe_completed"
        heartbeats = [e for e in events if e["type"] == "probe_heartbeat"]
        assert heartbeats, "a stream with no heartbeats cannot show it crossed the 900s mark"
        assert [e["index"] for e in heartbeats] == list(range(1, len(heartbeats) + 1))
        assert events[-1]["heartbeats"] == len(heartbeats)
        assert events[-1]["elapsed_seconds"] >= 0.05


class TestTheSyncPathReportsFromInsideTheSleep:
    """What the first long run lacked, and why it could not be concluded from.

    Probe A hung until the client's own 3600s read timeout while the container log stayed empty --
    yet an empty log was consistent with two opposite readings: the platform terminated the handler at
    ~900s, or the handler was never dispatched. ``BedrockAgentCoreApp`` logs only when a handler
    RETURNS, so neither reading could be ruled out. These two properties are what make the second
    round able to answer it.
    """

    @pytest.mark.asyncio
    async def test_the_sleep_length_comes_from_the_payload(self) -> None:
        """A control run at a few seconds has to be possible without rebuilding the image.

        Without this, comparing a short sleep against a long one through the identical code path
        means an ECR push per number -- and the comparison is the whole experiment.
        """
        result = await probe_app.invoke({"action": "probe_sync", "seconds": 0.01})
        assert result["requested_seconds"] == 0.01
        assert result["slept_seconds"] >= 0.01
        # And the default still applies when the payload is silent, so the long run needs no flag.
        assert probe_app.SYNC_SECONDS > 900

    @pytest.mark.asyncio
    async def test_the_sync_path_prints_before_it_finishes(self, monkeypatch, capfd) -> None:
        """Entry and liveness are logged DURING the sleep, not only at the end.

        This is the property that separates "terminated mid-sleep" from "never dispatched": a stopped
        handler still leaves the ENTERED line and the progress lines up to the moment it died. A test
        that only checked the final line would pass while the probe stayed unable to tell the two
        cases apart -- which is precisely the state the first run was in.
        """
        monkeypatch.setattr(probe_app, "PROGRESS_SECONDS", 0.01)
        await probe_app.invoke({"action": "probe_sync", "seconds": 0.05})
        out = capfd.readouterr().out

        assert "probe_sync ENTERED" in out
        alive = [line for line in out.splitlines() if "alive at" in line]
        assert alive, "no mid-sleep line: a handler stopped at 900s would again log nothing"
        # Ordering is the point: a progress line must precede the finish line, or it is not evidence
        # about a handler that never reached the finish.
        assert out.index("ENTERED") < out.index("alive at") < out.index("FINISHED")


class TestTheSleepLengthsStraddleTheQuotas:
    """The two durations are chosen against the two quota values, so pin that relationship."""

    def test_the_sync_sleep_exceeds_the_fifteen_minute_quota(self) -> None:
        # L-3ED45A13: 15 minutes, not adjustable. 1000s is 100s past it -- enough that a cut-off
        # cannot be confused with a slow container start, without wasting minutes per run.
        assert probe_app.SYNC_SECONDS > 900

    def test_the_stream_duration_sits_between_the_two_quotas(self) -> None:
        # L-C91AC63F: 60 minutes for streaming. 1200s is past the synchronous limit and far short of
        # the streaming one, so a normal return rules out "900s ends streams too" and a cut-off rules
        # out "the 3600s figure governs this path".
        assert 900 < probe_app.STREAM_SECONDS < 3600

    def test_the_heartbeat_matches_production(self) -> None:
        # `web/fanout.py` HEARTBEAT_SECONDS. Using production's own interval means the run also
        # exercises the real cadence rather than one invented for the probe.
        assert probe_app.HEARTBEAT_SECONDS == 15


class TestTheProbeCannotCallAModel:
    """Two layers, because either alone is escapable."""

    def test_the_module_imports_nothing_that_can_reach_a_model(self) -> None:
        """Parsed rather than imported.

        An import-table check would need `strands` and `openai` installed to be meaningful, and would
        pass vacuously in an environment lacking them -- i.e. it would be weakest exactly where the
        probe image lives.
        """
        tree = ast.parse(Path(probe_app.__file__).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        forbidden = {"strands", "strands_tools", "openai", "boto3", "botocore", "audio_storage"}
        assert not (roots & forbidden), "probe_app imports %s" % sorted(roots & forbidden)
        # `backend.agents` / `backend.orchestration` would show up as the root `backend`, so the
        # submodule names are checked directly too.
        source = Path(probe_app.__file__).read_text(encoding="utf-8")
        for name in ("backend.agents", "backend.orchestration", "backend.steps", "backend.model"):
            assert "import %s" % name not in source
            assert "from %s" % name not in source

    def test_the_probe_image_ships_none_of_the_generation_assets(self) -> None:
        """The image-level half of the same claim.

        With the skill pool and the model SDKs absent from the COPY and pip lines, "this container
        cannot generate anything" is visible in the Dockerfile. Someone later unifying the two
        Dockerfiles for tidiness would break that, and this is what says so.
        """
        lines = PROBE_DOCKERFILE.read_text(encoding="utf-8").splitlines()
        # Instructions only. Matching the whole file would match this Dockerfile's own header, which
        # names strands and skills/ in the course of explaining why they are absent -- so the test
        # would fail on its own rationale.
        copy_lines = [line for line in lines if line.startswith("COPY ")]
        run_lines = [line for line in lines if line.startswith("RUN ")]
        for directory in ("skills/", "config/", "audio_storage/"):
            assert not any(directory in line for line in copy_lines), \
                "probe.Dockerfile COPYs %s" % directory
        # A blanket `COPY backend/` would drag in agents.py and orchestration/ while satisfying the
        # loop above, so the copies are pinned to the two files the probe actually needs.
        assert not any(line.startswith("COPY backend/ ") for line in copy_lines)
        for package in ("strands", "openai", "pyyaml"):
            assert not any(package in line for line in run_lines), \
                "probe.Dockerfile installs %s" % package
        assert any("bedrock-agentcore" in line for line in run_lines), \
            "the probe needs the real SDK: it decides JSON vs SSE"


class TestTheTimingClientMeasuresThePlatform:
    """A misconfigured client produces a number about botocore, not about AgentCore."""

    def test_the_resolved_client_has_no_retries_and_an_hour_long_read_timeout(self) -> None:
        """Asserted on the built client, not on the dict passed in -- they differ.

        botocore rewrites `retries`: `max_attempts` counts RETRIES and becomes
        `total_max_attempts = value + 1`. Checking the literal would let a future edit to
        `max_attempts: 1` (one retry, i.e. a doubled measurement and a lost first error) sail through
        while the assertion still read as "retries are off".
        """
        client = probe_runtime_timing.build_client("us-east-1")
        config = client.meta.config
        assert config.retries["total_max_attempts"] == 1
        # Above the 60-minute streaming quota, so the platform is always the first to end the call.
        assert config.read_timeout == 3600
        assert config.read_timeout > 3600 - 1  # explicit: not merely "large"
        assert config.connect_timeout == 10

    def test_the_session_id_satisfies_the_api_minimum(self) -> None:
        # The API model requires 33 characters; `uuid4().hex` is 32 and is rejected at call time --
        # a 20-minute run that fails instantly for a reason unrelated to timing.
        assert len(probe_runtime_timing.new_session_id()) >= 33

    def test_error_detail_keeps_the_message_whole(self) -> None:
        """The error IS probe A's result, so it must not be summarised or truncated."""
        long = "x" * 5000

        class Boom(Exception):
            pass

        exc = Boom(long)
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "Whatever", "Message": long},
            "ResponseMetadata": {"HTTPStatusCode": 504, "RequestId": "abc-123"},
        }
        detail = probe_runtime_timing.error_detail(exc)
        assert detail["message"] == long
        assert detail["error_message"] == long
        # Kept even though it looks like noise: it is the only handle CloudWatch shares, and this
        # measurement's conclusion reverses a documented premise, so it has to be checkable.
        assert detail["request_id"] == "abc-123"
        assert detail["http_status"] == 504
        assert detail["exception_type"] == "Boom"

    def test_error_detail_survives_an_exception_with_no_response(self) -> None:
        # A botocore ReadTimeoutError has no `.response`. If this raised, probe A would lose the very
        # observation it exists to record.
        detail = probe_runtime_timing.error_detail(RuntimeError("read timeout"))
        assert detail["exception_type"] == "RuntimeError"
        assert detail["message"] == "read timeout"
        assert "request_id" not in detail
