"""Selection, synthesis wiring, and the non-blocking guarantee.

No AWS here. The object store is InMemoryObjectStore and Polly is a counting stub, which is what
lets these tests assert the two properties that matter and that a real call cannot demonstrate
cheaply:

  * a repeated selection makes zero further Polly requests (the "do not bill twice" rule);
  * the event loop stays free while synthesis runs, measured rather than asserted.

Whether Polly's audio is *correct* is settled by real synthesis against the live service; see
audio_storage/assumptions.py and the e2e script. These tests cover the plumbing.
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest

from audio_storage import synthesize as synth
from audio_storage.manifest import extract_turns
from audio_storage.mp3_duration import duration_ms
from audio_storage.object_store import InMemoryObjectStore
from audio_storage.state_store import PENDING, QUARANTINE, StateStore
from backend.orchestration import publish as publish_module
from backend.orchestration.publish import (
    AlreadySelected,
    Candidate,
    CandidateRegistry,
    UnknownMaterial,
    audio_status,
    scenario_key_for,
    select_material,
)

SCENARIO_KEY = "accommodation-rental"
MATERIAL_ID = "20260728-accommodation-rental-7f3a1c2d"


def _mp3(frames: int = 20) -> bytes:
    """MPEG1 Layer III frames. Real header bits, so duration parsing is exercised for real."""
    header = bytes([0xFF, 0xFB, 0x50, 0x80])
    from audio_storage.mp3_duration import _parse_header

    length = _parse_header(header)[0]
    return (header + b"\x00" * (length - 4)) * frames


class CountingPolly:
    """Polly's wire shape plus a request counter and an optional per-call delay."""

    def __init__(self, delay: float = 0.0) -> None:
        self.requests = []
        self.delay = delay

    def describe_voices(self, **kwargs):
        return {"Voices": [{"Id": v} for v in ("Amy", "Arthur", "Brian")]}

    def synthesize_speech(self, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        self.requests.append(kwargs)
        return {"AudioStream": io.BytesIO(_mp3(18 + len(self.requests) % 5))}


@pytest.fixture
def wiring():
    backing = InMemoryObjectStore()
    raw_polly = CountingPolly()
    return {
        "backing": backing,
        "state_store": StateStore(backing),
        "polly": synth.PollyClient(client=raw_polly),
        "raw": raw_polly,
        "registry": CandidateRegistry(),
    }


def make_candidate(material, blueprint, audit, *, material_id=MATERIAL_ID,
                   group_key="batch-1:accommodation-rental", **kwargs):
    return Candidate(
        material_id=material_id, scenario_key=SCENARIO_KEY, group_key=group_key,
        slot_id="slot-1", material=material, blueprint=blueprint, audit=audit, **kwargs
    )


class TestScenarioKey:
    def test_a_catalogue_scenario_uses_its_id(self):
        class S:
            id = "booking-hotel"
            prompt_hint = "hint"

        assert scenario_key_for(S()) == "booking-hotel"

    def test_a_custom_scenario_hashes_its_text(self):
        """The natural-language scenario field cannot be a key (audio design.md §8.1)."""

        class S:
            id = "custom"
            prompt_hint = "A student asks about a locker rental."

        key = scenario_key_for(S())
        assert key.startswith("custom-") and len(key) == len("custom-") + 8
        assert "/" not in key and " " not in key
        assert scenario_key_for(S()) == key  # stable across calls


class TestSelection:
    async def test_selecting_synthesises_and_publishes_to_pending(
        self, wiring, material, blueprint, audit_aligned
    ):
        candidate = make_candidate(material, blueprint, audit_aligned)
        wiring["registry"].register(candidate)
        turns = extract_turns(material)

        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert result["status"] == "ready", result
        job = audio_status(MATERIAL_ID, registry=wiring["registry"])
        assert job["progress"] == {"done": len(turns), "total": len(turns)}
        assert job["state"] == PENDING

        ref = wiring["state_store"].locate(MATERIAL_ID)
        assert ref.state == PENDING and ref.scenario_key == SCENARIO_KEY
        keys = wiring["backing"].list_keys(ref.prefix)
        assert ref.prefix + "audio/manifest.json" in keys
        assert sum(1 for k in keys if k.endswith(".mp3")) == len(turns)
        for name in ("material.json", "blueprint.json", "audit.json"):
            assert ref.prefix + name in keys

    async def test_the_manifest_is_written_after_every_clip(
        self, wiring, material, blueprint, audit_aligned
    ):
        """The completeness sentinel only works if it is genuinely last."""
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        puts = [c[1] for c in wiring["backing"].calls if c[0] == "put"]
        manifest_at = next(i for i, k in enumerate(puts) if k.endswith("audio/manifest.json"))
        last_clip_at = max(i for i, k in enumerate(puts) if k.endswith(".mp3"))
        assert manifest_at > last_clip_at

    async def test_repeating_a_selection_bills_nothing_further(
        self, wiring, material, blueprint, audit_aligned
    ):
        """Acceptance: 重复提交不重复计费."""
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        first = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        calls_after_first = len(wiring["raw"].requests)
        assert calls_after_first > 0

        second = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert second["repeat"] is True
        assert second["audio_job_id"] == first["audio_job_id"]
        assert len(wiring["raw"].requests) == calls_after_first

    async def test_idempotency_survives_a_lost_job_record(
        self, wiring, material, blueprint, audit_aligned
    ):
        """The cheap guarantee must not depend on this process's memory.

        A restart loses the registry. What must not happen is paying for the same audio again --
        and it does not, because the skip decision reads the cache key off each S3 object.
        """
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        calls_after_first = len(wiring["raw"].requests)

        fresh_registry = CandidateRegistry()
        fresh_registry.register(make_candidate(material, blueprint, audit_aligned))
        result = await select_material(
            MATERIAL_ID, registry=fresh_registry, state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert result["status"] == "ready"
        assert result["polly_calls"] == 0
        assert result["reused_clips"] == len(extract_turns(material))
        assert len(wiring["raw"].requests) == calls_after_first

    async def test_the_unselected_sibling_is_discarded(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        registry = wiring["registry"]
        registry.register(make_candidate(material, blueprint, audit_aligned))
        other_id = "20260728-accommodation-rental-aaaabbbb"
        registry.register(make_candidate(clone(material), blueprint, audit_aligned,
                                         material_id=other_id))

        result = await select_material(
            MATERIAL_ID, registry=registry, state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert result["siblings_discarded"] == [other_id]
        # Discarded means gone, not archived (audio design.md §14): no S3 objects, no candidate.
        assert wiring["backing"].list_keys("") and not any(
            other_id in key for key in wiring["backing"].list_keys("")
        )
        with pytest.raises(UnknownMaterial):
            registry.get(other_id)

    async def test_selecting_a_discarded_sibling_is_refused(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        registry = wiring["registry"]
        registry.register(make_candidate(material, blueprint, audit_aligned))
        other_id = "20260728-accommodation-rental-aaaabbbb"
        registry.register(make_candidate(clone(material), blueprint, audit_aligned,
                                         material_id=other_id, group_key="g"))
        # Same group this time, so the second pick competes with the first.
        registry.get(other_id).group_key = registry.get(MATERIAL_ID).group_key

        await select_material(
            MATERIAL_ID, registry=registry, state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        with pytest.raises(UnknownMaterial):
            await select_material(other_id, registry=registry,
                                  state_store=wiring["state_store"],
                                  backing=wiring["backing"], polly=wiring["polly"])

    async def test_a_second_pick_in_a_live_group_is_refused_not_published(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        """AlreadySelected rather than two published materials for one choice."""
        registry = wiring["registry"]
        group = "batch-9:accommodation-rental"
        a = make_candidate(material, blueprint, audit_aligned, group_key=group)
        b = make_candidate(clone(material), blueprint, audit_aligned,
                           material_id="20260728-accommodation-rental-ccccdddd", group_key=group)
        registry.register(a)
        registry.register(b)
        # Claim for `a`, then re-register `b` as if a stale client still held it.
        registry.claim(a, 1)
        registry.register(b)
        with pytest.raises(AlreadySelected):
            registry.claim(b, 1)

    async def test_unknown_material_is_refused(self, wiring):
        with pytest.raises(UnknownMaterial):
            await select_material("nope", registry=wiring["registry"],
                                  state_store=wiring["state_store"],
                                  backing=wiring["backing"], polly=wiring["polly"])

    def test_audio_status_before_selection(self, wiring):
        assert audio_status("anything", registry=wiring["registry"])["status"] == "not_requested"


class TestVerdictRouting:
    async def test_a_failed_material_is_quarantined_without_audio(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        audit = clone(audit_aligned)
        audit["verdict"] = "FAIL"
        audit["findings"] = [{"severity": "critical", "rule": "answer not recoverable",
                              "turn_index": 7}]
        wiring["registry"].register(make_candidate(material, blueprint, audit))

        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert result["status"] == "quarantined"
        assert result["state"] == QUARANTINE
        # Not one billable request: voicing a material no reviewer should see is pure waste.
        assert wiring["raw"].requests == []
        ref = wiring["state_store"].locate(MATERIAL_ID)
        keys = wiring["backing"].list_keys(ref.prefix)
        assert not [k for k in keys if k.endswith(".mp3")]
        assert ref.prefix + "quarantine_reason.json" in keys

    async def test_not_assessable_routes_like_fail_but_says_why(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        import json

        audit = clone(audit_aligned)
        audit["verdict"] = "NOT_ASSESSABLE"
        audit["assessable"] = False
        wiring["registry"].register(make_candidate(material, blueprint, audit))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        ref = wiring["state_store"].locate(MATERIAL_ID)
        reason = json.loads(wiring["backing"].get(ref.prefix + "quarantine_reason.json"))
        assert ref.state == QUARANTINE
        assert reason["reason_code"] == "no_assessable_script"

    async def test_a_degraded_pass_reaches_pending_carrying_its_flag(
        self, wiring, material, blueprint, audit_aligned
    ):
        """R2: 降级材料按自身 verdict 路由，但必须携带 degraded 标记."""
        import json

        wiring["registry"].register(
            make_candidate(material, blueprint, audit_aligned,
                           degraded=True, degraded_reason="time_budget")
        )
        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert result["status"] == "ready" and result["state"] == PENDING
        ref = wiring["state_store"].locate(MATERIAL_ID)
        manifest = json.loads(wiring["backing"].get(ref.prefix + "audio/manifest.json"))
        assert manifest["degraded"] is True
        assert manifest["degraded_reason"] == "time_budget"

    async def test_a_synthesis_failure_publishes_nothing(
        self, wiring, material, blueprint, audit_aligned, monkeypatch
    ):
        class Broken(CountingPolly):
            def synthesize_speech(self, **kwargs):
                self.requests.append(kwargs)
                raise RuntimeError("InvalidSsmlException")

        broken = Broken()
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=synth.PollyClient(client=broken), wait=True,
        )
        assert result["status"] == "failed" and "failed to synthesise" in result["error"]
        keys = wiring["backing"].list_keys("")
        assert not [k for k in keys if k.endswith("manifest.json")]
        assert not [k for k in keys if k.endswith("material.json")]


class TestNonBlocking:
    async def test_the_event_loop_stays_free_during_synthesis(
        self, wiring, material, blueprint, audit_aligned
    ):
        """The /ping guarantee, measured on the loop rather than asserted.

        A health check is a coroutine that has to be scheduled promptly. So this runs a ticker
        coroutine at 10ms while a slow synthesis is in flight and checks the worst gap between
        ticks. If synthesis ran inline the ticker would simply stop for the whole duration --
        which is precisely what AgentCore reads as an unhealthy instance.
        """
        slow = CountingPolly(delay=0.01)
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))

        gaps = []
        stop = asyncio.Event()

        async def ticker():
            previous = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        tick_task = asyncio.create_task(ticker())
        started = time.monotonic()
        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=synth.PollyClient(client=slow), wait=True,
        )
        elapsed = time.monotonic() - started
        stop.set()
        await tick_task

        assert result["status"] == "ready"
        # The synthesis really did take time, or the test proves nothing.
        assert elapsed > 0.05, elapsed
        assert gaps, "the ticker never ran"
        assert max(gaps) < 0.25, "loop stalled for %.3fs during synthesis" % max(gaps)

    async def test_select_returns_before_synthesis_finishes(
        self, wiring, material, blueprint, audit_aligned
    ):
        """Without wait=True the call must return promptly, leaving the job in flight."""
        slow = CountingPolly(delay=0.02)
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))

        started = time.monotonic()
        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=synth.PollyClient(client=slow),
        )
        handoff = time.monotonic() - started
        assert handoff < 0.1, handoff
        assert result["status"] in ("queued", "synthesizing")
        assert result["audio_job_id"]

        # Let it finish so the test does not leave a thread running into the next one.
        for _ in range(600):
            if audio_status(MATERIAL_ID, registry=wiring["registry"])["status"] == "ready":
                break
            await asyncio.sleep(0.02)
        assert audio_status(MATERIAL_ID, registry=wiring["registry"])["status"] == "ready"

    async def test_progress_is_observable_while_running(
        self, wiring, material, blueprint, audit_aligned
    ):
        slow = CountingPolly(delay=0.01)
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=synth.PollyClient(client=slow),
        )
        seen = set()
        for _ in range(600):
            status = audio_status(MATERIAL_ID, registry=wiring["registry"])
            seen.add(status["status"])
            if status["status"] == "ready":
                break
            await asyncio.sleep(0.01)
        assert "ready" in seen
        assert "synthesizing" in seen or "queued" in seen


class TestPresignedPlayback:
    async def test_presign_returns_one_url_per_turn(
        self, wiring, material, blueprint, audit_aligned
    ):
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        urls = wiring["state_store"].presign_audio(MATERIAL_ID)
        turns = extract_turns(material)
        assert sorted(urls) == list(range(len(turns)))
        assert all(url for url in urls.values())


class TestThreeWayAlignment:
    async def test_turn_index_lines_up_across_script_blueprint_and_manifest(
        self, wiring, material, blueprint, audit_aligned
    ):
        """The one automated defence for the zero-misalignment acceptance item."""
        import json

        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        ref = wiring["state_store"].locate(MATERIAL_ID)
        manifest = json.loads(wiring["backing"].get(ref.prefix + "audio/manifest.json"))
        turns = extract_turns(material)

        assert manifest["validation"]["alignment_ok"], manifest["validation"]
        assert [c["turn_index"] for c in manifest["clips"]] == list(range(len(turns)))
        for clip in manifest["clips"]:
            assert clip["speaker"] == turns[clip["turn_index"]]["speaker"]
        anchors = {item["turn_index"] for item in blueprint["items"]}
        assert anchors <= {c["turn_index"] for c in manifest["clips"]}
        # Durations are parsed from the stored bytes, not copied from a hopeful number.
        for clip in manifest["clips"]:
            stored = wiring["backing"].get(ref.prefix + clip["key"])
            assert clip["duration_ms"] == duration_ms(stored)

    async def test_verify_material_passes_end_to_end(
        self, wiring, material, blueprint, audit_aligned
    ):
        wiring["registry"].register(make_candidate(material, blueprint, audit_aligned))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        report = wiring["state_store"].verify_material(MATERIAL_ID)
        assert report["ok"], report


class TestConfiguration:
    def test_the_bucket_is_never_guessed(self, monkeypatch):
        from backend import audio

        monkeypatch.delenv(audio.BUCKET_ENV, raising=False)
        with pytest.raises(audio.AudioNotConfigured):
            audio.bucket_name()

    def test_publish_module_imports_without_aws(self):
        """backend.tests must run on a machine with no credentials at all."""
        assert publish_module.REGISTRY is not None
