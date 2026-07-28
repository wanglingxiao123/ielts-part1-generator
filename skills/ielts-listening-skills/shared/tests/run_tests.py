#!/usr/bin/env python3
"""Contract regression tests. Run: python3 shared/tests/run_tests.py

Covers the four checks added to validate_part1.py, the warning-downgrade behaviour, the blind
cross-check, and that the archived samples do not crash the deterministic metrics script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
ROOT = SKILLS.parents[1]
FIXTURES = HERE / "fixtures"
VALIDATE = SKILLS / "generate-ielts-listening-part1" / "scripts" / "validate_part1.py"
METRICS = SKILLS / "audit-ielts-listening-part1" / "scripts" / "audit_metrics.py"
CROSS_CHECK = SKILLS / "shared" / "cross_check.py"
RENDER = SKILLS / "shared" / "render_audit_report.py"
SCHEMAS = SKILLS / "shared" / "schemas"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        failures.append(f"{name}{': ' + detail if detail else ''}")


def run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True
    )


def validate(blueprint: str, *extra: str) -> subprocess.CompletedProcess:
    return run(VALIDATE, str(FIXTURES / "material_valid.json"), "--blueprint", str(FIXTURES / blueprint), *extra)


def test_schemas_are_valid() -> None:
    print("schemas")
    try:
        from jsonschema import Draft7Validator
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return
    for name in ("material.schema.json", "blueprint.schema.json", "audit.schema.json"):
        try:
            Draft7Validator.check_schema(json.loads((SCHEMAS / name).read_text(encoding="utf-8")))
            check(f"{name} is valid Draft-07", True)
        except Exception as exc:  # noqa: BLE001 - reporting any schema defect is the point
            check(f"{name} is valid Draft-07", False, str(exc))

    material = json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8"))
    blueprint = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))
    audit = json.loads((FIXTURES / "audit_valid.json").read_text(encoding="utf-8"))
    for label, data, schema in (
        ("material", material, "material.schema.json"),
        ("blueprint", blueprint, "blueprint.schema.json"),
        ("audit", audit, "audit.schema.json"),
    ):
        errors = list(Draft7Validator(json.loads((SCHEMAS / schema).read_text(encoding="utf-8"))).iter_errors(data))
        check(f"{label} fixture matches its schema", not errors, "; ".join(e.message for e in errors[:3]))


def test_warning_does_not_fail() -> None:
    print("warning downgrade (R6)")
    result = validate("blueprint_valid.json")
    check("valid fixture passes", result.returncode == 0, result.stdout)
    check("word count is inside hard limits but outside typical band",
          "outside preferred 600-650" in result.stdout, result.stdout)
    check("warning is reported without failing", "WARNING:" in result.stdout and result.returncode == 0)

    as_json = validate("blueprint_valid.json", "--json")
    payload = json.loads(as_json.stdout)
    check("--json reports ok with warnings separated",
          payload["ok"] is True and payload["errors"] == [] and len(payload["warnings"]) == 1,
          as_json.stdout)
    check("--json exposes metrics for the Loop",
          payload["metrics"]["dialogue_words"] > 0 and payload["metrics"]["dialogue_turns"] > 0)


def test_new_checks_catch_defects() -> None:
    print("new checks (R2, R3, R4, R5)")
    cases = [
        ("blueprint_bad_anchor.json", "does not carry its evidence", "anchor mismatch is caught"),
        ("blueprint_bad_grouping.json", "form_group", "unusable question-type grouping is caught"),
        ("blueprint_bad_answer_term.json", "must be the target of one of the ten items",
         "answer term absent from targets is caught"),
        ("blueprint_thin_confirmation.json", "at least 3 confirmed items",
         "thin confirmation density is caught"),
    ]
    for fixture, needle, label in cases:
        result = validate(fixture)
        check(label, result.returncode == 1 and needle in result.stdout, result.stdout)


def test_malformed_turns_stay_reportable() -> None:
    """A content defect must not become an orchestration crash (D1)."""
    print("malformed input robustness")
    import copy
    import tempfile

    base = json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8"))
    cases = {
        "turn missing text": {"speaker": "speaker2"},
        "turn text not a string": {"speaker": "speaker2", "text": 123},
        "turn not an object": "just a string",
    }
    scratch = Path(tempfile.mkdtemp())
    for label, bad_turn in cases.items():
        broken = copy.deepcopy(base)
        broken["listening_material_parts"][0]["script"]["turns"][5] = bad_turn
        path = scratch / "material.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        result = run(VALIDATE, str(path), "--blueprint", str(FIXTURES / "blueprint_valid.json"), "--json")
        parsed = None
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout)
            except json.JSONDecodeError:
                parsed = None
        check(f"{label}: emits parseable JSON instead of crashing",
              parsed is not None and parsed["ok"] is False, result.stderr[-200:])


def test_grouping_cannot_be_faked() -> None:
    """A shared group label must not stand in for a constructible table (D2)."""
    print("question-type grouping cannot be faked")
    import copy
    import tempfile

    base = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp())

    all_choice = copy.deepcopy(base)
    for item in all_choice["items"]:
        item["item_form"], item["form_group"] = "multiple_choice", None
    for item in all_choice["items"][:3]:
        item["form_group"] = "A"
    all_choice["question_type_coverage"] = {"multiple_choice": list(range(1, 11))}

    mixed = copy.deepcopy(base)
    for item, form in zip(mixed["items"][:3], ("form", "table", "note")):
        item["item_form"], item["form_group"] = form, "A"
    coverage: dict = {}
    for item in mixed["items"]:
        coverage.setdefault(item["item_form"], []).append(item["number"])
    mixed["question_type_coverage"] = coverage

    notes_only = copy.deepcopy(base)
    for item in notes_only["items"]:
        if item["form_group"] is not None:
            item["item_form"] = "note"
    coverage = {}
    for item in notes_only["items"]:
        coverage.setdefault(item["item_form"], []).append(item["number"])
    notes_only["question_type_coverage"] = coverage

    for label, payload in (
        ("no form/table item exists at all", all_choice),
        ("form_group mixes item_form values", mixed),
        ("group is made of standalone notes", notes_only),
    ):
        path = scratch / "blueprint.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = run(VALIDATE, str(FIXTURES / "material_valid.json"), "--blueprint", str(path))
        check(f"rejected: {label}", result.returncode == 1, result.stdout)


def test_spelled_name_rule_not_vacuous() -> None:
    """The rule must fire when no item records the spelling at all (D3)."""
    print("spelled-name requirement")
    import copy
    import tempfile

    payload = copy.deepcopy(json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8")))
    for item in payload["items"]:
        if item["type"] == "name":
            item["type"], item["confirmed"] = "option", False
    path = Path(tempfile.mkdtemp()) / "blueprint.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = run(VALIDATE, str(FIXTURES / "material_valid.json"), "--blueprint", str(path))
    check("blueprint with no name-typed item is rejected",
          result.returncode == 1 and "spelled-name" in result.stdout, result.stdout)


def test_metrics_absent_when_unmeasured() -> None:
    """Zeros must not masquerade as measurements (D7)."""
    print("metrics honesty")
    import copy
    import tempfile

    broken = copy.deepcopy(json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8")))
    broken["listening_material_parts"][0]["script"]["turns"] = []
    path = Path(tempfile.mkdtemp()) / "material.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    payload = json.loads(run(VALIDATE, str(path), "--blueprint", str(FIXTURES / "blueprint_valid.json"), "--json").stdout)
    check("unmeasured metrics are omitted, not zero",
          payload["metrics"] == {} and payload["ok"] is False, json.dumps(payload["metrics"]))


def test_typical_band_is_not_a_finding() -> None:
    """audit_metrics.py must report the typical band as a warning, not a finding (D5)."""
    print("audit metrics band handling")
    payload = json.loads(run(METRICS, str(FIXTURES / "material_valid.json"), "--json").stdout)
    band = [i for i in payload["issues"] if "preferred" in i["message"]]
    check("no finding for typical-band deviation", not band, json.dumps(band))
    check("warning is emitted instead",
          any("preferred" in w for w in payload.get("warnings", [])), json.dumps(payload.get("warnings")))
    check("compliant fixture yields zero findings", not payload["issues"], json.dumps(payload["issues"][:2]))


def test_audit_fixtures_are_coherent() -> None:
    """Verdict and findings must agree (D5)."""
    print("audit fixture coherence")
    for name in ("audit_valid.json", "audit_aligned.json"):
        audit = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        severities = {f["severity"] for f in audit["findings"]}
        verdict = audit["verdict"]
        if severities & {"critical", "major"}:
            expected = "FAIL"
        elif "minor" in severities:
            expected = "PASS_WITH_MINOR_EDITS"
        else:
            expected = "PASS"
        check(f"{name}: verdict {verdict} matches its findings", verdict == expected,
              f"expected {expected}")


def test_fixture_halves_are_balanced() -> None:
    """The reference fixture is copied downstream, so it must model 前后均衡 (D6)."""
    print("fixture shape")
    payload = json.loads(validate("blueprint_valid.json", "--json").stdout)
    first, second = payload["metrics"]["first_half_turns"], payload["metrics"]["second_half_turns"]
    check(f"halves are roughly even ({first}/{second})",
          min(first, second) / max(first, second) >= 0.6)
    check("no unevenness warning on the reference fixture",
          not any("uneven" in w for w in payload["warnings"]), json.dumps(payload["warnings"]))


def test_cross_check() -> None:
    print("blind cross-check (R8)")
    diverged = run(CROSS_CHECK, str(FIXTURES / "blueprint_valid.json"), str(FIXTURES / "audit_valid.json"), "--json")
    payload = json.loads(diverged.stdout)
    check("unrecoverable point detected",
          [row["number"] for row in payload["unrecoverable"]] == [5], diverged.stdout)
    check("unintended detail detected", len(payload["unintended_target"]) == 1, diverged.stdout)
    check("divergence exits non-zero", diverged.returncode == 1)

    aligned = run(CROSS_CHECK, str(FIXTURES / "blueprint_valid.json"), str(FIXTURES / "audit_aligned.json"), "--json")
    payload = json.loads(aligned.stdout)
    check("aligned maps pass", payload["ok"] is True and payload["matched"] == 10, aligned.stdout)
    check("aligned exits zero", aligned.returncode == 0)

    # D4: proximity alone must not pair points up, or a missing point and an unplanned detail
    # cancel each other out and the tool reports a clean result.
    sys.path.insert(0, str(SKILLS / "shared"))
    from cross_check import compare  # noqa: PLC0415 - imported here to keep the suite standalone

    def plan(anchors, kind="name"):
        return {"items": [{"number": i + 1, "type": kind, "target": f"t{i}",
                           "evidence": f"e{i}", "turn_index": a} for i, a in enumerate(anchors)]}

    def seen(anchors, kind="option"):
        return {"blind_information_map": [{"seq": i + 1, "type": kind, "evidence": f"o{i}",
                                           "turn_index": a} for i, a in enumerate(anchors)]}

    result = compare(plan([20, 21]), seen([20, 22]))
    check("off-by-one with mismatched type is not silently paired",
          [r["number"] for r in result["unrecoverable"]] == [2] and len(result["unintended_target"]) == 1,
          json.dumps(result))

    result = compare(plan([10, 11]), seen([11], kind="name"))
    check("exact anchor claims its row, so the right point is blamed",
          [r["number"] for r in result["unrecoverable"]] == [1], json.dumps(result))

    result = compare({"items": [{"number": 1, "type": "name", "target": "x",
                                 "evidence": "e", "turn_index": "bad"}]}, seen([0]))
    check("malformed anchor never matches a real row",
          result["matched"] == 0 and result["ok"] is False, json.dumps(result))

    result = compare(plan([4, 8]), seen([5, 9], kind="name"))
    check("same-type off-by-one drift still matches", result["ok"] is True, json.dumps(result))


def test_render_report() -> None:
    print("report rendering (R7)")
    audit = json.loads((FIXTURES / "audit_valid.json").read_text(encoding="utf-8"))
    result = run(RENDER, str(FIXTURES / "audit_valid.json"))
    check("renders without error", result.returncode == 0, result.stderr)
    for needle in ("# Audit Result", "## Information Map", "## Overall Score"):
        check(f"report contains {needle!r}", needle in result.stdout)
    check("score is taken from the data, not hardcoded",
          f"{audit['score']['total']}/100" in result.stdout, result.stdout[:200])
    check("verdict is humanised",
          audit["verdict"].replace("_", " ") in result.stdout, result.stdout[:200])
    check("no findings renders explicitly rather than as an empty section",
          "No findings." in result.stdout if not audit["findings"] else True)


def test_archive_samples_do_not_crash() -> None:
    print("archive regression")
    archive = sorted((ROOT / "material" / "归档").glob("*.json"))
    if not archive:
        print("  SKIP  no archived samples found")
        return
    crashed = []
    for path in archive:
        result = run(METRICS, str(path), "--json")
        if result.returncode not in (0, 1) or not result.stdout.strip():
            crashed.append(path.name)
        else:
            try:
                json.loads(result.stdout)
            except json.JSONDecodeError:
                crashed.append(path.name)
    check(f"audit_metrics.py survives all {len(archive)} archived samples", not crashed, ", ".join(crashed))

    # These four are genuinely defective; the metrics script must report rather than accept them.
    known_bad = {
        "snap_003_scripts_topic3678.json": "empty turns",
        "snap_006_scripts_topic3691.json": "891 dialogue words",
        "snap_018_scripts_topic8168.json": "18 dialogue turns",
        "snap_083_scripts_topic7187.json": "speaker4 present",
    }
    for name, why in known_bad.items():
        path = ROOT / "material" / "归档" / name
        if not path.exists():
            continue
        payload = json.loads(run(METRICS, str(path), "--json").stdout)
        flagged = not payload["assessable"] or payload["issues"]
        check(f"{name} flagged ({why})", bool(flagged))


def main() -> int:
    for suite in (
        test_schemas_are_valid,
        test_warning_does_not_fail,
        test_new_checks_catch_defects,
        test_malformed_turns_stay_reportable,
        test_grouping_cannot_be_faked,
        test_spelled_name_rule_not_vacuous,
        test_metrics_absent_when_unmeasured,
        test_typical_band_is_not_a_finding,
        test_audit_fixtures_are_coherent,
        test_fixture_halves_are_balanced,
        test_cross_check,
        test_render_report,
        test_archive_samples_do_not_crash,
    ):
        suite()
    print()
    if failures:
        print(f"{len(failures)} failure(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
