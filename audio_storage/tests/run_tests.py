#!/usr/bin/env python3
"""Offline test suite for audio_storage. Run: python3 audio_storage/tests/run_tests.py

Every test here runs without AWS. The script input is the real 535-word, 40-turn fixture from
the contract task rather than an invented one, so the SSML rules, the manifest alignment and
the state flow are all exercised against text that actually shipped: a hyphenated spelling
run (F-O-R-D-Y-C-E), an 11-digit phone number, a postcode, a correction cycle and three
narrator turns.

What is NOT tested here, because it cannot be: how Polly actually reads any of it. See
audio_storage/assumptions.py.
"""

from __future__ import annotations

import copy
import io
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from audio_storage import (
    assumptions,
    manifest as manifest_module,
    mp3_duration,
    ssml,
    state_store as state_store_module,
    synthesize,
    voice,
)
from audio_storage.manifest import ClipInput
from audio_storage.object_store import (
    ConditionalWriteUnsupported,
    InMemoryObjectStore,
    ObjectNotFound,
    PreconditionFailed,
)
from audio_storage.state_store import (
    APPROVED,
    PENDING,
    PRODUCTION,
    REJECTED,
    STATES,
    IllegalTransition,
    InjectedCrash,
    MaterialNotFound,
    StateStore,
    StateStoreError,
    TransitionInFlight,
)

FIXTURES = ROOT / "skills" / "ielts-listening-skills" / "shared" / "tests" / "fixtures"
MATERIAL_ID = "20260728-accommodation-rental-7f3a1c2d"
SCENARIO_KEY = "accommodation-rental"

failures: list = []


# Both say-as rules default OFF after the 2026-07-28 listening tests (see ssml.py).
# These tests exist to prove the rules still work when a caller opts in, so they pass this
# config explicitly rather than relying on defaults.
SAY_AS_ON = ssml.RenderConfig(spelling_say_as=True, digits_say_as=True)


def check(name: str, condition: bool, detail: str = "") -> None:
    print("  {0}  {1}".format("PASS" if condition else "FAIL", name))
    if not condition:
        failures.append("{0}{1}".format(name, ": " + detail if detail else ""))


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def turns_of(material: dict) -> list:
    return material["listening_material_parts"][0]["script"]["turns"]


# --------------------------------------------------------------------------- voice


def test_voice_map_is_deterministic() -> None:
    print("voice mapping determinism (R1)")
    first = voice.resolve_voice_map(MATERIAL_ID)
    check(
        "100 calls give an identical map",
        all(voice.resolve_voice_map(MATERIAL_ID) == first for _ in range(100)),
    )
    check("narrator is always Brian", first["speaker1"] == "Brian")
    check(
        "dialogue voices are Amy and Arthur, one each",
        {first["speaker2"], first["speaker3"]} == {"Amy", "Arthur"},
        json.dumps(first),
    )

    # A fresh interpreter must agree, or a resynthesis could silently change voices. This is
    # why the swap uses sha256 and not hash(): PYTHONHASHSEED makes str hashes vary per
    # process, so a hash()-based swap would pass the loop above and still fail here.
    import subprocess

    probe = (
        "import sys; sys.path.insert(0, {0!r});"
        "from audio_storage import voice; print(voice.resolve_voice_map({1!r})['speaker2'])"
    ).format(str(ROOT), MATERIAL_ID)
    outs = set()
    for seed in ("0", "1", "12345"):
        import os

        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.add(
            subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True, env=env
            ).stdout.strip()
        )
    check(
        "map survives a new process with a different hash seed",
        outs == {first["speaker2"]},
        "got {0}".format(outs),
    )


def test_voice_swap_distribution() -> None:
    print("voice swap distribution (R1: avoid a single-gender service side)")
    ids = ["20260728-{0}-{1:08x}".format(SCENARIO_KEY, n) for n in range(1000)]
    swapped = sum(voice.voice_swap_applied(i) for i in ids)
    check(
        "swap rate over 1000 ids is 45-55% (got {0})".format(swapped / 10.0),
        450 <= swapped <= 550,
        str(swapped),
    )
    amy_as_provider = sum(voice.resolve_voice_map(i)["speaker2"] == "Amy" for i in ids)
    check(
        "Amy is the provider in 45-55% of materials (got {0})".format(amy_as_provider / 10.0),
        450 <= amy_as_provider <= 550,
    )
    # The corpus-level property that motivates the whole design: a candidate must not be able
    # to learn "the information holder is female" as a content-free cue.
    check(
        "neither dialogue voice is pinned to one role across the corpus",
        0 < amy_as_provider < 1000,
    )


def test_voice_override_and_guards() -> None:
    print("voice override")
    pinned = voice.resolve_voice_map(MATERIAL_ID, override={"speaker2": "Amy"})
    check("override pins the requested speaker", pinned["speaker2"] == "Amy")
    check("override leaves the narrator alone", pinned["speaker1"] == "Brian")
    check(
        "override that collapses both dialogue voices is refused",
        _raises(
            voice.VoiceMapError,
            lambda: voice.resolve_voice_map(MATERIAL_ID, override={"speaker2": "Amy", "speaker3": "Amy"}),
        ),
    )
    check(
        "unknown speaker in an override is refused",
        _raises(
            voice.VoiceMapError,
            lambda: voice.resolve_voice_map(MATERIAL_ID, override={"speaker4": "Amy"}),
        ),
    )
    check(
        "empty material_id is refused",
        _raises(voice.VoiceMapError, lambda: voice.resolve_voice_map("")),
    )
    check(
        "unavailable voices are reported against a region's real voice list",
        voice.unavailable_voices(["Brian", "Amy"], voice.resolve_voice_map(MATERIAL_ID)) == ["Arthur"],
    )


def test_gender_marker_scan() -> None:
    print("gender-marker warning scan (design.md §2.2)")
    material = load("material_valid.json")
    real = voice.detect_gender_markers(turns_of(material), voice.resolve_voice_map(MATERIAL_ID))
    check(
        "the real fixture produces no false positives",
        real == [],
        json.dumps(real),
    )
    flagged = voice.detect_gender_markers(
        [
            {"speaker": "speaker2", "text": "Good morning, this is Mrs Green speaking."},
            {"speaker": "speaker3", "text": "Thank you, Sir. I'll call back later."},
            {"speaker": "speaker2", "text": "We have two bedrooms available."},
            {"speaker": "speaker1", "text": "Mr Smith will now read the questions."},
        ],
        {"speaker2": "Arthur", "speaker3": "Amy"},
    )
    kinds = {w["kind"] for w in flagged}
    check("self-introduction with a title is flagged", "self_introduction_with_title" in kinds)
    check("direct address as Sir is flagged", "direct_address_sir_madam" in kinds)
    check(
        "a neutral dialogue turn is not flagged",
        all(w["turn_index"] != 2 for w in flagged),
        json.dumps(flagged),
    )
    check(
        "narrator turns are not scanned (its voice is fixed, so no clash is possible)",
        all(w["turn_index"] != 3 for w in flagged),
    )
    check(
        "warning names the voice it may clash with",
        all(w["assigned_voice"] in ("Amy", "Arthur") for w in flagged),
    )
    check("warnings never raise", isinstance(flagged, list))


# --------------------------------------------------------------------------- ssml


def test_render_operation_order() -> None:
    print("SSML operation order (design.md §3.4)")
    # The order-of-operations bug this guards against: escape after inserting tags and the
    # tags become literal text Polly reads aloud; escape only and never insert and `&` is
    # fine but the digits are read as a cardinal. Both are silent failures in production.
    out = ssml.render_ssml("Ring 04196570156 about Fish & Chips.", SAY_AS_ON)
    check(
        "the ampersand is escaped",
        "Fish &amp; Chips" in out,
        out,
    )
    check(
        "the inserted say-as tag is NOT escaped",
        '<say-as interpret-as="digits">04196570156</say-as>' in out,
        out,
    )
    check("no double-escaped markup leaked", "&lt;say-as" not in out, out)
    check("wrapped in speak exactly once", out.count("<speak>") == 1 and out.endswith("</speak>"))

    # An override supplies raw SSML. If overrides ran after escaping was applied to their
    # output, or if a later rule scanned inside them, the fragment would be corrupted.
    cfg = ssml.RenderConfig(
        pronunciation_overrides={"BT14 9BJ": '<say-as interpret-as="characters">BT149BJ</say-as>'}
    )
    out = ssml.render_ssml("It's BT14 9BJ.", cfg)
    check(
        "override fragment survives verbatim",
        '<say-as interpret-as="characters">BT149BJ</say-as>' in out and "&lt;" not in out,
        out,
    )
    check(
        "a later rule does not reach inside an override's output",
        out.count("say-as") == 2,
        out,
    )

    # Rule 1 emits letters; rule 2 must not then see digits inside rule 1's output.
    cfg = ssml.RenderConfig(pronunciation_overrides={"code": '<say-as interpret-as="digits">12345</say-as>'})
    out = ssml.render_ssml("The code is here.", cfg)
    check(
        "digits rule does not re-tag digits an earlier rule emitted",
        out.count("interpret-as=\"digits\"") == 1,
        out,
    )


def test_spelling_rule_boundaries() -> None:
    print("SSML rule 1: spelling runs")
    out = ssml.render_ssml("It's F-O-R-D-Y-C-E and that's in Ballysillan.", SAY_AS_ON)
    check(
        "a 7-letter run is tagged with hyphens stripped",
        '<say-as interpret-as="characters">FORDYCE</say-as>' in out,
        out,
    )
    check("surrounding text is untouched", "in Ballysillan" in out, out)

    # The over-greedy-regex boundary. implement.md makes T-shirt and 2010 the review gate.
    # T-shirt / e-mail / well-known are safe structurally: the pattern needs a *single* letter
    # at each position, so a multi-letter component can never match. A-B is the case that
    # depends on the {2,} repetition count, and it is the one a loosened regex breaks first.
    for text, label in (
        ("I need a T-shirt in medium.", "T-shirt"),
        ("Send me an e-mail tomorrow.", "e-mail"),
        ("The A-B route is closed.", "A-B (two letters, below the 3-letter threshold)"),
        ("It is a well-known problem.", "well-known"),
        ("Ask at the X-ray department.", "X-ray"),
    ):
        out = ssml.render_ssml(text)
        check("{0} is not mistaken for a spelling run".format(label), "say-as" not in out, out)

    out = ssml.render_ssml("The reference is A-B-C.", ssml.RenderConfig(spelling_say_as=True, strip_spelling_hyphens=False))
    check(
        "hyphen stripping is toggleable (待实测: hyphen may be read as 'dash')",
        '<say-as interpret-as="characters">A-B-C</say-as>' in out,
        out,
    )
    out = ssml.render_ssml("The reference is A-B-C.", ssml.RenderConfig(spelling_say_as=False))
    check("the whole spelling rule is toggleable", "say-as" not in out, out)
    out = ssml.render_ssml("The reference is A-B-C.", ssml.RenderConfig(spelling_say_as=True, spelling_rate="90%"))
    check("spelling rate is only added when configured", 'rate="90%"' in out, out)


def test_digits_rule_boundaries() -> None:
    print("SSML rule 2: long digit runs")
    out = ssml.render_ssml("It's 07840051963.", SAY_AS_ON)
    check(
        "an 11-digit phone number is tagged",
        '<say-as interpret-as="digits">07840051963</say-as>' in out,
        out,
    )
    for text, label in (
        ("Built in 2010 by the council.", "2010 (a year)"),
        ("It's 118 Fordyce.", "118 (a house number)"),
        ("That'll be 45 pounds.", "45"),
        ("Room 9 is free.", "9"),
    ):
        out = ssml.render_ssml(text)
        check("{0} keeps Polly's own normalisation".format(label), "say-as" not in out, out)
    out = ssml.render_ssml("Call 07840051963.", ssml.RenderConfig(digits_say_as=False))
    check("the digits rule is toggleable", "say-as" not in out, out)


def test_untouched_patterns() -> None:
    print("SSML minimal intervention (design.md §3.2)")
    for text in (
        "That comes to £45.50 in total.",
        "We open at half past nine.",
        "The deadline is 3rd September.",
        "Dial 999 in an emergency.",
        "It's BT14 9BJ.",
    ):
        out = ssml.render_ssml(text)
        check(
            "left to Polly: {0!r}".format(text[:34]),
            "say-as" not in out,
            out,
        )


def test_invariant_and_strip_tags() -> None:
    print("SSML invariant (design.md §3.3)")
    material = load("material_valid.json")
    turns = turns_of(material)

    # Every turn of the real script, not a sample: render_turn raises on any violation, and
    # the stripped result is compared to the source with only the documented whitelist
    # difference (rule 1's hyphens) allowed.
    # Run with the say-as rules ON, which is the strictest case: rule 1 rewrites hyphenated
    # runs, so the whitelist below has something to allow. With the shipped defaults both rules
    # are off and the render is a plain wrap, which the next check covers.
    mismatched = []
    for index, turn in enumerate(turns):
        stripped = ssml.strip_tags(ssml.render_turn(turn["text"], SAY_AS_ON))
        expected = ssml.SPELLING_RE.sub(
            lambda m: m.group(0).replace("-", ""), turn["text"]
        )
        if stripped != expected:
            mismatched.append((index, stripped, expected))
    check(
        "all {0} turns of the real script satisfy the invariant".format(len(turns)),
        not mismatched,
        json.dumps(mismatched[:2]),
    )
    # Proof the loop above is not vacuous: turn 8 carries the spelling run, so its render
    # genuinely differs from a plain wrap and still passes.
    check(
        "the spelling turn was actually transformed, so the loop tested something",
        ssml.rendered_differs(turns[8]["text"], ssml.render_turn(turns[8]["text"], SAY_AS_ON))
        and "FORDYCE" in ssml.render_turn(turns[8]["text"], SAY_AS_ON),
    )
    # The shipped default must leave the spelling run exactly as written: bare hyphenated
    # letters already read out correctly, and this is what the archived real scripts do.
    check(
        "with shipped defaults the spelling run is passed through untouched",
        "F-O-R-D-Y-C-E" in ssml.strip_tags(ssml.render_turn(turns[8]["text"]))
        and "say-as" not in ssml.render_turn(turns[8]["text"]),
    )
    check(
        "with shipped defaults every turn round-trips unchanged",
        all(
            ssml.strip_tags(ssml.render_turn(t["text"])) == t["text"]
            for t in turns
        ),
    )
    check(
        "strip_tags reverses XML escaping",
        ssml.strip_tags(ssml.render_ssml("Fish & Chips at 7.")) == "Fish & Chips at 7.",
    )
    check(
        "a corrupted render is caught rather than shipped",
        _raises(
            ssml.TargetDamaged,
            lambda: ssml.assert_invariant("It's 07840051963.", "<speak>It's 0784005196.</speak>"),
        ),
    )


def test_render_guards() -> None:
    print("SSML guards")
    check("empty text is refused", _raises(ssml.SsmlError, lambda: ssml.render_ssml("   ")))
    check("non-string text is refused", _raises(ssml.SsmlError, lambda: ssml.render_ssml(None)))
    check(
        "an over-long turn errors instead of being silently truncated",
        _raises(ssml.SsmlError, lambda: ssml.render_ssml("word " * 700)),
    )
    check(
        "billable char limit is the documented 3000",
        ssml.MAX_BILLABLE_CHARS == 3000,
    )
    out = ssml.render_ssml("Ready.", ssml.RenderConfig(trailing_silence_ms=500))
    check("trailing break is baked when configured", out.endswith('<break time="500ms"/></speak>'), out)
    check(
        "no break is emitted when the pause is zero",
        "break" not in ssml.render_ssml("Ready.", ssml.RenderConfig(trailing_silence_ms=0)),
    )
    check(
        "no prosody rate is emitted by default (default WPM is unmeasured)",
        "prosody" not in ssml.render_ssml("Ready."),
    )
    check(
        "rendered_differs distinguishes a rule firing from a plain wrap",
        not ssml.rendered_differs("Plain text.", ssml.render_ssml("Plain text."))
        and ssml.rendered_differs("Call 07840051963.", ssml.render_ssml("Call 07840051963.", SAY_AS_ON)),
    )


def test_targets_intact() -> None:
    print("答案原词铁律 (R4, acceptance: targets unchanged)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    turns = turns_of(material)
    report = ssml.assert_targets_intact(turns, blueprint)
    check("all 10 blueprint items are reported on", len(report) == 10, json.dumps(report))
    damaged = [r for r in report if r["status"] == "damaged"]
    check("no target is damaged by rendering", not damaged, json.dumps(damaged))
    phone = [r for r in report if r["target"] == "07840051963"][0]
    check(
        "the phone number survives the digits rule intact",
        phone["status"] == "intact",
        json.dumps(phone),
    )
    postcode = [r for r in report if r["target"] == "BT14 9BJ"][0]
    check("the postcode survives intact", postcode["status"] == "intact", json.dumps(postcode))

    # A spelled surname is the case the naive check gets wrong: blueprint target `Fordyce`,
    # script `F-O-R-D-Y-C-E`, render `FORDYCE`. All three must count as the same word.
    spelled = copy.deepcopy(blueprint)
    spelled["items"][1]["target"] = "Fordyce"
    report = ssml.assert_targets_intact(turns, spelled)
    check(
        "a spelled surname counts as intact after hyphen stripping",
        [r for r in report if r["target"] == "Fordyce"][0]["status"] == "intact",
        json.dumps(report[1]),
    )

    # Proof the check has teeth: a rule that eats the answer must raise, not warn.
    broken = ssml.RenderConfig(pronunciation_overrides={"07840051963": "<break time=\"1s\"/>"})
    check(
        "a rule that deletes an answer word raises TargetDamaged",
        _raises(ssml.TargetDamaged, lambda: ssml.assert_targets_intact(turns, blueprint, broken)),
    )
    # And that it distinguishes a rule bug from a merely paraphrased target.
    paraphrased = copy.deepcopy(blueprint)
    paraphrased["items"][0]["target"] = "a completely absent phrase"
    statuses = {
        r["number"]: r["status"] for r in ssml.check_targets_intact(turns, paraphrased)
    }
    check(
        "a target absent from the source is reported separately, not as damage",
        statuses[1] == "absent_from_source",
        json.dumps(statuses),
    )
    bad_anchor = copy.deepcopy(blueprint)
    bad_anchor["items"][0]["turn_index"] = 999
    check(
        "an out-of-range anchor is reported, not crashed on",
        ssml.check_targets_intact(turns, bad_anchor)[0]["status"] == "bad_anchor",
    )


# --------------------------------------------------------------------------- mp3


def _silent_mp3(frames: int, bitrate_byte: int = 0x50, rate_byte: int = 0x80) -> bytes:
    """A byte-accurate MPEG1 Layer III frame stream. Header bits are what the parser reads,
    so a synthetic stream tests the arithmetic exactly as a real Polly MP3 would."""
    header = bytes([0xFF, 0xFB, bitrate_byte, rate_byte])
    parsed = mp3_duration._parse_header(header)
    frame_length = parsed[0]
    return (header + b"\x00" * (frame_length - 4)) * frames


def test_mp3_duration() -> None:
    print("MP3 duration parsing (design.md §5.1)")
    # MPEG1 Layer III at 44100 Hz: 1152 samples per frame = 26.122 ms.
    data = _silent_mp3(100)
    mpeg1_expected = int(round(100 * 1152 / 44100.0 * 1000))
    got = mp3_duration.duration_ms(data)
    check(
        "100 frames at 44.1kHz measure {0} ms (got {1})".format(mpeg1_expected, got),
        got == mpeg1_expected,
        str(got),
    )
    check(
        "duration scales linearly with frame count",
        mp3_duration.duration_ms(_silent_mp3(200)) == 2 * mpeg1_expected,
    )

    # 24 kHz is what Polly returns, and it is MPEG2 Layer III: 576 samples per frame, not
    # 1152. Getting that wrong doubles every duration in the manifest.
    # header[1]=0xF3 -> MPEG2 Layer III; header[2]=0x54 -> bitrate index 5, rate index 1 (24kHz).
    mpeg2 = bytes([0xFF, 0xF3, 0x54, 0x00])
    parsed = mp3_duration._parse_header(mpeg2)
    check("the 24kHz test header really is 24kHz", parsed[2] == 24000, str(parsed))
    stream = (mpeg2 + b"\x00" * (parsed[0] - 4)) * 50
    got = mp3_duration.duration_ms(stream)
    mpeg2_expected = int(round(50 * 576 / 24000.0 * 1000))
    check(
        "MPEG2 Layer III at 24kHz uses 576 samples/frame ({0} ms)".format(mpeg2_expected),
        got == mpeg2_expected,
        str(got),
    )
    check(
        "the same frame count at 24kHz is not mistaken for 1152 samples/frame",
        got != int(round(50 * 1152 / 24000.0 * 1000)),
    )
    info = mp3_duration.describe(stream)
    check("describe reports the sample rate Polly was asked for", info["sample_rates"] == [24000])

    # Real Polly output, not a synthesised frame. This is the case the checks above could not
    # catch: they build a stream whose frame length is derived from the same bitrate lookup they
    # then verify, so a wrong bitrate stayed self-consistent and passed. Real files carry an
    # independently-known duration. The bug this locks down keyed the bitrate rows by raw header
    # bits while _parse_header passes the decoded layer number, so MPEG2 Layer III read the
    # Layer I row -- 96 kbps instead of 48 -- and every duration came out exactly half.
    samples_dir = ROOT / "audio_storage" / "probe_samples"
    expected_ms = {
        "probe_nobreak.mp3": 2184,
        "probe_break.mp3": 2976,
        "final_turn8.mp3": 11496,
        "wpm_default.mp3": 48720,
    }
    available = {n: ms for n, ms in expected_ms.items() if (samples_dir / n).is_file()}
    if available:
        wrong = []
        for name, want in sorted(available.items()):
            got = mp3_duration.duration_ms((samples_dir / name).read_bytes())
            if abs(got - want) > 60:
                wrong.append("{0}: got {1} want {2}".format(name, got, want))
        check(
            "real Polly MP3s parse to their known durations ({0} files)".format(len(available)),
            not wrong,
            "; ".join(wrong),
        )
        check(
            "real Polly output is read as 48 kbps MPEG2 Layer III, not 96",
            mp3_duration.describe((samples_dir / "probe_break.mp3").read_bytes())["bitrates_kbps"] == [48],
        )
    else:
        print("  SKIP  no probe samples on disk")

    # ID3v2 is the systematic-bias trap the byte-count shortcut would fall into.
    tag = b"ID3\x03\x00\x00" + bytes([0, 0, 0x02, 0x01]) + b"\x00" * 257
    check(
        "an ID3v2 header is skipped, not counted as audio",
        mp3_duration.duration_ms(tag + data) == mp3_duration.duration_ms(data),
    )
    check("id3 size is reported", mp3_duration.describe(tag + data)["id3v2_bytes"] == 267)

    check("VBR is flagged", mp3_duration.describe(_silent_mp3(5) + _silent_mp3(5, 0x90))["vbr"])
    check(
        "a VBR stream sums per-frame durations rather than averaging",
        mp3_duration.duration_ms(_silent_mp3(5) + _silent_mp3(5, 0x90))
        == mp3_duration.duration_ms(_silent_mp3(10)),
    )

    # Failing loudly matters: a 0 would enter the manifest, trip out_of_band, and send a
    # reviewer after a script that is fine.
    for label, payload in (
        ("empty payload", b""),
        ("not MP3 at all", b"RIFFxxxxWAVEfmt "),
        ("truncated to a lone header", bytes([0xFF, 0xFB])),
        ("sync word but reserved version", bytes([0xFF, 0xFD, 0x50, 0x80]) * 40),
    ):
        check(
            "{0} raises instead of returning 0".format(label),
            _raises(mp3_duration.Mp3ParseError, lambda p=payload: mp3_duration.duration_ms(p)),
        )
    check(
        "a trailing partial frame does not corrupt the total",
        mp3_duration.duration_ms(data + bytes([0xFF, 0xFB, 0x50, 0x80]) + b"\x00" * 10)
        == mpeg1_expected,
    )


# --------------------------------------------------------------------------- manifest


def _build_clips(turns, *, overrides=None, durations=None):
    clips = []
    for index, turn in enumerate(turns):
        cfg = manifest_module.render_config_for(turns, index, overrides=overrides)
        clips.append(
            ClipInput(
                turn_index=index,
                ssml=ssml.render_turn(turn["text"], cfg),
                duration_ms=(durations or {}).get(index, 4000),
            )
        )
    return clips


def _build_manifest(material, blueprint, **kwargs):
    turns = turns_of(material)
    params = dict(
        material_id=MATERIAL_ID,
        scenario_key=SCENARIO_KEY,
        voice_map=voice.resolve_voice_map(MATERIAL_ID),
        clips=_build_clips(turns, durations=kwargs.pop("durations", None)),
        synthesized_at="2026-07-28T09:15:03Z",
        blueprint=blueprint,
    )
    params.update(kwargs)
    return manifest_module.build_manifest(material, **params)


def test_manifest_shape() -> None:
    print("manifest structure (R3)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    turns = turns_of(material)
    m = _build_manifest(material, blueprint)

    check("one clip per turn", len(m["clips"]) == len(turns), str(len(m["clips"])))
    check(
        "turn_index covers 0..N-1 with no gap or repeat",
        [c["turn_index"] for c in m["clips"]] == list(range(len(turns))),
    )
    check(
        "clip order is playback order",
        [c["turn_index"] for c in m["clips"]] == sorted(c["turn_index"] for c in m["clips"]),
    )
    check(
        "every key encodes its turn index and round-trips",
        all(
            manifest_module.turn_index_for_key(c["key"]) == c["turn_index"] for c in m["clips"]
        ),
    )
    check(
        "key naming is zero-padded to three digits",
        m["clips"][0]["key"] == "audio/turn_000.mp3" and m["clips"][12]["key"] == "audio/turn_012.mp3",
    )

    vmap = voice.resolve_voice_map(MATERIAL_ID)
    check(
        "each clip carries the voice for its speaker",
        all(c["voice_id"] == vmap[c["speaker"]] for c in m["clips"]),
    )
    check(
        "narrator clips use Brian",
        all(c["voice_id"] == "Brian" for c in m["clips"] if c["speaker"] == "speaker1"),
    )
    check(
        "the three narrator turns are all present (the exam frame is synthesised too, R2)",
        sum(c["speaker"] == "speaker1" for c in m["clips"]) == 3,
    )
    check(
        "role is derived, not hardcoded per clip",
        {c["role"] for c in m["clips"]} == {"narrator", "provider", "enquirer"},
    )
    check("voice_map is recorded as settled fact", m["synthesis"]["voice_map"] == vmap)
    check(
        "voice_swap_applied matches the id's hash",
        m["synthesis"]["voice_swap_applied"] == voice.voice_swap_applied(MATERIAL_ID),
    )
    check("synthesis params are auditable", m["synthesis"]["sample_rate"] == "24000"
          and m["synthesis"]["engine"] == "neural"
          and m["synthesis"]["language_code"] == "en-GB")
    check("manifest_version is 1", m["manifest_version"] == 1)
    # The manifest must mirror the register exactly, whichever state it is in. Asserting the
    # list is non-empty would have to be deleted the moment the last probe lands, and a test you
    # delete to make it pass was never protecting anything.
    check(
        "the manifest mirrors the assumption register rather than assuming anything away",
        m["synthesis"]["unverified_assumptions"] == assumptions.unresolved_ids(),
        "manifest={0} register={1}".format(
            m["synthesis"]["unverified_assumptions"], assumptions.unresolved_ids()
        ),
    )


def test_manifest_turn_index_roundtrip() -> None:
    print("three-way alignment (R3, acceptance: zero misalignment)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    turns = turns_of(material)
    m = _build_manifest(material, blueprint)

    check("alignment passes on the real fixture", m["validation"]["alignment_ok"], json.dumps(m["validation"]))
    check(
        "every clip's text_sha256 matches the turn it claims",
        all(
            c["text_sha256"] == manifest_module.sha256_text(turns[c["turn_index"]]["text"])
            for c in m["clips"]
        ),
    )
    check(
        "every blueprint anchor resolves to a clip",
        all(
            any(c["turn_index"] == item["turn_index"] for c in m["clips"])
            for item in blueprint["items"]
        ),
    )
    check(
        "all 10 anchors were actually checked, not skipped",
        m["validation"]["alignment"]["blueprint_anchors_checked"] == 10,
    )

    # The defence that matters: a misplaced annotation is nearly invisible to a human.
    for label, mutate, needle in (
        (
            "an out-of-range blueprint anchor",
            lambda bp: bp["items"][0].__setitem__("turn_index", 999),
            "has no clip",
        ),
        (
            "an anchor pointing at a narrator turn",
            lambda bp: bp["items"][0].__setitem__("turn_index", 0),
            "narrator",
        ),
    ):
        bad = copy.deepcopy(blueprint)
        mutate(bad)
        result = manifest_module.check_alignment(m, material, bad)
        check(
            "caught: {0}".format(label),
            not result["ok"] and any(needle in e for e in result["errors"]),
            json.dumps(result["errors"]),
        )

    # Script edited after synthesis: the audio no longer says what the text says.
    drifted = copy.deepcopy(material)
    turns_of(drifted)[4]["text"] = "It's Anna Wood."
    result = manifest_module.check_alignment(m, drifted, blueprint)
    check(
        "caught: script text changed after synthesis",
        not result["ok"] and any("diverged" in e for e in result["errors"]),
        json.dumps(result["errors"]),
    )

    for label, mutate, needle in (
        ("a clip removed", lambda mm: mm["clips"].pop(3), "do not cover"),
        (
            "clips reordered",
            lambda mm: mm["clips"].insert(0, mm["clips"].pop()),
            "playback order",
        ),
        (
            "a key that lies about its turn",
            lambda mm: mm["clips"][2].__setitem__("key", "audio/turn_099.mp3"),
            "does not encode its turn",
        ),
        (
            "a duplicated turn_index",
            lambda mm: mm["clips"].__setitem__(3, dict(mm["clips"][2])),
            "repeat",
        ),
    ):
        broken = copy.deepcopy(m)
        mutate(broken)
        result = manifest_module.check_alignment(broken, material, blueprint)
        check(
            "caught: {0}".format(label),
            not result["ok"] and any(needle in e for e in result["errors"]),
            json.dumps(result["errors"]),
        )


def test_manifest_refuses_incomplete() -> None:
    print("manifest as completeness sentinel (R7)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    turns = turns_of(material)
    clips = _build_clips(turns)

    check(
        "a missing clip prevents the manifest being built at all",
        _raises(
            manifest_module.ManifestError,
            lambda: manifest_module.build_manifest(
                material,
                material_id=MATERIAL_ID,
                scenario_key=SCENARIO_KEY,
                voice_map=voice.resolve_voice_map(MATERIAL_ID),
                clips=clips[:-1],
                synthesized_at="2026-07-28T09:15:03Z",
            ),
        ),
    )
    check(
        "a duplicate clip is refused",
        _raises(
            manifest_module.ManifestError,
            lambda: manifest_module.build_manifest(
                material,
                material_id=MATERIAL_ID,
                scenario_key=SCENARIO_KEY,
                voice_map=voice.resolve_voice_map(MATERIAL_ID),
                clips=list(clips) + [clips[0]],
                synthesized_at="2026-07-28T09:15:03Z",
            ),
        ),
    )
    unmeasured = [ClipInput(turn_index=i, ssml="<speak>x</speak>") for i in range(len(turns))]
    check(
        "a clip with no measured duration is refused (no estimating from word count, R5)",
        _raises(
            manifest_module.ManifestError,
            lambda: manifest_module.build_manifest(
                material,
                material_id=MATERIAL_ID,
                scenario_key=SCENARIO_KEY,
                voice_map=voice.resolve_voice_map(MATERIAL_ID),
                clips=unmeasured,
                synthesized_at="2026-07-28T09:15:03Z",
            ),
        ),
    )
    check(
        "a material with a fourth speaker is refused",
        _raises(
            manifest_module.ManifestError,
            lambda: manifest_module.extract_turns(_with_speaker4(material)),
        ),
    )
    check(
        "duration can be measured from bytes instead of supplied",
        ClipInput(turn_index=0, ssml="<speak>x</speak>", audio_bytes=_silent_mp3(40)).resolved_duration()
        == mp3_duration.duration_ms(_silent_mp3(40)),
    )


def _with_speaker4(material):
    bad = copy.deepcopy(material)
    turns_of(bad)[5]["speaker"] = "speaker4"
    return bad


def test_manifest_pauses_and_prep() -> None:
    print("playback rhythm (R6, design.md §7)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    turns = turns_of(material)
    m = _build_manifest(material, blueprint)
    by_index = {c["turn_index"]: c for c in m["clips"]}

    check(
        "after a narrator turn: 800ms",
        by_index[0]["trailing_silence_ms"] == 800 and by_index[21]["trailing_silence_ms"] == 800,
    )
    # Turns 29 and 30 are both speaker2 in the fixture (a short "Great." interjection).
    check(
        "same speaker twice in a row: 300ms",
        by_index[30]["trailing_silence_ms"] == 300,
        str(by_index[30]["trailing_silence_ms"]),
    )
    check("speaker switch: 500ms", by_index[1]["trailing_silence_ms"] == 500)
    check("the last turn gets no tail", by_index[len(turns) - 1]["trailing_silence_ms"] == 0)
    check(
        "the declared silence equals the break actually written into the SSML",
        all(
            (
                'time="{0}ms"'.format(c["trailing_silence_ms"]) in c.get("ssml", "")
                if c["trailing_silence_ms"] and "ssml" in c
                else True
            )
            for c in m["clips"]
        ),
    )
    # Verified directly, because a manifest that promises a pause the audio does not contain
    # would make the frontend's trimming logic wrong.
    for index in (0, 1, 30):
        rendered = ssml.render_turn(
            turns[index]["text"], manifest_module.render_config_for(turns, index)
        )
        check(
            "turn {0}: SSML break matches the declared trailing silence".format(index),
            'time="{0}ms"'.format(by_index[index]["trailing_silence_ms"]) in rendered,
            rendered[-60:],
        )

    prep = [c["turn_index"] for c in m["clips"] if "prep_pause_ms" in c]
    check(
        "reading time is declared on the two question-cue narrator turns only",
        prep == [0, 21],
        json.dumps(prep),
    )
    check("reading time is 30s", by_index[0]["prep_pause_ms"] == 30000)
    check(
        "reading time is declared, never baked (it would waste bytes and skew durations)",
        "30000ms" not in by_index[0].get("ssml", "")
        and by_index[0]["duration_ms"] < 30000,
    )
    check(
        "the closing narrator turn gets no reading time",
        "prep_pause_ms" not in by_index[len(turns) - 1],
    )


def test_duration_is_diagnostic_not_a_gate() -> None:
    print("duration as a diagnostic signal (R5, design.md §6.1)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    turns = turns_of(material)
    dialogue = [i for i, t in enumerate(turns) if t["speaker"] != "speaker1"]

    def with_dialogue_ms(total_ms):
        per = total_ms // len(dialogue)
        durations = {i: per for i in dialogue}
        durations.update({i: 20000 for i, t in enumerate(turns) if t["speaker"] == "speaker1"})
        return _build_manifest(material, blueprint, durations=durations)

    m = with_dialogue_ms(270000)
    check("4.5 minutes of dialogue is in_band", m["validation"]["duration_status"] == "in_band")

    # The case the whole clarification exists for: a 480-word script at 140 WPM runs ~3.4
    # minutes. It is inside the 450-750 hard word range, so it is compliant, and a derived
    # figure must not block it.
    m = with_dialogue_ms(204000)
    check(
        "a compliant short script (3.4 min) is flagged, not blocked",
        m["validation"]["duration_status"] == "out_of_band"
        and m["validation"]["duration_is_diagnostic_only"] is True,
        m["validation"]["duration_status"],
    )
    check(
        "the note explains the derivation so a reviewer is not misled into rejecting it",
        any("diagnostic band" in n and "not a gate" in n for n in m["validation"]["notes"]),
        json.dumps(m["validation"]["notes"]),
    )
    check(
        "out-of-band material still produces a valid, alignment-clean manifest",
        m["validation"]["alignment_ok"] and len(m["clips"]) == len(turns),
    )
    check("3:30-4:00 is near_band", with_dialogue_ms(225000)["validation"]["duration_status"] == "near_band")
    check("5:00-5:30 is near_band", with_dialogue_ms(315000)["validation"]["duration_status"] == "near_band")
    check("over 5:30 is out_of_band", with_dialogue_ms(345000)["validation"]["duration_status"] == "out_of_band")

    m = with_dialogue_ms(270000)
    totals = m["totals"]
    check(
        "dialogue and narrator time are counted separately",
        totals["dialogue_duration_ms"] == 270000
        and totals["narrator_duration_ms"] == 60000
        and totals["total_duration_ms"] == 330000,
        json.dumps(totals),
    )
    check(
        "dialogue words exclude the narrator (WPM must describe the dialogue only)",
        380 <= totals["dialogue_words"] <= 560,
        str(totals["dialogue_words"]),
    )
    expected_wpm = round(totals["dialogue_words"] / 4.5, 1)
    check(
        "measured WPM is derived from measured duration ({0})".format(expected_wpm),
        totals["measured_dialogue_wpm"] == expected_wpm,
        str(totals["measured_dialogue_wpm"]),
    )
    check("clip_count matches the clips array", totals["clip_count"] == len(m["clips"]))


def test_manifest_degraded_marker() -> None:
    print("degraded materials (design.md §14)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    m = _build_manifest(material, blueprint, degraded=True, degraded_reason="time budget")
    check("degraded is marked", m["degraded"] is True and m["degraded_reason"] == "time budget")
    check(
        "a degraded PASS is otherwise a normal manifest",
        m["validation"]["alignment_ok"] and m["totals"]["clip_count"] == len(m["clips"]),
    )
    plain = _build_manifest(material, blueprint)
    check("a normal material carries no degraded key", "degraded" not in plain)


def test_cache_key_semantics() -> None:
    print("idempotency key (R7, design.md §4.3)")
    a = manifest_module.cache_key("Amy", "neural", "24000", "<speak>Hello.</speak>")
    check("stable for identical inputs", a == manifest_module.cache_key("Amy", "neural", "24000", "<speak>Hello.</speak>"))
    check("changes with the voice", a != manifest_module.cache_key("Arthur", "neural", "24000", "<speak>Hello.</speak>"))
    check("changes with the sample rate", a != manifest_module.cache_key("Amy", "neural", "16000", "<speak>Hello.</speak>"))
    # Keyed on SSML, not source text: a rate or ruleset change must invalidate everything,
    # because a material with two different speaking rates across its clips is unusable.
    plain = ssml.render_ssml("Hello.")
    paced = ssml.render_ssml("Hello.", ssml.RenderConfig(rate="92%"))
    check(
        "a rate change invalidates the key (no mixed-rate audio sets)",
        manifest_module.cache_key("Amy", "neural", "24000", plain)
        != manifest_module.cache_key("Amy", "neural", "24000", paced),
    )
    check(
        "a pause change invalidates the key",
        manifest_module.cache_key("Amy", "neural", "24000", plain)
        != manifest_module.cache_key(
            "Amy", "neural", "24000", ssml.render_ssml("Hello.", ssml.RenderConfig(trailing_silence_ms=500))
        ),
    )
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    first = _build_manifest(material, blueprint)
    second = _build_manifest(material, blueprint)
    check(
        "rebuilding an unchanged material reproduces every cache key",
        [c["cache_key"] for c in first["clips"]] == [c["cache_key"] for c in second["clips"]],
    )
    edited = copy.deepcopy(material)
    turns_of(edited)[12]["text"] = "It's 07840051999."
    third = _build_manifest(edited, blueprint)
    changed = [
        i
        for i, (a2, b2) in enumerate(zip(first["clips"], third["clips"]))
        if a2["cache_key"] != b2["cache_key"]
    ]
    check(
        "editing one turn changes exactly that one cache key (single-turn resynthesis)",
        changed == [12],
        json.dumps(changed),
    )


# --------------------------------------------------------------------------- synthesis


class FakePolly:
    """Stands in for Polly's wire protocol, not for its behaviour.

    It returns a distinct MP3 per (ssml, voice) so a swapped clip is detectable, and it counts
    requests. The counter is the point: the idempotency requirement is "a second run makes zero
    billable calls", and only a counter can show that. What Polly actually *sounds* like is
    settled by real audio (assumptions.py), never here.
    """

    def __init__(self, frames_for=None, fail_on=()):
        self.requests = []
        self.fail_on = set(fail_on)
        # Every response is a different length. Real Polly is not byte-deterministic either, and
        # more importantly it makes "which objects did this run overwrite" answerable: with a
        # fixed payload a wrongly rewritten clip is indistinguishable from an untouched one, and
        # the single-turn test would pass while doing the wrong thing.
        self._frames_for = frames_for or (lambda ssml_text, voice_id: 18 + len(self.requests) % 7)

    def describe_voices(self, **kwargs):
        return {"Voices": [{"Id": v} for v in ("Amy", "Arthur", "Brian", "Emma")]}

    def synthesize_speech(self, **kwargs):
        text = kwargs["Text"]
        if any(marker in text for marker in self.fail_on):
            self.requests.append(kwargs)
            error = RuntimeError("rejected")
            error.response = {"Error": {"Code": "InvalidSsmlException"}}
            raise error
        frames = self._frames_for(text, kwargs["VoiceId"])
        self.requests.append(kwargs)
        return {"AudioStream": io.BytesIO(_silent_mp3(frames))}


def _synth_fixture(fail_on=(), frames_for=None):
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    backing = InMemoryObjectStore()
    polly = synthesize.PollyClient(client=FakePolly(fail_on=fail_on, frames_for=frames_for))
    return material, blueprint, backing, polly


def test_synthesis_idempotency() -> None:
    print("synthesis idempotency (R1, acceptance: no second billable call)")
    material, blueprint, backing, polly = _synth_fixture()
    turns = turns_of(material)

    first = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:15:03Z",
    )
    check("a first run synthesises every turn", first.ok and len(first.synthesized) == len(turns),
          json.dumps(first.summary()))
    check("one Polly request per turn, no more", first.polly_calls == len(turns), str(first.polly_calls))
    check("the manifest covers every turn", len(first.manifest["clips"]) == len(turns))

    calls_after_first = polly.calls
    second = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:20:00Z",
    )
    check(
        "a second run makes ZERO Polly requests",
        second.polly_calls == 0 and polly.calls == calls_after_first,
        "run={0} client_total={1}".format(second.polly_calls, polly.calls),
    )
    check("a second run costs nothing", second.cost_usd == 0.0, str(second.cost_usd))
    check("a second run still produces a complete manifest", second.ok
          and len(second.manifest["clips"]) == len(turns))
    check(
        "reuse is reported explicitly rather than looking like fresh work",
        second.reused == list(range(len(turns))) and second.synthesized == [],
    )
    check(
        "the reused manifest is byte-identical apart from its timestamp",
        json.dumps(_without_timestamp(second.manifest)) == json.dumps(_without_timestamp(first.manifest)),
    )

    # Interrupted-run resume: the sentinel is missing and three clips were deleted. Only those
    # three may be re-paid for -- this is design.md §4.3's "resume fills the gaps" case.
    backing.delete([first.prefix + "audio/manifest.json"])
    backing.delete([first.prefix + manifest_module.clip_key(i) for i in (3, 11, 27)])
    third = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:25:00Z",
    )
    check(
        "resuming an interrupted run pays for exactly the missing clips",
        third.polly_calls == 3 and third.synthesized == [3, 11, 27],
        "calls={0} synthesized={1}".format(third.polly_calls, third.synthesized),
    )

    # An edit to one turn changes that turn's cache key and nothing else.
    edited = copy.deepcopy(material)
    turns_of(edited)[12]["text"] = "It's 07840051999."
    fourth = synthesize.synthesize_material(
        edited, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:30:00Z",
    )
    check(
        "editing one turn re-synthesises only that turn",
        fourth.polly_calls == 1 and fourth.synthesized == [12],
        "calls={0} synthesized={1}".format(fourth.polly_calls, fourth.synthesized),
    )

    # A rate change must invalidate everything: a material with two speaking rates in it is
    # worse than the cost of redoing it (design.md §4.3).
    fifth = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, rate="85%", narrator_rate="85%",
        synthesized_at="2026-07-28T09:35:00Z",
    )
    check(
        "changing the rate constant forces a full resynthesis, not a mixed-rate material",
        fifth.polly_calls == len(turns),
        str(fifth.polly_calls),
    )


def _without_timestamp(m):
    trimmed = copy.deepcopy(m)
    trimmed["synthesis"].pop("synthesized_at", None)
    return trimmed


def test_synthesis_cache_key_is_on_the_object() -> None:
    print("synthesis cache key lives on the object, not only in the manifest")
    material, blueprint, backing, polly = _synth_fixture()
    result = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:15:03Z",
    )
    meta = backing.head_metadata(result.prefix + "audio/turn_005.mp3")
    clip = result.manifest["clips"][5]
    check(
        "each MP3 carries its cache key as user metadata",
        meta.get("cache-key") == clip["cache_key"],
        json.dumps(meta),
    )
    check(
        "and its text hash, so a drifted script is detectable from the object alone",
        meta.get("text-sha256") == clip["text_sha256"],
    )
    check("and its turn index, for human triage", meta.get("turn-index") == "5")

    # The manifest is written last, so a crashed run has none. Reading the key off the object is
    # what lets the resumed run skip anything -- a manifest-only check could not.
    backing.delete([result.prefix + "audio/manifest.json"])
    calls_before = polly.calls
    resumed = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:16:00Z",
    )
    check(
        "with no manifest at all, every clip is still recognised and skipped",
        resumed.polly_calls == 0 and polly.calls == calls_before and resumed.ok,
        str(resumed.polly_calls),
    )

    # A clip whose bytes were replaced with something else keeps its metadata, so the check has
    # to be on the key rather than on mere presence -- prove presence alone is not what passes.
    backing.put(result.prefix + "audio/turn_005.mp3", _silent_mp3(9), metadata={"cache-key": "wrong"})
    retried = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:17:00Z",
    )
    check(
        "a clip whose cache key does not match is re-synthesised even though it exists",
        retried.polly_calls == 1 and retried.synthesized == [5],
        "calls={0} synthesized={1}".format(retried.polly_calls, retried.synthesized),
    )


def test_synthesis_failure_writes_no_manifest() -> None:
    print("synthesis failure leaves no sentinel (design.md §4.5)")
    material, blueprint, backing, polly = _synth_fixture(fail_on=("F-O-R-D-Y-C-E",))
    result = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:15:03Z",
    )
    check("a failed turn is reported, not swallowed", not result.ok and result.failed, json.dumps(result.summary()))
    check("no manifest is produced over a gap", result.manifest is None)
    check(
        "the clips that did succeed are kept, so the retry pays only for the rest",
        len(result.synthesized) == len(turns_of(material)) - len(result.failed),
        "{0} of {1}".format(len(result.synthesized), len(turns_of(material))),
    )
    check(
        "a deterministic rejection is not retried three times",
        len(polly._client.requests) == len(turns_of(material)),
        str(len(polly._client.requests)),
    )
    # Retry with the fault cleared: only the failed turns cost anything.
    polly._client.fail_on = set()
    calls_before = polly.calls
    retried = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:16:00Z",
    )
    check(
        "the retry re-synthesises only the previously failed turns",
        retried.polly_calls == len(result.failed) and polly.calls - calls_before == len(result.failed),
        str(retried.polly_calls),
    )
    check("and now completes", retried.ok)


def test_synthesis_uses_measured_constants() -> None:
    print("synthesis uses the measured constants, not the design's guesses")
    check(
        "the dialogue rate is the measured 91%",
        synthesize.DIALOGUE_RATE == ssml.MEASURED_DIALOGUE_RATE == "91%",
    )
    material, blueprint, backing, polly = _synth_fixture()
    result = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:15:03Z",
    )
    sent = polly._client.requests
    check("every request is neural en-GB mp3 at 24kHz",
          all(r["Engine"] == "neural" and r["LanguageCode"] == "en-GB"
              and r["OutputFormat"] == "mp3" and r["SampleRate"] == "24000" for r in sent))
    check("every request is SSML, so the rate and pauses are actually applied",
          all(r["TextType"] == "ssml" for r in sent))
    check('every request carries rate="91%"',
          all('<prosody rate="91%">' in r["Text"] for r in sent), sent[0]["Text"][:80])
    check(
        "no request contains a say-as tag: both rules stay off after the listening tests",
        not any("say-as" in r["Text"] for r in sent),
    )
    check(
        "the phone-number turn is sent as bare digits, as the real scripts write them",
        any("07840051963" in r["Text"] for r in sent),
    )
    check(
        "the spelling turn is sent with its hyphens intact",
        any("F-O-R-D-Y-C-E" in r["Text"] for r in sent),
    )
    # Baked pauses: measured to survive (assumptions trailing-break-survives), so each
    # non-final clip must actually carry the break the manifest advertises.
    breaks = [r["Text"] for r in sent if "<break" in r["Text"]]
    check(
        "all but the last turn carry a baked trailing break",
        len(breaks) == len(turns_of(material)) - 1,
        str(len(breaks)),
    )
    check(
        "the manifest's trailing_silence_ms matches the break that was actually sent",
        all(
            ('<break time="{0}ms"/>'.format(c["trailing_silence_ms"]) in sent_text)
            if c["trailing_silence_ms"]
            else "<break" not in sent_text
            for c, sent_text in zip(
                result.manifest["clips"],
                [_sent_for(sent, c["turn_index"], turns_of(material)) for c in result.manifest["clips"]],
            )
        ),
    )
    check(
        "cost is reported as an upper bound from the characters actually sent",
        result.cost_usd == round(result.billable_chars * 16.0 / 1e6, 6) and result.cost_usd > 0,
        "{0} chars -> ${1}".format(result.billable_chars, result.cost_usd),
    )


def _sent_for(requests, turn_index, turns):
    """Find the request that carried a given turn, by matching its text."""
    needle = turns[turn_index]["text"][:40]
    from xml.sax.saxutils import escape

    escaped = escape(needle)
    for request in requests:
        if escaped in request["Text"]:
            return request["Text"]
    return ""


def test_synthesis_refuses_before_it_pays() -> None:
    print("synthesis validates before it bills")
    material, blueprint, backing, polly = _synth_fixture()
    # An over-long turn must fail during planning, i.e. with zero requests made.
    broken = copy.deepcopy(material)
    turns_of(broken)[5]["text"] = "word " * 700
    raised = _raises(
        ssml.SsmlError,
        lambda: synthesize.synthesize_material(
            broken, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
            polly=polly, blueprint=blueprint,
        ),
    )
    check("an over-limit turn is refused rather than truncated", raised)
    check("and nothing was billed before the refusal", polly.calls == 0, str(polly.calls))

    # A blueprint target damaged by rendering must also stop the run before any spend.
    damaged_bp = copy.deepcopy(blueprint)
    damaged_bp["items"][0]["turn_index"] = 0
    check(
        "an answer word anchored into narration is caught by manifest alignment",
        not manifest_module.check_alignment(
            _build_manifest(material, damaged_bp), material, damaged_bp
        )["ok"],
    )

    missing_voice = synthesize.PollyClient(client=_VoicelessPolly())
    check(
        "a region missing a required voice raises instead of substituting one",
        _raises(
            synthesize.VoicesUnavailable,
            lambda: synthesize.synthesize_material(
                material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY,
                store=InMemoryObjectStore(), polly=missing_voice, blueprint=blueprint,
                verify_voices=True,
            ),
        ),
    )


class _VoicelessPolly(FakePolly):
    def describe_voices(self, **kwargs):
        return {"Voices": [{"Id": "Brian"}, {"Id": "Amy"}]}


def test_single_turn_resynthesis() -> None:
    print("single-turn resynthesis touches exactly one object (R1)")
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    audit = load("audit_valid.json")
    backing = InMemoryObjectStore()
    polly = synthesize.PollyClient(client=FakePolly())
    store_state = StateStore(backing, clock=_fake_clock())

    first = synthesize.synthesize_material(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY, store=backing,
        polly=polly, blueprint=blueprint, synthesized_at="2026-07-28T09:15:03Z",
    )
    store_state.publish_material(
        material, blueprint, audit, scenario_key=SCENARIO_KEY, material_id=MATERIAL_ID,
        manifest=first.manifest,
    )
    before = backing.snapshot()
    backing.clear_calls()
    calls_before = polly.calls

    out = synthesize.resynthesize_turns(store_state, MATERIAL_ID, [12], polly=polly)
    check("exactly one Polly request", out.polly_calls == 1 and polly.calls - calls_before == 1,
          str(out.polly_calls))
    written = [c[1] for c in backing.calls if c[0] == "put"]
    check(
        "exactly one audio object written, plus the manifest",
        written == [first.prefix + "audio/turn_012.mp3", first.prefix + "audio/manifest.json"],
        json.dumps(written),
    )
    unchanged = [
        key
        for key, body in backing.snapshot().items()
        if key.endswith(".mp3") and body != before.get(key)
    ]
    check(
        "no other clip's bytes changed",
        unchanged == [first.prefix + "audio/turn_012.mp3"],
        json.dumps(unchanged),
    )
    check(
        "the voice map is read from the manifest, not recomputed",
        out.manifest["synthesis"]["voice_map"] == first.manifest["synthesis"]["voice_map"],
    )
    check("the manifest records which turns were redone", out.manifest["resynthesized_turns"] == [12])
    check(
        "totals are recomputed from the new clip, not left stale",
        out.manifest["totals"]["clip_count"] == first.manifest["totals"]["clip_count"],
    )
    check("alignment still holds after the partial rewrite", out.manifest["validation"]["alignment_ok"])

    # A partial resynthesis under changed render rules would leave two rates in one material.
    check(
        "a rate change makes a partial resynthesis refuse rather than mix rates",
        _raises(
            synthesize.StaleClips,
            lambda: synthesize.resynthesize_turns(
                store_state, MATERIAL_ID, [12], polly=polly, rate="85%", narrator_rate="85%"
            ),
        ),
    )
    check(
        "an unknown turn index is refused",
        _raises(
            synthesize.SynthesisError,
            lambda: synthesize.resynthesize_turns(store_state, MATERIAL_ID, [999], polly=polly),
        ),
    )


# --------------------------------------------------------------------------- state store


def _publish(store_state, *, verdict="PASS", material_id=MATERIAL_ID, degraded=False):
    """Publish one material. Every verdict takes the same path -- audio, then the sentinel.

    There is no longer a verdict-dependent branch here, which is the point: a FAIL material is
    published exactly like a PASS one and only `audit.json` differs.
    """
    material, blueprint = load("material_valid.json"), load("blueprint_valid.json")
    audit = load("audit_valid.json")
    audit = copy.deepcopy(audit)
    audit["verdict"] = verdict
    if verdict in ("FAIL", "NOT_ASSESSABLE"):
        audit["findings"] = [
            {"severity": "critical", "rule": "answer not recoverable", "turn_index": 7},
            {"severity": "major", "rule": "points too clustered", "turn_index": 12},
        ]
        audit["assessable"] = verdict != "NOT_ASSESSABLE"
    turns = turns_of(material)
    m = manifest_module.build_manifest(
        material,
        material_id=material_id,
        scenario_key=SCENARIO_KEY,
        voice_map=voice.resolve_voice_map(material_id),
        clips=_build_clips(turns),
        synthesized_at="2026-07-28T09:15:03Z",
        blueprint=blueprint,
    )
    audio = {c["key"]: _silent_mp3(20) for c in m["clips"]}
    return store_state.publish_material(
        material,
        blueprint,
        audit,
        scenario_key=SCENARIO_KEY,
        material_id=material_id,
        audio=audio,
        manifest=m,
        degraded=degraded,
    )


def test_verdict_routing() -> None:
    """Every verdict lands in pending, with audio, and is listed.

    This test used to pin the opposite for FAIL / NOT_ASSESSABLE: quarantine, no audio, a
    machine-readable `quarantine_reason.json`, invisible to the pending queue. The product owner's
    rule replaced it -- a user who asks for two materials receives two, and a flawed one is
    returned with its shortcomings stated so the user decides. The assertions are inverted rather
    than dropped, so the same properties are still covered: destination, audio, and visibility.
    """
    print("verdict routing (every verdict -> pending, with audio)")
    for verdict in ("PASS", "PASS_WITH_MINOR_EDITS", "FAIL", "NOT_ASSESSABLE"):
        backing = InMemoryObjectStore()
        store = StateStore(backing)
        ref = _publish(store, verdict=verdict)
        check("{0} routes to pending".format(verdict), ref.state == PENDING, ref.state)

    check("quarantine is no longer a state at all", "quarantine" not in STATES, str(STATES))

    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="FAIL")
    bundle = store.get_material(MATERIAL_ID)
    check(
        "the verdict and its findings travel in audit.json, which is the frontend's source",
        bundle["audit"]["verdict"] == "FAIL" and len(bundle["audit"]["findings"]) == 2,
    )
    check(
        "a FAIL material has audio like any other (the user may choose to listen to it)",
        bundle["manifest"] is not None and len(bundle["manifest"]["clips"]) == len(turns_of(load("material_valid.json"))),
    )
    check(
        "no quarantine sidecar is written",
        not [k for k in backing.list_keys("") if "quarantine" in k],
    )
    check(
        "a FAIL material is listed in pending alongside the rest, not in a separate view",
        [r.material_id for r in store.list_materials(PENDING)["items"]] == [MATERIAL_ID],
    )
    check(
        "verify_material holds a FAIL material to the same completeness standard",
        store.verify_material(MATERIAL_ID)["ok"] is True,
        json.dumps(store.verify_material(MATERIAL_ID), default=str),
    )

    backing = InMemoryObjectStore()
    store = StateStore(backing)
    ref = _publish(store, verdict="NOT_ASSESSABLE")
    check(
        "NOT_ASSESSABLE is recorded, not diverted -- the orchestrator re-runs such a slot upstream",
        ref.state == PENDING and store.get_material(MATERIAL_ID)["audit"]["assessable"] is False,
    )

    backing = InMemoryObjectStore()
    store = StateStore(backing)
    material = load("material_valid.json")
    m = manifest_module.build_manifest(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY,
        voice_map=voice.resolve_voice_map(MATERIAL_ID), clips=_build_clips(turns_of(material)),
        synthesized_at="2026-07-28T09:15:03Z", blueprint=load("blueprint_valid.json"),
    )
    ref = store.publish_material(
        material, load("blueprint_valid.json"), {"verdict": "SOMETHING_NEW"},
        scenario_key=SCENARIO_KEY, material_id=MATERIAL_ID,
        audio={c["key"]: _silent_mp3(20) for c in m["clips"]}, manifest=m,
    )
    check(
        "an unrecognised verdict still publishes to pending, normalised to NOT_ASSESSABLE",
        ref.state == PENDING and state_store_module.verdict_of({"verdict": "SOMETHING_NEW"}) == "NOT_ASSESSABLE",
        ref.state,
    )


def test_legacy_quarantine_prefix_is_inert() -> None:
    """Real buckets still hold a `quarantine/` prefix from before the concept was removed.

    No migration is written for it, so the requirement is only that the code does not crash or
    miscount: `quarantine` is not in STATES, so nothing scans it, and a material sitting there is
    simply not found rather than half-visible.
    """
    print("legacy quarantine/ prefix in a real bucket")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    stale = "quarantine/{0}/20260101-accommodation-rental-deadbeef/".format(SCENARIO_KEY)
    backing.put(stale + "material.json", b"{}")
    backing.put(stale + "audit.json", b'{"verdict": "FAIL"}')
    backing.put(stale + "quarantine_reason.json", b"{}")
    _publish(store, verdict="PASS")

    check(
        "listing pending is unaffected by the stale prefix",
        [r.material_id for r in store.list_materials(PENDING)["items"]] == [MATERIAL_ID],
    )
    check(
        "asking for the removed state is a clear error, not a silent empty page",
        _raises(StateStoreError, lambda: store.list_materials("quarantine")),
    )
    check(
        "reconcile ignores it rather than reporting the whole bucket as broken",
        store.reconcile().ok,
        json.dumps(store.reconcile().actions),
    )
    check(
        "a material left in the stale prefix is simply not found",
        _raises(MaterialNotFound,
                lambda: store.locate("20260101-accommodation-rental-deadbeef")),
    )
    check("the stale objects are untouched (no destructive migration)", backing.head(stale + "material.json"))


def test_degraded_routing() -> None:
    print("degraded but PASS routing (design.md §14)")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    ref = _publish(store, verdict="PASS", degraded=True)
    check("a degraded PASS goes to pending carrying its flag", ref.state == PENDING, ref.state)
    manifest_json = store.get_material(MATERIAL_ID)["manifest"]
    check(
        "the degraded marker travels with it so the frontend can say so",
        manifest_json["degraded"] is True and manifest_json["degraded_reason"],
        json.dumps(manifest_json.get("degraded_reason")),
    )
    check(
        "it is visible in the pending queue like any other material",
        [r.material_id for r in store.list_materials(PENDING)["items"]] == [MATERIAL_ID],
    )


def test_publish_guards() -> None:
    print("publish guards")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    material, blueprint, audit = load("material_valid.json"), load("blueprint_valid.json"), load("audit_valid.json")
    check(
        "a PASS material with no manifest is refused (it would never become visible)",
        _raises(
            StateStoreError,
            lambda: store.publish_material(
                material, blueprint, audit, scenario_key=SCENARIO_KEY, material_id=MATERIAL_ID
            ),
        ),
    )
    # The trap from design.md §8.1: material.json's `scenario` is a whole English sentence.
    check(
        "a scenario_key containing a slash is refused",
        _raises(
            StateStoreError,
            lambda: store.publish_material(
                material, blueprint, audit,
                scenario_key="A woman phones a service/agency.",
                material_id=MATERIAL_ID,
            ),
        ),
    )
    turns = turns_of(material)
    m = manifest_module.build_manifest(
        material, material_id=MATERIAL_ID, scenario_key=SCENARIO_KEY,
        voice_map=voice.resolve_voice_map(MATERIAL_ID), clips=_build_clips(turns),
        synthesized_at="2026-07-28T09:15:03Z", blueprint=blueprint,
    )
    audio = {c["key"]: _silent_mp3(20) for c in m["clips"]}
    audio.pop("audio/turn_005.mp3")
    check(
        "the manifest is withheld when a promised clip is absent",
        _raises(
            StateStoreError,
            lambda: store.publish_material(
                material, blueprint, audit, scenario_key=SCENARIO_KEY,
                material_id=MATERIAL_ID, audio=audio, manifest=m,
            ),
        ),
    )
    check(
        "no manifest object was written by the failed publish",
        not backing.head("pending/{0}/{1}/audio/manifest.json".format(SCENARIO_KEY, MATERIAL_ID)),
    )


def test_incomplete_is_invisible() -> None:
    print("completeness sentinel on the read side (R7, design.md §4.5)")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    prefix = "pending/{0}/{1}/".format(SCENARIO_KEY, MATERIAL_ID)
    check("complete material is listed", len(store.list_materials(PENDING)["items"]) == 1)

    backing.delete([prefix + "audio/manifest.json"])
    check(
        "without a manifest the material disappears from listings",
        store.list_materials(PENDING)["items"] == [],
    )
    check(
        "but its objects are still there for a resumed run to reuse",
        backing.head(prefix + "audio/turn_000.mp3"),
    )
    check(
        "reconcile reports it as incomplete rather than leaving it silently invisible",
        "pending/{0}".format(MATERIAL_ID) in store.reconcile().incomplete,
    )


def test_transition_whitelist() -> None:
    print("transition whitelist (design.md §9.1)")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    check(
        "pending -> production is refused (review cannot be skipped)",
        _raises(
            IllegalTransition,
            lambda: store.transition(MATERIAL_ID, PRODUCTION, actor="t", reason="x"),
        ),
    )
    record = store.transition(MATERIAL_ID, APPROVED, actor="reviewer", reason="ok")
    check("pending -> approved is allowed", record.to_state == APPROVED)
    check("approved -> production is allowed", store.transition(MATERIAL_ID, PRODUCTION, actor="r", reason="ship").to_state == PRODUCTION)
    check(
        "production -> approved is refused",
        _raises(IllegalTransition, lambda: store.transition(MATERIAL_ID, APPROVED, actor="t", reason="x")),
    )
    check("production -> rejected is allowed (recall)", store.transition(MATERIAL_ID, REJECTED, actor="r", reason="recall").to_state == REJECTED)
    check("rejected -> pending is allowed (resubmit)", store.transition(MATERIAL_ID, PENDING, actor="r", reason="fixed").to_state == PENDING)
    check(
        "a transition to the current state is refused",
        _raises(IllegalTransition, lambda: store.transition(MATERIAL_ID, PENDING, actor="t", reason="x")),
    )
    check(
        "an unknown material is reported, not silently created",
        _raises(MaterialNotFound, lambda: store.transition("nope", APPROVED, actor="t", reason="x")),
    )


def test_transition_moves_everything() -> None:
    print("transition completeness (acceptance: source empty, destination whole)")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    src = "pending/{0}/{1}/".format(SCENARIO_KEY, MATERIAL_ID)
    dst = "approved/{0}/{1}/".format(SCENARIO_KEY, MATERIAL_ID)
    before = set(k[len(src):] for k in backing.list_keys(src))

    backing.clear_calls()
    store.transition(MATERIAL_ID, APPROVED, actor="reviewer", reason="ok")

    check("source directory is empty", backing.list_keys(src) == [])
    check(
        "destination holds every object the source had",
        set(k[len(dst):] for k in backing.list_keys(dst)) == before,
        str(before ^ set(k[len(dst):] for k in backing.list_keys(dst))),
    )
    check(
        "all three JSON files plus the audio moved",
        {"material.json", "blueprint.json", "audit.json", "audio/manifest.json"} <= before,
    )
    check(
        "the transition marker is cleared",
        not backing.head(dst + "_transition.json"),
    )
    check(
        "material_id is unchanged across the move (it is the lifetime identifier)",
        store.locate(MATERIAL_ID).material_id == MATERIAL_ID,
    )
    check(
        "the material is in exactly one state directory",
        len([r for r in store._all_refs() if r.material_id == MATERIAL_ID]) == 1,
    )
    # Ordering property the sentinel depends on: the manifest must be the last copy, or the
    # destination becomes readable while still incomplete.
    copies = [c for c in backing.calls if c[0] == "copy"]
    manifest_position = [i for i, c in enumerate(copies) if c[2].endswith("manifest.json")]
    check(
        "the manifest is copied last of all objects",
        manifest_position == [len(copies) - 1],
        "manifest at {0} of {1} copies".format(manifest_position, len(copies)),
    )
    check(
        "server-side copy is used, never a download-and-reupload",
        all(len(c) == 3 for c in copies) and copies,
    )
    check(
        "presigned URLs are keyed by turn_index, so the frontend sees no keys",
        set(store.presign_audio(MATERIAL_ID)) == set(range(43)),
    )


def test_crash_between_copy_and_delete() -> None:
    """The failure mode that silently loses or duplicates a material (design.md §9.2)."""
    print("crash injection: copy+delete is not atomic")
    src_t = "pending/{0}/{1}/".format(SCENARIO_KEY, MATERIAL_ID)
    dst_t = "approved/{0}/{1}/".format(SCENARIO_KEY, MATERIAL_ID)

    # --- crash after step 1: marker written, nothing copied ---
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    check(
        "step 1 crash raises",
        _raises(InjectedCrash, lambda: store.transition(MATERIAL_ID, APPROVED, actor="t", reason="x", crash_after=1)),
    )
    check("step 1: marker exists at the destination", backing.head(dst_t + "_transition.json"))
    check("step 1: source is still complete and visible", len(store.list_materials(PENDING)["items"]) == 1)
    check("step 1: destination is not visible", store.list_materials(APPROVED)["items"] == [])
    report = store.reconcile(dry_run=True)
    check("step 1: dry-run reports the roll-forward without acting", MATERIAL_ID in report.forward_rolled and backing.head(src_t + "material.json"))
    store.reconcile(dry_run=False)
    check("step 1: reconcile completes the move", backing.list_keys(src_t) == [] and backing.head(dst_t + "material.json"))
    check("step 1: material visible in exactly one state", len(store.list_materials(APPROVED)["items"]) == 1 and store.list_materials(PENDING)["items"] == [])
    check("step 1: marker cleared", not backing.head(dst_t + "_transition.json"))
    check("step 1: history records the recovered move", any(h.recovered and h.to_state == APPROVED for h in store.history(MATERIAL_ID)))

    # --- crash mid-copy: destination has some objects but no manifest ---
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    check(
        "step 2 crash raises",
        _raises(InjectedCrash, lambda: store.transition(MATERIAL_ID, APPROVED, actor="t", reason="x", crash_after=2)),
    )
    check("step 2: destination has objects", len(backing.list_keys(dst_t)) > 1)
    check("step 2: destination has NO manifest", not backing.head(dst_t + "audio/manifest.json"))
    check("step 2: destination is invisible (incomplete)", store.list_materials(APPROVED)["items"] == [])
    check("step 2: source is still visible", len(store.list_materials(PENDING)["items"]) == 1)
    store.reconcile(dry_run=False)
    check("step 2: reconcile converges", backing.list_keys(src_t) == [] and len(store.list_materials(APPROVED)["items"]) == 1)

    # --- crash after step 3: BOTH copies complete. The ghost window. ---
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    check(
        "step 3 crash raises",
        _raises(InjectedCrash, lambda: store.transition(MATERIAL_ID, APPROVED, actor="t", reason="x", crash_after=3)),
    )
    check("step 3: both directories physically hold a complete copy", backing.head(src_t + "audio/manifest.json") and backing.head(dst_t + "audio/manifest.json"))
    pending_ids = [r.material_id for r in store.list_materials(PENDING)["items"]]
    approved_ids = [r.material_id for r in store.list_materials(APPROVED)["items"]]
    # This is the read rule that makes the window invisible without a distributed
    # transaction: the side holding the marker is authoritative, the other is residue.
    check(
        "step 3: the material appears in exactly one listing, not both",
        (pending_ids, approved_ids) == ([], [MATERIAL_ID]),
        "pending={0} approved={1}".format(pending_ids, approved_ids),
    )
    check("step 3: the destination is the authoritative one", store.locate(MATERIAL_ID).state == APPROVED)
    check("step 3: get_material resolves without ambiguity", store.get_material(MATERIAL_ID)["state"] == APPROVED)
    check("step 3: the residue is reported as a shadow", "pending/{0}".format(MATERIAL_ID) in store.reconcile().shadows)
    store.reconcile(dry_run=False)
    check("step 3: reconcile removes the residue", backing.list_keys(src_t) == [])
    check("step 3: no data lost", backing.head(dst_t + "audio/manifest.json") and backing.head(dst_t + "material.json"))
    check("step 3: still exactly one live copy", len([r for r in store._all_refs() if r.material_id == MATERIAL_ID]) == 1)

    # --- crash after step 4: source deleted, history not yet written ---
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    check(
        "step 4 crash raises",
        _raises(InjectedCrash, lambda: store.transition(MATERIAL_ID, APPROVED, actor="t", reason="x", crash_after=4)),
    )
    check("step 4: source is gone, destination complete", backing.list_keys(src_t) == [] and backing.head(dst_t + "audio/manifest.json"))
    check("step 4: material is visible in the destination", len(store.list_materials(APPROVED)["items"]) == 1)
    check("step 4: history has no record of the move yet", not any(h.to_state == APPROVED for h in store.history(MATERIAL_ID)))
    store.reconcile(dry_run=False)
    check("step 4: reconcile writes the missing history entry", any(h.to_state == APPROVED for h in store.history(MATERIAL_ID)))
    check("step 4: reconcile does not re-copy from the now-empty source", backing.head(dst_t + "material.json"))
    check("step 4: marker cleared", not backing.head(dst_t + "_transition.json"))

    # --- crash after step 5: history written, marker not cleared ---
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    check(
        "step 5 crash raises",
        _raises(InjectedCrash, lambda: store.transition(MATERIAL_ID, APPROVED, actor="t", reason="x", crash_after=5)),
    )
    check("step 5: history is already correct", any(h.to_state == APPROVED for h in store.history(MATERIAL_ID)))
    store.reconcile(dry_run=False)
    check("step 5: reconcile clears the stale marker", not backing.head(dst_t + "_transition.json"))
    check(
        "step 5: no duplicate history entry was written",
        len([h for h in store.history(MATERIAL_ID) if h.to_state == APPROVED]) == 1,
        json.dumps([h.to_state for h in store.history(MATERIAL_ID)]),
    )

    # Recovery is idempotent, so it is safe to run repeatedly on a schedule.
    store.reconcile(dry_run=False)
    store.reconcile(dry_run=False)
    check("reconcile is idempotent when everything is already consistent", store.reconcile().ok)
    check(
        "no material is ever left in two live states after recovery",
        len([r for r in store._all_refs() if r.material_id == MATERIAL_ID and not r.shadow]) == 1,
    )


def test_transition_mutex() -> None:
    print("transition mutex (design.md §9.2 step 1)")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    _publish(store, verdict="PASS")
    try:
        store.transition(MATERIAL_ID, APPROVED, actor="a", reason="x", crash_after=1)
    except InjectedCrash:
        pass
    check(
        "a second transition into the same destination is refused while one is in flight",
        _raises(
            TransitionInFlight,
            lambda: store.transition(MATERIAL_ID, APPROVED, actor="b", reason="y"),
        ),
    )
    check(
        "the in-memory store enforces create-if-absent like S3's 412",
        _raises(PreconditionFailed, lambda: backing.put("k", b"1", if_none_match=True) or backing.put("k", b"2", if_none_match=True)),
    )
    # 待实测 (assumptions.s3-conditional-put): if the region rejects IfNoneMatch, the mutex is
    # unavailable and the fallback in design.md §9.2 applies. The failure must be explicit.
    unsupported = InMemoryObjectStore(conditional_put_supported=False)
    check(
        "a backend without conditional writes fails loudly rather than losing the mutex",
        _raises(ConditionalWriteUnsupported, lambda: unsupported.put("k", b"1", if_none_match=True)),
    )


def test_history_outlives_the_material() -> None:
    print("history (R11)")
    backing = InMemoryObjectStore()
    store = StateStore(backing, clock=_fake_clock())
    _publish(store, verdict="PASS")
    store.transition(MATERIAL_ID, APPROVED, actor="alice", reason="looks good")
    store.transition(MATERIAL_ID, PRODUCTION, actor="bob", reason="ship it")
    store.transition(MATERIAL_ID, REJECTED, actor="carol", reason="recall")

    path = [(h.from_state, h.to_state) for h in store.history(MATERIAL_ID)]
    check(
        "the full path is reconstructible",
        path == [(None, PENDING), (PENDING, APPROVED), (APPROVED, PRODUCTION), (PRODUCTION, REJECTED)],
        json.dumps(path),
    )
    check("actor and reason are recorded", [h.actor for h in store.history(MATERIAL_ID)][1:] == ["alice", "bob", "carol"])
    check("records are time-ordered", [h.at for h in store.history(MATERIAL_ID)] == sorted(h.at for h in store.history(MATERIAL_ID)))
    check("object counts are recorded", all(h.object_count > 0 for h in store.history(MATERIAL_ID)[1:]))
    check(
        "history lives outside the state directories",
        all(k.startswith("_history/") for k in backing.list_keys("_history/")) and backing.list_keys("_history/"),
    )
    # The substance of "keeps an audit trail": the record survives the material itself.
    ref = store.locate(MATERIAL_ID)
    backing.delete(backing.list_keys(ref.prefix))
    check(
        "history survives the material being deleted entirely",
        len(store.history(MATERIAL_ID)) == 4,
        str(len(store.history(MATERIAL_ID))),
    )
    check("the material itself is now gone", _raises(MaterialNotFound, lambda: store.locate(MATERIAL_ID)))
    # Undoing a bad transition must be another transition, not an S3 edit, or history and
    # reality drift apart (design.md §13).
    backing2 = InMemoryObjectStore()
    store2 = StateStore(backing2, clock=_fake_clock())
    _publish(store2, verdict="PASS")
    store2.transition(MATERIAL_ID, REJECTED, actor="d", reason="mistake")
    store2.transition(MATERIAL_ID, PENDING, actor="d", reason="undo the mistake")
    check(
        "an undo leaves two records rather than erasing one",
        len(store2.history(MATERIAL_ID)) == 3 and store2.locate(MATERIAL_ID).state == PENDING,
    )


def test_listing_and_lookup() -> None:
    print("listing, pagination, integrity")
    backing = InMemoryObjectStore()
    store = StateStore(backing)
    ids = ["20260728-accommodation-rental-{0:08x}".format(n) for n in range(5)]
    for material_id in ids:
        _publish(store, verdict="PASS", material_id=material_id)
    page = store.list_materials(PENDING, limit=2)
    check("limit is honoured", len(page["items"]) == 2)
    check("a cursor is returned when more remain", page["next_cursor"] == sorted(ids)[1])
    page2 = store.list_materials(PENDING, limit=2, cursor=page["next_cursor"])
    check("the cursor advances without repeating", [r.material_id for r in page2["items"]] == sorted(ids)[2:4])
    check("scenario filter works", len(store.list_materials(PENDING, scenario_key=SCENARIO_KEY)["items"]) == 5)
    check("a wrong scenario filter returns nothing", store.list_materials(PENDING, scenario_key="other")["items"] == [])
    check("an unknown state is refused", _raises(StateStoreError, lambda: store.list_materials("nowhere")))

    verified = store.verify_material(ids[0])
    check("verify_material passes on a whole material", verified["ok"], json.dumps(verified))
    ref = store.locate(ids[0])
    backing.delete([ref.prefix + "audio/turn_003.mp3"])
    verified = store.verify_material(ids[0])
    check(
        "verify_material catches a clip deleted underneath the manifest",
        not verified["ok"] and verified["missing_audio"] == ["audio/turn_003.mp3"],
        json.dumps(verified),
    )
    check(
        "get_material returns all three JSON files plus the manifest",
        all(store.get_material(ids[1])[k] is not None for k in ("material", "blueprint", "audit", "manifest")),
    )
    check("a missing material raises", _raises(MaterialNotFound, lambda: store.get_material("absent")))


def test_no_aws_at_import() -> None:
    print("import safety")
    import subprocess

    probe = (
        "import sys; sys.path.insert(0, {0!r});\n"
        "import builtins\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in ('boto3', 'botocore'):\n"
        "        raise AssertionError('AWS SDK imported: ' + name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "from audio_storage import voice, ssml, manifest, mp3_duration, state_store, object_store, assumptions, synthesize\n"
        "print('clean')\n"
    ).format(str(ROOT))
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    check(
        "no module imports boto3 at import time",
        result.stdout.strip() == "clean",
        (result.stdout + result.stderr)[-300:],
    )


def test_phase0_is_still_a_gate() -> None:
    print("phase 0 gate (implement.md)")
    # The gate stays shut while ANY assumption is unmeasured, not until all seven are. Four were
    # measured on 2026-07-28; the remaining three need a human to listen to audio, which no
    # amount of code can substitute for.
    check("the register still holds all seven assumptions", len(assumptions.ASSUMPTIONS) == 7)
    check(
        "phase 0 stays gated while anything is unmeasured",
        assumptions.PHASE0_BLOCKED == bool(assumptions.unresolved()),
        "unresolved={0} blocked={1}".format(len(assumptions.unresolved()), assumptions.PHASE0_BLOCKED),
    )
    for required in (
        "arthur-available",
        "spelling-say-as",
        "digits-zero",
        "trailing-break-survives",
        "default-wpm",
        "s3-conditional-put",
        "ssml-tags-not-billed",
    ):
        entry = assumptions.BY_ID[required]
        # Every entry keeps its probe instructions so a measurement can be reproduced or
        # re-run. What differs is whether a human has recorded what they observed: unresolved
        # entries must still say what to look for; resolved ones must carry the finding, not a
        # bare "done", so a later reader can tell measurement from assertion.
        check(
            "{0}: keeps its probe command and expected observation".format(required),
            bool(entry.probe_command) and bool(entry.listen_for),
        )
        if entry.resolution is None:
            check("{0}: still flagged 待实测".format(required), required in assumptions.unresolved_ids())
        else:
            check(
                "{0}: resolution records what was measured".format(required),
                len(entry.resolution) > 40 and "Measured" in entry.resolution,
            )
    # require_phase0 must gate exactly on the register: refuse while anything is unmeasured,
    # allow once everything is. Both directions matter -- a gate that never opens gets bypassed.
    if assumptions.unresolved():
        check(
            "code depending on unmeasured Polly behaviour refuses to run",
            _raises(assumptions.Phase0NotRun, lambda: assumptions.require_phase0("synthesis")),
        )
    else:
        check(
            "with every probe measured the gate opens",
            assumptions.require_phase0("synthesis") is None
            or assumptions.require_phase0("synthesis") is not False,
        )
    # The gate no longer means "synthesize.py must not exist" -- it exists now that all seven
    # probes are measured. What must remain true is that it cannot run behind the gate's back:
    # both entry points call require_phase0, so re-opening any assumption re-closes the path.
    synth_source = (ROOT / "audio_storage" / "synthesize.py").read_text(encoding="utf-8")
    check(
        "both synthesis entry points still ask the phase 0 gate first",
        synth_source.count("assumptions.require_phase0") == 2,
        str(synth_source.count("assumptions.require_phase0")),
    )
    check(
        "the exception table is empty rather than pre-filled with guesses",
        "overrides: {}" in (ROOT / "audio_storage" / "config" / "pronunciation.yaml").read_text(encoding="utf-8"),
    )


def test_python39_compatible() -> None:
    print("Python 3.9 compatibility")
    import ast

    for path in sorted((ROOT / "audio_storage").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            check("{0} parses".format(path.name), False, str(exc))
            continue
        check("{0} parses".format(path.name), True)
        has_match = any(m.__class__.__name__ == "Match" for m in ast.walk(tree))
        check("{0} uses no match statement (3.10+)".format(path.name), not has_match)


def _fake_clock():
    counter = {"n": 0}

    def clock():
        counter["n"] += 1
        return "2026-07-28T09:{0:02d}:00Z".format(counter["n"])

    return clock


def _raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 - a different exception is still a failure to report
        return False
    return False


def main() -> int:
    for suite in (
        test_voice_map_is_deterministic,
        test_voice_swap_distribution,
        test_voice_override_and_guards,
        test_gender_marker_scan,
        test_render_operation_order,
        test_spelling_rule_boundaries,
        test_digits_rule_boundaries,
        test_untouched_patterns,
        test_invariant_and_strip_tags,
        test_render_guards,
        test_targets_intact,
        test_mp3_duration,
        test_manifest_shape,
        test_manifest_turn_index_roundtrip,
        test_manifest_refuses_incomplete,
        test_manifest_pauses_and_prep,
        test_duration_is_diagnostic_not_a_gate,
        test_manifest_degraded_marker,
        test_cache_key_semantics,
        test_synthesis_idempotency,
        test_synthesis_cache_key_is_on_the_object,
        test_synthesis_failure_writes_no_manifest,
        test_synthesis_uses_measured_constants,
        test_synthesis_refuses_before_it_pays,
        test_single_turn_resynthesis,
        test_verdict_routing,
        test_legacy_quarantine_prefix_is_inert,
        test_degraded_routing,
        test_publish_guards,
        test_incomplete_is_invisible,
        test_transition_whitelist,
        test_transition_moves_everything,
        test_crash_between_copy_and_delete,
        test_transition_mutex,
        test_history_outlives_the_material,
        test_listing_and_lookup,
        test_no_aws_at_import,
        test_phase0_is_still_a_gate,
        test_python39_compatible,
    ):
        # A suite that raises must be reported, not allowed to abort the run: otherwise one
        # broken invariant hides every check that would have run after it, and the output
        # says less the more badly the code is broken.
        try:
            suite()
        except Exception as exc:  # noqa: BLE001 - reporting any failure is the point
            import traceback

            print("  FAIL  {0} raised {1}".format(suite.__name__, type(exc).__name__))
            failures.append(
                "{0} raised {1}: {2}".format(suite.__name__, type(exc).__name__, exc)
            )
            traceback.print_exc(limit=3)
    print()
    if failures:
        print("{0} failure(s):".format(len(failures)))
        for item in failures:
            print("  - {0}".format(item))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
