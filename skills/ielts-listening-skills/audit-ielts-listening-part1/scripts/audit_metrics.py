#!/usr/bin/env python3
"""Extract deterministic metrics from supported IELTS Part 1 JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SPELLING_RE = re.compile(r"\b(?:[A-Z]\s*[-,]\s*){2,}[A-Z]\b|\b[A-Z](?:-[A-Z]){2,}\b|\bdouble\s+[A-Z]\b", re.I)
NUMBER_RE = re.compile(r"(?:[$£€]\s*\d|\b\d+\b)")
CORRECTION_CANDIDATE_RE = re.compile(r"\b(actually|I mean|not .{1,35} but|used to)\b", re.I)
INDIRECT_CANDIDATE_RE = re.compile(r"\b(the former|the latter|that one|on paper|in other words|which means)\b", re.I)
FIRST_RANGE_RE = re.compile(r"questions?\s+1\s*(?:to|[-~–])\s*(\d+)", re.I)
SECOND_RANGE_RE = re.compile(r"questions?\s+(\d+)\s*(?:to|[-~–])\s*10", re.I)


def words(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value))


def issue(severity: str, message: str) -> dict[str, str]:
    return {"severity": severity, "message": message}


def inspect(data: object) -> dict[str, object]:
    issues: list[dict[str, str]] = []
    warnings: list[str] = []
    manual = [
        "inferred scenario has participants, setting, and practical purpose",
        "narrator contains no testable answer information",
        "provider holds information and enquirer drives the enquiry",
        "8-10 ordered factual details form separate, clearly cued micro-cycles",
        "at least four information-detail types are represented",
        "an earlier value is explicitly replaced by a final value",
        "an answer term is followed later by a genuine indirect reference",
        "exactly 2-3 deliberate distractor cycles are used",
        "language is natural, everyday, polite, and Part 1 level",
        "opening/purpose/body/decision/closing progression is coherent",
    ]
    if not isinstance(data, dict):
        return {"assessable": False, "issues": [issue("critical", "top-level JSON must be an object")], "warnings": [], "parts": [], "manual_checks_required": manual}
    parts = data.get("listening_material_parts")
    if not isinstance(parts, list) or len(parts) != 1:
        issues.append(issue("critical", "listening_material_parts must contain exactly one object"))
        return {"assessable": False, "issues": issues, "warnings": warnings, "parts": [], "manual_checks_required": manual}
    results, any_script = [], False
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            issues.append(issue("critical", f"part[{index}] must be an object"))
            continue
        script = part.get("script")
        if not isinstance(script, dict):
            issues.append(issue("critical", f"part[{index}].script must be an object"))
            continue
        turns = script.get("turns")
        if not isinstance(turns, list) or not turns:
            issues.append(issue("critical", f"part[{index}] turns are empty"))
            continue
        any_script = True
        speakers, narrator_indices, dialogue_indices = set(), [], []
        for turn_index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                issues.append(issue("critical", f"turn[{turn_index}] must be an object"))
                continue
            speaker, text = turn.get("speaker"), turn.get("text")
            if speaker not in {"speaker1", "speaker2", "speaker3"}:
                issues.append(issue("critical", f"turn[{turn_index}] has invalid speaker"))
            if not isinstance(text, str) or not text.strip():
                issues.append(issue("critical", f"turn[{turn_index}] has empty text"))
            speakers.add(speaker)
            (narrator_indices if speaker == "speaker1" else dialogue_indices).append(turn_index)
        if len(speakers) != 3:
            issues.append(issue("critical", f"expected three identifiable roles; found {sorted(map(str, speakers))}"))
        if len(narrator_indices) != 3:
            issues.append(issue("major", f"exactly 3 narrator turns required; found {len(narrator_indices)}"))
            continue
        opening, midpoint, closing = (turns[i]["text"] for i in narrator_indices)
        first, second = FIRST_RANGE_RE.search(opening), SECOND_RANGE_RE.search(midpoint)
        first_end, second_start = (int(first.group(1)), int(second.group(1))) if first and second else (0, 0)
        if first_end not in {5, 6} or second_start != first_end + 1:
            issues.append(issue("major", "narrator question ranges are missing or non-contiguous"))
        if narrator_indices[0] != 0 or narrator_indices[-1] != len(turns) - 1:
            issues.append(issue("major", "speaker1 must open and close the script"))
        if "once only" not in opening.casefold():
            issues.append(issue("major", "opening omits once only"))
        if not re.search(r"end of (?:section one|part 1|part one)", closing, re.I) or "check your answers" not in closing.casefold():
            issues.append(issue("major", "closing omits Part 1 end or checking prompt"))
        if not re.search(r"(?:turn|move on) to (?:section|part) (?:two|2)", closing, re.I):
            issues.append(issue("major", "closing does not direct candidates to Part/Section 2"))
        narration = " ".join(turns[i]["text"] for i in narrator_indices)
        full_mode = "four different recordings" in opening.casefold() or "four parts" in opening.casefold()
        if full_mode:
            if "four different recordings" not in opening.casefold() or "four parts" not in opening.casefold():
                issues.append(issue("major", "full opening is missing four recordings or four parts"))
            if not 160 <= words(narration) <= 230:
                issues.append(issue("minor", f"full narration words {words(narration)} outside 160-230"))
        elif not 70 <= words(narration) <= 110:
            issues.append(issue("minor", f"short narration words {words(narration)} outside 70-110"))
        dialogue = " ".join(turns[i]["text"] for i in dialogue_indices)
        dwords, dturns = words(dialogue), len(dialogue_indices)
        if not 450 <= dwords <= 750:
            issues.append(issue("major", f"dialogue words {dwords} outside 450-750"))
        elif not 600 <= dwords <= 650:
            warnings.append(f"dialogue words {dwords} outside preferred 600-650")
        if not 20 <= dturns <= 48:
            issues.append(issue("major", f"dialogue turns {dturns} outside 20-48"))
        elif not 30 <= dturns <= 40:
            warnings.append(f"dialogue turns {dturns} outside preferred 30-40")
        before = sum(i < narrator_indices[1] for i in dialogue_indices)
        after = dturns - before
        if before < 8 or after < 8:
            issues.append(issue("major", f"half turns {before}/{after}; minimum 8/8"))
        if not SPELLING_RE.search(dialogue):
            issues.append(issue("major", "no spelling sequence detected"))
        if not NUMBER_RE.search(dialogue):
            issues.append(issue("major", "no numeric detail detected"))
        results.append({
            "index": index,
            "scenario": part.get("scenario"),
            "speaker_ids": sorted(map(str, speakers)),
            "dialogue_words": dwords,
            "dialogue_turns": dturns,
            "first_half_turns": before,
            "second_half_turns": after,
            "narrator_words": words(narration),
            "narration_mode_inferred": "full" if full_mode else "short",
            "spelling_detected": bool(SPELLING_RE.search(dialogue)),
            "numeric_detected": bool(NUMBER_RE.search(dialogue)),
            "correction_marker_candidates": CORRECTION_CANDIDATE_RE.findall(dialogue),
            "indirect_marker_candidates": INDIRECT_CANDIDATE_RE.findall(dialogue),
        })
    return {"assessable": any_script, "issues": issues, "warnings": warnings, "parts": results, "manual_checks_required": manual}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("material", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        data = json.loads(args.material.read_text(encoding="utf-8"))
        result = inspect(data)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"assessable": False, "issues": [issue("critical", f"invalid JSON: {exc}")], "warnings": [], "parts": [], "manual_checks_required": []}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Assessable: {result['assessable']}")
        for part in result["parts"]:
            print(f"Part {part['index']}: {part['dialogue_words']} words, {part['dialogue_turns']} turns, {part['first_half_turns']}/{part['second_half_turns']} halves")
        for item in result["issues"]:
            print(f"{item['severity'].upper()}: {item['message']}")
        for message in result.get("warnings", []):
            print(f"WARNING: {message}")
        print(f"Manual checks required: {len(result['manual_checks_required'])}")
    return 0 if result["assessable"] else 1


if __name__ == "__main__":
    sys.exit(main())
