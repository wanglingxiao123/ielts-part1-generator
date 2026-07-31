#!/usr/bin/env python3
"""Compare the auditor's blind information map against the generator's blueprint.

The audit step reads the script without ever seeing the blueprint, so its map is an
independent reconstruction. Points the auditor could not recover are genuine defects: if a
careful reader working from the script alone cannot find information point 7, a candidate
under once-only listening will not find it either. Points the auditor found that the
blueprint never planned are also worth reporting -- an unintended recordable detail can
create a second defensible answer and make a later question ambiguous.

This is deterministic Python. No model is involved, so it costs nothing to run on every
material and its verdict cannot drift.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The auditor may anchor a point to its confirmation turn rather than its first mention, so
# nearby anchors count as the same point. Wider tolerance would start merging genuinely
# distinct adjacent points, which spec 4B-2 requires to stay separate.
ANCHOR_TOLERANCE = 1

# Shortest normalised `target` allowed to pair two entries by appearing in the auditor's evidence.
# Without a floor a one-character target ("6") would be found inside most rows and claim an
# arbitrary one. Three characters admits every reference code, name and multi-digit quantity in the
# corpus; below that the pair still has the anchor+type route available.
MIN_TARGET_MATCH = 3

_PUNCTUATION = re.compile(r"[^0-9a-z]+")


def normalise_evidence(value: object) -> str:
    """Casefold, drop punctuation and whitespace: the comparison key for evidence text.

    Deliberately aggressive. The blueprint quotes the script; the auditor retypes what it heard,
    so the same span routinely differs by a comma, a capital, or a trailing full stop. Comparing
    raw strings would call those different points, which is the whole defect this function exists
    to close. Digits and letters survive, and those are what a reference code or a price IS.
    """
    if not isinstance(value, str):
        return ""
    return _PUNCTUATION.sub("", value.casefold())


def read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: invalid {label} JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {label} must be a JSON object")
    return data


def anchor_of(entry: object):
    """Return the anchor, or None when absent or malformed.

    Deliberately not a -1 sentinel: -1 sits within ANCHOR_TOLERANCE of both another -1 and a
    real anchor at turn 0, so two unanchored entries -- or one unanchored entry and a genuine
    opening-turn detail -- would match each other and report a clean result.
    """
    if isinstance(entry, dict):
        value = entry.get("turn_index")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _anchor_gap(item: object, row: object) -> int:
    """Distance between two anchors; a huge number when either is missing.

    A sentinel rather than an exception because the evidence-identity pass deliberately admits
    rows with no usable anchor -- an auditor that recorded the right span but no turn index has
    still recovered the point. Such a row simply loses every tie.
    """
    anchor, seen = anchor_of(item), anchor_of(row)
    if anchor is None or seen is None:
        return 1 << 30
    return abs(seen - anchor)


def compare(blueprint: dict, audit: dict) -> dict:
    planned = [item for item in blueprint.get("items", []) if isinstance(item, dict)]
    observed = [row for row in audit.get("blind_information_map", []) if isinstance(row, dict)]

    unmatched_observed = list(range(len(observed)))
    pairing: dict[int, int] = {}

    def near(item: dict, position: int) -> bool:
        anchor, seen = anchor_of(item), anchor_of(observed[position])
        return (anchor is not None and seen is not None
                and abs(seen - anchor) <= ANCHOR_TOLERANCE)

    def same_evidence(item: dict, position: int) -> bool:
        """Character-identical evidence, once punctuation and case are normalised away.

        The strongest identity signal available and the reason it is checked before anything
        else. Measured failure it fixes: a booking reference stated at turn 32 and confirmed at
        turn 33 was recorded by the auditor as `number` where the blueprint called it `name`, so
        neither the exact-anchor pass nor the type-gated tolerant pass could pair them -- and a
        point the script says twice, in full, was reported 听不出来. The evidence string was
        identical throughout. Anchor and type are both derived judgements about a span; the span
        itself is not, so it outranks them.
        """
        planned_text = normalise_evidence(item.get("evidence"))
        return bool(planned_text) and planned_text == normalise_evidence(
            observed[position].get("evidence"))

    def target_in_evidence(item: dict, position: int) -> bool:
        """The planned ANSWER (`target`) appears in the row the auditor wrote, anchors adjacent.

        The auditor writes down what it heard, so it routinely records a shorter or longer span
        than the blueprint quotes: "HGR482" against "Your booking reference is HGR482", or the
        confirmation sentence instead of the first mention. Full-string equality misses all of
        those, but `target` is the one substring that must survive -- it is the answer a candidate
        writes on the answer sheet, and the 命题铁律 (spec §4B-4) requires it to be spoken verbatim.
        If it is inside the auditor's evidence, the auditor recovered this point.

        Weaker than identity, so it carries one guard: proximity. NOT type -- type is exactly what
        disagrees in the case this exists to catch, and gating on it would restore the defect.
        """
        target = normalise_evidence(item.get("target"))
        seen_text = normalise_evidence(observed[position].get("evidence"))
        if len(target) < MIN_TARGET_MATCH or not seen_text or target not in seen_text:
            return False
        return near(item, position)

    def exact_anchor(item: dict, position: int) -> bool:
        anchor, seen = anchor_of(item), anchor_of(observed[position])
        return anchor is not None and seen is not None and seen == anchor

    def near_same_type(item: dict, position: int) -> bool:
        """Proximity alone is too weak: planned [20, 21] against observed [20, 22] would pair
        both up, hiding one unrecoverable point AND one unintended detail at once. An off-by-one
        pairing with nothing in common but its anchor must therefore also agree on detail type.
        """
        return near(item, position) and observed[position].get("type") == item.get("type")

    # Passes run strongest-signal-first, and a row claimed by an earlier pass is gone. Ordering by
    # strength rather than by convenience is the whole design:
    #
    #   1. identical evidence -- the same span written down twice; nothing outranks it.
    #   2. exact anchor -- kept ahead of every tolerant rule so planned [10, 11] against observed
    #      [11] cannot let item 1 take turn 11 on tolerance and leave the tool blaming item 2.
    #      The count would be right and the identity wrong, and the revise instruction would send
    #      the writer to fix an information point that is actually fine.
    #   3. planned target inside the auditor's evidence + adjacent anchor -- the same answer span
    #      written down at a different length.
    #   4. adjacent anchor + same type -- no shared text at all, so it needs both guards.
    #
    # Pass 1 is intentionally free of anchor and type conditions. Passes 3 and 4 both require
    # proximity, so neither can pair two entries that share nothing.
    for admits in (same_evidence, exact_anchor, target_in_evidence, near_same_type):
        for index, item in enumerate(planned):
            if index in pairing:
                continue
            candidates = [p for p in unmatched_observed if admits(item, p)]
            # Closest anchor wins among equally admissible rows. Entries with no usable anchor
            # sort last rather than raising: pass 1 admits them, and `abs(None - n)` would crash.
            hit = min(candidates, key=lambda p: _anchor_gap(item, observed[p]), default=None)
            if hit is not None:
                pairing[index] = hit
                unmatched_observed.remove(hit)

    matched, unrecoverable = [], []
    for index, item in enumerate(planned):
        hit = pairing.get(index)
        if hit is None:
            unrecoverable.append({
                "number": item.get("number"),
                "type": item.get("type"),
                "target": item.get("target"),
                "turn_index": anchor_of(item),
                "evidence": item.get("evidence"),
                "reason": "auditor reading the script blind did not record this point",
            })
        else:
            matched.append({
                "number": item.get("number"),
                "turn_index": anchor_of(item),
                "audit_seq": observed[hit].get("seq"),
                "audit_clarity": observed[hit].get("clarity"),
            })

    unintended = [{
        "audit_seq": observed[position].get("seq"),
        "type": observed[position].get("type"),
        "turn_index": anchor_of(observed[position]),
        "evidence": observed[position].get("evidence"),
        "reason": "auditor recorded a detail the blueprint never planned; may create a second defensible answer",
    } for position in unmatched_observed]

    ambiguous = [{
        "number": row["number"],
        "turn_index": row["turn_index"],
        "audit_clarity": row["audit_clarity"],
        "reason": "point is recoverable but the auditor flagged it as ambiguous",
    } for row in matched if row["audit_clarity"] == "ambiguous"]

    return {
        "ok": not unrecoverable and not unintended and not ambiguous,
        "planned": len(planned),
        "observed": len(observed),
        "matched": len(matched),
        "unrecoverable": unrecoverable,
        "unintended_target": unintended,
        "ambiguous": ambiguous,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()

    result = compare(read_json(args.blueprint, "blueprint"), read_json(args.audit, "audit"))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print(f"Planned: {result['planned']}  Observed: {result['observed']}  Matched: {result['matched']}")
    for row in result["unrecoverable"]:
        print(f"UNRECOVERABLE: item {row['number']} ({row['type']}) at turn {row['turn_index']}: {row['target']!r}")
    for row in result["unintended_target"]:
        print(f"UNINTENDED: audit seq {row['audit_seq']} ({row['type']}) at turn {row['turn_index']}: {row['evidence']!r}")
    for row in result["ambiguous"]:
        print(f"AMBIGUOUS: item {row['number']} at turn {row['turn_index']}")
    print("PASS: blind map aligns with blueprint" if result["ok"] else "FAIL: blind map diverges from blueprint")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
