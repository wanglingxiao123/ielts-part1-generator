#!/usr/bin/env python3
"""Render audit.json as the human-readable Markdown report.

audit.json is the source of truth; this report is one view of it. Keeping the data primary is
what lets the Loop read verdicts programmatically, while reviewers still get the report layout
defined in audit-rubric.md section 6.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DIMENSION_LABELS = [
    ("scenario_purpose_frame", "Part 1 scenario, purpose, and frame", 20),
    ("information_map_quality", "Information-map quality and item-writing support", 25),
    ("role_consistency", "Role consistency, progression, and coherence", 20),
    ("naturalness_level", "Naturalness, grammar, and level", 15),
    ("difficulty_distractor_control", "Difficulty mechanisms and distractor control", 15),
    ("transcript_readiness", "Transcript completeness and production readiness", 5),
]
SEVERITIES = ("critical", "major", "minor")
METRIC_LABELS = [
    ("dialogue_words", "Dialogue words"),
    ("dialogue_turns", "Dialogue turns"),
    ("first_half_turns", "First-half turns"),
    ("second_half_turns", "Second-half turns"),
    ("narrator_words", "Narrator words"),
]


def readiness_label(verdict: str, total: int) -> str:
    if verdict == "NOT_ASSESSABLE":
        return "Not assessable"
    if verdict == "FAIL":
        return "Substantial revision needed"
    if verdict == "PASS_WITH_MINOR_EDITS":
        return "Minor editing needed"
    return "Ready" if total >= 80 else "Minor editing needed"


def render(audit: dict, cross_check: dict | None = None) -> str:
    verdict = str(audit.get("verdict", "NOT_ASSESSABLE"))
    findings = [f for f in audit.get("findings", []) if isinstance(f, dict)]
    counts = {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES}
    score = audit.get("score") or {}
    total = score.get("total", 0)
    dimensions = score.get("dimensions") or {}

    lines = [
        "# Audit Result",
        "",
        f"**Verdict:** {verdict.replace('_', ' ')}",
        f"**Findings:** {counts['critical']} critical, {counts['major']} major, {counts['minor']} minor",
        "",
        "## Findings",
    ]

    if not findings:
        lines += ["", "No findings."]
    for severity in SEVERITIES:
        group = [f for f in findings if f.get("severity") == severity]
        if not group:
            continue
        lines += ["", f"### {severity.capitalize()}", ""]
        for finding in group:
            where = finding.get("turn_index")
            location = f" (turn {where})" if isinstance(where, int) else ""
            lines.append(f"- **{finding.get('rule', 'unspecified')}**{location}")
            lines.append(f"  - Evidence: {finding.get('evidence', '')}")
            lines.append(f"  - Fix: {finding.get('fix', '')}")

    info_map = [row for row in audit.get("blind_information_map", []) if isinstance(row, dict)]
    if info_map:
        lines += [
            "",
            "## Information Map",
            "",
            "Built by reading the script only, without the generator's blueprint.",
            "",
            "| # | Type | Turn | Speaker | Clarity | Mechanism | Script evidence |",
            "|---:|---|---:|---|---|---|---|",
        ]
        for row in info_map:
            lines.append(
                "| {seq} | {type} | {turn} | {speaker} | {clarity} | {mech} | {ev} |".format(
                    seq=row.get("seq", ""),
                    type=row.get("type", ""),
                    turn=row.get("turn_index", ""),
                    speaker=row.get("speaker", ""),
                    clarity=row.get("clarity", ""),
                    mech=row.get("mechanism") or "-",
                    ev=str(row.get("evidence", "")).replace("|", "\\|"),
                )
            )

    if cross_check:
        lines += [
            "",
            "## Blind Cross-Check",
            "",
            "Deterministic comparison against the generator's blueprint.",
            "",
            f"- Planned points: {cross_check.get('planned', 0)}",
            f"- Recovered blind: {cross_check.get('matched', 0)}",
        ]
        for row in cross_check.get("unrecoverable", []):
            lines.append(
                f"- **Unrecoverable** — item {row.get('number')} ({row.get('type')}) at turn "
                f"{row.get('turn_index')}: {row.get('target')!r}"
            )
        for row in cross_check.get("unintended_target", []):
            lines.append(
                f"- **Unintended detail** — turn {row.get('turn_index')} ({row.get('type')}): "
                f"{row.get('evidence')!r}"
            )
        for row in cross_check.get("ambiguous", []):
            lines.append(f"- **Ambiguous** — item {row.get('number')} at turn {row.get('turn_index')}")

    metrics = audit.get("metrics") or {}
    if metrics:
        lines += ["", "## Metrics", ""]
        for key, label in METRIC_LABELS:
            if key in metrics:
                lines.append(f"- {label}: {metrics[key]}")

    warnings = audit.get("warnings") or []
    if warnings:
        lines += [
            "",
            "## Advisory",
            "",
            "Non-blocking; the 600-650 word and 30-40 turn bands are observed typical values,",
            "not authoring limits.",
            "",
        ]
        lines += [f"- {message}" for message in warnings]

    fixes = [f for f in findings if f.get("severity") in ("critical", "major")]
    if fixes:
        lines += ["", "## Priority Fixes", ""]
        for number, finding in enumerate(fixes, 1):
            lines.append(f"{number}. {finding.get('fix', '')}")

    lines += [
        "",
        "## Scope Notes",
        "",
        "- The verdict covers script readiness only. Without actual questions, question wording,",
        "  word limits, option quality, and answer-key correctness are not verified.",
        "",
        "## Overall Score",
        "",
        f"**{total}/100 — {readiness_label(verdict, total)}**",
        "",
        "| Dimension | Maximum | Awarded |",
        "|---|---:|---:|",
    ]
    for key, label, maximum in DIMENSION_LABELS:
        lines.append(f"| {label} | {maximum} | {dimensions.get(key, 0)} |")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--cross-check", type=Path, help="cross_check.py --json output")
    args = parser.parse_args()

    try:
        audit = json.loads(args.audit.read_text(encoding="utf-8"))
        cross = json.loads(args.cross_check.read_text(encoding="utf-8")) if args.cross_check else None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(render(audit, cross), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
