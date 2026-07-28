"""Audio synthesis and S3 state flow for IELTS Listening Part 1 materials.

Import-safe without AWS: boto3 is only imported when object_store.S3ObjectStore is
constructed. Everything else -- voice mapping, SSML rendering, MP3 duration parsing,
manifest building, and the whole state machine -- is pure Python and unit-tested offline.

synthesize.py is the one module that talks to Polly. It stayed unwritten until design.md §0's
seven assumptions were measured against real audio, because three of its design decisions -- the
two say-as rules and the baked-in trailing pause -- turn on behaviour no document could settle.
All seven now carry a recorded resolution (assumptions.py), and two of the three rules were
switched off as a result. It still calls ``assumptions.require_phase0`` so that adding a new
unmeasured assumption re-closes the gate.
"""

from __future__ import annotations

__all__ = [
    "assumptions",
    "manifest",
    "mp3_duration",
    "object_store",
    "ssml",
    "state_store",
    "synthesize",
    "voice",
]
