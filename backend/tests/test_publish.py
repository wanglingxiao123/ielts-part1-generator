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
import json
import time
import types
from unittest import mock

import pytest

from audio_storage import synthesize as synth
from audio_storage.manifest import extract_turns
from audio_storage.mp3_duration import duration_ms
from audio_storage.object_store import InMemoryObjectStore
from audio_storage.state_store import PENDING, StateStore
from backend.orchestration import publish as publish_module
from backend.orchestration.candidate_store import InMemoryCandidateStore
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
        first = make_candidate(material, blueprint, audit_aligned)
        registry.register(first)
        other_id = "20260728-accommodation-rental-aaaabbbb"
        # The shared group is set at construction, not by mutating a registry lookup: the store is
        # authoritative now, so an edit to a returned Candidate is a local copy and never persists.
        registry.register(make_candidate(clone(material), blueprint, audit_aligned,
                                         material_id=other_id, group_key=first.group_key))

        await select_material(
            MATERIAL_ID, registry=registry, state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        # Discarded siblings are removed from the store, so the id is genuinely gone rather than
        # merely losing the group race.
        assert registry.store.load(other_id) is None
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
    """The client's rule: whatever the verdict, the user gets the material they asked for.

    These tests used to pin the opposite -- that a FAIL was quarantined and voiced nothing. The
    intent is preserved and inverted: the same properties are still asserted (where it lands,
    whether Polly was called, what is in the prefix), now with the answers the product requires.
    """

    async def test_a_failed_material_is_selectable_and_gets_audio(
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
        assert result["status"] == "ready", result
        assert result["state"] == PENDING
        # Billed like any other material: the user chose it knowing the defects, so they intend
        # to listen to it.
        turns = extract_turns(material)
        assert len(wiring["raw"].requests) == len(turns)
        ref = wiring["state_store"].locate(MATERIAL_ID)
        keys = wiring["backing"].list_keys(ref.prefix)
        assert sum(1 for k in keys if k.endswith(".mp3")) == len(turns)
        assert ref.prefix + "audio/manifest.json" in keys
        # The quarantine sidecar is gone; audit.json is the only record of the verdict, and the
        # frontend states the shortcomings from it.
        assert not [k for k in keys if "quarantine" in k]
        assert json.loads(wiring["backing"].get(ref.prefix + "audit.json"))["verdict"] == "FAIL"

    async def test_a_failed_material_is_listed_in_pending_like_any_other(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        """It must be reachable through the normal listing, not a separate view."""
        audit = clone(audit_aligned)
        audit["verdict"] = "FAIL"
        wiring["registry"].register(make_candidate(material, blueprint, audit))
        await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        listed = wiring["state_store"].list_materials(PENDING)["items"]
        assert [r.material_id for r in listed] == [MATERIAL_ID]
        assert wiring["state_store"].verify_material(MATERIAL_ID)["ok"]

    async def test_a_not_assessable_selection_is_still_published_with_audio(
        self, wiring, material, blueprint, audit_aligned, clone
    ):
        """NOT_ASSESSABLE is refilled upstream (batch.py), so it should not reach select at all.

        If it somehow does -- a stale candidate from an earlier batch, say -- there is no longer a
        second code path to fall into. It publishes to pending with audio like everything else,
        which is a strictly better failure mode than a state nothing else understands.
        """
        audit = clone(audit_aligned)
        audit["verdict"] = "NOT_ASSESSABLE"
        audit["assessable"] = False
        wiring["registry"].register(make_candidate(material, blueprint, audit))
        result = await select_material(
            MATERIAL_ID, registry=wiring["registry"], state_store=wiring["state_store"],
            backing=wiring["backing"], polly=wiring["polly"], wait=True,
        )
        assert result["status"] == "ready" and result["state"] == PENDING
        ref = wiring["state_store"].locate(MATERIAL_ID)
        assert ref.state == PENDING
        assert not [k for k in wiring["backing"].list_keys(ref.prefix) if "quarantine" in k]

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


class TestSurvivesAcrossInstances:
    """The bug this fixes: AgentCore routes each invocation to whichever microVM is warm, so the
    `select` after a `generate` is routinely a different process. With an in-process registry,
    generation returned a material_id that the next call reported as unknown.

    Each test builds TWO registries over ONE shared store. That is the honest model of two
    microVMs: separate memory, same S3.
    """

    def test_a_second_instance_sees_a_candidate_it_never_registered(
        self, material, blueprint, audit_aligned
    ):
        shared = InMemoryCandidateStore()
        first = CandidateRegistry(store=shared)
        first.register(make_candidate(material, blueprint, audit_aligned))

        second = CandidateRegistry(store=shared)
        assert [c.material_id for c in second.all()] == [MATERIAL_ID]
        recovered = second.get(MATERIAL_ID)
        # The artifacts must survive too: the instance that synthesises needs the script, and it
        # is not the one that generated it.
        assert recovered.material == material
        assert recovered.blueprint == blueprint
        assert recovered.verdict == audit_aligned["verdict"]

    def test_an_unknown_id_still_raises_rather_than_inventing_a_candidate(self):
        registry = CandidateRegistry(store=InMemoryCandidateStore())
        with pytest.raises(UnknownMaterial):
            registry.get("20260728-nope-00000000")

    def test_only_one_of_two_instances_wins_the_group(
        self, material, blueprint, audit_aligned, clone
    ):
        """Two instances claiming the same group concurrently. Exactly one may proceed; the other
        must be refused, because the loser would otherwise pay for a second full synthesis."""
        shared = InMemoryCandidateStore()
        a = CandidateRegistry(store=shared)
        b = CandidateRegistry(store=shared)
        group = "batch-9:shared"
        first = make_candidate(material, blueprint, audit_aligned, group_key=group)
        second = make_candidate(clone(material), blueprint, audit_aligned,
                                material_id="20260728-accommodation-rental-cccc1111",
                                group_key=group)
        a.register(first)
        a.register(second)

        # b resolves its candidate BEFORE a claims. That ordering is the race: in production both
        # instances have already loaded a candidate when the two selects arrive. Resolving after
        # the claim would instead test discard, which is a different property.
        b_candidate = b.get(second.material_id)

        job, is_new, _ = a.claim(first, total=3)
        assert is_new and job.job_id
        with pytest.raises(AlreadySelected) as caught:
            b.claim(b_candidate, total=3)
        # The message names the winner, so the UI can say which candidate was kept.
        assert first.material_id in str(caught.value)

    def test_repeating_a_select_on_the_same_material_is_not_a_race(
        self, material, blueprint, audit_aligned
    ):
        """A retry from the browser must return the SAME job, not be refused and not re-bill."""
        shared = InMemoryCandidateStore()
        a = CandidateRegistry(store=shared)
        a.register(make_candidate(material, blueprint, audit_aligned))
        job, is_new, _ = a.claim(a.get(MATERIAL_ID), total=3)
        assert is_new

        b = CandidateRegistry(store=shared)
        again, is_new_again, _ = b.claim(b.get(MATERIAL_ID), total=3)
        assert not is_new_again
        assert again.job_id == job.job_id

    def test_progress_written_by_one_instance_is_readable_by_another(
        self, material, blueprint, audit_aligned
    ):
        """audio_status is polled repeatedly and may land anywhere. Without shared job state the
        UI would sit at 'queued' while synthesis actually ran to completion elsewhere."""
        shared = InMemoryCandidateStore()
        worker = CandidateRegistry(store=shared)
        worker.register(make_candidate(material, blueprint, audit_aligned))
        job, _, _ = worker.claim(worker.get(MATERIAL_ID), total=40)
        job.status = "synthesizing"
        job.done = 17
        job.polly_calls = 17
        worker.save_job(job)

        poller = CandidateRegistry(store=shared)
        seen = poller.job(MATERIAL_ID)
        assert seen is not None
        assert (seen.status, seen.done, seen.total) == ("synthesizing", 17, 40)
        # started_at is persisted rather than re-stamped: otherwise elapsed_seconds would reset to
        # zero on every poll served by a different instance.
        assert seen.started_at == job.started_at

    def test_a_discarded_sibling_is_gone_from_shared_storage(
        self, material, blueprint, audit_aligned, clone
    ):
        shared = InMemoryCandidateStore()
        a = CandidateRegistry(store=shared)
        group = "batch-7:shared"
        keep = make_candidate(material, blueprint, audit_aligned, group_key=group)
        drop = make_candidate(clone(material), blueprint, audit_aligned,
                              material_id="20260728-accommodation-rental-dddd2222",
                              group_key=group)
        a.register(keep)
        a.register(drop)
        _, _, discarded = a.claim(keep, total=3)
        assert discarded == [drop.material_id]

        b = CandidateRegistry(store=shared)
        assert shared.load(drop.material_id) is None
        with pytest.raises(UnknownMaterial):
            b.get(drop.material_id)

    def test_a_non_serialisable_cross_check_still_round_trips(
        self, material, blueprint, audit_aligned
    ):
        """The Loop hands over a CrossCheckResult object, not a dict. Persisting it raised
        `TypeError: Object of type CrossCheckResult is not JSON serializable`, which surfaced as an
        opaque 500 from the Runtime on `select` -- long after generation had reported success."""
        import json as _json

        class NotSerialisable:
            def __init__(self):
                self.matched = 10
                self.unrecoverable = []

            def as_dict(self):
                return {"matched": self.matched, "unrecoverable": self.unrecoverable}

        shared = InMemoryCandidateStore()
        registry = CandidateRegistry(store=shared)
        candidate = make_candidate(material, blueprint, audit_aligned)
        candidate.cross_check = NotSerialisable()
        registry.register(candidate)

        record = shared.load(MATERIAL_ID)
        # The real assertion: the record must survive an actual encode, which is what S3 does.
        _json.dumps(record)
        assert record["cross_check"] == {"matched": 10, "unrecoverable": []}

    def test_an_opaque_object_degrades_to_a_string_rather_than_raising(
        self, material, blueprint, audit_aligned
    ):
        """A field nobody can encode must not make the candidate unstorable: losing one diagnostic
        value is recoverable, losing the candidate means the user cannot select their material."""
        import json as _json

        class Opaque:
            __slots__ = ()

            def __repr__(self):
                return "<opaque>"

        shared = InMemoryCandidateStore()
        registry = CandidateRegistry(store=shared)
        candidate = make_candidate(material, blueprint, audit_aligned)
        candidate.cross_check = Opaque()
        registry.register(candidate)
        record = shared.load(MATERIAL_ID)
        _json.dumps(record)
        assert record["cross_check"] == "<opaque>"
        assert record["material"] == material


class TestAFailedSelectLeavesNothingStuck:
    """Observed in the Runtime: `claim` wrote the group marker, then the write of the winning
    candidate raised, leaving two claims in `_claims/` pointing at material_ids that had no
    candidate. Every later select on those groups was refused, naming a material nobody could
    find, and no amount of retrying could recover -- the batch was permanently unselectable.
    """

    def _failing_store(self, fail_on, times=1):
        """Fails the first `times` saves of `fail_on`, then behaves normally.

        A permanently-failing store cannot show recovery: the retry would fail for the original
        reason rather than because of a leftover claim, so the test would pass even with the
        rollback removed.
        """

        class Failing(InMemoryCandidateStore):
            def __init__(self):
                super().__init__()
                self.failures = 0

            def save(self, material_id, record):
                if material_id == fail_on and self.failures < times:
                    self.failures += 1
                    raise RuntimeError("s3 write refused")
                super().save(material_id, record)

        return Failing()

    def test_the_group_is_selectable_again_after_the_winner_fails_to_persist(
        self, material, blueprint, audit_aligned
    ):
        shared = self._failing_store(fail_on=MATERIAL_ID)
        registry = CandidateRegistry(store=shared)
        candidate = make_candidate(material, blueprint, audit_aligned)
        # Registration goes through the same failing path, so seed the record directly: the
        # property under test is claim's rollback, not register's.
        InMemoryCandidateStore.save(shared, MATERIAL_ID, candidate.as_record())

        with pytest.raises(RuntimeError):
            registry.claim(candidate, total=3)

        # The claim must be gone. If it survived, this next call would raise AlreadySelected.
        assert shared._claims == {}
        fresh = CandidateRegistry(store=shared)
        job, is_new, _ = fresh.claim(fresh.get(MATERIAL_ID), total=3)
        assert is_new and job.job_id

    def test_siblings_survive_a_failed_claim(
        self, material, blueprint, audit_aligned, clone
    ):
        """Discarding used to happen before the winner was persisted, so a failure destroyed the
        alternatives as well -- leaving the group with neither a winner nor a fallback."""
        shared = self._failing_store(fail_on=MATERIAL_ID)
        registry = CandidateRegistry(store=shared)
        group = "batch-11:shared"
        winner = make_candidate(material, blueprint, audit_aligned, group_key=group)
        sibling = make_candidate(clone(material), blueprint, audit_aligned,
                                 material_id="20260728-accommodation-rental-eeee3333",
                                 group_key=group)
        InMemoryCandidateStore.save(shared, winner.material_id, winner.as_record())
        registry.register(sibling)

        with pytest.raises(RuntimeError):
            registry.claim(winner, total=3)

        assert shared.load(sibling.material_id) is not None
        assert CandidateRegistry(store=shared).get(sibling.material_id).state != "discarded"

    def test_a_material_id_is_not_reported_when_registration_fails(
        self, material, blueprint, audit_aligned
    ):
        """The frontend offers whatever material_id a slot reports. If registration failed, that id
        resolves to nothing, so the UI showed a ready material whose select answered 'unknown'."""
        from backend.orchestration import batch as batch_mod

        class Boom:
            def register(self, candidate):
                raise RuntimeError("store unavailable")

        result = types.SimpleNamespace(
            candidate=types.SimpleNamespace(
                gen=types.SimpleNamespace(material=material, blueprint=blueprint),
                audit=audit_aligned, cross_check={"matched": 1},
            ),
            slot_id="slot-1", material_id=None, scenario_key=None, group_key=None,
            degraded=False, degraded_reason=None,
        )
        scenario = types.SimpleNamespace(id="accommodation-rental", key="accommodation-rental")

        # Patched on publish, not batch: `_register` imports REGISTRY from `.publish` at call
        # time, so rebinding the name on batch would have no effect and the test would pass
        # vacuously against the unfixed code.
        with mock.patch.object(publish_module, "REGISTRY", Boom()):
            with pytest.raises(RuntimeError):
                batch_mod._register(result, scenario, "batch-12:x")

        assert result.material_id is None
