"""Tests for the deterministic layer, running the real skill scripts as subprocesses.

These are the authoritative scripts, not stubs. Wrapping them without exercising them would
leave the contract between the backend and the skill untested -- and that contract (errors vs
warnings, exit code vs stdout) is where the Loop's grading decisions come from.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend import paths
from backend.deterministic.crosscheck import crosscheck
from backend.deterministic.metrics import run_metrics
from backend.deterministic.runner import ScriptError, run_script_json
from backend.deterministic.validate import validate

BACKEND = Path(__file__).resolve().parents[1]


class TestValidateWrapper:
    async def test_valid_fixture_passes_with_a_warning(self, material, blueprint):
        result = await validate(material, blueprint)
        assert result.ok, result.errors
        assert result.errors == []
        assert result.warnings, "the reference fixture is outside the typical band by design"

    async def test_warnings_never_make_ok_false(self, material, blueprint):
        """exit 0 with warnings must not be read as a failure (prd.md R5)."""
        result = await validate(material, blueprint)
        assert result.warnings and result.ok

    async def test_errors_and_warnings_stay_in_separate_fields(self, material, blueprint, clone):
        broken = clone(blueprint)
        broken["items"][0]["turn_index"] = 99
        result = await validate(material, broken)
        assert not result.ok
        assert any("turn_index" in e for e in result.errors)
        assert not any("turn_index" in w for w in result.warnings)

    async def test_metrics_are_exposed_for_the_loop(self, material, blueprint):
        result = await validate(material, blueprint)
        assert result.metrics["dialogue_words"] > 0

    async def test_unmeasured_metrics_are_omitted_not_zeroed(self, material, blueprint, clone):
        broken = clone(material)
        broken["listening_material_parts"][0]["script"]["turns"] = []
        result = await validate(broken, blueprint)
        assert result.metrics == {}, "zeros would read as a real measurement of zero words"

    async def test_malformed_turn_is_reported_not_raised(self, material, blueprint, clone):
        """skill-contract D1: a content defect must not become an orchestration crash."""
        broken = clone(material)
        broken["listening_material_parts"][0]["script"]["turns"][5] = {"speaker": "speaker2"}
        result = await validate(broken, blueprint)
        assert not result.ok and result.errors


class TestMetricsWrapper:
    async def test_metrics_extracted_from_the_valid_fixture(self, material):
        result = await run_metrics(material)
        assert result.assessable
        counts = result.audit_metrics()
        assert set(counts) == {"dialogue_words", "dialogue_turns", "first_half_turns",
                               "second_half_turns", "narrator_words"}
        assert counts["dialogue_words"] > 400

    async def test_typical_band_arrives_as_a_warning_not_an_issue(self, material):
        result = await run_metrics(material)
        assert any("preferred" in w for w in result.warnings)
        assert not [i for i in result.issues if "preferred" in i.get("message", "")]

    async def test_empty_turns_yield_no_metrics_rather_than_zeros(self, material, clone):
        broken = clone(material)
        broken["listening_material_parts"][0]["script"]["turns"] = []
        result = await run_metrics(broken)
        assert result.audit_metrics() == {}


class TestCrossCheckWrapper:
    def test_aligned_maps_pass(self, blueprint, audit_aligned):
        result = crosscheck(blueprint, audit_aligned)
        assert result.ok and result.matched == 10
        assert result.hard_defects == []

    def test_diverged_maps_surface_both_defect_classes(self, blueprint, audit_diverged):
        result = crosscheck(blueprint, audit_diverged)
        assert not result.ok
        assert [r["number"] for r in result.unrecoverable] == [5]
        assert len(result.unintended_target) == 1
        assert len(result.hard_defects) == 2

    def test_wrapper_delegates_to_the_skill_implementation(self):
        """A second copy of this comparison would become a second source of truth."""
        from backend.deterministic import crosscheck as module

        module._load_compare()
        assert module._compare.__module__ == "cross_check"


class TestRunnerIsNonBlocking:
    async def test_missing_script_is_an_infrastructure_error(self):
        with pytest.raises(ScriptError):
            await run_script_json(Path("/nonexistent/script.py"), ["--json"])

    async def test_exit_code_one_is_not_treated_as_an_error(self, material, blueprint, clone):
        """Every one of these scripts exits 1 to mean "found problems" -- a normal outcome."""
        broken = clone(blueprint)
        broken["items"][0]["turn_index"] = 99
        result = await validate(material, broken)
        assert result.errors  # parsed successfully despite exit 1


class TestLayerBoundaries:
    def test_deterministic_layer_imports_no_model_sdk(self):
        """implement.md phase 1 CI gate, as a test: this layer must stay offline-testable."""
        for path in sorted((BACKEND / "deterministic").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root = name.split(".")[0]
                    assert root not in ("strands", "openai"), "%s imports %s" % (path.name, name)

    def test_steps_do_not_branch_on_quality_verdicts(self):
        """Steps must not decide anything; loop.py owns every branch (design.md §1).

        Narrowed from "must not mention a verdict at all" once the audit step became an agent call:
        it has to assert the reply *contains* a verdict, because an audit whose verdict is missing is
        an unusable reply and returning it silently would hand loop.py a phantom result. Checking for
        presence is not deciding.

        What stays forbidden is *reading the value* -- comparing it, ranking it, or testing severity.
        The AST is walked rather than the text so the module's own explanation neither satisfies nor
        breaks the assertion.
        """
        QUALITY = {"verdict", "findings", "severity", "score"}
        tree = ast.parse((BACKEND / "steps" / "agent_steps.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `x["verdict"] == "PASS"`, `severity in (...)`: a comparison against a quality value.
            if isinstance(node, ast.Compare):
                for side in [node.left, *node.comparators]:
                    if isinstance(side, ast.Subscript) and isinstance(side.slice, ast.Constant):
                        assert side.slice.value not in QUALITY, ast.dump(node)[:120]
            # `audit.get("severity")`, `.verdict`: reaching for the value to act on it.
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("verdict", "findings"), node.attr
        # The permitted use, asserted as behaviour rather than as a source string. The earlier form
        # pinned the exact line `if "verdict" not in audit`, which broke the moment the presence check
        # grew to cover the other three load-bearing keys -- a test that fails when the thing it
        # guards gets stronger is testing the spelling, not the property.
        from backend.steps.agent_steps import _audit_envelope
        from backend.steps.call import ModelCallError

        with pytest.raises(ModelCallError):
            _audit_envelope('{"score": {"total": 90}}', "audit")
        # Presence is checked; the value is not judged. A FAIL is returned as readily as a PASS.
        for verdict in ("PASS", "FAIL"):
            audit = _audit_envelope(
                '{"verdict": "%s", "score": {"total": 10}, "findings": [], '
                '"blind_information_map": []}' % verdict, "audit")
            assert audit["verdict"] == verdict

    def test_only_the_loop_ranks_verdicts(self):
        """The verdict ranking table must exist in exactly one place."""
        hits = [
            path.name
            for path in sorted(BACKEND.rglob("*.py"))
            if "VERDICT_RANK" in path.read_text(encoding="utf-8")
            and "tests" not in str(path)
        ]
        assert hits == ["loop.py"], hits

    def test_the_step_modules_register_no_tools_of_their_own(self):
        """prd.md R2, restated for the current architecture.

        The original form of this test asserted that no step module mentioned the word "tools" at
        all, which stopped meaning anything once the agents gained tools deliberately -- and it broke
        on a prose comment that happened to contain the word. What still matters is narrower: tool
        wiring belongs to ``agents.py``, so a step module quietly attaching an extra tool to a call
        would be a second place where capability is decided.

        Note what R2 does and does not require. The generator *does* run the validator, and that is
        safe because validation is a report rather than a gate: ``orchestration/loop.py`` re-runs it
        on the delivered artifact, so an agent claiming a clean run gains nothing.
        """
        import ast

        source = (BACKEND / "steps" / "agent_steps.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.keyword) and node.arg == "tools":
                raise AssertionError("agent_steps.py passes tools=; that belongs to agents.py")

        # `call.py` is exempt, and narrowly: its one `tools=[]` belongs to `call_json`, which no
        # pipeline step uses any more -- only `scripts/smoke_model.py`, to prove the model endpoint
        # answers at all. An empty list is the safe direction anyway.
        call_source = (BACKEND / "steps" / "call.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(call_source)):
            if isinstance(node, ast.keyword) and node.arg == "tools":
                assert isinstance(node.value, ast.List) and not node.value.elts, \
                    "call.py may only ever pass tools=[]"

    def test_skill_prompts_are_read_from_files_not_transcribed(self):
        """prd.md constraint: the skill files are the single source of truth."""
        # Prompts are no longer assembled in Python at all: the agents activate a skill and read
        # its reference files themselves. What must stay true is that no step module carries a
        # transcribed copy of the specification or the rubric -- a second source of truth that
        # would drift from the file reviewers actually edit.
        for name in ("agent_steps.py", "call.py"):
            source = (BACKEND / "steps" / name).read_text(encoding="utf-8")
            for smell in ("Privately plan ten recordable details",
                          "dialogue words outside",
                          "160-230 words"):
                assert smell not in source, "%s transcribes specification text: %r" % (name, smell)

    def test_skill_scripts_stay_python_39_parseable(self):
        """The system python3 is 3.9.6 and the Trellis scripts must keep working under it."""
        import subprocess
        import sys

        for script in (paths.validate_script(), paths.metrics_script(),
                       paths.skills_root() / "shared" / "cross_check.py"):
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())",
                 str(script)],
                capture_output=True,
            )
            assert proc.returncode == 0, script
