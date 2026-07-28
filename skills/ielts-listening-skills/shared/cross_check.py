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
import sys
from pathlib import Path

# The auditor may anchor a point to its confirmation turn rather than its first mention, so
# nearby anchors count as the same point. Wider tolerance would start merging genuinely
# distinct adjacent points, which spec 4B-2 requires to stay separate.
ANCHOR_TOLERANCE = 1


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


def compare(blueprint: dict, audit: dict) -> dict:
    planned = [item for item in blueprint.get("items", []) if isinstance(item, dict)]
    observed = [row for row in audit.get("blind_information_map", []) if isinstance(row, dict)]

    unmatched_observed = list(range(len(observed)))
    pairing: dict[int, int] = {}

    def eligible(item: dict, position: int, exact_only: bool) -> bool:
        """Can this planned item claim this observed row?

        Proximity alone is too weak: planned [20, 21] against observed [20, 22] would pair both
        up, hiding one unrecoverable point AND one unintended detail at once. An off-by-one
        pairing must therefore also agree on detail type.
        """
        anchor, seen = anchor_of(item), anchor_of(observed[position])
        if anchor is None or seen is None:
            return False
        if seen == anchor:
            return True
        if exact_only or abs(seen - anchor) > ANCHOR_TOLERANCE:
            return False
        return observed[position].get("type") == item.get("type")

    # Two passes so exact anchors claim their own row first. With one greedy pass, planned
    # [10, 11] against observed [11] lets item 1 take turn 11 on tolerance, and the tool then
    # blames item 2 -- the count is right but the identity is wrong, and the revise instruction
    # sends the writer to fix an information point that is actually fine.
    for exact_only in (True, False):
        for index, item in enumerate(planned):
            if index in pairing:
                continue
            candidates = [p for p in unmatched_observed if eligible(item, p, exact_only)]
            hit = min(
                candidates,
                key=lambda p: abs(anchor_of(observed[p]) - anchor_of(item)),
                default=None,
            )
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
