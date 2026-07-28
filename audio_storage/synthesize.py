"""Per-turn Polly synthesis, idempotent on the clip's own bytes.

One request per turn is a Polly limit, not a choice: SynthesizeSpeech takes a single VoiceId,
and a Part 1 script has three speakers. So a material is 30-45 requests, and everything here
exists to make that set of requests safe to repeat.

**Where idempotency lives.** The cache key (design.md §4.3) is written as S3 user metadata on
each MP3, not only into the manifest. That matters because the manifest is the completeness
sentinel and therefore the one object a crashed run is missing -- if the skip decision read the
manifest, a resumed run would have nothing to read and would pay for every clip again. Reading
it back from the object gives one HeadObject that answers both questions at once: the clip
exists, and it is the clip this SSML would produce.

    metadata = {cache-key, text-sha256, turn-index, voice-id}

The key covers voice, engine, sample rate and the rendered SSML, so it changes when the text
changes, when the voice map changes, and when a render rule or the rate constant changes. The
last case forces a full resynthesis on purpose: a material whose clips mixed two speaking rates
would be worse than the cost of redoing it.

**Where the clips are written.** Straight into the destination state prefix
(``pending/{scenario_key}/{material_id}/audio/``), not to a staging area. Two reasons: the
skip-if-present check above only means anything if the bytes stay where they were paid for, and
``state_store.publish_material`` already treats a missing manifest as "incomplete, do not show
this" -- so clips on their own are invisible rather than half-published. publish_material is
then called with no audio payload; it heads every clip the manifest promises and writes the
sentinel last.

**Failure.** A turn that will not synthesise after its retries leaves the successful clips in
place and returns without a manifest. Nothing becomes visible, and the next run pays only for
what is still missing.

Rate and rules come from the measured constants (assumptions.py): ``MEASURED_DIALOGUE_RATE``,
and both say-as rules off, because real audio showed bare text already reads spelling and long
numbers correctly while the markup made Polly say "dash" or turned the British "oh" into "zero".
"""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from . import assumptions, manifest as manifest_module, ssml as ssml_module, voice
from .mp3_duration import duration_ms as parse_duration
from .state_store import PENDING, MANIFEST_NAME, StateStore

# design.md §4.1. Fixed, and part of the cache key: changing any of them invalidates every clip.
SAMPLE_RATE = "24000"
OUTPUT_FORMAT = "mp3"
TEXT_TYPE = "ssml"

# design.md §4.4. Serial would be 30-60s and synthesis happens after the user clicks "use this
# one", so the latency is UX. Four is deliberately below Polly's neural per-account TPS, which
# is a small single-digit number -- pushing it would trade throttling retries for nothing.
CONCURRENCY = 4

MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5

# Measured 2026-07-28 via the Pricing API (assumptions.BY_ID["ssml-tags-not-billed"]).
COST_PER_CHARACTER_USD = 16.0 / 1_000_000

# Narrator is fixed at the same measured rate as the dialogue. design.md §4.2 floats a slower
# narrator, but the exam frame is boilerplate the candidate does not transcribe, and a second
# rate constant would be a second thing to calibrate for no measurable gain. Both are stated in
# the manifest either way.
DIALOGUE_RATE = ssml_module.MEASURED_DIALOGUE_RATE
NARRATOR_RATE = ssml_module.MEASURED_DIALOGUE_RATE

_RETRYABLE_CODES = frozenset(
    [
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "ServiceFailureException",
        "InternalFailure",
        "RequestTimeout",
        "SlowDown",
        "503",
        "500",
    ]
)


class SynthesisError(RuntimeError):
    """Synthesis cannot proceed at all."""


class TurnSynthesisFailed(SynthesisError):
    """One turn exhausted its retries. Carries the index so the caller can report it."""

    def __init__(self, turn_index: int, cause: BaseException) -> None:
        super().__init__("turn {0} failed: {1}: {2}".format(turn_index, type(cause).__name__, cause))
        self.turn_index = turn_index
        self.cause = cause


class VoicesUnavailable(SynthesisError):
    """describe-voices does not offer a voice the map needs (design.md §2.1)."""


class StaleClips(SynthesisError):
    """A partial resynthesis would leave clips rendered under different rules side by side."""


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport and capacity errors; never retry a rejected request.

    An InvalidSsmlException or a TextLengthExceeded is deterministic -- retrying it three times
    just spends three times as long arriving at the same failure, and hides the real cause
    behind a timeout.
    """
    code = _error_code(exc)
    if code:
        return code in _RETRYABLE_CODES
    return isinstance(exc, (OSError, TimeoutError))


class PollyClient:
    """SynthesizeSpeech with a call counter and a character counter.

    The counters are the evidence for the idempotency requirement: a second run over an
    unchanged material must show ``calls == 0``. An assertion in a test can be written to pass;
    a counter that stays at zero while the manifest still comes out complete cannot.

    boto3 is imported in __init__, never at module scope -- nothing in this package may require
    AWS to be importable.
    """

    def __init__(self, client=None, *, region_name: Optional[str] = None) -> None:
        if client is None:
            import boto3  # noqa: PLC0415 - deliberately lazy
            from botocore.config import Config  # noqa: PLC0415

            client = boto3.client(
                "polly",
                region_name=region_name,
                # Adaptive mode backs off on the account-level TPS ceiling; the business-layer
                # retry below sits on top of it for the errors boto3 does not own.
                config=Config(retries={"max_attempts": MAX_ATTEMPTS, "mode": "adaptive"}),
            )
        self._client = client
        self._lock = threading.Lock()
        self.calls = 0
        self.failed_calls = 0
        self.billable_chars = 0
        self.audio_bytes = 0

    def describe_voices(self) -> List[str]:
        response = self._client.describe_voices(LanguageCode=voice.LANGUAGE_CODE, Engine=voice.ENGINE)
        return sorted(v["Id"] for v in response.get("Voices", []))

    def synthesize(self, rendered_ssml: str, voice_id: str, *, sample_rate: str = SAMPLE_RATE) -> bytes:
        with self._lock:
            self.calls += 1
        try:
            response = self._client.synthesize_speech(
                Engine=voice.ENGINE,
                LanguageCode=voice.LANGUAGE_CODE,
                VoiceId=voice_id,
                TextType=TEXT_TYPE,
                OutputFormat=OUTPUT_FORMAT,
                SampleRate=sample_rate,
                Text=rendered_ssml,
            )
        except BaseException:
            with self._lock:
                self.failed_calls += 1
            raise
        audio = response["AudioStream"].read()
        with self._lock:
            # Counting the rendered SSML rather than the plain text keeps every cost figure an
            # upper bound (assumptions.BY_ID["ssml-tags-not-billed"]: the Pricing API gives the
            # rate, not the counting rule, and the gap is at most 1.8x in our favour).
            self.billable_chars += len(rendered_ssml)
            self.audio_bytes += len(audio)
        return audio


def cost_usd(billable_chars: int) -> float:
    return round(billable_chars * COST_PER_CHARACTER_USD, 6)


def check_voices(polly: PollyClient, voice_map: Dict[str, str]) -> List[str]:
    """Confirm the region offers every voice in the map. Raises rather than substituting.

    design.md §2.1 is explicit that a missing Arthur needs a human decision -- every fallback
    (change region, two same-gender voices, narrator on the standard engine) has a real cost.
    """
    available = polly.describe_voices()
    missing = voice.unavailable_voices(available, voice_map)
    if missing:
        raise VoicesUnavailable(
            "voices {0} are not offered for {1}/{2} in this region (available: {3}); "
            "design.md §2.1 requires a human decision, not an automatic substitution".format(
                missing, voice.LANGUAGE_CODE, voice.ENGINE, available
            )
        )
    return available


@dataclass(frozen=True)
class TurnPlan:
    """Everything needed to synthesise one turn, and to decide not to."""

    turn_index: int
    speaker: str
    voice_id: str
    text: str
    ssml: str
    key: str
    cache_key: str
    text_sha256: str
    trailing_silence_ms: int

    def metadata(self) -> Dict[str, str]:
        """Written onto the MP3 so the next run can recognise it without the manifest."""
        return {
            "cache-key": self.cache_key,
            "text-sha256": self.text_sha256,
            "turn-index": str(self.turn_index),
            "voice-id": self.voice_id,
            "ssml-ruleset-version": str(ssml_module.SSML_RULESET_VERSION),
        }

    def matches(self, metadata: Optional[Dict[str, str]]) -> bool:
        return bool(metadata) and metadata.get("cache-key") == self.cache_key


@dataclass
class SynthesisResult:
    """Outcome of one synthesis pass. Reports what was paid for, not just what exists."""

    material_id: str
    scenario_key: str
    state: str
    prefix: str
    voice_map: Dict[str, str]
    manifest: Optional[dict] = None
    synthesized: List[int] = field(default_factory=list)
    reused: List[int] = field(default_factory=list)
    failed: Dict[int, str] = field(default_factory=dict)
    polly_calls: int = 0
    billable_chars: int = 0
    audio_bytes: int = 0
    elapsed_seconds: float = 0.0
    warnings: List[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.failed

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.billable_chars)

    def summary(self) -> dict:
        return {
            "material_id": self.material_id,
            "state": self.state,
            "clip_count": len(self.synthesized) + len(self.reused),
            "synthesized": len(self.synthesized),
            "reused": len(self.reused),
            "failed": sorted(self.failed),
            "polly_calls": self.polly_calls,
            "billable_chars": self.billable_chars,
            "cost_usd": self.cost_usd,
            "audio_bytes": self.audio_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "total_duration_ms": (self.manifest or {}).get("totals", {}).get("total_duration_ms"),
            "measured_dialogue_wpm": (self.manifest or {}).get("totals", {}).get(
                "measured_dialogue_wpm"
            ),
            "ok": self.ok,
        }


def plan_material(
    material: dict,
    *,
    voice_map: Dict[str, str],
    sample_rate: str = SAMPLE_RATE,
    rate: Optional[str] = DIALOGUE_RATE,
    narrator_rate: Optional[str] = NARRATOR_RATE,
    overrides: Optional[Dict[str, str]] = None,
    bake_pauses: bool = True,
    blueprint: Optional[dict] = None,
) -> List[TurnPlan]:
    """Render every turn and derive its cache key. Pure: no AWS, no clock, no randomness.

    Called before any request is made, so a rendering fault or an over-long turn fails before
    a single character is billed. The answer-word check runs here too: if a render rule damaged
    a blueprint target, that is caught while nothing has been spent.
    """
    turns = manifest_module.extract_turns(material)
    rendered: Dict[int, str] = {}
    plans: List[TurnPlan] = []
    for index, turn in enumerate(turns):
        config = manifest_module.render_config_for(
            turns, index, rate=rate, narrator_rate=narrator_rate,
            overrides=overrides, bake_pauses=bake_pauses,
        )
        # render_turn asserts strip_tags(render(text)) == text modulo the rule whitelist, so a
        # rule that silently dropped a word cannot reach Polly.
        rendered_ssml = ssml_module.render_turn(turn["text"], config)
        rendered[index] = rendered_ssml
        speaker = turn["speaker"]
        plans.append(
            TurnPlan(
                turn_index=index,
                speaker=speaker,
                voice_id=voice_map[speaker],
                text=turn["text"],
                ssml=rendered_ssml,
                key=manifest_module.clip_key(index),
                cache_key=manifest_module.cache_key(
                    voice_map[speaker], voice.ENGINE, sample_rate, rendered_ssml
                ),
                text_sha256=manifest_module.sha256_text(turn["text"]),
                trailing_silence_ms=config.trailing_silence_ms,
            )
        )
    if blueprint is not None:
        # Raises on damage. Cheaper to fail here than to discover it in a reviewer's ear.
        ssml_module.assert_targets_intact(turns, blueprint, rendered=rendered)
    return plans


def _synthesize_one(
    plan: TurnPlan,
    polly: PollyClient,
    store,
    prefix: str,
    sample_rate: str,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> bytes:
    """One turn, with business-layer retries on top of boto3's adaptive ones."""
    last: Optional[BaseException] = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            audio = polly.synthesize(plan.ssml, plan.voice_id, sample_rate=sample_rate)
            # Parse before writing: a truncated response would otherwise land in S3 carrying a
            # cache key that says "this clip is correct", and the next run would trust it.
            parse_duration(audio)
            store.put(prefix + plan.key, audio, metadata=plan.metadata())
            return audio
        except BaseException as exc:  # noqa: BLE001 - re-raised below once retries are spent
            last = exc
            if attempt == MAX_ATTEMPTS - 1 or not _is_retryable(exc):
                break
            delay = BACKOFF_BASE ** attempt + random.random()
            if on_event:
                on_event(
                    "turn_retry",
                    {"turn_index": plan.turn_index, "attempt": attempt + 1,
                     "error": str(exc)[:200], "retry_in": round(delay, 2)},
                )
            time.sleep(delay)
    raise TurnSynthesisFailed(plan.turn_index, last if last else RuntimeError("no error recorded"))


def _partition(
    plans: Sequence[TurnPlan], store, prefix: str, force: Sequence[int] = ()
) -> Dict[str, List[TurnPlan]]:
    """Split into clips already paid for and clips still to synthesise.

    The existence check is a HeadObject against the real object, never the manifest alone: an
    object deleted underneath a manifest must be noticed, and a resumed run has no manifest to
    consult in the first place (design.md §4.3).
    """
    forced = set(force)
    reuse: List[TurnPlan] = []
    todo: List[TurnPlan] = []
    for plan in plans:
        if plan.turn_index in forced:
            todo.append(plan)
            continue
        if plan.matches(store.head_metadata(prefix + plan.key)):
            reuse.append(plan)
        else:
            todo.append(plan)
    return {"reuse": reuse, "todo": todo}


def synthesize_material(
    material: dict,
    *,
    material_id: str,
    scenario_key: str,
    store,
    polly: PollyClient,
    blueprint: Optional[dict] = None,
    voice_map: Optional[Dict[str, str]] = None,
    voice_override: Optional[Dict[str, str]] = None,
    state: str = PENDING,
    sample_rate: str = SAMPLE_RATE,
    rate: Optional[str] = DIALOGUE_RATE,
    narrator_rate: Optional[str] = NARRATOR_RATE,
    overrides: Optional[Dict[str, str]] = None,
    concurrency: int = CONCURRENCY,
    degraded: bool = False,
    degraded_reason: Optional[str] = None,
    synthesized_at: Optional[str] = None,
    force_turns: Sequence[int] = (),
    verify_voices: bool = False,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> SynthesisResult:
    """Synthesise every turn of one material into its destination prefix.

    Returns the manifest rather than writing it: publish_material owns the sentinel, so there is
    exactly one place that decides a material has become visible. On any turn failure the
    manifest is None and the successful clips stay in S3 for the next run to reuse.
    """
    assumptions.require_phase0("Polly synthesis")
    started = time.monotonic()
    resolved_map = dict(voice_map) if voice_map else voice.resolve_voice_map(material_id, voice_override)
    if verify_voices:
        check_voices(polly, resolved_map)

    prefix = "{0}/{1}/{2}/".format(state, scenario_key, material_id)
    plans = plan_material(
        material, voice_map=resolved_map, sample_rate=sample_rate, rate=rate,
        narrator_rate=narrator_rate, overrides=overrides, blueprint=blueprint,
    )
    split = _partition(plans, store, prefix, force_turns)
    result = SynthesisResult(
        material_id=material_id, scenario_key=scenario_key, state=state,
        prefix=prefix, voice_map=resolved_map,
        reused=[p.turn_index for p in split["reuse"]],
    )
    if on_event:
        on_event(
            "synthesis_started",
            {"material_id": material_id, "total": len(plans),
             "to_synthesize": len(split["todo"]), "reused": len(split["reuse"])},
        )

    calls_before = polly.calls
    chars_before = polly.billable_chars
    bytes_before = polly.audio_bytes
    fresh: Dict[int, bytes] = {}

    if split["todo"]:
        workers = max(1, min(concurrency, len(split["todo"])))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _synthesize_one, plan, polly, store, prefix, sample_rate, on_event
                ): plan
                for plan in split["todo"]
            }
            for future in concurrent.futures.as_completed(futures):
                plan = futures[future]
                try:
                    fresh[plan.turn_index] = future.result()
                except TurnSynthesisFailed as exc:
                    # Collected, not raised: the remaining turns are still worth completing so
                    # the next run has less to pay for (design.md §4.4).
                    result.failed[plan.turn_index] = str(exc.cause)[:300]
                    if on_event:
                        on_event("turn_failed", {"turn_index": plan.turn_index,
                                                 "error": str(exc.cause)[:200]})
                else:
                    result.synthesized.append(plan.turn_index)
                    if on_event:
                        on_event(
                            "turn_done",
                            {"turn_index": plan.turn_index,
                             "done": len(result.synthesized) + len(result.reused),
                             "total": len(plans)},
                        )

    result.synthesized.sort()
    result.polly_calls = polly.calls - calls_before
    result.billable_chars = polly.billable_chars - chars_before
    result.audio_bytes = polly.audio_bytes - bytes_before
    result.elapsed_seconds = time.monotonic() - started

    if result.failed:
        # No manifest: the read side treats the directory as incomplete, so nothing half-built
        # is ever shown. This is the sentinel doing its job, not an error path of its own.
        if on_event:
            on_event("synthesis_failed", {"material_id": material_id,
                                          "missing": sorted(result.failed)})
        return result

    clips = []
    for plan in plans:
        audio = fresh.get(plan.turn_index)
        if audio is None:
            # Reused clip: fetch the bytes to measure the duration from the MP3 itself rather
            # than trusting a number copied out of the old manifest. A GET is free next to a
            # SynthesizeSpeech call, and duration is what the reviewer's band check runs on.
            audio = store.get(prefix + plan.key)
        clips.append(
            manifest_module.ClipInput(
                turn_index=plan.turn_index, ssml=plan.ssml, audio_bytes=audio
            )
        )

    result.manifest = manifest_module.build_manifest(
        material,
        material_id=material_id,
        scenario_key=scenario_key,
        voice_map=resolved_map,
        clips=clips,
        sample_rate=sample_rate,
        rate=rate,
        narrator_rate=narrator_rate,
        synthesized_at=synthesized_at or _utc_now(),
        blueprint=blueprint,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
    result.warnings = list(result.manifest.get("warnings") or [])
    if on_event:
        on_event("synthesis_done", result.summary())
    return result


def resynthesize_turns(
    state_store: StateStore,
    material_id: str,
    turn_indexes: Sequence[int],
    *,
    polly: PollyClient,
    store=None,
    sample_rate: str = SAMPLE_RATE,
    rate: Optional[str] = DIALOGUE_RATE,
    narrator_rate: Optional[str] = NARRATOR_RATE,
    overrides: Optional[Dict[str, str]] = None,
    synthesized_at: Optional[str] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> SynthesisResult:
    """Resynthesise the named turns of a published material and rewrite its manifest.

    The point is that a one-line edit costs one Polly request, not forty. Two things make that
    safe:

    * The voice map is read from the manifest, not recomputed. The manifest is the settled fact
      for this material (design.md §5), so a later change to the assignment algorithm cannot
      make one clip come back in a different voice.
    * Every turn that is *not* being resynthesised is checked against its stored cache key. If
      the render rules or the rate constant have moved since the material was made, the untouched
      clips no longer match and a partial run would leave two speaking rates in one material --
      so it raises and asks for a full resynthesis instead.
    """
    assumptions.require_phase0("Polly synthesis")
    backing = store if store is not None else state_store.object_store
    ref = state_store.locate(material_id)
    bundle = state_store.get_material(material_id)
    old_manifest = bundle.get("manifest")
    if not old_manifest:
        raise SynthesisError(
            "{0} has no manifest; it was never completely synthesised, so run "
            "synthesize_material rather than a partial resynthesis".format(material_id)
        )
    requested = sorted({int(i) for i in turn_indexes})
    if not requested:
        raise SynthesisError("no turn indexes given")

    resolved_map = dict(old_manifest["synthesis"]["voice_map"])
    plans = plan_material(
        bundle["material"], voice_map=resolved_map, sample_rate=sample_rate, rate=rate,
        narrator_rate=narrator_rate, overrides=overrides, blueprint=bundle.get("blueprint"),
    )
    by_index = {p.turn_index: p for p in plans}
    unknown = [i for i in requested if i not in by_index]
    if unknown:
        raise SynthesisError("turns {0} do not exist in this material".format(unknown))

    stale = [
        p.turn_index
        for p in plans
        if p.turn_index not in requested
        and not p.matches(backing.head_metadata(ref.prefix + p.key))
    ]
    if stale:
        raise StaleClips(
            "turns {0} no longer match their stored audio, so resynthesising only {1} would "
            "mix render rules or speaking rates within one material; resynthesise the whole "
            "material instead".format(stale, requested)
        )

    result = SynthesisResult(
        material_id=material_id, scenario_key=ref.scenario_key, state=ref.state,
        prefix=ref.prefix, voice_map=resolved_map,
        reused=[p.turn_index for p in plans if p.turn_index not in requested],
    )
    calls_before, chars_before, bytes_before = polly.calls, polly.billable_chars, polly.audio_bytes
    started = time.monotonic()
    fresh: Dict[int, bytes] = {}
    for index in requested:
        plan = by_index[index]
        try:
            fresh[index] = _synthesize_one(
                plan, polly, backing, ref.prefix, sample_rate, on_event
            )
        except TurnSynthesisFailed as exc:
            result.failed[index] = str(exc.cause)[:300]
        else:
            result.synthesized.append(index)
    result.polly_calls = polly.calls - calls_before
    result.billable_chars = polly.billable_chars - chars_before
    result.audio_bytes = polly.audio_bytes - bytes_before
    result.elapsed_seconds = time.monotonic() - started
    if result.failed:
        # The old manifest stays in place: it still describes audio that exists and is
        # internally consistent. Leaving the material readable beats blanking it out.
        return result

    old_durations = {c["turn_index"]: c["duration_ms"] for c in old_manifest.get("clips", [])}
    clips = []
    for plan in plans:
        audio = fresh.get(plan.turn_index)
        if audio is not None:
            clips.append(
                manifest_module.ClipInput(
                    turn_index=plan.turn_index, ssml=plan.ssml, audio_bytes=audio
                )
            )
        else:
            # Untouched clip: its cache key was verified above, so the recorded duration still
            # describes the object in S3. Re-downloading 40 MP3s to re-measure numbers that
            # cannot have changed would defeat the purpose of a single-turn resynthesis.
            clips.append(
                manifest_module.ClipInput(
                    turn_index=plan.turn_index, ssml=plan.ssml,
                    duration_ms=old_durations[plan.turn_index],
                )
            )

    new_manifest = manifest_module.build_manifest(
        bundle["material"],
        material_id=material_id,
        scenario_key=ref.scenario_key,
        voice_map=resolved_map,
        clips=clips,
        sample_rate=sample_rate,
        rate=rate,
        narrator_rate=narrator_rate,
        synthesized_at=synthesized_at or _utc_now(),
        blueprint=bundle.get("blueprint"),
        degraded=bool(old_manifest.get("degraded")),
        degraded_reason=old_manifest.get("degraded_reason"),
    )
    if old_manifest.get("degraded"):
        new_manifest["degraded"] = True
        new_manifest["degraded_reason"] = old_manifest.get("degraded_reason")
    new_manifest["resynthesized_turns"] = requested
    backing.put(ref.prefix + MANIFEST_NAME, _dumps(new_manifest))
    result.manifest = new_manifest
    result.warnings = list(new_manifest.get("warnings") or [])
    if on_event:
        on_event("resynthesis_done", result.summary())
    return result


def _dumps(payload: dict) -> bytes:
    import json  # noqa: PLC0415 - keeps the module's import list to what the hot path needs

    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


def _utc_now() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
