"""Role to Polly voice mapping (deterministic) and the gender-marker warning scan.

Two things matter here and both are testable without AWS:

1. The map is a pure function of material_id, so a crash-resume or a single-turn
   resynthesis necessarily lands on the same voices (R1: "重合成不得换音色"). No state,
   no clock, no randomness.
2. Amy and Arthur swap between speaker2 and speaker3 across the corpus, so a candidate
   cannot learn "the service side is always female" -- a cue unrelated to the content,
   which for practice material is a real defect (design.md §2.2, option A rejected).

The chosen voices still get written into the manifest, and a rebuild reads the manifest in
preference to recomputing. That way changing this algorithm later leaves old materials alone.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Sequence

from . import assumptions

NARRATOR = "speaker1"
PROVIDER = "speaker2"
ENQUIRER = "speaker3"

SPEAKERS = (NARRATOR, PROVIDER, ENQUIRER)

ROLES = {NARRATOR: "narrator", PROVIDER: "provider", ENQUIRER: "enquirer"}

# Fixed: the real exam narrator is steady, slightly slow and announcement-like.
NARRATOR_VOICE = "Brian"
# Swapped as a pair, never mixed with a third voice.
DIALOGUE_VOICES = ("Amy", "Arthur")

# Every voice this module can emit. describe-voices must confirm all three exist in the
# target region before synthesis runs -- see assumptions.BY_ID["arthur-available"].
REQUIRED_VOICES = (NARRATOR_VOICE,) + DIALOGUE_VOICES

LANGUAGE_CODE = "en-GB"
ENGINE = "neural"


class VoiceMapError(ValueError):
    """An override or a material_id that cannot produce a usable voice map."""


def _swap_bit(material_id: str) -> int:
    """Stable coin flip derived from the id alone.

    sha256 rather than hash() because Python's str hash is salted per process: with hash()
    a resynthesis in a new process could pick different voices, which is exactly the
    failure R1 forbids.
    """
    digest = hashlib.sha256(material_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2


def voice_swap_applied(material_id: str) -> bool:
    return _swap_bit(material_id) == 1


def resolve_voice_map(
    material_id: str, override: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Return {speaker: voice_id}. Deterministic in material_id; override wins.

    The override exists so a human who hears a clash between the voice and the script
    (see detect_gender_markers) can pin the map for one material without touching code.
    """
    if not isinstance(material_id, str) or not material_id:
        raise VoiceMapError("material_id must be a non-empty string")

    swapped = voice_swap_applied(material_id)
    first, second = DIALOGUE_VOICES if not swapped else tuple(reversed(DIALOGUE_VOICES))
    mapping = {NARRATOR: NARRATOR_VOICE, PROVIDER: first, ENQUIRER: second}

    if override:
        unknown = sorted(set(override) - set(SPEAKERS))
        if unknown:
            raise VoiceMapError("override has unknown speakers: {0}".format(", ".join(unknown)))
        for speaker, voice in override.items():
            if not isinstance(voice, str) or not voice:
                raise VoiceMapError("override for {0} must be a voice id".format(speaker))
            mapping[speaker] = voice

    if mapping[PROVIDER] == mapping[ENQUIRER]:
        # Two indistinguishable dialogue voices make the transcript unfollowable, which is a
        # worse outcome than refusing the override.
        raise VoiceMapError(
            "speaker2 and speaker3 would both use {0}; the two dialogue voices must "
            "differ".format(mapping[PROVIDER])
        )
    return mapping


def unavailable_voices(available: Sequence[str], voice_map: Dict[str, str]) -> List[str]:
    """Voices this map needs that describe-voices did not return.

    Kept separate from resolve_voice_map so the mapping stays pure and offline-testable;
    the caller supplies the region's real voice list at synthesis time.
    """
    have = set(available)
    return sorted({v for v in voice_map.values() if v not in have})


# Conservative on purpose: only first-person self-identification and direct address, where
# the speaker's own gender is being asserted. Reliable detection would need to work out who
# is referring to whom, which is not achievable with a regex, so this under-reports by
# design and never blocks (design.md §2.2).
_TITLE = r"(?:Mr|Mrs|Ms|Miss|Sir|Madam|Sr|Madame)"
_SELF_INTRO = re.compile(
    r"\b(?:I'm|I am|this is|it's|my name(?:'s| is)|speaking to)\s+" + _TITLE + r"\b",
    re.IGNORECASE,
)
_TITLE_SPEAKING = re.compile(r"\b" + _TITLE + r"\s+[A-Z][a-z]+\s+(?:here|speaking)\b")
_DIRECT_ADDRESS = re.compile(r"(?:^|[,.!?]\s*|\bthank you,?\s+)(?:Sir|Madam)\b", re.IGNORECASE)
_GENDERED_PRONOUN_SELF = re.compile(
    r"\b(?:as|being)\s+(?:a|an)\s+(?:woman|man|lady|gentleman|girl|boy)\b", re.IGNORECASE
)

_SCANS = (
    ("self_introduction_with_title", _SELF_INTRO),
    ("title_speaking", _TITLE_SPEAKING),
    ("direct_address_sir_madam", _DIRECT_ADDRESS),
    ("self_described_gender", _GENDERED_PRONOUN_SELF),
)


def detect_gender_markers(
    turns: Sequence[dict], voice_map: Optional[Dict[str, str]] = None
) -> List[dict]:
    """Warnings where a dialogue turn asserts a speaker's gender.

    Narrator turns are skipped: the narrator voice is fixed, so a marker there cannot clash
    with the hash assignment. Output is a warning list for the reviewer, never an error --
    a false positive that blocked publication would cost more than the clash it prevents.
    """
    warnings: List[dict] = []
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        speaker = turn.get("speaker")
        text = turn.get("text")
        if speaker not in (PROVIDER, ENQUIRER) or not isinstance(text, str):
            continue
        for kind, pattern in _SCANS:
            match = pattern.search(text)
            if not match:
                continue
            warnings.append(
                {
                    "code": "voice_gender_warning",
                    "kind": kind,
                    "turn_index": index,
                    "speaker": speaker,
                    "assigned_voice": (voice_map or {}).get(speaker),
                    "match": match.group(0).strip(),
                    "detail": (
                        "turn asserts a gender for {0}; confirm it matches the assigned "
                        "voice or pin the map with the voice_map override".format(speaker)
                    ),
                }
            )
    return warnings


def voice_notes() -> List[str]:
    """Notes to carry into the manifest while the voice assumptions are unmeasured."""
    pending = [
        a.id for a in assumptions.unresolved() if a.id in {"arthur-available", "default-wpm"}
    ]
    if not pending:
        return []
    return ["unverified: " + assumptions.describe(*pending)]
