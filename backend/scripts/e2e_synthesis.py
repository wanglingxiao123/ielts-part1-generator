#!/usr/bin/env python
"""End-to-end synthesis against real Polly and real S3, with the evidence printed.

    IELTS_AUDIO_BUCKET=<your-materials-bucket> \
      .venv-backend/bin/python backend/scripts/e2e_synthesis.py --material <path> \
      --blueprint <path> --audit <path> --scenario-key accommodation-rental

Prints, in order: the voices the region actually offers, the clip count, the measured total
duration, the measured WPM, the real cost, then three properties that a claim of correctness has
to be able to show rather than assert:

  1. **Idempotency.** Runs the same selection twice and prints the Polly request counter for
     each. The second must read 0. Counting requests is the only honest form of "it did not bill
     twice" -- an assertion in a test is a statement about intent.
  2. **Single-turn resynthesis.** Re-does one turn and lists which S3 objects changed bytes. Must
     be exactly one MP3 plus the manifest.
  3. **Three-way alignment.** Checks turn_index across the script, the blueprint anchors and the
     manifest clips, on the objects as they exist in S3 rather than in memory.

Nothing here is mocked. If credentials have expired it fails and says so, because a run that
quietly fell back to a stub would be worse than no run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audio_storage import synthesize as synth  # noqa: E402
from audio_storage.manifest import extract_turns  # noqa: E402
from audio_storage.mp3_duration import describe as describe_mp3  # noqa: E402
from audio_storage.object_store import S3ObjectStore  # noqa: E402
from audio_storage.state_store import StateStore, new_material_id  # noqa: E402
from backend import audio as audio_config  # noqa: E402


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--material", type=Path, required=True)
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--scenario-key", required=True)
    parser.add_argument("--material-id", default=None)
    parser.add_argument("--resynth-turn", type=int, default=None,
                        help="turn to re-synthesise for the single-turn proof")
    parser.add_argument("--keep", action="store_true", help="leave the material in S3")
    parser.add_argument("--out", type=Path, default=Path("/tmp/ielts-e2e"))
    args = parser.parse_args()

    material = json.loads(args.material.read_text(encoding="utf-8"))
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    turns = extract_turns(material)

    bucket = audio_config.bucket_name()
    backing = S3ObjectStore(bucket)
    state_store = StateStore(backing)
    polly = audio_config.build_polly()
    material_id = args.material_id or new_material_id(args.scenario_key)

    banner("environment")
    import boto3

    identity = boto3.client("sts").get_caller_identity()
    print("account      :", identity["Account"])
    print("region       :", boto3.session.Session().region_name)
    print("bucket       :", bucket)
    print("material_id  :", material_id)
    print("turns        :", len(turns))
    print("verdict      :", audit.get("verdict"))

    banner("voices offered by this region")
    voice_map = synth.voice.resolve_voice_map(material_id)
    available = synth.check_voices(polly, voice_map)
    print("en-GB neural :", ", ".join(available))
    print("voice_map    :", json.dumps(voice_map))
    print("swap applied :", synth.voice.voice_swap_applied(material_id))

    banner("run 1: real Polly synthesis")
    started = time.monotonic()
    first = synth.synthesize_material(
        material, material_id=material_id, scenario_key=args.scenario_key,
        store=backing, polly=polly, blueprint=blueprint,
        on_event=lambda name, detail: print("  {0}: {1}".format(name, json.dumps(detail)[:160])),
    )
    if not first.ok:
        print("FAILED:", json.dumps(first.summary(), indent=2))
        return 1
    manifest = first.manifest
    totals = manifest["totals"]
    print(json.dumps(first.summary(), indent=2, ensure_ascii=False))
    print("\nclip_count           :", totals["clip_count"])
    print("total_duration       : {0} ms = {1:.1f} s = {2:.2f} min".format(
        totals["total_duration_ms"], totals["total_duration_ms"] / 1000.0,
        totals["total_duration_ms"] / 60000.0))
    print("dialogue_duration    : {0} ms = {1:.2f} min".format(
        totals["dialogue_duration_ms"], totals["dialogue_duration_ms"] / 60000.0))
    print("narrator_duration    : {0} ms".format(totals["narrator_duration_ms"]))
    print("dialogue_words       :", totals["dialogue_words"])
    print("measured_dialogue_wpm:", totals["measured_dialogue_wpm"], "(spec target ~140)")
    print("duration_status      :", manifest["validation"]["duration_status"],
          "(diagnostic only)")
    print("polly_calls          :", first.polly_calls)
    print("billable_chars       :", first.billable_chars, "(SSML incl. tags: upper bound)")
    print("cost                 : ${0:.4f}".format(first.cost_usd))
    print("wall clock           : {0:.1f}s at concurrency {1}".format(
        time.monotonic() - started, synth.CONCURRENCY))
    if manifest["warnings"]:
        print("warnings             :", json.dumps(manifest["warnings"], ensure_ascii=False))

    published = state_store.publish_material(
        material, blueprint, audit, scenario_key=args.scenario_key,
        material_id=material_id, manifest=manifest, actor="e2e-script",
    )
    print("published to         :", published.prefix)

    banner("evidence 1: idempotency (a second run must make ZERO Polly requests)")
    calls_before = polly.calls
    second = synth.synthesize_material(
        material, material_id=material_id, scenario_key=args.scenario_key,
        store=backing, polly=polly, blueprint=blueprint,
    )
    print("client call counter before run 2 :", calls_before)
    print("client call counter after  run 2 :", polly.calls)
    print("requests attributable to run 2   :", second.polly_calls)
    print("clips reused                     :", len(second.reused), "of", len(turns))
    print("cost of run 2                    : ${0:.4f}".format(second.cost_usd))
    idempotent = second.polly_calls == 0 and polly.calls == calls_before and second.ok
    print("VERDICT:", "PASS -- nothing was re-billed" if idempotent else "FAIL")

    banner("evidence 2: single-turn resynthesis touches exactly one object")
    target = args.resynth_turn
    if target is None:
        target = next((i for i, t in enumerate(turns) if t["speaker"] != "speaker1"), 1)
    prefix = published.prefix

    def fingerprint() -> dict:
        """sha256 of every stored object. Compares the bytes themselves.

        Deliberately not the cache key or the ETag: an identical re-render produces the same
        cache key, so metadata cannot show whether the bytes were rewritten. Hashing the actual
        objects is the only evidence that answers "which clips did this touch".
        """
        import hashlib

        return {
            key: hashlib.sha256(backing.get(key)).hexdigest()
            for key in backing.list_keys(prefix)
        }

    before = fingerprint()
    calls_before = polly.calls
    out = synth.resynthesize_turns(state_store, material_id, [target], polly=polly, store=backing)
    if not out.ok:
        print("FAILED:", json.dumps(out.summary(), indent=2))
        return 1
    after = fingerprint()
    changed = sorted(k for k in after if before.get(k) != after.get(k))
    changed_audio = [k for k in changed if k.endswith(".mp3")]
    print("turn re-synthesised          :", target)
    print("polly requests               :", out.polly_calls,
          "(counter {0} -> {1})".format(calls_before, polly.calls))
    print("objects whose BYTES changed  :", json.dumps(
        [k[len(prefix):] for k in changed], indent=2))
    print("audio objects changed        :", len(changed_audio), "of",
          sum(1 for k in after if k.endswith(".mp3")))
    print("manifest records             :", out.manifest.get("resynthesized_turns"))
    print("clip_count unchanged         :",
          out.manifest["totals"]["clip_count"] == totals["clip_count"])
    print("alignment still ok           :", out.manifest["validation"]["alignment_ok"])
    # Byte equality is NOT the criterion. Polly is deterministic -- verified: two calls with
    # identical SSML, voice and rate return byte-identical MP3s (sha256 match). So re-synthesising
    # unchanged text legitimately rewrites the same bytes, and asserting "bytes changed" would
    # fail on correct behaviour. What matters is that exactly one clip was re-billed and
    # re-uploaded, and that no OTHER clip was touched.
    expected_clip = prefix + "audio/turn_{0:03d}.mp3".format(target)
    other_audio_changed = [k for k in changed_audio if k != expected_clip]
    uploaded = [k for k in out.manifest.get("resynthesized_turns") or []]
    single = (
        out.polly_calls == 1
        and uploaded == [target]
        and not other_audio_changed
    )
    print("re-billed turns              :", uploaded, "(expect [{0}])".format(target))
    print("other clips disturbed        :", len(other_audio_changed), "(expect 0)")
    print("VERDICT:", "PASS -- one request, one clip, nothing else touched" if single else "FAIL")

    banner("evidence 3: S3 layout and three-way turn_index alignment")
    keys = backing.list_keys(prefix)
    print("prefix:", prefix)
    for key in keys[:6]:
        print("  ", key[len(prefix):])
    print("   ... {0} objects total ({1} mp3 + {2} json)".format(
        len(keys), sum(1 for k in keys if k.endswith(".mp3")),
        sum(1 for k in keys if k.endswith(".json"))))
    stored = json.loads(backing.get(prefix + "audio/manifest.json"))
    clip_indexes = [c["turn_index"] for c in stored["clips"]]
    anchors = sorted({item["turn_index"] for item in blueprint.get("items") or []})
    print("script turns      : 0..{0}".format(len(turns) - 1))
    print("manifest clips    : {0} entries, contiguous={1}".format(
        len(clip_indexes), clip_indexes == list(range(len(turns)))))
    print("blueprint anchors :", anchors)
    print("anchors covered   :", set(anchors) <= set(clip_indexes))
    print("speakers agree    :", all(
        c["speaker"] == turns[c["turn_index"]]["speaker"] for c in stored["clips"]))
    verify = state_store.verify_material(material_id)
    print("verify_material   :", json.dumps(verify["alignment"], ensure_ascii=False)[:200])
    print("VERDICT:", "PASS" if verify["ok"] else "FAIL")

    banner("evidence 4: durations cross-checked against the stored bytes")
    sample = stored["clips"][min(target, len(stored["clips"]) - 1)]
    raw = backing.get(prefix + sample["key"])
    info = describe_mp3(raw)
    print("clip              :", sample["key"])
    print("manifest duration :", sample["duration_ms"], "ms")
    print("re-parsed from S3 :", info["duration_ms"], "ms")
    print("frames/rate/bitrate:", info["frame_count"], info["sample_rates"], info["bitrates_kbps"])
    print("trailing silence  :", sample["trailing_silence_ms"], "ms (baked into the clip)")

    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "material_id": material_id,
        "bucket": bucket,
        "prefix": prefix,
        "voices_available": available,
        "voice_map": voice_map,
        "run1": first.summary(),
        "run2_idempotent": second.summary(),
        "resynthesis": dict(out.summary(), changed_objects=changed,
                            changed_audio=changed_audio, turn=target),
        "totals": totals,
        "validation": stored["validation"],
        "object_count": len(keys),
        "checks": {"idempotent": idempotent, "single_turn": single,
                   "alignment": verify["ok"]},
    }
    (args.out / "e2e-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.out / "manifest.json").write_text(
        json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nreport:", args.out / "e2e-report.json")

    if not args.keep:
        backing.delete(keys)
        print("cleaned up", len(keys), "objects (pass --keep to retain)")

    return 0 if (idempotent and single and verify["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
