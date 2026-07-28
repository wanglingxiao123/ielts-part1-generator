"""Build and validate audio/manifest.json.

The manifest is two things at once:

  * the playback contract -- clip order is full-text playback order, and clips[].turn_index
    is the key the frontend, the blueprint annotations and the script text all index by;
  * the completeness sentinel -- it is written last, in a single PutObject. A material
    directory without a manifest is incomplete by definition, so no lock or extra status
    field is needed to express "half synthesised" (design.md §4.5).

turn_index is stored explicitly rather than implied by array position. A consumer that
filters or re-sorts the array would otherwise silently misalign every annotation, and
misaligned annotations are close to invisible to a human reviewer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import assumptions, ssml as ssml_module, voice

MANIFEST_VERSION = 1

# design.md §7. Baked into the clip via a trailing <break>, so the rhythm travels with the
# audio and does not depend on browser gap timing (R6). 待实测: Polly may trim tail silence,
# in which case these become the values the player must insert instead.
PAUSE_SAME_SPEAKER_MS = 300
PAUSE_SPEAKER_SWITCH_MS = 500
PAUSE_AFTER_NARRATOR_MS = 800
PAUSE_LAST_TURN_MS = 0

# Real exam reading time. Declared only, never baked: 30s of silence would waste bytes and
# corrupt the duration statistics. The player decides whether to honour it.
PREP_PAUSE_MS = 30000
_PREP_CUE_RE = re.compile(
    r"(?:you have|there will be)\s+(?:some\s+)?time to (?:look at|read)", re.IGNORECASE
)

# Diagnostic band for dialogue duration (design.md §6.1). The specification's 4-5 minutes
# describes the *typical* 600-650 word case, not the whole 450-750 hard range: a compliant
# 480-word script runs about 3.4 minutes at 140 WPM. So this band never blocks publication;
# it exists to catch a synthesis fault, e.g. a 620-word script coming out at 6 minutes means
# the rate constant or the pauses are wrong, not that the script is.
BAND_MS = (240000, 300000)
NEAR_BAND_MS = (210000, 330000)

_WORD_RE = re.compile(r"\b[\w'-]+\b")


class ManifestError(ValueError):
    """The manifest cannot be built, or an existing one is inconsistent."""


def count_words(text: str) -> int:
    """Same tokenisation as audit_metrics.words, so WPM is comparable across the system."""
    return len(_WORD_RE.findall(text))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(voice_id: str, engine: str, sample_rate: str, rendered_ssml: str) -> str:
    """Idempotency key (design.md §4.3).

    Keyed on the SSML rather than the source text: if a rule or a rate constant changes,
    every key changes and the whole set is resynthesised. A material that mixed two speaking
    rates across its clips would be worse than the cost of redoing it.
    """
    payload = "{0}|{1}|{2}|{3}".format(voice_id, engine, sample_rate, rendered_ssml)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_turns(material: dict) -> List[dict]:
    """The single part's turns. Raises rather than guessing on a malformed material."""
    if not isinstance(material, dict):
        raise ManifestError("material must be a JSON object")
    parts = material.get("listening_material_parts")
    if not isinstance(parts, list) or len(parts) != 1:
        raise ManifestError("material must contain exactly one listening_material_part")
    script = parts[0].get("script") if isinstance(parts[0], dict) else None
    turns = script.get("turns") if isinstance(script, dict) else None
    if not isinstance(turns, list) or not turns:
        raise ManifestError("material has no script turns")
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict) or not isinstance(turn.get("text"), str):
            raise ManifestError("turn {0} is malformed".format(index))
        if turn.get("speaker") not in voice.SPEAKERS:
            raise ManifestError(
                "turn {0} has speaker {1!r}, outside the frozen three".format(
                    index, turn.get("speaker")
                )
            )
    return turns


def clip_key(turn_index: int) -> str:
    """Relative key. Absolute keys are assembled by state_store so a material can change
    state without the manifest being rewritten (design.md §5)."""
    return "audio/turn_{0:03d}.mp3".format(turn_index)


def trailing_silence_for(turns: Sequence[dict], index: int) -> int:
    """Pause baked after this turn, per design.md §7."""
    if index >= len(turns) - 1:
        return PAUSE_LAST_TURN_MS
    if turns[index].get("speaker") == voice.NARRATOR:
        return PAUSE_AFTER_NARRATOR_MS
    if turns[index].get("speaker") == turns[index + 1].get("speaker"):
        return PAUSE_SAME_SPEAKER_MS
    return PAUSE_SPEAKER_SWITCH_MS


def prep_pause_for(turns: Sequence[dict], index: int) -> Optional[int]:
    """30s declared after a narrator turn that tells candidates to read the questions."""
    turn = turns[index]
    if turn.get("speaker") != voice.NARRATOR:
        return None
    return PREP_PAUSE_MS if _PREP_CUE_RE.search(turn.get("text", "")) else None


def render_config_for(
    turns: Sequence[dict],
    index: int,
    *,
    rate: Optional[str] = None,
    narrator_rate: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
    bake_pauses: bool = True,
) -> ssml_module.RenderConfig:
    """Per-turn render config: speaker-dependent rate plus the position-dependent pause."""
    is_narrator = turns[index].get("speaker") == voice.NARRATOR
    return ssml_module.RenderConfig(
        rate=(narrator_rate if is_narrator else rate),
        trailing_silence_ms=trailing_silence_for(turns, index) if bake_pauses else 0,
        pronunciation_overrides=dict(overrides or {}),
    )


def duration_status(dialogue_ms: int) -> str:
    """in_band / near_band / out_of_band. Never a gate -- see BAND_MS."""
    if BAND_MS[0] <= dialogue_ms <= BAND_MS[1]:
        return "in_band"
    if NEAR_BAND_MS[0] <= dialogue_ms <= NEAR_BAND_MS[1]:
        return "near_band"
    return "out_of_band"


@dataclass
class ClipInput:
    """What synthesis produced for one turn. Duration is measured, never estimated."""

    turn_index: int
    ssml: str
    duration_ms: Optional[int] = None
    audio_bytes: Optional[bytes] = None

    def resolved_duration(self) -> Optional[int]:
        if self.duration_ms is not None:
            return self.duration_ms
        if self.audio_bytes is None:
            return None
        from .mp3_duration import duration_ms as parse

        return parse(self.audio_bytes)


def build_manifest(
    material: dict,
    *,
    material_id: str,
    scenario_key: str,
    voice_map: Dict[str, str],
    clips: Sequence[ClipInput],
    sample_rate: str = "24000",
    rate: Optional[str] = None,
    narrator_rate: Optional[str] = None,
    synthesized_at: str,
    blueprint: Optional[dict] = None,
    degraded: bool = False,
    degraded_reason: Optional[str] = None,
    extra_warnings: Optional[Sequence[dict]] = None,
) -> dict:
    """Assemble the manifest. Raises if the clip set does not cover the turns exactly.

    Refusing to build is the point: a manifest is the completeness sentinel, so producing
    one over an incomplete clip set would make a half-synthesised material look ready.
    """
    turns = extract_turns(material)
    by_index = {}
    for clip in clips:
        if clip.turn_index in by_index:
            raise ManifestError("turn {0} has two clips".format(clip.turn_index))
        by_index[clip.turn_index] = clip

    expected = set(range(len(turns)))
    missing = sorted(expected - set(by_index))
    extra = sorted(set(by_index) - expected)
    if missing:
        raise ManifestError(
            "incomplete audio set: turns {0} have no clip; refusing to write a manifest".format(
                missing
            )
        )
    if extra:
        raise ManifestError("clips reference non-existent turns {0}".format(extra))

    entries: List[dict] = []
    for index in range(len(turns)):
        turn = turns[index]
        clip = by_index[index]
        speaker = turn["speaker"]
        duration = clip.resolved_duration()
        if duration is None:
            raise ManifestError(
                "turn {0} has no measured duration; duration must be parsed from the "
                "MP3, not estimated".format(index)
            )
        entry = {
            "turn_index": index,
            "speaker": speaker,
            "role": voice.ROLES[speaker],
            "voice_id": voice_map[speaker],
            "key": clip_key(index),
            "duration_ms": duration,
            "trailing_silence_ms": trailing_silence_for(turns, index),
            "text_sha256": sha256_text(turn["text"]),
            "cache_key": cache_key(
                voice_map[speaker], voice.ENGINE, sample_rate, clip.ssml
            ),
        }
        prep = prep_pause_for(turns, index)
        if prep is not None:
            entry["prep_pause_ms"] = prep
        # Only recorded when a rule fired, so the manifest documents "how this line was
        # read" without carrying a copy of the whole script (design.md §5).
        if ssml_module.rendered_differs(turn["text"], clip.ssml):
            entry["ssml"] = clip.ssml
        entries.append(entry)

    dialogue_ms = sum(e["duration_ms"] for e in entries if e["speaker"] != voice.NARRATOR)
    narrator_ms = sum(e["duration_ms"] for e in entries if e["speaker"] == voice.NARRATOR)
    dialogue_words = sum(
        count_words(turns[e["turn_index"]]["text"])
        for e in entries
        if e["speaker"] != voice.NARRATOR
    )
    status = duration_status(dialogue_ms)

    warnings: List[dict] = list(extra_warnings or [])
    warnings.extend(voice.detect_gender_markers(turns, voice_map))

    notes: List[str] = []
    if status != "in_band":
        notes.append(
            "dialogue audio is {0:.1f} min against the diagnostic band {1:.0f}-{2:.0f} min; "
            "this is a signal, not a gate -- the specification's 4-5 minutes describes the "
            "typical 600-650 word case and {3} dialogue words at ~140 WPM implies "
            "{4:.1f} min".format(
                dialogue_ms / 60000.0,
                BAND_MS[0] / 60000.0,
                BAND_MS[1] / 60000.0,
                dialogue_words,
                dialogue_words / 140.0,
            )
        )
    pending = assumptions.unresolved_ids()
    if pending:
        notes.append(
            "built while design.md §0 probes were unrun; unverified: " + ", ".join(pending)
        )

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "material_id": material_id,
        "scenario_key": scenario_key,
        "synthesis": {
            "provider": "aws-polly",
            "engine": voice.ENGINE,
            "language_code": voice.LANGUAGE_CODE,
            "output_format": "mp3",
            "sample_rate": sample_rate,
            "voice_map": dict(voice_map),
            "voice_swap_applied": voice.voice_swap_applied(material_id),
            "rate": {
                voice.NARRATOR: narrator_rate,
                voice.PROVIDER: rate,
                voice.ENQUIRER: rate,
            },
            "ssml_ruleset_version": ssml_module.SSML_RULESET_VERSION,
            "synthesized_at": synthesized_at,
            "unverified_assumptions": pending,
        },
        "clips": entries,
        "totals": {
            "clip_count": len(entries),
            "total_duration_ms": dialogue_ms + narrator_ms,
            "dialogue_duration_ms": dialogue_ms,
            "narrator_duration_ms": narrator_ms,
            "dialogue_words": dialogue_words,
            "measured_dialogue_wpm": (
                round(dialogue_words / (dialogue_ms / 60000.0), 1) if dialogue_ms else None
            ),
        },
        "validation": {
            "duration_band_ms": list(BAND_MS),
            "duration_status": status,
            "duration_is_diagnostic_only": True,
            "alignment_ok": True,
            "notes": notes,
        },
        "warnings": warnings,
    }
    if degraded:
        # Routed on its own verdict (design.md §14): a PASS that skipped the revise step is
        # still a PASS, but the frontend must say so.
        manifest["degraded"] = True
        manifest["degraded_reason"] = degraded_reason or "revise/re-audit skipped"

    alignment = check_alignment(manifest, material, blueprint)
    manifest["validation"]["alignment_ok"] = alignment["ok"]
    manifest["validation"]["alignment"] = alignment
    return manifest


def check_alignment(
    manifest: dict, material: dict, blueprint: Optional[dict] = None
) -> dict:
    """Three-way alignment: manifest clips, material turn text, blueprint turn_index.

    This is the only automated defence for the zero-misalignment acceptance item. A
    misplaced annotation in the player is very hard for a human to spot, so each leg is
    checked explicitly rather than inferred from array lengths matching.
    """
    turns = extract_turns(material)
    clips = manifest.get("clips")
    if not isinstance(clips, list):
        return {"ok": False, "errors": ["manifest has no clips array"]}

    errors: List[str] = []
    indexes = [c.get("turn_index") for c in clips if isinstance(c, dict)]

    if len(indexes) != len(clips):
        errors.append("some clips are not objects")
    if len(set(indexes)) != len(indexes):
        errors.append("clips repeat a turn_index")
    if sorted(i for i in indexes if isinstance(i, int)) != list(range(len(turns))):
        errors.append(
            "clips do not cover turns 0..{0} exactly (got {1} entries)".format(
                len(turns) - 1, len(indexes)
            )
        )
    # Order is the full-text playback order; the frontend must not have to sort.
    if indexes != sorted(i for i in indexes if isinstance(i, int)):
        errors.append("clips are not in playback order")

    for clip in clips:
        if not isinstance(clip, dict):
            continue
        index = clip.get("turn_index")
        if not isinstance(index, int) or not 0 <= index < len(turns):
            errors.append("clip turn_index {0!r} is out of range".format(index))
            continue
        turn = turns[index]
        if clip.get("text_sha256") != sha256_text(turn["text"]):
            errors.append(
                "clip {0} text_sha256 does not match the turn text; audio and script have "
                "diverged".format(index)
            )
        if clip.get("speaker") != turn["speaker"]:
            errors.append("clip {0} speaker does not match the turn".format(index))
        if clip.get("key") != clip_key(index):
            errors.append("clip {0} key {1!r} does not encode its turn".format(index, clip.get("key")))

    anchors: List[int] = []
    if isinstance(blueprint, dict):
        available = {c.get("turn_index") for c in clips if isinstance(c, dict)}
        for item in blueprint.get("items") or []:
            if not isinstance(item, dict):
                continue
            anchor = item.get("turn_index")
            anchors.append(anchor)
            if anchor not in available:
                errors.append(
                    "blueprint item {0} anchors turn {1!r}, which has no clip".format(
                        item.get("number"), anchor
                    )
                )
            elif isinstance(anchor, int) and turns[anchor]["speaker"] == voice.NARRATOR:
                errors.append(
                    "blueprint item {0} anchors narrator turn {1}; answers must not live in "
                    "narration".format(item.get("number"), anchor)
                )
    return {
        "ok": not errors,
        "errors": errors,
        "clip_count": len(clips),
        "turn_count": len(turns),
        "blueprint_anchors_checked": len(anchors),
    }


def turn_index_for_key(key: str) -> int:
    """Reverse audio/turn_NNN.mp3 -> index. For human triage and reconcile; the frontend
    must use clips[].key rather than rebuilding names (design.md §12)."""
    match = re.search(r"turn_(\d{3})\.mp3$", key)
    if not match:
        raise ManifestError("{0!r} is not an audio clip key".format(key))
    return int(match.group(1))
