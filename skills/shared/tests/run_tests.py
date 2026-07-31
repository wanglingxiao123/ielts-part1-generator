#!/usr/bin/env python3
"""Contract regression tests. Run: python3 shared/tests/run_tests.py

Covers the four checks added to validate_part1.py, the warning-downgrade behaviour, the blind
cross-check, and that the archived samples do not crash the deterministic metrics script.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent
SKILLS = SHARED.parent
ROOT = SKILLS.parent
FIXTURES = HERE / "fixtures"

# Assets live in pools now (`skills/generate/`, `skills/audit/`), one directory per subject inside
# each, and the schemas are no longer shared -- each pool carries the ones its side needs. That split
# is the blindness boundary expressed as files: the audit pool has no blueprint schema at all.
#
# Located by glob rather than by name so this suite keeps working when a second subject appears, and
# so a moved directory fails loudly here rather than silently skipping a check.
def _one(pool: str, pattern: str) -> Path:
    matches = sorted((SKILLS / pool).glob(pattern))
    if len(matches) != 1:
        raise SystemExit("expected exactly one %s under skills/%s, found %d"
                         % (pattern, pool, len(matches)))
    return matches[0]


VALIDATE = _one("generate", "*/scripts/validate_part1.py")
METRICS = _one("audit", "*/scripts/audit_metrics.py")
CROSS_CHECK = SHARED / "cross_check.py"
RENDER = SHARED / "render_audit_report.py"
GENERATE_SCHEMAS = VALIDATE.parents[1] / "schemas"
AUDIT_SCHEMAS = METRICS.parents[1] / "schemas"


def _schema(name: str) -> Path:
    """A schema by filename, from whichever pool holds it."""
    for directory in (GENERATE_SCHEMAS, AUDIT_SCHEMAS):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise SystemExit("schema %s not found in either pool" % name)

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
            Draft7Validator.check_schema(json.loads(_schema(name).read_text(encoding="utf-8")))
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
        errors = list(Draft7Validator(json.loads(_schema(schema).read_text(encoding="utf-8"))).iter_errors(data))
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


def test_closing_rules_match_the_real_corpus() -> None:
    """A closing that omits the Part 2 pointer must not be rejected.

    This rule was written as a hard error and had no test. It rejected 18 of the 30 closings in
    the archived papers -- including all 12 that use the "part one" naming, none of which mention
    part 2 at all -- so the generator burned every retry attempting to satisfy a convention the
    source material does not follow. The corpus is the authority here, which is why this test
    reads it rather than asserting against a fixture.
    """
    print("closing rules match the real corpus")
    import copy
    import tempfile

    base = json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8"))
    turns = base["listening_material_parts"][0]["script"]["turns"]
    closing_index = max(i for i, t in enumerate(turns) if t["speaker"] == "speaker1")
    scratch = Path(tempfile.mkdtemp())

    # Taken verbatim from the archive: the "part one" form, which never points at part 2.
    for label, closing in (
        ("omits the part 2 pointer",
         "That is the end of part one. You now have one minute to check your answers to part one."),
        ("uses a comma before 'turn to'",
         "That is the end of section one. You now have half a minute to check your answers now, "
         "turn to section two."),
    ):
        variant = copy.deepcopy(base)
        variant["listening_material_parts"][0]["script"]["turns"][closing_index]["text"] = closing
        path = scratch / "material.json"
        path.write_text(json.dumps(variant, ensure_ascii=False), encoding="utf-8")
        result = run(VALIDATE, str(path), "--blueprint", str(FIXTURES / "blueprint_valid.json"),
                     "--json")
        parsed = json.loads(result.stdout) if result.stdout.strip() else {}
        errors = [e for e in (parsed.get("errors") or []) if "Part/Section 2" in e]
        check(f"real closing that {label} is not an error", not errors, str(errors))

    # The genuinely broken case must still fail: no checking time at all.
    variant = copy.deepcopy(base)
    variant["listening_material_parts"][0]["script"]["turns"][closing_index]["text"] = (
        "That is the end of section one. Now turn to section two.")
    path = scratch / "material.json"
    path.write_text(json.dumps(variant, ensure_ascii=False), encoding="utf-8")
    parsed = json.loads(run(VALIDATE, str(path), "--blueprint",
                            str(FIXTURES / "blueprint_valid.json"), "--json").stdout)
    check("a closing with no checking time is still rejected",
          any("checking time" in e for e in (parsed.get("errors") or [])),
          str(parsed.get("errors")))


def test_mode_and_split_rules_match_the_real_corpus() -> None:
    """Two more rules that rejected real papers and drove regeneration.

    `once only` belongs to the whole-test preamble: 7/7 full-mode openings in the archive carry
    it, 0/20 short-mode ones do, so requiring it unconditionally was unsatisfiable in short mode.
    The split rule accepted only first_end 5 or 6, but the real papers split at (4,5)x9, (6,7)x8,
    (5,6)x6, (3,4)x2, (7,8)x1 -- 14/27 passing, versus 26/27 once a contiguous 3-7 is allowed.
    The spec says "如 1-5 / 6-10", where 如 introduces an example.
    """
    print("narration mode and split rules match the real corpus")
    import copy
    import tempfile

    base = json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8"))
    blueprint = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))
    turns = base["listening_material_parts"][0]["script"]["turns"]
    narr = [i for i, t in enumerate(turns) if t["speaker"] == "speaker1"]
    scratch = Path(tempfile.mkdtemp())

    def verdict(material: dict, bp: dict) -> dict:
        mp, bpp = scratch / "m.json", scratch / "b.json"
        mp.write_text(json.dumps(material, ensure_ascii=False), encoding="utf-8")
        bpp.write_text(json.dumps(bp, ensure_ascii=False), encoding="utf-8")
        out = run(VALIDATE, str(mp), "--blueprint", str(bpp), "--json").stdout
        return json.loads(out) if out.strip() else {}

    # A short-mode opening with no "once only" is what 20 of the 27 real papers look like.
    short_bp = copy.deepcopy(blueprint)
    short_bp["narration_mode"] = "short"
    short = copy.deepcopy(base)
    short["listening_material_parts"][0]["script"]["turns"][narr[0]]["text"] = (
        "Part one. You will hear a conversation between a woman and a letting agent. "
        "First, you have some time to look at questions 1 to 5. "
        "Now listen carefully and answer questions 1 to 5.")
    got = verdict(short, short_bp)
    check("short-mode opening without 'once only' is not an error",
          not [e for e in (got.get("errors") or []) if "once only" in e],
          str(got.get("errors")))

    # But a full-mode opening still must carry it: that is where the spec puts it.
    full_bp = copy.deepcopy(blueprint)
    full_bp["narration_mode"] = "full"
    stripped = copy.deepcopy(base)
    stripped["listening_material_parts"][0]["script"]["turns"][narr[0]]["text"] = (
        turns[narr[0]]["text"].replace("once only", "one time"))
    got = verdict(stripped, full_bp)
    check("full-mode opening without 'once only' is still rejected",
          any("once only" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))

    # The most common real split (1-4 / 5-10) used to be rejected outright.
    shifted = copy.deepcopy(base)
    sturns = shifted["listening_material_parts"][0]["script"]["turns"]
    sturns[narr[0]]["text"] = re.sub(r"1 to \d+", "1 to 4", sturns[narr[0]]["text"])
    sturns[narr[1]]["text"] = re.sub(r"\d+ to 10", "5 to 10", sturns[narr[1]]["text"])
    got = verdict(shifted, blueprint)
    check("a 1-4/5-10 split is not rejected for its range",
          not [e for e in (got.get("errors") or []) if "contiguous groups" in e],
          str(got.get("errors")))

    # A non-contiguous split is still a real defect: question 5 would belong to no group.
    broken = copy.deepcopy(base)
    bturns = broken["listening_material_parts"][0]["script"]["turns"]
    bturns[narr[0]]["text"] = re.sub(r"1 to \d+", "1 to 4", bturns[narr[0]]["text"])
    bturns[narr[1]]["text"] = re.sub(r"\d+ to 10", "7 to 10", bturns[narr[1]]["text"])
    got = verdict(broken, blueprint)
    check("a non-contiguous split is still rejected",
          any("contiguous groups" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))


def test_indirect_confirmation_is_optional() -> None:
    """Requiring a paraphrase cycle in every material contradicted the spec and the corpus.

    §4B-4 asks for 2-3 distraction cycles chosen from five mechanisms; it never singles this one
    out. Measured over the 27 usable archived papers: 先说后改 in 24, a qualifier in 21, an
    indirect reference in only 4. The schema nonetheless listed `indirect_confirmation` as
    required, so a live batch spent all three generation attempts failing on
    "indirect answer term must occur before its reference phrase" and returned one material short.

    What must NOT relax: when the field IS present, the answer term still has to be spoken before
    the phrase referring back to it. That is the 命题铁律 (§4B-4) -- the key has to exist verbatim
    in the audio -- and it is the reason the check exists at all.
    """
    print("indirect_confirmation is optional")
    import copy
    import tempfile

    blueprint = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp())

    def verdict(bp: dict) -> dict:
        path = scratch / "b.json"
        path.write_text(json.dumps(bp, ensure_ascii=False), encoding="utf-8")
        out = run(VALIDATE, str(FIXTURES / "material_valid.json"), "--blueprint", str(path),
                  "--json").stdout
        return json.loads(out) if out.strip() else {}

    without = copy.deepcopy(blueprint)
    without.pop("indirect_confirmation", None)
    got = verdict(without)
    check("a blueprint with no indirect_confirmation is accepted",
          got.get("ok") is True, str(got.get("errors")))

    # Distraction density is still enforced separately, so dropping this one cannot yield a
    # material with no distractors at all.
    thin = copy.deepcopy(without)
    for item in thin["items"]:
        item["distractor"] = False
    got = verdict(thin)
    check("distractor density is still required without it",
          any("2-3 distractor" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))

    # And the iron rule survives: a reference phrase preceding its answer term is still an error.
    reversed_pair = copy.deepcopy(blueprint)
    reversed_pair["indirect_confirmation"] = {
        "answer_term": blueprint["indirect_confirmation"]["reference_phrase"],
        "reference_phrase": blueprint["indirect_confirmation"]["answer_term"],
    }
    got = verdict(reversed_pair)
    check("an answer term after its reference is still rejected",
          any("before its reference phrase" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))


def test_remaining_rules_do_not_reject_real_papers() -> None:
    """The client's audit: "真题能过的，校验就应该过" -- confirm no over-strict rule is left.

    Every remaining rule that appends to `errors` was measured against the 27 usable archived
    papers (31 minus the 4 known-bad ones). Five rejected real papers and are fixed here; this
    test pins each fix with the concrete paper shape that failed, so the numbers cannot silently
    regress. Percentages are of the 27:

    | rule                          | rejected | verdict                                     |
    |-------------------------------|----------|---------------------------------------------|
    | spelling sequence present     |  14 (52%)| downgraded to a warning                     |
    | blueprint split == {5,6}      |  20 (74%)| widened to 3-7, matching the sibling rule   |
    | short narration 70-110 words  |   1 ( 4%)| ceiling raised to 115 (real max is 111)     |
    | each half >= 8 turns          |   1 ( 4%)| floor lowered to 7 (real min is 7)          |
    | numeric detail present        |   1 ( 4%)| \\b\\d+\\b -> \\d ("80th" is a numeral)        |
    | question-range regex          |   1 ( 4%)| \\s+ -> \\s* ("questions1~4" occurs)          |

    Left hard on purpose, and NOT relaxed: exactly 2 main speakers + 1 narrator, exactly 10 items,
    contiguous question groups, dialogue 450-750 words / 20-48 turns, and every output-schema rule.
    The one paper still rejected (snap_038) opens with a worked EXAMPLE question, giving it four
    narrator turns; that is a real structural difference from what we generate, not an over-strict
    rule, so the rule stands.
    """
    print("remaining rules do not reject real papers")
    import copy
    import tempfile

    base = json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8"))
    blueprint = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))
    turns = base["listening_material_parts"][0]["script"]["turns"]
    narr = [i for i, t in enumerate(turns) if t["speaker"] == "speaker1"]
    scratch = Path(tempfile.mkdtemp())

    def verdict(material: dict, bp: dict) -> dict:
        mp, bpp = scratch / "m.json", scratch / "b.json"
        mp.write_text(json.dumps(material, ensure_ascii=False), encoding="utf-8")
        bpp.write_text(json.dumps(bp, ensure_ascii=False), encoding="utf-8")
        out = run(VALIDATE, str(mp), "--blueprint", str(bpp), "--json").stdout
        return json.loads(out) if out.strip() else {}

    # 1. A 1-4/5-10 split, declared consistently in BOTH narration and blueprint. This is the
    # commonest real split (9/27) and used to pass the narration rule and then fail the blueprint
    # one, so the generator could not satisfy both at once.
    shifted = copy.deepcopy(base)
    sturns = shifted["listening_material_parts"][0]["script"]["turns"]
    sturns[narr[0]]["text"] = re.sub(r"1 to \d+", "1 to 4", sturns[narr[0]]["text"])
    sturns[narr[1]]["text"] = re.sub(r"\d+ to 10", "5 to 10", sturns[narr[1]]["text"])
    shifted_bp = copy.deepcopy(blueprint)
    shifted_bp["split_after"] = 4
    for item in shifted_bp["items"]:
        item["group"] = 1 if item["number"] <= 4 else 2
    got = verdict(shifted, shifted_bp)
    check("a consistent 1-4/5-10 split is accepted by BOTH the narration and blueprint rules",
          not [e for e in (got.get("errors") or []) if "split" in e.lower()],
          str(got.get("errors")))

    # But a blueprint that splits somewhere else than the narration announced is still an error:
    # the candidate would be told to answer 1-5 while the blueprint groups 1-4.
    disagreeing = copy.deepcopy(blueprint)
    disagreeing["split_after"] = 4
    for item in disagreeing["items"]:
        item["group"] = 1 if item["number"] <= 4 else 2
    got = verdict(base, disagreeing)
    check("a blueprint split that contradicts the narration is still rejected",
          any("split_after" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))

    # 2. No letter-by-letter spelling: 14/27 real papers carry none, so it is advisory now.
    plain = copy.deepcopy(base)
    pturns = plain["listening_material_parts"][0]["script"]["turns"]
    for turn in pturns:
        turn["text"] = re.sub(r"\b(?:[A-Z]\s*[-,]\s*){2,}[A-Z]\b|\b[A-Z](?:-[A-Z]){2,}\b"
                              r"|\bdouble\s+[A-Z]\b", "Sutcliff", turn["text"])
    got = verdict(plain, blueprint)
    check("a script with no spelling sequence is a warning, not an error",
          not [e for e in (got.get("errors") or []) if "spelling" in e]
          and any("spelling" in w for w in (got.get("warnings") or [])),
          str(got.get("errors")))

    # What replaces it as the hard rule: a name-typed item must exist and one must be confirmed.
    # That is the property a 填空题 needs, and it is NOT relaxed.
    nameless = copy.deepcopy(blueprint)
    for item in nameless["items"]:
        if item["type"] == "name":
            item["type"], item["confirmed"] = "option", False
    got = verdict(base, nameless)
    check("a blueprint with no confirmed name-typed item is still rejected",
          any("spelled-name" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))

    # 3. "80th" as the only numeral (snap_002's real shape): `\b\d+\b` found nothing there.
    ordinal = copy.deepcopy(base)
    oturns = ordinal["listening_material_parts"][0]["script"]["turns"]
    for i, turn in enumerate(oturns):
        if turn["speaker"] != "speaker1":
            oturns[i]["text"] = re.sub(r"\d+", lambda m: m.group(0) + "th", turn["text"])
    got = verdict(ordinal, blueprint)
    check("a digit glued to a suffix counts as numeric information",
          not [e for e in (got.get("errors") or []) if "numeric information" in e],
          str(got.get("errors")))

    # A dialogue with no digit at all is still rejected: the spec §3 requires one.
    wordy = copy.deepcopy(base)
    wturns = wordy["listening_material_parts"][0]["script"]["turns"]
    for turn in wturns:
        if turn["speaker"] != "speaker1":
            turn["text"] = re.sub(r"[\d$£€]", "", turn["text"])
    got = verdict(wordy, blueprint)
    check("a dialogue with no digit at all is still rejected",
          any("numeric information" in e for e in (got.get("errors") or [])),
          str(got.get("errors")))

    # 4. "questions1~4" with no space -- snap_022_scripts_topic8163's real wording.
    tight = copy.deepcopy(base)
    tturns = tight["listening_material_parts"][0]["script"]["turns"]
    tturns[narr[0]]["text"] = re.sub(r"questions\s+1\s+to\s+(\d+)", r"questions1~\1",
                                     tturns[narr[0]]["text"], flags=re.I)
    tturns[narr[1]]["text"] = re.sub(r"questions\s+(\d+)\s+to\s+10", r"questions\1~10",
                                     tturns[narr[1]]["text"], flags=re.I)
    got = verdict(tight, blueprint)
    check("an un-spaced question range is still parsed as a split",
          not [e for e in (got.get("errors") or []) if "contiguous" in e or "split_after" in e],
          str(got.get("errors")))


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


def test_cross_check_pairs_on_evidence_text() -> None:
    """A false 听不出来 tells a writer to rewrite a point that is fine (D8).

    Measured defect: a booking reference stated at turn 32 ("Your booking reference is HGR482")
    and confirmed at turn 33 ("I've noted HGR482") was reported unrecoverable. Pairing used only
    `turn_index` within ANCHOR_TOLERANCE plus exact `type` equality, and the auditor had recorded
    the code as `number` where the blueprint called it `name` -- so the exact-anchor pass could not
    fire (32 != 33) and the tolerant pass was gated on the type that disagreed. The evidence string
    was character-identical the whole time and was never looked at.

    Both directions are covered here: the point must now pair, AND the two properties that keep
    the tool from blaming the wrong item must still hold (they are asserted in test_cross_check
    and re-asserted below against the evidence-aware passes).
    """
    print("cross-check pairs on evidence text (D8)")
    sys.path.insert(0, str(SKILLS / "shared"))
    from cross_check import compare  # noqa: PLC0415 - keeps the suite standalone

    def planned(**over):
        item = {"number": 1, "type": "name", "target": "HGR482",
                "evidence": "Your booking reference is HGR482", "turn_index": 32}
        item.update(over)
        return {"items": [item]}

    def heard(**over):
        row = {"seq": 1, "type": "number", "evidence": "Your booking reference is HGR482.",
               "turn_index": 33}
        row.update(over)
        return {"blind_information_map": [row]}

    # ---- the HGR482 shape itself, and the ways the auditor writes the same span -------------
    for label, audit in (
        ("identical evidence, adjacent turn, disagreeing type", heard()),
        ("auditor recorded only the answer span", heard(evidence="HGR482")),
        ("auditor recorded the confirmation sentence", heard(evidence="I've noted HGR482")),
        ("evidence differs only by punctuation and case",
         heard(evidence="your booking reference is hgr482!")),
    ):
        result = compare(planned(), audit)
        check(f"recovered: {label}",
              result["ok"] is True and result["matched"] == 1, json.dumps(result))

    # Identity outranks proximity: the same span recorded far away is still the same span, and an
    # anchor the auditor got wrong is a worse reason to cry 听不出来 than no anchor at all.
    result = compare(planned(), heard(turn_index=2))
    check("identical evidence pairs even when the anchor is far off",
          result["ok"] is True, json.dumps(result))
    result = compare(planned(), heard(turn_index="not an int"))
    check("identical evidence pairs even with no usable auditor anchor",
          result["ok"] is True, json.dumps(result))

    # ---- the other direction: evidence must not become a licence to pair anything ------------
    result = compare(planned(), heard(evidence="the price is 40 pounds"))
    check("an unrelated row at an adjacent turn with a different type stays unpaired",
          result["ok"] is False and [r["number"] for r in result["unrecoverable"]] == [1],
          json.dumps(result))

    # A short target must not be found inside every row. `MIN_TARGET_MATCH` is what stops it.
    result = compare(
        planned(target="6", evidence="six", turn_index=10),
        {"blind_information_map": [{"seq": 1, "type": "number",
                                    "evidence": "sixty-six pounds", "turn_index": 11}]},
    )
    check("a 1-character target does not claim a row by substring",
          result["ok"] is False, json.dumps(result))

    # And the two documented properties survive the new pass order, stated against evidence text
    # rather than the synthetic anchors used in test_cross_check.
    two_planned = {"items": [
        {"number": 1, "type": "name", "target": "Sutcliff",
         "evidence": "the surname is Sutcliff", "turn_index": 20},
        {"number": 2, "type": "number", "target": "40 kilograms",
         "evidence": "the limit is 40 kilograms", "turn_index": 21},
    ]}
    result = compare(two_planned, {"blind_information_map": [
        {"seq": 1, "type": "name", "evidence": "the surname is Sutcliff", "turn_index": 20},
        {"seq": 2, "type": "option", "evidence": "email or post", "turn_index": 22},
    ]})
    check("planned [20,21] vs observed [20,22] still reports one missing AND one unintended",
          [r["number"] for r in result["unrecoverable"]] == [2]
          and len(result["unintended_target"]) == 1, json.dumps(result))

    result = compare({"items": [
        {"number": 1, "type": "name", "target": "a1", "evidence": "point one", "turn_index": 10},
        {"number": 2, "type": "name", "target": "b2", "evidence": "point two", "turn_index": 11},
    ]}, {"blind_information_map": [
        {"seq": 1, "type": "name", "evidence": "point two", "turn_index": 11},
    ]})
    check("the item whose evidence the auditor recorded is the one credited",
          [r["number"] for r in result["unrecoverable"]] == [1], json.dumps(result))


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
        test_closing_rules_match_the_real_corpus,
        test_mode_and_split_rules_match_the_real_corpus,
        test_indirect_confirmation_is_optional,
        test_remaining_rules_do_not_reject_real_papers,
        test_grouping_cannot_be_faked,
        test_spelled_name_rule_not_vacuous,
        test_metrics_absent_when_unmeasured,
        test_typical_band_is_not_a_finding,
        test_audit_fixtures_are_coherent,
        test_fixture_halves_are_balanced,
        test_cross_check,
        test_cross_check_pairs_on_evidence_text,
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
