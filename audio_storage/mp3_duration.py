"""MP3 duration from bytes, zero dependencies.

ffmpeg is out (image size, ARM64 constraint) and so are the two shortcuts:

  * bytes / bitrate -- Polly's MP3 bitrate is not user-specified or documented, and an ID3
    header would skew every estimate in the same direction.
  * Polly speech marks -- a second request for the same text, billed again, to obtain a
    number that is derivable from the bytes already paid for.

So: walk the frame headers and sum samples_per_frame / sample_rate. Correct for CBR and VBR
alike, because each frame contributes its own duration rather than an average.

A truncated or non-MP3 payload raises. Returning 0 would be worse than failing: 0 ms flows
into the manifest, out_of_band fires, and a reviewer investigates a script that is fine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# [(version_id, layer)][bitrate_index], kbps. version_id: 3=MPEG1, 2=MPEG2, 0=MPEG2.5.
# `layer` here is the DECODED layer number (1/2/3 for Layer I/II/III), matching what
# _parse_header computes as `4 - layer_bits`. Keying these rows by the raw header bits instead
# silently maps Layer III onto the Layer I row: for Polly's MPEG2 output that reads 96 kbps
# where the frame says 48, halving every reported duration while still parsing "successfully".
_BITRATES = {
    (3, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, None],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, None],
    (3, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, None],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
}
# MPEG2.5 shares MPEG2's bitrate tables.
for _layer in (1, 2, 3):
    _BITRATES[(0, _layer)] = _BITRATES[(2, _layer)]

_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}

# Samples per frame by (version_id, layer). Layer 3 on MPEG2/2.5 is 576, not 1152 -- getting
# this wrong doubles the reported duration for Polly's 22.05/24 kHz output, which is exactly
# the sample-rate family Polly returns.
_SAMPLES = {
    (3, 3): 1152, (3, 2): 1152, (3, 1): 384,
    (2, 3): 576, (2, 2): 1152, (2, 1): 384,
    (0, 3): 576, (0, 2): 1152, (0, 1): 384,
}


class Mp3ParseError(ValueError):
    """The payload is not parseable MP3."""


def _skip_id3v2(data: bytes) -> int:
    """Offset of the first frame, past any ID3v2 tag."""
    if len(data) >= 10 and data[:3] == b"ID3":
        # Synchsafe size: 7 bits per byte, excluding the 10-byte header itself.
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        offset = 10 + size
        if data[5] & 0x10:  # footer present
            offset += 10
        return min(offset, len(data))
    return 0


def _parse_header(header: bytes) -> Optional[Tuple[int, int, int, int]]:
    """(frame_length, samples, sample_rate, bitrate_kbps) or None if not a valid header."""
    if len(header) < 4:
        return None
    if header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        return None
    version_id = (header[1] >> 3) & 0x03
    if version_id == 1:  # reserved
        return None
    layer_bits = (header[1] >> 1) & 0x03
    if layer_bits == 0:  # reserved
        return None
    layer = 4 - layer_bits
    bitrate_index = (header[2] >> 4) & 0x0F
    if bitrate_index in (0, 15):  # free-format or invalid
        return None
    rate_index = (header[2] >> 2) & 0x03
    if rate_index == 3:
        return None
    bitrate = _BITRATES[(version_id, layer)][bitrate_index]
    if not bitrate:
        return None
    sample_rate = _SAMPLE_RATES[version_id][rate_index]
    padding = (header[2] >> 1) & 0x01
    samples = _SAMPLES[(version_id, layer)]

    if layer == 1:
        frame_length = (12 * bitrate * 1000 // sample_rate + padding) * 4
    else:
        frame_length = (samples // 8) * bitrate * 1000 // sample_rate + padding
    if frame_length <= 4:
        return None
    return frame_length, samples, sample_rate, bitrate


def parse_frames(data: bytes) -> List[Tuple[int, int, int]]:
    """(samples, sample_rate, bitrate_kbps) per frame, in order."""
    if not data:
        raise Mp3ParseError("empty payload")
    frames: List[Tuple[int, int, int]] = []
    position = _skip_id3v2(data)
    size = len(data)
    resyncs = 0

    while position + 4 <= size:
        parsed = _parse_header(data[position : position + 4])
        if parsed is None:
            # Resync: scan forward for the next sync word. Bounded, because unbounded
            # resyncing turns a corrupt file into a plausible-looking duration.
            resyncs += 1
            if resyncs > 64 and not frames:
                raise Mp3ParseError("no MP3 frame header found in the first 64 sync attempts")
            nxt = data.find(b"\xff", position + 1)
            if nxt == -1:
                break
            position = nxt
            continue
        frame_length, samples, sample_rate, bitrate = parsed
        if position + frame_length > size:
            # Trailing partial frame. Tolerated only if we already have a body of frames;
            # otherwise the file is truncated to the point of being unmeasurable.
            break
        frames.append((samples, sample_rate, bitrate))
        position += frame_length

    if not frames:
        raise Mp3ParseError("no complete MP3 frames found; payload is truncated or not MP3")
    return frames


def duration_ms(data: bytes) -> int:
    """Duration in whole milliseconds, rounded to nearest."""
    total_seconds = 0.0
    for samples, sample_rate, _ in parse_frames(data):
        total_seconds += samples / float(sample_rate)
    return int(round(total_seconds * 1000))


def describe(data: bytes) -> dict:
    """Duration plus what the frames say, for cross-checking against afinfo."""
    frames = parse_frames(data)
    seconds = sum(s / float(r) for s, r, _ in frames)
    rates = sorted({r for _, r, _ in frames})
    bitrates = sorted({b for _, _, b in frames})
    return {
        "duration_ms": int(round(seconds * 1000)),
        "frame_count": len(frames),
        "sample_rates": rates,
        "bitrates_kbps": bitrates,
        "vbr": len(bitrates) > 1,
        "id3v2_bytes": _skip_id3v2(data),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report MP3 duration without ffmpeg.")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    status = 0
    for path in args.paths:
        try:
            info = describe(path.read_bytes())
        except (OSError, Mp3ParseError) as exc:
            print("{0}: ERROR {1}".format(path, exc))
            status = 1
            continue
        print(
            "{0}: {1} ms  frames={2}  rate={3}  bitrate={4}{5}".format(
                path,
                info["duration_ms"],
                info["frame_count"],
                ",".join(str(r) for r in info["sample_rates"]),
                ",".join(str(b) for b in info["bitrates_kbps"]),
                " (VBR)" if info["vbr"] else "",
            )
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
