#!/usr/bin/env python3
"""Contract regression tests. Run: python3 shared/tests/run_tests.py

Covers the four checks added to validate_part1.py, the warning-downgrade behaviour, the blind
cross-check, and that the archived samples do not crash the deterministic metrics script.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
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
# The question stage is a SECOND skill in the same pool, so both globs above and here must keep
# matching exactly one file: `validate_questions_part1.py` was named to stay outside
# `*/scripts/validate_part1.py` rather than turning `_one`'s assertion into a two-match failure.
QUESTION_VALIDATE = _one("generate", "*/scripts/validate_questions_part1.py")
METRICS = _one("audit", "*/scripts/audit_metrics.py")
# The question audit is a SECOND skill in the audit pool, and it ships a schema and no script -- so it
# is located by its schema, the way the feasibility pool is. It cannot be reached through `METRICS`:
# that path derives from audit_metrics.py's own skill directory and stops there.
QUESTION_AUDIT_SCHEMAS = _one("audit", "*/schemas/audit_questions.schema.json").parent
CROSS_CHECK = SHARED / "cross_check.py"
RENDER = SHARED / "render_audit_report.py"
GENERATE_SCHEMAS = VALIDATE.parents[1] / "schemas"
QUESTION_SCHEMAS = QUESTION_VALIDATE.parents[1] / "schemas"
AUDIT_SCHEMAS = METRICS.parents[1] / "schemas"
# The feasibility pool ships a schema and no script, so it is located by its schema rather than by a
# scripts/ glob like the other two. `_one` doubles as the pool-membership assertion: a second skill
# directory, or the directory moved into another pool, exits here instead of skipping the checks.
FEASIBILITY_SCHEMAS = _one("feasibility", "*/schemas/feasibility.schema.json").parent

# Every pool that carries schemas. Named once, because three places need the same list and the failure
# mode of updating two of them is a schema that is never checked -- which reads as passing.
#
# `QUESTION_SCHEMAS` is a separate entry rather than being covered by `GENERATE_SCHEMAS`: the pool
# holds two skills now and each carries its own schemas/ directory, so a directory derived from one
# skill's validator does not reach the other's. Omitting it leaves question_package.schema.json
# unresolvable by `_schema` and unvalidated by `test_schemas_are_valid` -- which reads as passing.
# `QUESTION_AUDIT_SCHEMAS` is the same case in the audit pool, which now also holds two skills.
SCHEMA_DIRS = (GENERATE_SCHEMAS, QUESTION_SCHEMAS, AUDIT_SCHEMAS, QUESTION_AUDIT_SCHEMAS,
               FEASIBILITY_SCHEMAS)


def _schema(name: str) -> Path:
    """A schema by filename, from whichever pool holds it."""
    for directory in SCHEMA_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise SystemExit("schema %s not found in any pool" % name)

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


def validate_mutated(mutate, *extra: str, base: str = "blueprint_valid.json") -> subprocess.CompletedProcess:
    """Validate a one-off variant of a fixture, built in memory and written to a temp file.

    For cases that do not deserve a committed fixture: `build_fixtures.py` owns fixtures/ entirely,
    so a hand-placed file there disappears on the next rebuild, and a builder variant only earns its
    place if more than one test reads it.
    """
    payload = copy.deepcopy(json.loads((FIXTURES / base).read_text(encoding="utf-8")))
    mutate(payload)
    path = Path(tempfile.mkdtemp()) / base
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return run(VALIDATE, str(FIXTURES / "material_valid.json"), "--blueprint", str(path), *extra)


SCHEMA_NAMES = (
    "material.schema.json",
    "blueprint.schema.json",
    "blueprint.read.schema.json",
    "question_package.schema.json",
    "audit.schema.json",
    "audit_questions.schema.json",
    "feasibility.schema.json",
)


def _validator(name: str):
    """A validator that can resolve this schema's cross-file ``$ref``s.

    ``blueprint.schema.json`` is ``allOf: [blueprint.read.schema.json, <v2 narrowing>]``, so it is
    meaningless without the referenced document. A bare ``Draft7Validator(schema)`` would try to
    fetch ``blueprint.read.schema.json`` over the network and fail -- or worse, in a future
    jsonschema, resolve to nothing and report zero errors on everything. The store is built from the
    directory rather than listing files, so a schema added later is resolvable without editing this.
    """
    from jsonschema import Draft7Validator, RefResolver

    store: dict[str, object] = {}
    for directory in SCHEMA_DIRS:
        for path in sorted(directory.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            store[path.name] = document
            store[path.resolve().as_uri()] = document
    target = _schema(name)
    document = store[target.name]
    return Draft7Validator(document, resolver=RefResolver(
        base_uri=target.resolve().as_uri(), referrer=document, store=store))


def _schema_errors(name: str, data: object) -> list[str]:
    return [e.message for e in _validator(name).iter_errors(data)]


def test_schemas_are_valid() -> None:
    print("schemas")
    try:
        from jsonschema import Draft7Validator
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return
    for name in SCHEMA_NAMES:
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
        ("blueprint (read side)", blueprint, "blueprint.read.schema.json"),
        ("audit", audit, "audit.schema.json"),
    ):
        errors = _schema_errors(schema, data)
        check(f"{label} fixture matches its schema", not errors, "; ".join(errors[:3]))


def test_write_and_read_schemas_disagree_where_they_should() -> None:
    """The two blueprint schemas answer different questions, so they must differ on real inputs.

    Requirement: "明确 schema 是写侧 schema，或实现真正的 v1/v2 读侧 schema，不要让文档与行为冲突."
    Before this split, ``blueprint.schema.json`` described itself as the write-side contract while an
    ``else`` branch required v1's ``question_type_coverage`` -- so it also validated the archived
    records it claimed not to govern. Doc and behaviour said different things and nothing caught it.

    Every case below is a shape that must land differently on the two documents, or land the same on
    both for a stated reason. A single merged schema cannot satisfy this table: JSON Schema branches
    only intersect, so one document admitting an archived record also lets the generator emit one.
    """
    print("write-side vs read-side blueprint schema")
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return

    WRITE, READ = "blueprint.schema.json", "blueprint.read.schema.json"
    v1 = json.loads((FIXTURES / "blueprint_v1_legacy.json").read_text(encoding="utf-8"))
    v2 = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))

    def mutate(base: dict, fn) -> dict:
        payload = copy.deepcopy(base)
        fn(payload)
        return payload

    def add_response_form(bp: dict) -> None:
        bp["items"][0]["response_form"] = "word"

    def both_coverage_names(bp: dict) -> None:
        bp["completion_layout_coverage"] = bp["question_type_coverage"]

    def drop_coverage(bp: dict) -> None:
        del bp["question_type_coverage"]

    def to_mc(bp: dict) -> None:
        bp["items"][0]["item_form"] = "multiple_choice"

    def null_group(bp: dict) -> None:
        bp["items"][0]["form_group"] = None

    def drop_response_form(bp: dict) -> None:
        del bp["items"][0]["response_form"]

    def rename_coverage(bp: dict) -> None:
        bp["question_type_coverage"] = bp.pop("completion_layout_coverage")

    def drop_version(bp: dict) -> None:
        del bp["blueprint_schema_version"]

    def bogus_layout(bp: dict) -> None:
        bp["items"][0]["item_form"] = "essay"

    def version_three(bp: dict) -> None:
        bp["blueprint_schema_version"] = 3

    # (label, payload, want_write_ok, want_read_ok)
    cases = (
        # The split itself: one real archived record, two different and both-correct answers.
        ("the real v1 archive", v1, False, True),
        ("the v2 fixture", v2, True, True),
        # v1 leniency is bounded. A v1 carrying v2's item fields is a v2 that lost its version field,
        # and reading it as a lenient v1 would skip every v2 recomputation on it.
        ("v1 + a v2 item field", mutate(v1, add_response_form), False, False),
        ("v1 carrying both coverage names", mutate(v1, both_coverage_names), False, False),
        ("v1 with no coverage map at all", mutate(v1, drop_coverage), False, False),
        ("v1 + a layout that never existed", mutate(v1, bogus_layout), False, False),
        # Requirement 2's second half: v2 is strict on BOTH sides. Read leniency is for records that
        # predate the contract, not for records that declare it and then break it.
        ("v2 + multiple_choice", mutate(v2, to_mc), False, False),
        ("v2 + a null form_group", mutate(v2, null_group), False, False),
        ("v2 missing response_form", mutate(v2, drop_response_form), False, False),
        ("v2 using the v1 coverage name", mutate(v2, rename_coverage), False, False),
        # The one line that turns the read contract into a write contract.
        ("a v2-shaped record with no version field", mutate(v2, drop_version), False, False),
        # 'Readable' means v1 or v2. An unknown version is to be surfaced, not rendered through
        # whichever field name it happens to carry.
        ("version 3", mutate(v2, version_three), False, False),
    )
    for label, payload, write_ok, read_ok in cases:
        for schema, want in ((WRITE, write_ok), (READ, read_ok)):
            errors = _schema_errors(schema, payload)
            side = "write" if schema is WRITE else "read"
            check(f"{side}: {label} -> {'accepted' if want else 'rejected'}",
                  (not errors) == want, "; ".join(errors[:3]) or "accepted unexpectedly")

    # Asserting the DIRECTION of the difference, not merely that a difference exists: the write side
    # must be strictly the narrower of the two. If a future edit made the write schema accept
    # something the read schema rejects, every case above could still pass while the two documents
    # had quietly swapped roles.
    write_only = [label for label, payload, w, r in cases if w and not r]
    check("nothing is writable but unreadable", not write_only, str(write_only))
    check("the archived record is the case that separates them",
          bool(_schema_errors(WRITE, v1)) and not _schema_errors(READ, v1))


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
        # v2: narrator_window_id is derived from the same split, so moving the split moves it too.
        # Leaving it stale would make this test fail on a window mismatch it did not mean to create
        # -- which is the check doing its job, since a stale window is exactly what it hunts for.
        item["narrator_window_id"] = 1 if item["number"] <= 4 else 2
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

    def recover(payload: dict) -> dict:
        """Rebuild completion_layout_coverage so it agrees with the mutated item_forms.

        Without this the coverage/item_form cross-check fires and the blueprint is rejected for
        a reason the case is not about -- which is exactly how a grouping test goes vacuous.
        """
        coverage: dict = {}
        for item in payload["items"]:
            coverage.setdefault(item["item_form"], []).append(item["number"])
        payload["completion_layout_coverage"] = coverage
        return payload

    # `note` is now the only non-table item_form, so "a shared label on points that cannot become
    # a table" and "the group is made of notes" are the same case -- they were two only while
    # multiple_choice existed. Merged rather than kept as synonyms.
    notes_only = copy.deepcopy(base)
    for item in notes_only["items"]:
        item["item_form"] = "note"
    recover(notes_only)

    mixed = copy.deepcopy(base)
    for item, form in zip(mixed["items"][:3], ("form", "table", "note")):
        item["item_form"], item["form_group"] = form, "A"
    recover(mixed)

    # Assert on the REASON, not just returncode 1. Every mutation here also happens to be
    # rejectable on other grounds, so a bare exit-code check would keep passing even if
    # validate_grouping stopped working -- this test was already passing vacuously once, when
    # multiple_choice was deleted from ITEM_FORMS and the enum check began doing its job for it.
    for label, payload, expected in (
        ("no form/table group exists, only labelled notes", notes_only,
         "needs one homogeneous form/table form_group"),
        ("form_group mixes item_form values", mixed,
         "mixes item_form values"),
    ):
        path = scratch / "blueprint.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = run(VALIDATE, str(FIXTURES / "material_valid.json"), "--blueprint", str(path))
        check(f"rejected: {label}",
              result.returncode == 1 and expected in result.stdout, result.stdout)


def test_blueprint_version_is_read_not_guessed() -> None:
    """Version comes from the version field alone, never from which fields happen to be present.

    Guessing "no response_form, so probably v1" would let a v2 that simply forgot the field pass as
    a v1, and the v2 checks would then never run on the records they exist for. An unrecognised
    version is an error for the same reason: falling back to v1 turns a typo into a silent downgrade.
    """
    print("blueprint version detection")
    for label, fixture, extra, want_pass, expected in (
        ("v2 fixture passes with no flag", "blueprint_valid.json", (), True, ""),
        ("v1 archive passes with --allow-v1", "blueprint_v1_legacy.json", ("--allow-v1",), True, ""),
        ("v1 archive is rejected by default", "blueprint_v1_legacy.json", (), False,
         "blueprint_schema_version is missing"),
        ("an unknown version is an error, not a fallback to v1", "blueprint_bad_version.json", (),
         False, "only 2 is supported"),
    ):
        result = validate(fixture, *extra)
        passed = result.returncode == 0
        check(f"{label}", passed == want_pass and (expected in result.stdout),
              result.stdout)

    # A v2 missing one required field must FAIL rather than be read as a v1 that never had it.
    # Built in memory rather than committed as a fixture: `build_fixtures.py` is the only writer of
    # fixtures/, so a hand-placed file there would be deleted on the next rebuild.
    def strip_response_form(bp: dict) -> None:
        for item in bp["items"]:
            item.pop("response_form")
    result = validate_mutated(strip_response_form)
    check("a v2 missing response_form fails instead of degrading to v1",
          result.returncode == 1 and "response_form" in result.stdout, result.stdout)


def test_v1_leniency_is_scoped_to_the_layout_enum() -> None:
    """v1 reading tolerates `multiple_choice` and nothing else, and never for a v2 record.

    Requirements: "v1 读取允许历史 MC；默认新生成和所有 v2 仍严格禁止 MC."

    Two failure directions, both real. Too strict and every archived record looks malformed halfway
    through -- which is what happened when the coverage-key check exempted MC inline while the
    per-item check did not. Too lenient and `--allow-v1` becomes a blanket "skip the layout rules"
    switch: new generation would be one forgotten flag away from writing a layout the client removed.

    So leniency is keyed on the READ version, not on the flag. `--allow-v1` only decides whether a
    versionless record is an error; it does not widen what a v2 record may contain. And the widening
    stops at the enum: a layout that never existed is still an error under v1, and the homogeneity
    rule is not relaxed at all -- measured, all 9 MC points in every archived record and capture in
    this repo have `form_group: null`, so no real record needs a mixed group tolerated.
    """
    print("v1 layout leniency is scoped")

    v1_ok = validate("blueprint_v1_legacy.json", "--allow-v1")
    # Asserted on the MESSAGES, not on the exit code. The archived record has three MC points and a
    # `multiple_choice` coverage key; a bare returncode check would keep passing if leniency were
    # removed from one of the two checks and re-added as some other error the fixture also trips.
    check("the archived v1 record's multiple_choice draws no layout complaint",
          v1_ok.returncode == 0
          and "item_form must be one" not in v1_ok.stdout
          and "unknown layout" not in v1_ok.stdout,
          v1_ok.stdout)

    def to_mc(bp: dict) -> None:
        """One v2 item becomes MC, with coverage moved to match.

        Coverage is kept in agreement on purpose: otherwise the cross-check fires and the case would
        pass on a reason it is not about, which is how this kind of test goes vacuous.
        """
        bp["items"][0]["item_form"] = "multiple_choice"
        for numbers in bp["completion_layout_coverage"].values():
            if 1 in numbers:
                numbers.remove(1)
        bp["completion_layout_coverage"]["multiple_choice"] = [1]

    for label, extra in (("by default", ()), ("even with --allow-v1", ("--allow-v1",))):
        result = validate_mutated(to_mc, *extra)
        check(f"a v2 record carrying multiple_choice is rejected {label}",
              result.returncode == 1
              and "item_form must be one of ['form', 'note', 'table']" in result.stdout
              and "unknown layout 'multiple_choice'" in result.stdout,
              result.stdout[:600])

    def to_invented_layout(bp: dict) -> None:
        bp["items"][4]["item_form"] = "matching"
        bp["question_type_coverage"]["matching"] = bp["question_type_coverage"].pop("multiple_choice")

    result = validate_mutated(to_invented_layout, "--allow-v1", base="blueprint_v1_legacy.json")
    check("a v1 record carrying a layout that never existed is still rejected",
          result.returncode == 1 and "item_form must be one" in result.stdout, result.stdout[:600])

    def mc_joins_a_named_group(bp: dict) -> None:
        """The MC point stops being standalone and joins the form group.

        This is the case leniency deliberately does NOT cover: the group is then heterogeneous, and
        relaxing that would weaken a v2 constraint to buy a compatibility no real record needs.

        Coverage is left alone -- item 5 keeps its `multiple_choice` layout, so it stays under the
        `multiple_choice` key and the cross-check has nothing to say. Only the grouping changes.
        """
        bp["items"][4]["form_group"] = "A"

    result = validate_mutated(mc_joins_a_named_group, "--allow-v1", base="blueprint_v1_legacy.json")
    check("v1 leniency does not extend to a group mixing multiple_choice with form",
          result.returncode == 1 and "mixes item_form values" in result.stdout, result.stdout[:600])


def test_response_form_derivation() -> None:
    """Table-driven, because the derivation is where this field can quietly go wrong.

    Two rules are easy to get backwards and both are covered here: hyphens are NOT split (word_limit
    counts a hyphenated compound as one word, and serving word_limit is the field's whole purpose),
    and `numeric` requires every token to be a pure number form rather than merely to contain a
    digit. The second is the same trap as `address` sitting in NUMERIC_TYPES: under "contains a
    digit" the postcode BT14 9BJ reads as numeric when it is plainly something to spell.
    """
    print("response_form / qr027_class derivation")
    sys.path.insert(0, str(VALIDATE.parent))
    import validate_part1 as vp

    for target, want_form, want_class in (
        ("Room 4B", "phrase", "mixed"),            # §6.4 Q14's own example
        ("3 Oakwood Lane", "phrase", "mixed"),     # a `type: "address"` mixed case
        ("118 Fordyce", "phrase", "mixed"),        # the fixture's address point
        ("BT14 9BJ", "phrase", "mixed"),           # postcode: NOT numeric
        ("9.30", "numeric", "numeric"),
        ("07840051963", "numeric", "numeric"),
        ("£38", "numeric", "numeric"),
        ("two-thirty", "word", "lexical"),         # hyphen not split
        ("two-bedroom", "word", "lexical"),        # hyphen not split
        ("Anna Woods", "phrase", "lexical"),
        ("park", "word", "lexical"),
        ("Tuesdays at 6 p.m.", "phrase", "mixed"),
    ):
        got_form = vp.derive_response_form(target)
        got_class = vp.derive_qr027_class(target)
        check(f"{target!r} -> {want_form}/{want_class}",
              (got_form, got_class) == (want_form, want_class),
              f"got {got_form}/{got_class}")

    # The two axes are not the same axis. If someone merges the functions, this fails.
    check("response_form and qr027_class split on different axes",
          vp.derive_response_form("Room 4B") == "phrase" and vp.derive_qr027_class("Room 4B") == "mixed",
          "Room 4B must be phrase (two tokens) but mixed (digits among letters)")


def test_v2_declarations_are_recomputed() -> None:
    """The three v2 fields are only worth having if a wrong declaration is caught.

    §9.2 names the likely failure directly: implementing narrator_window_id as "read the field,
    check it is 1 or 2" hands SC-019's window attribution back to the model's own say-so. Each case
    below asserts the SPECIFIC message, so the test cannot pass on some unrelated rejection.
    """
    print("v2 declarations are recomputed, not trusted")
    for label, fixture, expected in (
        ("response_form contradicted by its target", "blueprint_bad_response_form.json",
         "response_form declares 'numeric' but '118 Fordyce' derives 'phrase'"),
        ("narrator_window_id contradicted by the narration", "blueprint_bad_window.json",
         "narrator_window_id declares 2 but item 1 falls in window 1"),
        ("answer_category outside the taxonomy", "blueprint_bad_answer_category.json",
         "is not in the taxonomy"),
    ):
        result = validate(fixture)
        check(f"rejected: {label}", result.returncode == 1 and expected in result.stdout,
              result.stdout)

    # Naming the item is part of the requirement: an error that says only "response_form is wrong"
    # leaves a ten-point blueprint with no indication of which point to fix.
    result = validate("blueprint_bad_response_form.json")
    check("the error names the offending item",
          "items[1]" in result.stdout, result.stdout)


def test_target_must_fit_some_rubric() -> None:
    """A target no standard rubric can carry is rejected here, where it can still be fixed.

    Measured, 2026-08-08: a blueprint carried target `9 and 1` (1 word + 2 numbers) from "The driver
    calls between 9 and 1." The question stage may neither replace a blueprint target nor edit the
    Script, so it had no legal move: every rubric rejected the answer, narrowing it to `9-1` broke
    blueprint fidelity, and loosening the group's rubric broke the marking key. The material spent its
    entire time budget cycling and delivered nothing. Two numbers is the case to assert because it is
    the one the loosest rubric refuses while the *word* count still looks harmless.
    """
    print("blueprint targets must fit some standard rubric")
    sys.path.insert(0, str(VALIDATE.parent))
    import validate_part1 as vp

    for target, want in (
        ("9 and 1", False),                 # 1 word + 2 numbers: no rubric permits two numbers
        ("9-1", True),                      # the same range as one token
        ("between 9 and 1 o'clock", False),
        ("three words plus 7", True),       # exactly the loosest rubric's bound
        ("four whole words plus 7", False),  # one word over it
        ("two-bedroom", True),              # hyphen is one word (AR-014)
        ("Tuesdays at 6 p.m.", True),
    ):
        got = vp.fits_any_rubric(target)
        check(f"{target!r} fits a rubric: {want}", got == want,
              f"got {got}; budget {vp.budget_of(target)}")

    widest, max_words, allowance = vp.widest_rubric()
    check("the widest rubric is the loosest entry of WORD_LIMITS",
          (widest, max_words, allowance) == vp.WORD_LIMITS[-1] and allowance == 1,
          f"got {vp.widest_rubric()}")

    def unfittable_target(bp: dict) -> None:
        # Keep the target inside its own evidence, so the failure under test is the rubric budget and
        # not the anchor check firing first.
        bp["items"][6]["target"] = "9 and 1"
        bp["items"][6]["evidence"] = "calls between 9 and 1 every day"
        bp["items"][6]["response_form"] = "phrase"

    result = validate_mutated(unfittable_target)
    check("a target that fits no rubric is rejected at the blueprint stage",
          result.returncode == 1 and "no standard rubric permits" in result.stdout, result.stdout)
    check("the error names the item, the cost and the widest rubric",
          "items[6]" in result.stdout and "1 word(s) and 2 number(s)" in result.stdout
          and "NO MORE THAN THREE WORDS AND/OR A NUMBER" in result.stdout, result.stdout)

    # The arithmetic must be the SAME arithmetic the question stage prices rubrics with. Two copies
    # would let a target pass here and fail there, which is the loop this check exists to break.
    sys.path.insert(0, str(QUESTION_VALIDATE.parent))
    import validate_questions_part1 as vq

    check("the question validator imports this budget rather than keeping its own",
          vq.budget_of is vp.budget_of and vq.WORD_LIMITS is vp.WORD_LIMITS,
          "validate_questions_part1 must not redefine budget_of/WORD_LIMITS")


def test_group_constraints_are_distinguishable() -> None:
    """Four printed-group constraints, plus a positive cross-window layout case.

    A single error covering all five would let this test pass while four of them were broken, which
    is how stage 1's grouping test managed to be vacuous. Note constraints 3 and 4 are asserted on
    ONE fixture: they cannot be separated, because evidence turns are already required to be
    strictly increasing, so a group with non-contiguous item numbers is always also interrupted in
    the evidence sequence. Measured over every single- and double-point regrouping of the fixture
    (50 and 1125 cases): constraint 3 never fires alone. See design.md D10.
    """
    print("v2 group constraints are distinguishable")
    for label, fixture, expected in (
        ("1. every item must belong to a group", "blueprint_bad_group_missing.json",
         "form_group must be a non-empty string in v2"),
        ("2. a group must be homogeneous", "blueprint_bad_group_mixed.json",
         "mixes item_form values"),
        ("3. a group's item numbers must be contiguous", "blueprint_bad_group_split.json",
         "covers non-contiguous item numbers [1, 3, 4]"),
        ("4. a group must not be interrupted in the evidence sequence",
         "blueprint_bad_group_split.json", "are interrupted in the evidence sequence"),
    ):
        result = validate(fixture)
        check(f"rejected: {label}", result.returncode == 1 and expected in result.stdout,
              result.stdout)

    crossing = validate("blueprint_bad_group_window.json")
    check("a continuous blueprint group may cross the narrator evidence-window boundary",
          crossing.returncode == 0, crossing.stdout)

    # The four messages must actually differ. Reusing one message for two constraints would make
    # the assertions above pass while telling a reviewer nothing about which property broke.
    messages = set()
    for fixture in ("blueprint_bad_group_missing.json", "blueprint_bad_group_mixed.json",
                    "blueprint_bad_group_split.json"):
        for line in validate(fixture).stdout.splitlines():
            if line.startswith("ERROR:") and ("form_group" in line or "narrator windows" in line
                                              or "evidence sequence" in line):
                messages.add(line.split("ERROR: ")[1][:40])
    check("the group constraints report at least four distinct messages",
          len(messages) >= 4, f"got {len(messages)}: {sorted(messages)}")


def test_item_labels_are_zero_based_indices() -> None:
    """`blueprint.items[N]` must mean the array index, in every message that uses it.

    The reader parses the bracket and renders `N + 1` as the item number
    (`frontend/src/domain/validationNotes.ts`), so a 1-based label points the reviewer's jump button
    at the next point -- and item 10's label `[10]` is discarded entirely, since the parser only
    accepts 0-9. `validate_grouping` had three messages built from `item["number"]` instead of the
    enumeration index; two of them predate v2. Asserting the message text alone (as the tests above
    do) cannot see this, because both conventions contain the substring being matched.
    """
    print("item labels are 0-based array indices")

    # Two errors on the LAST item: [9] under the shared convention, [10] under the broken one --
    # and [10] is the case the frontend silently drops rather than mis-renders.
    def break_last_item(bp: dict) -> None:
        bp["items"][9]["form_group"] = ""
        bp["items"][9]["item_form"] = "multiple_choice"
    result = validate_mutated(break_last_item)
    lines = [ln for ln in result.stdout.splitlines()
             if "form_group must be a non-empty string in v2" in ln or "item_form must be one" in ln]
    check("both grouping errors label item 10 as index 9",
          len(lines) == 2 and all("blueprint.items[9]" in ln for ln in lines),
          "\n".join(lines) or result.stdout[:400])

    # Asserted on THIS result, not the item-5 one below: `[10]` is only reachable from the last item,
    # so checking it against a mid-blueprint failure would pass under either convention.
    check("no message emits an index the reader cannot parse",
          "blueprint.items[10]" not in result.stdout, result.stdout[:400])

    # The convention has to be ONE convention: the same item, broken two ways, must produce the same
    # label from `validate_grouping` and from `validate_blueprint`'s per-item loop.
    def break_item_five(bp: dict) -> None:
        bp["items"][4]["form_group"] = ""
        del bp["items"][4]["response_form"]
    result = validate_mutated(break_item_five)
    check("validate_grouping and validate_blueprint agree on the label for one item",
          "blueprint.items[4].form_group must be a non-empty string in v2" in result.stdout
          and "blueprint.items[4] missing fields: ['response_form']" in result.stdout,
          result.stdout[:600])


def test_qr027_counts_are_reported_not_enforced() -> None:
    """Stage 2 measures QR-027; stage 3 gates on it. Both halves of that are asserted here.

    The counting axis is qr027_class, not the persisted response_form: they split on token count vs
    character composition, so `Room 4B` is a phrase whose class is mixed. The same-category limit is
    <3, not <=3 -- the client's wording is "三题或以上不得...", so three already violates.
    """
    print("QR-027 counts are metrics, not a gate")
    parsed = json.loads(validate("blueprint_valid.json", "--json").stdout)
    metrics = parsed["metrics"]
    check("the counts are emitted", "qr027_largest_category" in metrics, str(metrics.keys()))
    check("the reference fixture satisfies QR-027", metrics.get("qr027_within_limits") is True,
          str({k: v for k, v in metrics.items() if k.startswith("qr027")}))
    check("no category is tested by 3+ items (<3, not <=3)",
          metrics["qr027_largest_category"] < 3, str(metrics.get("qr027_category_counts")))
    check("numeric answers stay within 4", metrics["qr027_numeric_answers"] <= 4, str(metrics))
    check("at least 4 answers require spelling", metrics["qr027_spelled_answers"] >= 4, str(metrics))

    # Not a gate yet: a blueprint that breaks QR-027 must still pass stage 2, because the exit
    # decision and its recorded justification belong to stage 3's aggregator.
    def all_one_category(bp: dict) -> None:
        for item in bp["items"]:
            item["answer_category"] = "location"
    result = validate_mutated(all_one_category, "--json")
    parsed = json.loads(result.stdout)
    check("a QR-027 violation is measured but not blocked in stage 2",
          result.returncode == 0
          and parsed["metrics"]["qr027_largest_category"] == 10
          and parsed["metrics"]["qr027_within_limits"] is False,
          result.stdout[:400])


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


def _preflight():
    """The stage 3A aggregator, imported the same way validate_part1 is (see above)."""
    sys.path.insert(0, str(VALIDATE.parent))
    import question_feasibility_preflight as pf

    return pf


def _validation_of(blueprint: str, *extra: str) -> dict:
    """A real `validate_part1.py --json` payload, parsed.

    Two construction methods are used across the preflight suites, and the split is deliberate:

    * END-TO-END (this helper) for anything whose point is that the aggregator reads the keys the
      validator actually writes. A hand-built dict cannot catch "metrics calls it something else",
      which is the single most likely way this module breaks.
    * HAND-BUILT dicts for threshold boundaries and malformed shapes. Building a real blueprint
      whose numeric answers come to exactly 5 means satisfying a dozen unrelated v2 constraints at
      the same time, and what is under test there is the aggregator's comparison, not the
      validator's counting -- which stage 2 already covers. Malformed input cannot be produced by a
      working validator at all, so constructing it is the only route.

    Neither is a shortcut for the other. Do not "tidy" one into the other.
    """
    result = validate(blueprint, "--json", *extra)
    return json.loads(result.stdout)


def _feasible() -> dict:
    """A minimal usable semantic conclusion: the three required keys, all well-typed."""
    return {"feasible": True, "reasons": [], "category_semantics_ok": True}


# A sentinel for "delete this key", which `None` cannot express: `errors: null` and "no errors key at
# all" are different malformed inputs and the aggregator reports them differently.
_ABSENT = object()


def test_preflight_three_exits() -> None:
    """AC1 / AC2 / AC3 / AC4 / AC5 / AC6: the six outcomes and the QR-027 boundaries."""
    print("preflight verdicts")
    pf = _preflight()
    valid = _validation_of("blueprint_valid.json")

    # AC1, end-to-end: blueprint_valid.json measures numeric=1, spelled=9, largest=2 -- all three
    # thresholds clear, so it is the natural PASS baseline and needs no new fixture.
    verdict = pf.preflight(valid, _feasible())
    check("clean v2 + feasible semantics -> PASS", verdict.outcome == pf.PASS,
          f"{verdict.outcome}: {verdict.reasons}")
    check("PASS carries the qr027 snapshot for the delivery report",
          verdict.qr027.get("qr027_numeric_answers") == 1 and verdict.qr027.get("qr027_spelled_answers") == 9,
          repr(verdict.qr027))

    def with_metrics(**changes) -> dict:
        """A metrics variant that keeps numeric + spelled == 10.

        Set `qr027_numeric_answers` and let `qr027_spelled_answers` follow, because the counts are
        not independent: the three QR-027 classes partition all ten items, so the aggregator now
        rejects any pair that does not sum to ten as VALIDATION_INCOMPLETE. Pinning one and deriving
        the other keeps these boundary cases *arithmetically reachable* -- a test that had to
        violate the invariant to reach a threshold would be testing an input the system can never
        actually receive.
        """
        payload = copy.deepcopy(valid)
        if "qr027_numeric_answers" in changes and "qr027_spelled_answers" not in changes:
            changes["qr027_spelled_answers"] = 10 - changes["qr027_numeric_answers"]
        payload["metrics"].update(changes)
        return payload

    # AC2: both sides of every threshold. The passing side must assert PASS, not merely "this rule
    # was not reported" -- otherwise the boundary is only half tested.
    #
    # Note what is NOT here: an independent `spelled 4 pass / spelled 3 breach` pair. Under
    # numeric + spelled == 10, `spelled == 3` forces `numeric == 7`, which already breaches the
    # numeric rule -- so "spelled below the minimum" is not independently reachable, and a test
    # constructing it would be asserting on an input the validator cannot emit. The spelled rule is
    # instead covered where it IS reachable: `numeric 6 / spelled 4` is the last pair satisfying it
    # and `numeric 7 / spelled 3` the first violating it, both listed below. This is a real
    # consequence of the invariant, not a gap being papered over.
    for label, changes, want_pass in (
        ("numeric 4 (spelled 6)", {"qr027_numeric_answers": 4}, True),
        ("numeric 5 (spelled 5)", {"qr027_numeric_answers": 5}, False),
        ("numeric 0 (spelled 10)", {"qr027_numeric_answers": 0}, True),
        ("numeric 6 / spelled 4 -- spelled still satisfied", {"qr027_numeric_answers": 6}, False),
        ("numeric 7 / spelled 3 -- both rules breached", {"qr027_numeric_answers": 7}, False),
        ("largest category 2", {"qr027_largest_category": 2}, True),
        ("largest category 3", {"qr027_largest_category": 3}, False),
    ):
        got = pf.preflight(with_metrics(**changes), _feasible())
        want = pf.PASS if want_pass else pf.REGENERATE_MATERIAL
        check(f"QR-027 boundary: {label} -> {want}", got.outcome == want,
              f"{got.outcome}: {got.reasons}")

    # The spelled rule must still fire on its own terms when it is violated, rather than the
    # numeric breach masking it. Both reasons are expected at numeric 7 / spelled 3.
    both = pf.preflight(with_metrics(qr027_numeric_answers=7), _feasible())
    check("numeric 7 / spelled 3 reports BOTH broken rules, not just the first",
          any("numeric" in r for r in both.reasons) and any("spelled" in r for r in both.reasons),
          repr(both.reasons))

    # `largest_category 3 -> breach` is what proves the rule is `< 3` and not `<= 3`: three items
    # sharing one category already violates QR-027's wording.
    breach = with_metrics(qr027_numeric_answers=5)
    check("breach reason names the rule, the measured value and the threshold",
          any("5" in reason and "4" in reason for reason in pf.preflight(breach, _feasible()).reasons),
          repr(pf.preflight(breach, _feasible()).reasons))

    # AC3: PASS_WITH_JUSTIFICATION requires a specific recorded reason. Structure only -- whether
    # the reason holds is reviewed at the question-audit stage.
    with_reason = dict(_feasible(),
                       qr027_exception={"requested": True, "justification": "Script offers no alternative evidence"})
    got = pf.preflight(breach, with_reason)
    check("QR-027 breach + recorded justification -> PASS_WITH_JUSTIFICATION",
          got.outcome == pf.PASS_WITH_JUSTIFICATION, f"{got.outcome}: {got.reasons}")
    check("the justification text is carried through",
          got.justification == "Script offers no alternative evidence", repr(got.justification))

    for label, exception in (
        ("absent", None),
        ("requested without text", {"requested": True}),
        ("empty text", {"requested": True, "justification": ""}),
        ("whitespace only", {"requested": True, "justification": "   \n "}),
        ("not requested", {"requested": False, "justification": "reason"}),
        ("not an object", "yes please"),
    ):
        semantics = _feasible() if exception is None else dict(_feasible(), qr027_exception=exception)
        got = pf.preflight(breach, semantics)
        check(f"QR-027 exception {label} -> REGENERATE_MATERIAL",
              got.outcome == pf.REGENERATE_MATERIAL, f"{got.outcome}: {got.reasons}")

    # AC4: the semantic layer can veto even when every deterministic check is green. This is what
    # stops "Python is all green, so generate".
    for label, semantics in (
        ("feasible: false", dict(_feasible(), feasible=False, reasons=["four points share one turn"])),
        ("category_semantics_ok: false", dict(_feasible(), category_semantics_ok=False,
                                              reasons=["item 3 is a facility, not a location"])),
    ):
        got = pf.preflight(valid, semantics)
        check(f"clean deterministic + {label} -> REGENERATE_MATERIAL",
              got.outcome == pf.REGENERATE_MATERIAL, f"{got.outcome}: {got.reasons}")
        check(f"{label} keeps the audit's own reasons",
              any("share one turn" in r or "facility" in r for r in got.reasons), repr(got.reasons))

    # AC6: an absent semantic conclusion may not be read as approval. §8.2(5) -- incomplete must
    # not be dressed up as delivered.
    got = pf.preflight(valid, None)
    check("semantics absent -> SEMANTICS_MISSING, not PASS", got.outcome == pf.SEMANTICS_MISSING,
          f"{got.outcome}: {got.reasons}")


def test_preflight_version_gate() -> None:
    """AC5 + AC11's version half. End-to-end, because the landing of the version value is upstream
    behaviour, not something this suite should restate from memory."""
    print("preflight version gate")
    pf = _preflight()

    # A v1 record read WITH --allow-v1 comes back ok:true, zero errors, zero qr027_* keys
    # (measured 2026-08-06). So the deterministic gate has nothing to say about it and the QR-027
    # gate has nothing to read: the version gate is the only thing that can stop it, with no second
    # line of defence behind it. Without --allow-v1 the record carries a "version is missing" error
    # and would be caught one gate later, which would test the wrong gate.
    v1 = _validation_of("blueprint_v1_legacy.json", "--allow-v1")
    check("the v1 path this test relies on really is error-free",
          v1["ok"] and not v1["errors"] and not [k for k in v1["metrics"] if k.startswith("qr027_")],
          json.dumps(v1["metrics"], sort_keys=True)[:300])
    got = pf.preflight(v1, _feasible())
    check("v1 (version read as 1) -> UNSUPPORTED_VERSION", got.outcome == pf.UNSUPPORTED_VERSION,
          f"{got.outcome}: {got.reasons}")
    check("v1 is NOT reported as REGENERATE_MATERIAL", got.outcome != pf.REGENERATE_MATERIAL)

    # An unrecognisable version is a different thing from an unsupported one. validate_part1.py
    # writes metrics.blueprint_schema_version = None when it could not read the value, so None
    # means "I failed to determine the version". Calling that UNSUPPORTED_VERSION would file a NEW
    # record with a corrupt version number as "this is a historical archive" -- and then nobody
    # goes and fixes the corrupt version number.
    unreadable = _validation_of("blueprint_bad_version.json")
    check("an unrecognisable version really does land as null upstream",
          unreadable["metrics"].get("blueprint_schema_version") is None,
          repr(unreadable["metrics"].get("blueprint_schema_version")))
    got = pf.preflight(unreadable, _feasible())
    check("version 3 (unreadable -> null) -> VALIDATION_INCOMPLETE",
          got.outcome == pf.VALIDATION_INCOMPLETE, f"{got.outcome}: {got.reasons}")
    check("an unreadable version is NOT UNSUPPORTED_VERSION", got.outcome != pf.UNSUPPORTED_VERSION)

    # Same for a version that is the right number but the wrong type.
    def stringify(blueprint: dict) -> None:
        blueprint["blueprint_schema_version"] = "2"

    payload = json.loads(validate_mutated(stringify, "--json").stdout)
    check('version "2" lands as null upstream too',
          payload["metrics"].get("blueprint_schema_version") is None,
          repr(payload["metrics"].get("blueprint_schema_version")))
    got = pf.preflight(payload, _feasible())
    check('version "2" -> VALIDATION_INCOMPLETE', got.outcome == pf.VALIDATION_INCOMPLETE,
          f"{got.outcome}: {got.reasons}")


def test_preflight_incomplete_validation() -> None:
    """AC10 + AC11: everything malformed on the validation side is VALIDATION_INCOMPLETE, and
    nothing raises. Hand-built dicts, because a working validator cannot emit these."""
    print("preflight malformed validation input")
    pf = _preflight()
    valid = _validation_of("blueprint_valid.json")

    # AC10: qr027_metrics always runs for version 2 (validate_part1.py:588), so a missing key means
    # the validation flow did not finish -- a system-side problem. REGENERATE_MATERIAL asserts the
    # material is unfit: it spends one outer-quota candidate swap and a whole regeneration, so
    # using it here burns a generation AND hides the real fault. Absent metrics mean "not
    # measured", not zero (deterministic/validate.py:65); read as zero, "spelled 0 < 4" would
    # condemn a material set nobody ever measured.
    for key in ("qr027_numeric_answers", "qr027_spelled_answers", "qr027_largest_category"):
        payload = copy.deepcopy(valid)
        del payload["metrics"][key]
        got = pf.preflight(payload, _feasible())
        check(f"metrics.{key} missing -> VALIDATION_INCOMPLETE",
              got.outcome == pf.VALIDATION_INCOMPLETE, f"{got.outcome}: {got.reasons}")
        check(f"metrics.{key} missing is not REGENERATE_MATERIAL",
              got.outcome != pf.REGENERATE_MATERIAL)
        check(f"the reason names {key}", any(key in reason for reason in got.reasons),
              repr(got.reasons))

    # AC11, the seven shapes. Each asserts the outcome AND that the call returned at all: raising
    # would push the decision onto a caller who can only guess from a traceback whether to
    # regenerate or to page someone.
    def metrics_with(**changes) -> dict:
        payload = copy.deepcopy(valid)
        payload["metrics"].update(changes)
        return payload

    def metrics_without(key: str) -> dict:
        payload = copy.deepcopy(valid)
        del payload["metrics"][key]
        return payload

    cases = [
        ("validation is None", None),
        ("validation is a string", "not a result"),
        ("validation is a list", [1, 2, 3]),
        ("metrics absent", {"ok": True, "errors": [], "warnings": []}),
        ("metrics is a string", {"ok": True, "errors": [], "warnings": [], "metrics": "none"}),
        ("metrics is a list", {"ok": True, "errors": [], "warnings": [], "metrics": []}),
        ("version key absent", metrics_without("blueprint_schema_version")),
        ("version is None", metrics_with(blueprint_schema_version=None)),
        ("numeric count is a string", metrics_with(qr027_numeric_answers="1")),
        ("numeric count is a float", metrics_with(qr027_numeric_answers=1.5)),
        ("numeric count is None", metrics_with(qr027_numeric_answers=None)),
        # A bool IS an int in Python, so `True` would silently compare as 1 against the threshold.
        # A validator that hands over a boolean where a count belongs has not counted anything.
        ("numeric count is a bool", metrics_with(qr027_numeric_answers=True)),
        ("spelled count is a dict", metrics_with(qr027_spelled_answers={})),
        ("largest category is a list", metrics_with(qr027_largest_category=[2])),
    ]
    for label, payload in cases:
        try:
            got = pf.preflight(payload, _feasible())
        except Exception as exc:  # noqa: BLE001 - any raise is the failure being tested for
            check(f"{label} -> VALIDATION_INCOMPLETE", False, f"raised {type(exc).__name__}: {exc}")
            continue
        check(f"{label} -> VALIDATION_INCOMPLETE", got.outcome == pf.VALIDATION_INCOMPLETE,
              f"{got.outcome}: {got.reasons}")
        check(f"{label} is neither UNSUPPORTED_VERSION nor REGENERATE_MATERIAL",
              got.outcome not in (pf.UNSUPPORTED_VERSION, pf.REGENERATE_MATERIAL), got.outcome)
        check(f"{label} says missing or invalid",
              any("missing" in reason or "invalid" in reason for reason in got.reasons),
              repr(got.reasons))

    # A deterministic error is a different matter: that IS a material defect. `ok` has to move with
    # it -- the validator derives one from the other, so setting only `errors` would now (correctly)
    # be caught as a contradiction rather than reaching the deterministic gate.
    payload = copy.deepcopy(valid)
    payload["errors"] = ["blueprint.items[3].target is empty"]
    payload["ok"] = False
    got = pf.preflight(payload, _feasible())
    check("deterministic errors -> REGENERATE_MATERIAL", got.outcome == pf.REGENERATE_MATERIAL,
          f"{got.outcome}: {got.reasons}")
    check("the deterministic error text is carried into the reasons",
          any("items[3].target" in reason for reason in got.reasons), repr(got.reasons))


def test_preflight_missing_semantics() -> None:
    """AC12: everything malformed on the feasibility side is SEMANTICS_MISSING -- never an
    exception, never REGENERATE_MATERIAL, and the reason keeps missing apart from invalid."""
    print("preflight malformed semantics input")
    pf = _preflight()
    valid = _validation_of("blueprint_valid.json")

    def without(key: str) -> dict:
        payload = _feasible()
        del payload[key]
        return payload

    # "missing" (the key is absent -> upstream produced nothing) and "invalid" (the key is there
    # with the wrong type -> upstream produced the wrong thing) send an investigator to different
    # places. One outcome drives control flow; the reasons carry the diagnosis.
    missing_cases = [
        ("feasibility is None", None),
        ("feasible absent", without("feasible")),
        ("reasons absent", without("reasons")),
        ("category_semantics_ok absent", without("category_semantics_ok")),
    ]
    invalid_cases = [
        ("feasibility is a string", "looks fine to me"),
        ("feasibility is a list", [{"feasible": True}]),
        ("feasibility is a bool", True),
        # The trap this rules out: "false" is a non-empty string and therefore truthy, so a
        # truthiness test would read an unfeasible verdict as feasible -- the verdict inverts.
        ('feasible is the string "false"', dict(_feasible(), feasible="false")),
        ("feasible is 0", dict(_feasible(), feasible=0)),
        ("feasible is None", dict(_feasible(), feasible=None)),
        ("category_semantics_ok is a string", dict(_feasible(), category_semantics_ok="false")),
        ("reasons is a string", dict(_feasible(), reasons="one big reason")),
        ("reasons is None", dict(_feasible(), reasons=None)),
    ]
    for label, semantics, marker in ([(a, b, "missing") for a, b in missing_cases]
                                     + [(a, b, "invalid") for a, b in invalid_cases]):
        try:
            got = pf.preflight(valid, semantics)
        except Exception as exc:  # noqa: BLE001 - any raise is the failure being tested for
            check(f"{label} -> SEMANTICS_MISSING", False, f"raised {type(exc).__name__}: {exc}")
            continue
        check(f"{label} -> SEMANTICS_MISSING", got.outcome == pf.SEMANTICS_MISSING,
              f"{got.outcome}: {got.reasons}")
        check(f"{label} is neither PASS nor REGENERATE_MATERIAL",
              got.outcome not in (pf.PASS, pf.PASS_WITH_JUSTIFICATION, pf.REGENERATE_MATERIAL),
              got.outcome)
        check(f"{label} reason is marked {marker}",
              any(marker in reason for reason in got.reasons), repr(got.reasons))

    # A malformed qr027_exception is NOT one of these. It is a *request*, and a request that says
    # nothing is simply no request, so it falls through to "no justification recorded". The three
    # required keys are *conclusions*: an unusable conclusion means the verdict cannot be reached.
    breach = copy.deepcopy(valid)
    breach["metrics"].update(qr027_numeric_answers=9, qr027_spelled_answers=1)
    got = pf.preflight(breach, dict(_feasible(), qr027_exception="please"))
    check("a malformed qr027_exception falls through to REGENERATE_MATERIAL, not SEMANTICS_MISSING",
          got.outcome == pf.REGENERATE_MATERIAL, f"{got.outcome}: {got.reasons}")


def test_preflight_thresholds_have_one_source() -> None:
    """AC7: the thresholds live in validate_part1 and are read at call time.

    `from validate_part1 import QR027_MAX_NUMERIC` binds the value into the importing module at
    import time, after which patching the source module does nothing -- measured. A test written
    against that form PASSES while proving nothing, which is worse than no test: it hands out a
    false guarantee about exactly the property it claims to check.
    """
    print("preflight threshold single source")
    pf = _preflight()
    vp = __import__("validate_part1")
    valid = _validation_of("blueprint_valid.json")
    payload = copy.deepcopy(valid)
    # spelled follows numeric: numeric + spelled == 10 is enforced ahead of the threshold gate, so a
    # 5/9 pair would be rejected as VALIDATION_INCOMPLETE and never reach the comparison under test.
    payload["metrics"].update(qr027_numeric_answers=5, qr027_spelled_answers=5)

    # Three points, not two. "Patch it wider and it passes" alone would still be satisfied by an
    # implementation that hardcodes a number which happens to equal the patched value; patching
    # narrower as well pins that down.
    original = vp.QR027_MAX_NUMERIC
    try:
        check("unpatched (max 4): numeric 5 is a breach",
              pf.preflight(payload, _feasible()).outcome == pf.REGENERATE_MATERIAL)
        vp.QR027_MAX_NUMERIC = 5
        check("patched to 5: numeric 5 now passes",
              pf.preflight(payload, _feasible()).outcome == pf.PASS,
              f"{pf.preflight(payload, _feasible()).outcome}")
        vp.QR027_MAX_NUMERIC = 0
        check("patched to 0: numeric 5 is a breach again",
              pf.preflight(payload, _feasible()).outcome == pf.REGENERATE_MATERIAL)
    finally:
        # Restoring matters beyond tidiness: everything below runs in this same process, including
        # the within_limits cross-check, which is only meaningful at the real thresholds.
        vp.QR027_MAX_NUMERIC = original
    check("threshold restored", vp.QR027_MAX_NUMERIC == original)

    # Cross-check against the composite boolean the validator already computes. The aggregator
    # deliberately does not read qr027_within_limits -- it could not then say which rule missed, by
    # how much, and a PASS_WITH_JUSTIFICATION report needs exactly that. But the two must agree at
    # the real thresholds; asserting this while a threshold is patched would be wrong, since
    # divergence is then the expected behaviour.
    # Every pair sums to ten, for the same reason as above -- and that is also why (1, 3, x) is
    # absent: it is not a state the validator can produce.
    for numeric, spelled, largest in ((1, 9, 2), (4, 6, 2), (5, 5, 2), (7, 3, 2), (1, 9, 3)):
        probe = copy.deepcopy(valid)
        probe["metrics"].update(qr027_numeric_answers=numeric, qr027_spelled_answers=spelled,
                                qr027_largest_category=largest)
        probe["metrics"]["qr027_within_limits"] = (numeric <= vp.QR027_MAX_NUMERIC
                                                   and spelled >= vp.QR027_MIN_SPELLED
                                                   and largest < vp.QR027_MAX_SAME_CATEGORY)
        got = pf.preflight(probe, _feasible())
        agrees = (got.outcome == pf.PASS) == probe["metrics"]["qr027_within_limits"]
        check(f"rule-by-rule verdict agrees with qr027_within_limits at ({numeric},{spelled},{largest})",
              agrees, f"{got.outcome} vs within_limits={probe['metrics']['qr027_within_limits']}")


def test_preflight_ok_and_errors_must_agree() -> None:
    """`ok` and `errors` are shape-checked AND cross-checked, because the validator derives one from
    the other (`"ok": not errors`, validate_part1.py:638).

    Before this gate existed, `ok: false` with an empty `errors` list reached PASS, and so did
    `errors: None`, `errors: "boom"`, and either key being absent -- all measured. The dangerous
    direction is that one: a payload whose `errors` list was emptied in transit would be reported as
    ready for question generation.
    """
    print("preflight ok/errors consistency")
    pf = _preflight()
    valid = _validation_of("blueprint_valid.json")

    def variant(**changes) -> dict:
        payload = copy.deepcopy(valid)
        for key, value in changes.items():
            if value is _ABSENT:
                payload.pop(key, None)
            else:
                payload[key] = value
        return payload

    # POSITIVE: the two agreeing, in both directions.
    check("ok: true + errors: [] -> PASS", pf.preflight(valid, _feasible()).outcome == pf.PASS)
    agreeing = variant(ok=False, errors=["blueprint.items[3].target is empty"])
    got = pf.preflight(agreeing, _feasible())
    check("ok: false + a real error -> REGENERATE_MATERIAL", got.outcome == pf.REGENERATE_MATERIAL,
          f"{got.outcome}: {got.reasons}")

    # NEGATIVE: contradictions and malformed shapes. None of these may reach PASS.
    for label, payload in (
        ("ok: false but errors is empty", variant(ok=False)),
        ("ok: true but errors is non-empty", variant(ok=True, errors=["boom"])),
        ("ok absent", variant(ok=_ABSENT)),
        ("errors absent", variant(errors=_ABSENT)),
        ("errors is None", variant(errors=None)),
        ("errors is a string", variant(errors="boom")),
        ("errors is a dict", variant(errors={"0": "boom"})),
        ("ok is None", variant(ok=None)),
        # Same trap as `feasible`: "false" is a non-empty string and therefore truthy, so a
        # truthiness test here would read a failed validation as a passing one.
        ('ok is the string "false"', variant(ok="false")),
        ("ok is 0", variant(ok=0)),
        ("ok is 1 with errors present", variant(ok=1, errors=["boom"])),
        # These three exist because the consistency check alone cannot catch them, so they are the
        # only cases that hold the *type* checks in place. `1 == True` and `{} `/`None` are falsy, so
        # each of these agrees with its partner under coercion and then sails past `if errors:` --
        # measured: with the isinstance checks removed all three reach PASS.
        ("ok is 1 with errors empty (agrees under coercion)", variant(ok=1)),
        ("ok is 0 with errors present (agrees under coercion)", variant(ok=0, errors=["boom"])),
        ("errors is an empty dict (falsy non-list)", variant(errors={})),
    ):
        try:
            got = pf.preflight(payload, _feasible())
        except Exception as exc:  # noqa: BLE001 - any raise is the failure being tested for
            check(f"{label} -> VALIDATION_INCOMPLETE", False, f"raised {type(exc).__name__}: {exc}")
            continue
        check(f"{label} -> VALIDATION_INCOMPLETE", got.outcome == pf.VALIDATION_INCOMPLETE,
              f"{got.outcome}: {got.reasons}")
        check(f"{label} never reaches PASS",
              got.outcome not in (pf.PASS, pf.PASS_WITH_JUSTIFICATION), got.outcome)

    # The contradiction has to be reported as a contradiction, not as one of the two readings.
    got = pf.preflight(variant(ok=False), _feasible())
    check("the contradiction reason mentions both ok and errors",
          any("ok" in reason and "errors" in reason for reason in got.reasons), repr(got.reasons))


def test_preflight_counts_are_range_and_sum_checked() -> None:
    """The QR-027 counts must lie in 0..10 and numeric + spelled must be exactly 10.

    Both follow from measured upstream behaviour rather than from taste: a blueprint carries exactly
    ten items (validate_part1.py:504), and `derive_qr027_class` returns numeric | mixed | lexical for
    every possible target -- including the empty string and bare punctuation, probed -- so the three
    classes partition the items totally and `spelled` (lexical + mixed) plus `numeric` must account
    for all ten. A pair that does not sum to ten therefore does not describe ten items, and
    comparing it against the thresholds would be arithmetic on numbers measuring something else.

    Before this gate, `numeric: -1` and `numeric: 0, spelled: 10`-with-a-broken-partner both reached
    PASS.
    """
    print("preflight count range and sum")
    pf = _preflight()
    valid = _validation_of("blueprint_valid.json")

    def counts(numeric, spelled, largest=2) -> dict:
        payload = copy.deepcopy(valid)
        payload["metrics"].update(qr027_numeric_answers=numeric, qr027_spelled_answers=spelled,
                                  qr027_largest_category=largest)
        return payload

    # POSITIVE: every pair that sums to ten and is inside range must get past this gate. The two
    # ends (0/10 and 10/0) are included because an off-by-one in the range check would reject them.
    for numeric in range(0, 11):
        got = pf.preflight(counts(numeric, 10 - numeric), _feasible())
        check(f"numeric {numeric} / spelled {10 - numeric} is accepted as measurable",
              got.outcome != pf.VALIDATION_INCOMPLETE, f"{got.outcome}: {got.reasons}")
    for largest in (0, 1, 10):
        got = pf.preflight(counts(1, 9, largest), _feasible())
        check(f"largest_category {largest} is in range", got.outcome != pf.VALIDATION_INCOMPLETE,
              f"{got.outcome}: {got.reasons}")

    # NEGATIVE: out of range. Note -1 and 11 are the just-outside values, not absurd ones -- an
    # off-by-one in the bound is the realistic mistake.
    for label, payload in (
        ("numeric -1", counts(-1, 11)),
        ("numeric 11", counts(11, -1)),
        ("spelled -1", counts(11, -1)),
        ("largest_category -1", counts(1, 9, -1)),
        ("largest_category 11", counts(1, 9, 11)),
        ("largest_category 99", counts(1, 9, 99)),
    ):
        got = pf.preflight(payload, _feasible())
        check(f"{label} -> VALIDATION_INCOMPLETE", got.outcome == pf.VALIDATION_INCOMPLETE,
              f"{got.outcome}: {got.reasons}")
        check(f"{label} never reaches PASS",
              got.outcome not in (pf.PASS, pf.PASS_WITH_JUSTIFICATION), got.outcome)

    # NEGATIVE: in range individually, but the pair cannot describe ten items.
    for numeric, spelled in ((1, 2), (0, 0), (5, 4), (4, 5), (10, 10), (0, 9), (2, 9)):
        got = pf.preflight(counts(numeric, spelled), _feasible())
        check(f"numeric {numeric} + spelled {spelled} = {numeric + spelled} -> VALIDATION_INCOMPLETE",
              got.outcome == pf.VALIDATION_INCOMPLETE, f"{got.outcome}: {got.reasons}")
        check(f"the sum {numeric + spelled} is never read as a material defect",
              got.outcome != pf.REGENERATE_MATERIAL, got.outcome)

    got = pf.preflight(counts(1, 2), _feasible())
    check("the sum reason states both counts and the expected total",
          any("1" in r and "2" in r and "10" in r for r in got.reasons), repr(got.reasons))

    # The invariant really does hold on live validator output -- the whole gate rests on this.
    metrics = valid["metrics"]
    check("the live validator satisfies numeric + spelled == 10",
          metrics["qr027_numeric_answers"] + metrics["qr027_spelled_answers"] == 10,
          f"{metrics['qr027_numeric_answers']} + {metrics['qr027_spelled_answers']}")


def test_preflight_rejection_must_be_explained() -> None:
    """A false verdict with no usable reason is SEMANTICS_MISSING, not REGENERATE_MATERIAL.

    REGENERATE_MATERIAL spends an outer-quota candidate swap and a full material regeneration, and
    the material stage is then meant to act on the reasons. With none recorded there is nothing to
    act on, so the replacement is as likely to repeat the same fault. An unexplained rejection is
    also indistinguishable from an audit that crashed and defaulted its output to false -- which is
    exactly the case that must not consume a regeneration.
    """
    print("preflight rejections carry a reason")
    pf = _preflight()
    valid = _validation_of("blueprint_valid.json")

    # POSITIVE: a rejection with a real reason still regenerates, and the reason survives.
    for key in ("feasible", "category_semantics_ok"):
        semantics = dict(_feasible(), reasons=["items 3, 4 and 5 share one turn"])
        semantics[key] = False
        got = pf.preflight(valid, semantics)
        check(f"{key}: false with a real reason -> REGENERATE_MATERIAL",
              got.outcome == pf.REGENERATE_MATERIAL, f"{got.outcome}: {got.reasons}")
        check(f"{key}: false keeps the reason text",
              any("share one turn" in reason for reason in got.reasons), repr(got.reasons))

    # POSITIVE: a mixed list counts as explained as long as one entry is a usable string.
    got = pf.preflight(valid, dict(_feasible(), feasible=False, reasons=[None, "", "the real reason"]))
    check("one usable string among junk entries still counts as explained",
          got.outcome == pf.REGENERATE_MATERIAL, f"{got.outcome}: {got.reasons}")

    # POSITIVE: an empty reasons list is fine when nothing is being rejected. The rule must not
    # turn into "reasons may never be empty" -- a clean PASS has nothing to explain.
    check("feasible: true with empty reasons -> PASS",
          pf.preflight(valid, _feasible()).outcome == pf.PASS)

    # NEGATIVE: nothing in the list carries information. [""] and ["   "] are non-empty lists, which
    # is why the check strips rather than merely testing the list's length.
    for key in ("feasible", "category_semantics_ok"):
        for label, reasons in (("empty list", []), ("empty string", [""]),
                               ("whitespace only", ["   \n\t "]), ("None entry", [None]),
                               ("all junk", [None, "", "  ", 0]), ("numeric entry", [42]),
                               ("nested list", [["reason"]])):
            semantics = dict(_feasible(), reasons=reasons)
            semantics[key] = False
            got = pf.preflight(valid, semantics)
            check(f"{key}: false with {label} -> SEMANTICS_MISSING",
                  got.outcome == pf.SEMANTICS_MISSING, f"{got.outcome}: {got.reasons}")
            check(f"{key}: false with {label} does not spend a regeneration",
                  got.outcome != pf.REGENERATE_MATERIAL, got.outcome)

    got = pf.preflight(valid, dict(_feasible(), feasible=False))
    check("the unexplained-rejection reason names the field and says a reason is required",
          any("feasible" in r and "reason" in r for r in got.reasons), repr(got.reasons))


def test_preflight_outcome_names_are_pinned() -> None:
    """The three client-named exits are matched verbatim downstream, so a rename must fail here.

    The earlier round's PASS_WITH_RATIONALE / BLOCK were renamed by the client:. BLOCK read like a
    terminal state, while REGENERATE_MATERIAL names the next action and which layer owns it.
    """
    print("preflight outcome names")
    pf = _preflight()
    for name, want in (("PASS", "PASS"),
                       ("PASS_WITH_JUSTIFICATION", "PASS_WITH_JUSTIFICATION"),
                       ("REGENERATE_MATERIAL", "REGENERATE_MATERIAL"),
                       ("SEMANTICS_MISSING", "SEMANTICS_MISSING"),
                       ("VALIDATION_INCOMPLETE", "VALIDATION_INCOMPLETE"),
                       ("UNSUPPORTED_VERSION", "UNSUPPORTED_VERSION")):
        check(f"{name} is spelled {want!r}", getattr(pf, name) == want, repr(getattr(pf, name)))

    verdict = pf.preflight(_validation_of("blueprint_valid.json"), _feasible())
    as_dict = verdict.as_dict()
    check("Verdict.as_dict is JSON-serialisable with the four documented keys",
          set(as_dict) == {"outcome", "reasons", "qr027", "justification"}
          and json.loads(json.dumps(as_dict))["outcome"] == pf.PASS,
          repr(sorted(as_dict)))


# --- layer 1: the feasibility schema IS the contract -----------------------------------------------
#
# design.md AC2 splits the same contract across three layers, and this is the authoritative one. The
# backend's `_feasibility_envelope` restates its values-and-types subset in plain Python because
# `jsonschema` is a dev dependency the container never installs; the preflight's
# `_semantics_problem` restates a smaller subset again as a last-resort check at the verdict. When the
# three disagree, THIS file wins and the others are the bug.

def _feasibility_case(**over) -> dict:
    base = {"feasible": True, "reasons": ["item 4 reads cleanly"], "category_semantics_ok": True}
    for key, value in over.items():
        if value is _ABSENT:
            base.pop(key, None)
        else:
            base[key] = value
    return base


def test_feasibility_schema_contract() -> None:
    """The whole shape: three required keys, the two `if/then` rules, and no unknown keys."""
    print("feasibility schema")
    try:
        import jsonschema  # noqa: F401 - availability probe
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return

    def errors(data: object) -> list[str]:
        return _schema_errors("feasibility.schema.json", data)

    check("a feasible reply with a reason validates", errors(_feasibility_case()) == [],
          repr(errors(_feasibility_case())))
    # The anti-tightening assertion, and the counterpart of stage 3A's "feasible:true + empty reasons
    # must PASS": `reasons` is required to be PRESENT always and non-empty only on a rejection. Without
    # this case, `minItems: 1` could move up to the property itself and every negative case below
    # would still pass.
    check("a feasible reply with an empty reasons list validates",
          errors(_feasibility_case(reasons=[])) == [])

    for label, data in (
        ("feasible missing", _feasibility_case(feasible=_ABSENT)),
        ("reasons missing", _feasibility_case(reasons=_ABSENT)),
        ("category_semantics_ok missing", _feasibility_case(category_semantics_ok=_ABSENT)),
        # `"false"` explicitly, because it is a measured false-green shape: read by truthiness it
        # becomes a PASS, so the material would ship as feasible while the model said the opposite.
        ("feasible is the string 'false'", _feasibility_case(feasible="false")),
        ("category_semantics_ok is 1", _feasibility_case(category_semantics_ok=1)),
        ("reasons is a bare string", _feasibility_case(reasons="item 6 is ambiguous")),
        ("reasons carries a non-string", _feasibility_case(reasons=[{"item": 6}])),
        # `[""]` explicitly: a non-empty list carrying zero information, the second measured
        # false-green shape. The schema catches it with `minLength: 1` on the items.
        ("reasons carries an empty string", _feasibility_case(reasons=[""])),
        ("feasible:false with no reasons", _feasibility_case(feasible=False, reasons=[])),
        ("category_semantics_ok:false with no reasons",
         _feasibility_case(category_semantics_ok=False, reasons=[])),
        ("an unknown top-level key", _feasibility_case(confidence=0.4)),
        ("not an object at all", ["feasible"]),
    ):
        check(f"schema rejects: {label}", errors(data) != [])


def test_feasibility_schema_qr027_exception() -> None:
    """design.md D2's four positives and ten negatives, one row per case.

    ONE ROW IS AN INTENDED DIVERGENCE: a whitespace-only `justification` (negative 7) passes here and
    is rejected by `_feasibility_envelope`'s `strip()`. That is not a gap to be closed by adding a
    `pattern` -- expressing "not blank" in JSON Schema means keeping a regex aligned with Python's
    `str.strip()` semantics, and each layer here uses the tool it is good at. The divergence is
    asserted below so it stays a decision rather than becoming a surprise.

    Positives 2 and 4 are the anti-tightening assertions. An earlier draft of this schema required
    `["requested", "justification"]` unconditionally, which rejects the entirely legal
    `{"requested": false}` -- declining to request an exception leaves nothing to justify. Every
    negative case below passed against that broken version.
    """
    print("feasibility schema: qr027_exception")
    try:
        import jsonschema  # noqa: F401 - availability probe
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return

    def errors(exception) -> list[str]:
        data = _feasibility_case() if exception is _ABSENT else _feasibility_case(
            qr027_exception=exception)
        return _schema_errors("feasibility.schema.json", data)

    for label, exception in (
        ("1: the key is absent", _ABSENT),
        ("2: {'requested': false}", {"requested": False}),
        ("3: requested with a justification",
         {"requested": True, "justification": "three items share one category inherently"}),
        ("4: not requested, justification present anyway",
         {"requested": False, "justification": "n/a"}),
    ):
        check(f"schema accepts positive {label}", errors(exception) == [], repr(errors(exception)))

    for label, exception in (
        ("1a: a string", "yes"),
        ("1b: a list", []),
        ("1c: a number", 0),
        ("1d: null", None),
        ("2: an empty object", {}),
        ("3: justification with no requested", {"justification": "the venue names are lexical"}),
        ("4a: requested is the string 'true'", {"requested": "true"}),
        ("4b: requested is 1", {"requested": 1}),
        ("4c: requested is null", {"requested": None}),
        ("5: requested true with no justification", {"requested": True}),
        ("6: an empty justification", {"requested": True, "justification": ""}),
        ("8: a numeric justification", {"requested": True, "justification": 5}),
        ("9: an unknown key alongside",
         {"requested": True, "justification": "inherent", "confidence": 0.4}),
        ("10: justification misspelled", {"requested": True, "justifcation": "inherent"}),
    ):
        check(f"schema rejects negative {label}", errors(exception) != [])

    # Negative 7, stated as the divergence it is rather than omitted.
    blank = {"requested": True, "justification": "   "}
    check("schema ACCEPTS a whitespace-only justification -- rejected by the envelope's strip(), "
          "not here (intended, see docstring)", errors(blank) == [], repr(errors(blank)))


# --- the question stage ----------------------------------------------------------------------------
#
# Three tests, deliberately: the schema contract, the validator's error catalogue, and the one check
# whose first implementation was wrong in a way no schema can express (AR-003 tiering). Everything
# below builds its package IN MEMORY from the committed material and blueprint fixtures, for the same
# reason `validate_mutated` does -- `build_fixtures.py` owns fixtures/ entirely, so a hand-placed
# question package there disappears on the next rebuild.
#
# The carriers are hand-written because carrier prose cannot be derived, but every value that IS in
# the blueprint (`canonical`, `answer_category`) is read from it rather than restated. A fixture
# rebuild that changes item 6's target therefore breaks these tests loudly instead of leaving a
# package that silently disagrees with the plan it claims to implement.

# number -> (group_id, carrier_before, carrier_after, blank_position, response_form,
#            turn_index, quote, paraphrase_relation, carrier_entity, evidence_entity)
_QUESTION_ROWS = (
    (1, "G1", "Full name:", "", "final", "phrase", 4, "It's Anna Woods.", "exact",
     "the caller's full name", "the name she gives"),
    (2, "G2", "Street:", ", Ballysillan", "initial", "phrase", 8, "It's 118 Fordyce.", "exact",
     "her current street", "the street she states"),
    (3, "G2", "Postcode:", "", "final", "phrase", 10, "It's BT14 9BJ.", "exact",
     "her postcode", "the postcode she states"),
    (4, "G2", "Mobile:", "", "final", "numeric", 12, "It's 07840051963.", "exact",
     "her contact number", "the mobile number she gives"),
    (5, "G3", "Son currently attends:", "", "final", "phrase", 20,
     "He is still in primary school.", "paraphrase", "the son's current stage of education",
     "the stage she confirms after correcting herself"),
    (6, "G4", "Would like a", "nearby for son to play", "initial", "word", 29,
     "he'd love a park nearby", "paraphrase", "the outdoor amenity the family wants",
     "the amenity she says her son would love"),
    (7, "G4", "Prefers a", "rather than a flat", "initial", "word", 32,
     "we've always lived in a house", "paraphrase", "the property kind preferred",
     "the kind she says they stick with"),
    (8, "G5", "Property size required:", "property", "medial", "word", 35,
     "you definitely need a two-bedroom property", "signpost", "the minimum size",
     "the size the agent confirms"),
    (9, "G5", "Extra space wanted:", "for visiting family", "medial", "phrase", 37,
     "how about having a guest room", "paraphrase", "the additional space wanted",
     "the space the agent proposes and she accepts"),
    (10, "G5", "Other useful feature:", "for home working", "medial", "word", 40,
     "it'd be handy to have an office", "paraphrase", "the non-essential extra",
     "the room she calls handy"),
)

# group_id -> (window, layout, title or None, signposts, structure, question_range, word_limit)
_QUESTION_GROUPS = (
    ("G1", 1, "form", None, ["Personal details taken by phone"], {"row_labels": ["Full name"]},
     "1", "NO MORE THAN TWO WORDS"),
    ("G2", 1, "form", None, [], {"row_labels": ["Street", "Postcode", "Mobile"]},
     "2-4", "NO MORE THAN TWO WORDS AND/OR A NUMBER"),
    ("G3", 1, "note", "Family background", [],
     {"note_sections": [{"heading": "Child's education", "question_numbers": [5]}]},
     "5", "NO MORE THAN TWO WORDS"),
    ("G4", 2, "note", "Property preferences", ["Requirements for the new home are discussed next"],
     {"note_sections": [{"heading": "Location and lifestyle", "question_numbers": [6, 7]}]},
     "6-7", "ONE WORD ONLY"),
    ("G5", 2, "table", None, [], {
        "column_labels": ["Category", "Requirement"],
        "table_rows": [
            {"cells": [{"text": "Size"}, {"question_number": 8}]},
            {"cells": [{"text": "Extra space"}, {"question_number": 9}]},
            {"cells": [{"text": "Other"}, {"question_number": 10}]},
        ]},
     "8-10", "NO MORE THAN TWO WORDS"),
)


def _question_package() -> dict:
    """A package the validator passes clean, built from the two committed fixtures."""
    material = json.loads((FIXTURES / "material_valid.json").read_text(encoding="utf-8"))
    blueprint = json.loads((FIXTURES / "blueprint_valid.json").read_text(encoding="utf-8"))
    items = {item["number"]: item for item in blueprint["items"]}

    groups, instructions, limit_of = [], [], {}
    for group_id, window, layout, title, signposts, structure, question_range, limit in _QUESTION_GROUPS:
        group = {"group_id": group_id, "narrator_window_id": window, "layout": layout,
                 "signposts": list(signposts), "structure": copy.deepcopy(structure)}
        if title is not None:
            group["title"] = title
        groups.append(group)
        limit_of[group_id] = limit
        instructions.append({
            "group_id": group_id, "question_range": question_range,
            "instruction_text": "Complete the notes below. Write %s for each answer." % limit,
            "word_limit": limit, "numeral_allowance": 1 if "NUMBER" in limit else 0})

    questions, answer_key, evidence = [], [], []
    for row in _QUESTION_ROWS:
        (number, group_id, before, after, position, response_form, turn_index, quote,
         relation, carrier_entity, evidence_entity) = row
        item, limit = items[number], limit_of[group_id]
        questions.append({
            "number": number, "group_id": group_id, "carrier_before": before,
            "blank": "%d ................" % number, "carrier_after": after,
            "blank_position": position, "answer_category": item["answer_category"],
            "response_form": response_form})
        answer_key.append({
            "number": number, "canonical": item["target"], "alternatives": [],
            "word_limit": limit, "numeral_allowance": 1 if "NUMBER" in limit else 0,
            "counting_rule": "whitespace splits tokens; a hyphenated compound counts as one word"})
        evidence.append({
            "number": number, "turn_index": turn_index, "quote": quote,
            "narrator_window_id": item["narrator_window_id"], "paraphrase_relation": relation,
            "carrier_entity": carrier_entity, "evidence_entity": evidence_entity,
            "proposition_relation": "same subject, same request, same point in the call",
            "proposition_alignment_result": "aligned"})

    return {"reference": "Part 1", "test_package": material["test_package"],
            "material_id": "mat-0001",
            "question_face": {"instructions": instructions, "groups": groups,
                              "questions": questions},
            "answer_key": answer_key, "evidence": evidence}


def _validate_questions(mutate=None) -> dict:
    """Run the question validator over a (possibly mutated) package and return its JSON report."""
    package = _question_package()
    if mutate is not None:
        mutate(package)
    path = Path(tempfile.mkdtemp()) / "questions.json"
    path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    result = run(QUESTION_VALIDATE, str(FIXTURES / "material_valid.json"),
                 "--blueprint", str(FIXTURES / "blueprint_valid.json"),
                 "--questions", str(path), "--json")
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "errors": ["validator produced no JSON: %s%s"
                                       % (result.stdout, result.stderr)],
                "warnings": [], "metrics": {}}


def test_question_package_schema_contract() -> None:
    """The three-block separation and the two-layer split, as the schema enforces them.

    The positives matter as much as the negatives here: an earlier draft of the schema could have
    required `title` on every group, which rejects the entirely normal untitled form group -- and
    every negative case below would still have passed against that broken version.
    """
    print("question package schema")
    try:
        import jsonschema  # noqa: F401 - availability probe
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return

    def errors(data: object) -> list[str]:
        return _schema_errors("question_package.schema.json", data)

    base = _question_package()
    check("the reference package validates", errors(base) == [], repr(errors(base))[:400])

    def mutated(mutate) -> dict:
        payload = copy.deepcopy(base)
        mutate(payload)
        return payload

    def drop_title(payload: dict) -> None:
        for group in payload["question_face"]["groups"]:
            group.pop("title", None)

    check("a form/table group with no title still validates (QR-031 is note-only, and it is the "
          "validator that knows the layout)", errors(mutated(drop_title)) == [])

    def leak_answer(payload: dict) -> None:
        payload["question_face"]["questions"][0]["canonical"] = "Anna Woods"

    def leak_quote(payload: dict) -> None:
        payload["question_face"]["questions"][0]["quote"] = "It's Anna Woods."

    def restate_layout(payload: dict) -> None:
        payload["question_face"]["instructions"][0]["layout"] = "form"

    def nine_items(payload: dict) -> None:
        payload["question_face"]["questions"].pop()

    def free_limit(payload: dict) -> None:
        payload["question_face"]["instructions"][0]["word_limit"] = "UP TO 2 WORDS"

    def other_category(payload: dict) -> None:
        payload["question_face"]["questions"][0]["answer_category"] = "other"

    def table_cell_with_text_and_question(payload: dict) -> None:
        table = next(group for group in payload["question_face"]["groups"]
                     if group["layout"] == "table")
        table["structure"]["table_rows"][0]["cells"][0]["question_number"] = 8

    def empty_fixed_table_cell(payload: dict) -> None:
        table = next(group for group in payload["question_face"]["groups"]
                     if group["layout"] == "table")
        table["structure"]["table_rows"][0]["cells"][0]["text"] = ""

    for label, mutate in (
        # The separation is what the schema is for: block A must be unable to carry block B or C.
        ("an answer inside the question face", leak_answer),
        ("a quote inside the question face", leak_quote),
        # The two-layer rule, from the layer that must NOT own it.
        ("layout restated on an instruction", restate_layout),
        ("nine questions instead of ten", nine_items),
        ("a free-text word limit", free_limit),
        # There is no catch-all category, by decision. A string schema would have accepted this.
        ("answer_category 'other'", other_category),
        ("question_type declared at the top level",
         lambda p: p.update({"question_type": "completion"})),
        ("a fourth top-level block", lambda p: p.update({"quality_report": {}})),
        ("reference is not Part 1", lambda p: p.update({"reference": "Part 2"})),
        ("a layout outside form/note/table",
         lambda p: p["question_face"]["groups"][0].update({"layout": "flowchart"})),
        ("narrator_window_id 3", lambda p: p["question_face"]["groups"][0].update(
            {"narrator_window_id": 3})),
        ("blank_position outside the three classes",
         lambda p: p["question_face"]["questions"][0].update({"blank_position": "end"})),
        ("proposition_alignment_result as free text",
         lambda p: p["evidence"][0].update({"proposition_alignment_result": "mostly"})),
        ("an empty accepted alternative", lambda p: p["answer_key"][0].update({"alternatives": [""]})),
        ("a table cell containing both fixed text and a question", table_cell_with_text_and_question),
        ("an empty fixed-text table cell", empty_fixed_table_cell),
        ("an unknown key inside a structure",
         lambda p: p["question_face"]["groups"][0]["structure"].update({"unknown_cells": []})),
    ):
        check("schema rejects: %s" % label, errors(mutated(mutate)) != [])


def test_question_validator_catches_the_stage_defects() -> None:
    """The validator's error catalogue: one row per defect the schema cannot express.

    Each row asserts on the SUBSTANCE of the message, not merely that some error appeared. A package
    with a swapped answer also breaks AR-003, so `errors != []` would pass while the check that was
    supposed to fire stayed silent -- which is how a rule ends up never having worked.
    """
    print("question validator")

    def merge_note_group_across_windows(package: dict) -> None:
        face = package["question_face"]
        g3 = next(group for group in face["groups"] if group["group_id"] == "G3")
        g4 = next(group for group in face["groups"] if group["group_id"] == "G4")
        g3.pop("narrator_window_id")
        g3["title"] = "Family and property requirements"
        g3["signposts"] = [
            "The caller gives the minimum age for the child.",
            *g4["signposts"],
        ]
        g3["structure"]["note_sections"].append({
            "heading": "Location and lifestyle", "question_numbers": [6, 7]})
        face["groups"] = [group for group in face["groups"] if group["group_id"] != "G4"]
        for question in face["questions"]:
            if question["number"] in (6, 7):
                question["group_id"] = "G3"
        instruction = next(row for row in face["instructions"] if row["group_id"] == "G3")
        instruction["question_range"] = "5-7"
        face["instructions"] = [row for row in face["instructions"] if row["group_id"] != "G4"]
        for answer in package["answer_key"]:
            if answer["number"] in (6, 7):
                answer["word_limit"] = "NO MORE THAN TWO WORDS"

    clean = _validate_questions()
    check("the reference package passes clean", clean["ok"] is True,
          repr(clean["errors"])[:600])
    check("metrics report the measured group and position counts",
          clean["metrics"].get("groups") == 5
          and clean["metrics"].get("blank_positions", {}).get("final") == 4,
          repr(clean["metrics"]))
    check("layouts are reported as the mix actually used",
          clean["metrics"].get("layouts") == ["form", "note", "table"],
          repr(clean["metrics"].get("layouts")))
    reference_table = next(
        group for group in _question_package()["question_face"]["groups"]
        if group["group_id"] == "G5"
    )
    check("a rectangular table with explicit question cells passes",
          reference_table["structure"]["column_labels"] == ["Category", "Requirement"]
          and [row["cells"][1]["question_number"]
               for row in reference_table["structure"]["table_rows"]] == [8, 9, 10]
          and clean["ok"] is True,
          repr(reference_table["structure"]))

    def authentic_three_column_table(package: dict) -> None:
        group = next(
            row for row in package["question_face"]["groups"] if row["group_id"] == "G5")
        group["structure"] = {
            "column_labels": ["Property size", "Extra space", "Other feature"],
            "table_rows": [{
                "cells": [
                    {"question_number": 8},
                    {"question_number": 9},
                    {"question_number": 10},
                ]
            }],
        }

    three_column = _validate_questions(authentic_three_column_table)
    check("a genuine three-column row may contain several question blanks",
          three_column["ok"] is True, repr(three_column["errors"])[:600])
    spanning = _validate_questions(merge_note_group_across_windows)
    check("one natural note group may cover Q5-Q7 across the narrator midpoint",
          spanning["ok"] is True, repr(spanning["errors"])[:600])

    def spanning_group_with_one_signpost(package: dict) -> None:
        merge_note_group_across_windows(package)
        face = package["question_face"]
        group = next(row for row in face["groups"] if row["group_id"] == "G3")
        group["signposts"] = group["signposts"][:1]

    under_signposted = _validate_questions(spanning_group_with_one_signpost)
    check("one signpost cannot satisfy both windows of a spanning group",
          under_signposted["ok"] is False
          and any("one specific line per covered window" in error
                  for error in under_signposted["errors"]),
          repr(under_signposted["errors"])[:600])

    def replace_point(package: dict) -> None:
        package["answer_key"][5]["canonical"] = "playground"

    def reorder_evidence(package: dict) -> None:
        package["evidence"][5]["turn_index"] = 3

    def move_evidence_to_previous_window(package: dict) -> None:
        package["evidence"][5]["turn_index"] = 20
        package["evidence"][5]["quote"] = "He is still in primary school."

    def loosen_limit(package: dict) -> None:
        for instruction in package["question_face"]["instructions"]:
            if instruction["group_id"] == "G4":
                instruction["word_limit"] = "NO MORE THAN THREE WORDS"
                instruction["instruction_text"] = (
                    "Complete the notes below. Write NO MORE THAN THREE WORDS for each answer.")
        for entry in package["answer_key"]:
            if entry["number"] in (6, 7):
                entry["word_limit"] = "NO MORE THAN THREE WORDS"

    def leak_in_title(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G4":
                group["title"] = "Park and house preferences"

    def leak_inflected(package: dict) -> None:
        """Q9's `guest room` as `Rooms for guests` -- pluralised AND reordered.

        A bare plural would not exercise the inflection branch at all: `parks` contains `park`, so
        the exact-phrase scan catches it first and the inflection code could be deleted with this
        test still passing. Splitting and pluralising both words is what makes the phrase scan miss
        and the per-word inflection scan the only thing left.
        """
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"] = {"row_labels": ["Size", "Rooms for guests", "Other"],
                                      "row_header_label": "Category",
                                      "column_labels": ["Requirement"],
                                      "table_rows": [
                                          {"cells": [{"question_number": 8}]},
                                          {"cells": [{"question_number": 9}]},
                                          {"cells": [{"question_number": 10}]},
                                      ]}

    def drop_evidence(package: dict) -> None:
        package["evidence"].pop()

    def wrong_quote(package: dict) -> None:
        package["evidence"][6]["quote"] = "we have always preferred a house"

    def ambiguous_quote(package: dict) -> None:
        """Q9's anchor shortened to `bedroom`, which turns 35, 36 and 37 all contain.

        AL-007 is satisfied -- the span really is in turn 37 -- and the anchor is still unusable. The
        question stage's cross-check reconciles the writer's turn against the blind auditor's across
        exactly +-1, and a span occurring more than once inside that width resolves to the declared turn
        by preference alone: it reads as located while several readings fit it equally. Measured on a
        real run, an anchor that pins nothing is what leaves an item parked for human reading on the
        strength of a quote the writer could have made one clause longer.
        """
        package["evidence"][8]["quote"] = "bedroom"

    def note_without_title(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G3":
                group.pop("title")

    def note_without_sections(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G4":
                group["structure"].pop("note_sections")

    def note_with_duplicate_assignment(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G4":
                group["structure"]["note_sections"] = [
                    {"heading": "Location", "question_numbers": [6]},
                    {"heading": "Property", "question_numbers": [6]},
                ]

    def table_without_columns(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"].pop("column_labels")

    def legacy_table_without_cell_mapping(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"] = {
                    "row_header_label": "Category",
                    "row_labels": ["Size", "Extra space", "Other"],
                    "column_labels": ["Requirement"],
                }

    def table_with_ragged_row(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][0]["cells"].pop()

    def table_with_duplicate_question(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][1]["cells"][1] = {"question_number": 8}

    def table_with_unknown_question(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][2]["cells"][1] = {"question_number": 7}

    def table_with_out_of_order_questions(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                rows = group["structure"]["table_rows"]
                rows[0]["cells"][1], rows[1]["cells"][1] = (
                    rows[1]["cells"][1], rows[0]["cells"][1])

    def table_cell_with_both_variants(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][0]["cells"][0] = {
                    "text": "Size",
                    "question_number": 8,
                }

    def table_cell_with_boolean_question(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][0]["cells"][0] = {
                    "question_number": True,
                }

    def table_cell_with_unknown_key(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][0]["cells"][0] = {"label": "Size"}

    def leak_in_fixed_table_cell(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G5":
                group["structure"]["table_rows"][0]["cells"][0]["text"] = "two-bedroom"

    def no_signpost(package: dict) -> None:
        for group in package["question_face"]["groups"]:
            if group["group_id"] == "G4":
                group["signposts"] = []

    def self_reported_failure(package: dict) -> None:
        package["evidence"][2]["proposition_alignment_result"] = "not_aligned"

    def relabel_category(package: dict) -> None:
        package["question_face"]["questions"][6]["answer_category"] = "service"

    def mis_declare_position(package: dict) -> None:
        package["question_face"]["questions"][0]["blank_position"] = "initial"

    def mis_declare_form(package: dict) -> None:
        package["question_face"]["questions"][7]["response_form"] = "phrase"

    def numberless_blank(package: dict) -> None:
        package["question_face"]["questions"][3]["blank"] = "................"

    def placeholder_answer(package: dict) -> None:
        package["answer_key"][4]["canonical"] = "TBD"

    def wrong_material(package: dict) -> None:
        package["test_package"] = "Test 9"

    for label, mutate, needle in (
        # §1.4: the ten points are given input.
        ("a point replaced by a better-sounding one", replace_point, "may not be replaced"),
        ("a point's category relabelled", relabel_category, "answer_category"),
        # Printed-group evidence continuity.
        ("a group interrupted in the evidence sequence", reorder_evidence, "interrupted"),
        # SC-019 remains a strict per-item evidence boundary even though printed groups may cross it.
        ("Q6 evidence moved into the Q1-Q5 window", move_evidence_to_previous_window,
         "outside window 2"),
        # §6.4 #3: strictest fitting rubric, per group.
        ("a rubric looser than the group's answers need", loosen_limit, "stricter"),
        # QR-040, both the exact word and an inflection.
        ("the answer printed in the group's title", leak_in_title, "without listening"),
        ("the answer's words pluralised and reordered in a label", leak_inflected, "inflections"),
        # AL-001 / AL-010 / AL-007.
        ("an evidence entry missing", drop_evidence, "exactly once"),
        ("a quote absent from the turn it names", wrong_quote, "proves nothing"),
        ("a quote in its turn AND in a neighbour", ambiguous_quote, "identifies no single sentence"),
        # QR-031 / QR-015 / QR-026.
        ("a note group with no title", note_without_title, "QR-031"),
        ("a note group with no explicit sections", note_without_sections, "note_sections"),
        ("a note question assigned twice", note_with_duplicate_assignment, "exactly once"),
        ("a table group with no column labels", table_without_columns, "column_labels"),
        ("a legacy table with no cell mapping", legacy_table_without_cell_mapping, "table_rows"),
        ("a table with a ragged row", table_with_ragged_row, "rectangular"),
        ("a table with a duplicated question cell", table_with_duplicate_question,
         "exactly once"),
        ("a table with a question outside its group", table_with_unknown_question,
         "exactly once"),
        ("a table whose question cells are out of order", table_with_out_of_order_questions,
         "ascending printed order"),
        ("a table cell containing text and a question", table_cell_with_both_variants,
         "exactly one"),
        ("a boolean used as a table question number", table_cell_with_boolean_question,
         "booleans are invalid"),
        ("a table cell with an unknown key", table_cell_with_unknown_key, "exactly one"),
        ("an answer printed in a fixed table cell", leak_in_fixed_table_cell,
         "without listening"),
        ("a window with no blank-free signpost", no_signpost, "signpost"),
        # A package that reports its own AL-018 failure.
        ("a self-reported alignment failure", self_reported_failure, "not_aligned"),
        # The recomputed declarations. These are the whole point of persisting them.
        ("blank_position mis-declared", mis_declare_position, "blank_position"),
        ("a hyphenated compound declared a phrase", mis_declare_form, "response_form"),
        # AR-013 / QR-015 / material pairing.
        ("a blank carrying no question number", numberless_blank, "question number"),
        ("a placeholder answer", placeholder_answer, "placeholder"),
        ("the package pointing at another material", wrong_material, "test_package"),
    ):
        report = _validate_questions(mutate)
        matched = [error for error in report["errors"] if needle in error]
        check("validator catches: %s" % label, report["ok"] is False and bool(matched),
              "wanted %r among %r" % (needle, report["errors"][:4]))


def test_question_structural_context_and_position_guidelines() -> None:
    """Labels are context; position variety must not manufacture parenthetical filler."""
    print("question validator: structural context and position guidelines")

    def bare_labelled_form(package: dict) -> None:
        question = package["question_face"]["questions"][0]
        question.update({"carrier_before": "", "carrier_after": "", "blank_position": "final"})

    form = _validate_questions(bare_labelled_form)
    check("a labelled form row needs no carrier filler", form["ok"] is True,
          repr(form["errors"])[:500])

    def bare_labelled_table(package: dict) -> None:
        question = package["question_face"]["questions"][7]
        question.update({"carrier_before": "", "carrier_after": "", "blank_position": "final"})

    table = _validate_questions(bare_labelled_table)
    check("a table cell with real row and column labels needs no carrier filler",
          table["ok"] is True, repr(table["errors"])[:500])

    def bare_unlabelled_note(package: dict) -> None:
        question = package["question_face"]["questions"][4]
        question.update({"carrier_before": "", "carrier_after": "", "blank_position": "medial"})

    note = _validate_questions(bare_unlabelled_note)
    check("an unlabelled blank with no carrier is still rejected",
          any("no form/table label" in error for error in note["errors"]),
          repr(note["errors"])[:500])

    def all_final(package: dict) -> None:
        for question in package["question_face"]["questions"]:
            question.update({
                "carrier_before": "Recorded value:",
                "carrier_after": "",
                "blank_position": "final",
            })

    final_only = _validate_questions(all_final)
    check("natural position imbalance is a warning rather than a generation blocker",
          final_only["ok"] is True
          and any("do not invent carrier text" in warning for warning in final_only["warnings"])
          and any("guideline 7" in warning for warning in final_only["warnings"]),
          "errors=%r warnings=%r" % (final_only["errors"], final_only["warnings"]))

    authoring = (QUESTION_VALIDATE.parents[1] / "references" / "question-rules.md").read_text(
        encoding="utf-8")
    audit = (QUESTION_AUDIT_SCHEMAS.parent / "references" / "question-audit-rules.md").read_text(
        encoding="utf-8")
    check("authoring rules preserve a necessary day/month qualifier",
          "(day and month)" in authoring)
    check("authoring and audit rules reject spelling/process metadiscourse",
          "(as spelt)" in authoring and "(as mentioned)" in authoring
          and "(as spelt)" in audit and "(as mentioned)" in audit)


def test_form_table_semantics_are_consistent_across_agents() -> None:
    """Keep the production pseudo-table example visible at all three decision layers."""
    print("question guidance: form/table semantics")
    documents = {
        "material specification": VALIDATE.parents[1] / "references" / "specification.md",
        "question rules": QUESTION_VALIDATE.parents[1] / "references" / "question-rules.md",
        "audit rules": QUESTION_AUDIT_SCHEMAS.parent / "references"
        / "question-audit-rules.md",
    }
    for label, path in documents.items():
        text = " ".join(path.read_text(encoding="utf-8").lower().split())
        check("%s keeps the production field/value counterexample" % label,
              "service stage / arrangement" in text)


def test_question_ar003_tiers_follow_the_canonical() -> None:
    """AR-003's tier is decided by tokenising the canonical, NEVER by the declared word_limit.

    This is the one check whose first implementation was wrong in a way nothing else would have
    caught. Reading the rubric instead sends every one-word answer inside a `NO MORE THAN TWO WORDS`
    group down the loose multi-word path, where a derived form passes -- and Q10's group is exactly
    that shape, so the bug is reachable with the reference package unmodified.

    Both directions are asserted. The loose tier must stay loose: demanding that a multi-word answer
    equal a single token, as the first draft of this rule did, fails every legitimate two-word answer
    (`guest room` is two tokens and always will be).
    """
    print("question validator: AR-003 tiering")

    def canonical(number: int, value: str):
        def mutate(package: dict) -> None:
            package["answer_key"][number - 1]["canonical"] = value
            # Keep the blueprint-fidelity and response_form checks out of the way: this test is about
            # AR-003 alone, and a message from another rule would satisfy a bare `errors != []`.
            for item in package["question_face"]["questions"]:
                if item["number"] == number:
                    item["response_form"] = "word" if " " not in value else "phrase"
        return mutate

    def ar003_errors(number: int, value: str) -> list:
        report = _validate_questions(canonical(number, value))
        return [error for error in report["errors"] if "AR-003" in error]

    # Q10's group prints NO MORE THAN TWO WORDS; its canonical `office` is one token, so the strict
    # tier applies. `offices` satisfies the printed limit and is still wrong.
    check("a derived plural fails inside a two-word group (strict tier chosen by the canonical, "
          "not by the rubric)", ar003_errors(10, "offices") != [])
    check("a synonym fails the same way", ar003_errors(6, "playground") != [])
    # The AR-003 direction that reads backwards: a substring of an evidence token is not credit
    # either. `hous` is inside `house` and is not a token of it.
    check("a substring of an evidence token is refused", ar003_errors(7, "hous") != [])
    # Q9's `guest room` is two tokens in the same evidence and must pass -- the anti-tightening case.
    check("a legitimate two-token answer passes", ar003_errors(9, "guest room") == [],
          repr(ar003_errors(9, "guest room")))
    # AR-014: one word for the limit, whole token for the match. `bedroom` alone is not the token.
    check("half a hyphenated compound is refused", ar003_errors(8, "bedroom") != [])
    check("the whole hyphenated token passes", ar003_errors(8, "two-bedroom") == [],
          repr(ar003_errors(8, "two-bedroom")))


def test_question_blank_number_is_matched_as_a_whole_numeral() -> None:
    """A blank's printed number is compared as a whole numeral, not as a substring.

    With ten items the numbering contains exactly one overlapping pair, and a substring test lets
    the wrong half of it through: `"1" in "10 ................"` is true, so Q1 printed with Q10's
    number passed silently. That is the one mislabelling that matters, because the number is how a
    marker pairs an answer sheet row with an item -- a Q1 blank labelled 10 is marked against
    Q10's key.

    Both directions are asserted, and each is checked to name the item that is actually wrong. The
    reverse case (Q10 printed as `1`) already failed before the fix, so asserting only that
    direction would have left the bug in place.
    """
    print("question validator: blank numbering")
    needle = "whole numeral"

    def blank(number: int, printed: str):
        def mutate(package: dict) -> None:
            for question in package["question_face"]["questions"]:
                if question["number"] == number:
                    question["blank"] = printed
        return mutate

    def numbering_errors(number: int, printed: str) -> list:
        report = _validate_questions(blank(number, printed))
        return [error for error in report["errors"] if needle in error]

    q1_as_ten = numbering_errors(1, "10 ................")
    check("Q1 printed with Q10's number is rejected", q1_as_ten != [])
    check("and the message names Q1, not Q10",
          any(error.startswith("Q1's blank") for error in q1_as_ten), repr(q1_as_ten))

    q10_as_one = numbering_errors(10, "1 ................")
    check("Q10 printed with Q1's number is rejected", q10_as_one != [])
    check("and that message names Q10",
          any(error.startswith("Q10's blank") for error in q10_as_one), repr(q10_as_one))

    # The anti-tightening direction: surrounding punctuation and dot leaders are ordinary Part 1
    # blank styling (QR-015 puts visual form outside this gate), so they must not be read as part of
    # the numeral.
    check("Q10's own number still passes when styled", numbering_errors(10, "(10) .....") == [],
          repr(numbering_errors(10, "(10) .....")))


_QUESTION_AUDIT_SCHEMA = "audit_questions.schema.json"


def _question_audit_review() -> dict:
    """A complete, clean ten-item review, shaped to match the schema.

    Built here rather than committed as a fixture: `build_fixtures.py` owns fixtures/ and rebuilds it,
    and this document has exactly one reader.
    """
    answers = []
    for number in range(1, 11):
        answers.append({
            "number": number,
            "answer": "answer %d" % number,
            "turn_index": number + 2,
            "quote": "verbatim span for item %d" % number,
            "confidence": "high",
            "competing_candidates": [{
                "text": "rival %d" % number,
                "equally_supported": False,
                "reason": "the carrier limits the row to the caller's own address",
            }],
            "derivable_without_recording": False,
        })
    zero = {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "INFO": 0, "ADVISORY_WARNING": 0}
    return {
        "reconstructed_answers": answers,
        "per_question_findings": [],
        "group_findings": [],
        "coverage": {"reviewed_question_ids": list(range(1, 11)), "unreviewed": []},
        "summary": {"counts": dict(zero), "visual_counts": dict(zero)},
        "question_qc_status": "PASS",
        "content_review_readiness": "READY_FOR_HUMAN_REVIEW",
        "visual_qc_status": "NOT_RUN",
        "visual_findings": [],
    }


def test_question_audit_schema_contract() -> None:
    """The output contract of the blind question audit.

    The positives carry as much weight as the negatives. A schema that required a finding per item, or
    forbade an empty `competing_candidates`, would reject a genuinely clean review -- and every negative
    case below would still pass against that broken version, so the clean review is asserted first.
    """
    print("question audit schema")
    try:
        import jsonschema  # noqa: F401 - availability probe
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return

    def errors(data: object) -> list[str]:
        return _schema_errors(_QUESTION_AUDIT_SCHEMA, data)

    base = _question_audit_review()
    check("a clean ten-item review validates", errors(base) == [], repr(errors(base))[:400])

    def mutated(mutate) -> dict:
        payload = copy.deepcopy(base)
        mutate(payload)
        return payload

    def real_finding(payload: dict) -> None:
        payload["per_question_findings"].append({
            "number": 4, "rule_id": "AR-012", "severity": "MAJOR",
            "evidence": "turn 19 offers Tuesday and Thursday with equal support",
            "fix": "add 'the earlier of the two' to the carrier on row 4",
            "state": "open"})
        payload["summary"]["counts"]["MAJOR"] = 1
        payload["question_qc_status"] = "FAIL"

    check("a real finding with its status validates", errors(mutated(real_finding)) == [],
          repr(errors(mutated(real_finding)))[:400])

    def waived_with_reason(payload: dict) -> None:
        payload["group_findings"].append({
            "group_id": "A", "rule_id": "QR-031", "severity": "MINOR",
            "evidence": "the note group prints no heading",
            "fix": "add a heading naming the record the four rows describe",
            "state": "waived", "waiver_reason": "accepted for this draft by the request"})
        payload["summary"]["counts"]["MINOR"] = 1

    check("a waived finding carrying its reason validates",
          errors(mutated(waived_with_reason)) == [], repr(errors(mutated(waived_with_reason)))[:400])

    def nine_reviewed(payload: dict) -> None:
        payload["reconstructed_answers"].pop()
        payload["coverage"] = {"reviewed_question_ids": list(range(1, 10)), "unreviewed": [10],
                               "reason": "item 10's carrier references a group that is not printed"}

    check("a nine-item review with an explained omission validates (the schema records coverage; the "
          "orchestrator decides whether nine is acceptable)", errors(mutated(nine_reviewed)) == [],
          repr(errors(mutated(nine_reviewed)))[:400])

    for label, mutate in (
        # The three blocks the auditor must never receive, arriving back in its own output. Each would
        # mean the review had them, which is the leak this whole path exists to prevent.
        ("a supplied answer key echoed at the top level",
         lambda p: p.update({"answer_key": [{"number": 1, "canonical": "Anna Woods"}]})),
        ("an evidence table echoed at the top level",
         lambda p: p.update({"evidence": [{"number": 1, "turn_index": 3}]})),
        ("the item plan echoed at the top level", lambda p: p.update({"blueprint": {}})),
        # Coverage is the field that makes a truncated review visible, so an unexplained gap must fail.
        ("an unreviewed item with no reason",
         lambda p: p["coverage"].update({"unreviewed": [10]})),
        ("a reviewed id outside 1-10",
         lambda p: p["coverage"].update({"reviewed_question_ids": [0, 1, 2]})),
        ("a duplicated reviewed id",
         lambda p: p["coverage"].update({"reviewed_question_ids": [1, 1, 2]})),
        # A waived finding is the one state the auditor may not assign itself, and the reason is the
        # only thing distinguishing an authorised waiver from a self-issued one.
        ("a waived finding with no waiver_reason", lambda p: p["per_question_findings"].append(
            {"number": 2, "rule_id": "QR-040", "severity": "MAJOR", "evidence": "the heading prints "
             "the answer", "fix": "rename the heading", "state": "waived"})),
        # Findings must be actionable and attributable, one rule each.
        ("a finding with an empty fix", lambda p: p["per_question_findings"].append(
            {"number": 2, "rule_id": "QR-040", "severity": "MAJOR", "evidence": "x", "fix": "",
             "state": "open"})),
        ("a finding citing a free-text rule", lambda p: p["per_question_findings"].append(
            {"number": 2, "rule_id": "leakage", "severity": "MAJOR", "evidence": "x", "fix": "y",
             "state": "open"})),
        ("a severity outside the enum", lambda p: p["per_question_findings"].append(
            {"number": 2, "rule_id": "QR-040", "severity": "BLOCKER", "evidence": "x", "fix": "y",
             "state": "open"})),
        ("a finding state outside the four", lambda p: p["per_question_findings"].append(
            {"number": 2, "rule_id": "QR-040", "severity": "MINOR", "evidence": "x", "fix": "y",
             "state": "wontfix"})),
        ("a status outside PASS/WARNING/FAIL",
         lambda p: p.update({"question_qc_status": "PASS_WITH_MINOR_EDITS"})),
        # The two separated statuses from severity.md 3.1. This audit inspects no typography, so any
        # other visual value would be a claim about something never looked at.
        ("visual_qc_status claiming a pass", lambda p: p.update({"visual_qc_status": "PASS"})),
        ("a visual finding smuggled in", lambda p: p.update({"visual_findings": [{"note": "border"}]})),
        # The reconstruction is the product. A rival recorded without a verdict, or an answer without
        # its evidence, is a reconstruction that cannot be cross-checked.
        ("a rival with no equally_supported verdict",
         lambda p: p["reconstructed_answers"][0]["competing_candidates"].append(
             {"text": "Thursday", "reason": "also in the list"})),
        ("an answer with no quote",
         lambda p: p["reconstructed_answers"][0].pop("quote")),
        ("an answer with no confidence",
         lambda p: p["reconstructed_answers"][0].pop("confidence")),
        ("confidence as a number",
         lambda p: p["reconstructed_answers"][0].update({"confidence": 0.9})),
        ("a negative turn_index",
         lambda p: p["reconstructed_answers"][0].update({"turn_index": -1})),
        ("an eleventh reconstructed answer",
         lambda p: p["reconstructed_answers"].append(copy.deepcopy(p["reconstructed_answers"][0]))),
        ("counts missing a severity", lambda p: p["summary"]["counts"].pop("INFO")),
        ("visual_counts dropped entirely", lambda p: p["summary"].pop("visual_counts")),
        ("an unknown top-level block", lambda p: p.update({"score": {"total": 80}})),
    ):
        check("schema rejects: %s" % label, errors(mutated(mutate)) != [])


def test_question_audit_coverage_must_account_for_all_ten() -> None:
    """Nine reviewed items must be visible as nine, at both layers that can see it.

    The failure this pins is the quiet one. A review that stops at nine has the shape of a complete
    review: the findings list is plausible, the status computes, and nothing in the document says a
    tenth item exists. So `coverage` is the field that has to carry it, and it has to be impossible to
    leave an omission unexplained -- an unexplained gap is indistinguishable from an oversight, and the
    caller cannot decide what to do about it.

    Both layers are asserted because they answer different questions. The schema can only require that
    an omission is *declared and explained*; whether nine is acceptable at all is the orchestrator's
    call, so that comparison is made against the ten items it asked for.
    """
    print("question audit coverage")
    try:
        import jsonschema  # noqa: F401 - availability probe
    except ImportError:
        print("  SKIP  jsonschema not installed")
        return

    def errors(data: object) -> list[str]:
        return _schema_errors(_QUESTION_AUDIT_SCHEMA, data)

    nine = copy.deepcopy(_question_audit_review())
    nine["reconstructed_answers"].pop()
    nine["coverage"] = {"reviewed_question_ids": list(range(1, 10)), "unreviewed": [10]}

    check("nine reviewed with an undeclared reason is rejected by the schema", errors(nine) != [])

    nine["coverage"]["reason"] = "no decisive turn could be located for item 10 inside its window"
    check("nine reviewed with a stated reason is accepted by the schema", errors(nine) == [],
          repr(errors(nine))[:300])

    # The orchestrator's layer: the schema accepted the document above, and it still is not a review
    # of ten items. This is the comparison a caller has to make, expressed as the caller would.
    expected = set(range(1, 11))
    reviewed = set(nine["coverage"]["reviewed_question_ids"])
    check("and the caller can still tell it did not cover Q1-Q10",
          reviewed != expected and expected - reviewed == {10})

    # The truncation that hides itself: nine answers rebuilt while coverage claims ten. Nothing in the
    # schema can catch this, so the caller must compare the two lists rather than trusting either.
    lying = copy.deepcopy(_question_audit_review())
    lying["reconstructed_answers"].pop()
    check("a review claiming ten while rebuilding nine passes the schema", errors(lying) == [],
          repr(errors(lying))[:300])
    rebuilt = {answer["number"] for answer in lying["reconstructed_answers"]}
    claimed = set(lying["coverage"]["reviewed_question_ids"])
    check("so the caller compares the reconstruction against the claim, and the gap shows",
          claimed - rebuilt == {10})


def test_answer_category_decision_table() -> None:
    """The `answer_category` tie-break, pinned as data.

    Production had prose boundaries and no ranking, so the feasibility reviewer invented a ranking
    per call and contradicted itself inside one run: it rejected an included `breakfast` for being
    "a service, not a physical facility" and then rejected a named restaurant for being "a physical
    venue, so a facility rather than a purchasable service". Both verdicts were right. The axis they
    were argued from -- purchasable versus described -- cannot produce both, which is what made them
    read as contradictory and cost a material each.

    So the fix is an ORDER, and an order is only worth having if a later edit cannot quietly break
    it. Three things are asserted: the table decides every case it carries by the rule it names, the
    ranking is a total order over the 13 values with no value orphaned or claimed twice, and the two
    prose surfaces state the same seven rules in the same sequence. The last one is the real
    regression guard -- the reviewer reads the prose, not this JSON, so prose that drifts out of
    order reinstates exactly the defect above while every other check here still passes.
    """
    print("answer_category decision table")
    table = json.loads((FEASIBILITY_SCHEMAS.parent / "references"
                        / "answer-category-decisions.json").read_text(encoding="utf-8"))
    categories = table["categories"]
    procedure = table["procedure"]

    # The taxonomy is the schema's, not a second copy of it. A value added to one and not the other
    # would leave the new value with no rule to decide it -- and no test would say so.
    enum = json.loads(_schema("blueprint.read.schema.json").read_text(
        encoding="utf-8"))["definitions"]["item"]["properties"]["answer_category"]["enum"]
    check("the table's 13 values are the schema's enum, in the same order",
          categories == enum, "%r vs %r" % (categories, enum))
    check("there is no catch-all value", "other" not in categories)

    check("the procedure is numbered 1..N in file order",
          [rule["order"] for rule in procedure] == list(range(1, len(procedure) + 1)),
          repr([rule["order"] for rule in procedure]))

    # Every category is decided by exactly one rule. Two rules claiming one value is the ambiguity
    # this table exists to remove; zero rules claiming it leaves the reviewer back on intuition.
    claimed: dict[str, list[str]] = {}
    for rule in procedure:
        for name in rule["decides"]:
            claimed.setdefault(name, []).append(rule["rule"])
    check("no category is claimed by two rules",
          all(len(owners) == 1 for owners in claimed.values()),
          repr({k: v for k, v in claimed.items() if len(v) > 1}))
    unclaimed = [name for name in categories if name not in claimed]
    # person_name..quantity fall to rule 1, the rest are named explicitly; nothing may be left over.
    check("every category is decided by some rule", unclaimed == [], repr(unclaimed))

    order = {rule["rule"]: rule["order"] for rule in procedure}
    for case in table["cases"]:
        answer, want, rejected = case["answer"], case["category"], case["not"]
        deciding = case["rule"]
        check("%r -> %s (rule %d, not %s)" % (answer, want, order[deciding], rejected),
              want in categories and rejected in categories and want != rejected
              and want in dict((r["rule"], r["decides"]) for r in procedure)[deciding],
              "case cites rule %s, which decides %r" % (deciding, claimed.get(want)))

    # The pair that broke production, asserted as a pair. Either one alone is satisfiable by a table
    # that has simply moved the contradiction somewhere else; together they pin the axis.
    by_answer = {case["answer"]: case for case in table["cases"]}
    breakfast, venue = by_answer["breakfast"], by_answer["Riverside Brasserie"]
    check("an included breakfast is a service and a named venue is a facility",
          (breakfast["category"], venue["category"]) == ("service", "facility"))
    check("and both are decided by the same rule, so the two cannot be argued apart",
          breakfast["rule"] == venue["rule"] == "performed_or_merely_present",
          "%s vs %s" % (breakfast["rule"], venue["rule"]))
    check("a reference code is a document rather than a contact",
          (by_answer["KJ47"]["category"], by_answer["KJ47"]["not"]) == ("document", "contact"))
    check("an attribute with no alternative offered is a requirement rather than a preference",
          (by_answer["furnished"]["category"], by_answer["furnished"]["not"])
          == ("requirement", "preference"))

    # Both prose surfaces, in order. The reviewer is bound by the rubric; the specification is
    # authoritative over both it and the JSON, so a rule missing from either is a live divergence.
    phrases = [rule["doc_phrase"] for rule in procedure]
    for label, path in (
        ("rubric", FEASIBILITY_SCHEMAS.parent / "references" / "feasibility-rubric.md"),
        ("specification", VALIDATE.parents[1] / "references" / "specification.md"),
    ):
        text = path.read_text(encoding="utf-8")
        positions = [text.find(phrase) for phrase in phrases]
        missing = [phrase for phrase, at in zip(phrases, positions) if at < 0]
        check("%s states all seven rules" % label, missing == [], repr(missing))
        check("%s states them in the table's order" % label,
              missing == [] and positions == sorted(positions),
              repr(list(zip(phrases, positions))))

    # The rubric must bind the reviewer, not merely inform it: a reviewer free to reject on an
    # unrankable objection is the reviewer that produced the contradictory pair.
    rubric = (FEASIBILITY_SCHEMAS.parent / "references" / "feasibility-rubric.md").read_text(
        encoding="utf-8")
    check("the rubric forbids opposite conclusions on inputs one rule decides",
          "may not reach opposite conclusions" in rubric)
    check("the rubric requires a rejection to name its rule",
          "name the rule number" in rubric)
    check("the rubric points at the decision table by name",
          "answer-category-decisions.json" in rubric)
    skill = (FEASIBILITY_SCHEMAS.parent / "SKILL.md").read_text(encoding="utf-8")
    check("and the skill tells the reviewer to read it",
          "answer-category-decisions.json" in skill)


def main() -> int:
    for suite in (
        test_schemas_are_valid,
        test_write_and_read_schemas_disagree_where_they_should,
        test_warning_does_not_fail,
        test_new_checks_catch_defects,
        test_malformed_turns_stay_reportable,
        test_closing_rules_match_the_real_corpus,
        test_mode_and_split_rules_match_the_real_corpus,
        test_indirect_confirmation_is_optional,
        test_remaining_rules_do_not_reject_real_papers,
        test_grouping_cannot_be_faked,
        test_blueprint_version_is_read_not_guessed,
        test_v1_leniency_is_scoped_to_the_layout_enum,
        test_response_form_derivation,
        test_v2_declarations_are_recomputed,
        test_target_must_fit_some_rubric,
        test_group_constraints_are_distinguishable,
        test_item_labels_are_zero_based_indices,
        test_qr027_counts_are_reported_not_enforced,
        test_spelled_name_rule_not_vacuous,
        test_metrics_absent_when_unmeasured,
        test_typical_band_is_not_a_finding,
        test_audit_fixtures_are_coherent,
        test_fixture_halves_are_balanced,
        test_cross_check,
        test_cross_check_pairs_on_evidence_text,
        test_render_report,
        test_archive_samples_do_not_crash,
        test_preflight_three_exits,
        test_preflight_version_gate,
        test_preflight_incomplete_validation,
        test_preflight_missing_semantics,
        test_preflight_ok_and_errors_must_agree,
        test_preflight_counts_are_range_and_sum_checked,
        test_preflight_rejection_must_be_explained,
        test_preflight_thresholds_have_one_source,
        test_preflight_outcome_names_are_pinned,
        test_feasibility_schema_contract,
        test_feasibility_schema_qr027_exception,
        test_answer_category_decision_table,
        test_question_package_schema_contract,
        test_question_validator_catches_the_stage_defects,
        test_question_structural_context_and_position_guidelines,
        test_form_table_semantics_are_consistent_across_agents,
        test_question_ar003_tiers_follow_the_canonical,
        test_question_blank_number_is_matched_as_a_whole_numeral,
        test_question_audit_schema_contract,
        test_question_audit_coverage_must_account_for_all_ten,
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
