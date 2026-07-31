"""Loop tests: every branch is asserted with mocked steps, so no tokens are spent.

That is the point of putting control flow in Python. An agentic loop's behaviour could only be
observed by running it against a live model; this one is a state machine whose every path can be
pinned down offline and re-checked on every commit.
"""

from __future__ import annotations

import copy
import json

import pytest

from backend.deterministic.crosscheck import CrossCheckResult
from backend.deterministic.validate import ValidationResult
from backend.orchestration import loop as loop_module
from backend.orchestration.loop import (
    Candidate,
    MaterialResult,
    is_assessable,
    is_clean,
    pick_better,
    route_for,
    run_one,
)
from backend.steps.call import ModelCallError
from backend.steps.agent_steps import GenOutput
from backend.orchestration.revision_plan import build_revise_instruction


class FakeScenario:
    id = "accommodation-rental"
    category = "accommodation"
    title_zh = "租房咨询"
    prompt_hint = "A tenant enquires about a property to rent."
    default_count = 1


def audit_doc(verdict="PASS", total=80, findings=None, warnings=None, map_rows=None):
    return {
        "verdict": verdict,
        "assessable": True,
        "score": {"total": total, "dimensions": {}},
        "findings": findings or [],
        "blind_information_map": map_rows or [],
        "metrics": {"dialogue_words": 618},
        "warnings": warnings or [],
    }


def clean_crosscheck():
    return CrossCheckResult({"ok": True, "planned": 10, "observed": 10, "matched": 10,
                             "unrecoverable": [], "unintended_target": [], "ambiguous": []})


def defective_crosscheck():
    return CrossCheckResult({
        "ok": False, "planned": 10, "observed": 10, "matched": 8,
        "unrecoverable": [{"number": 5, "type": "price", "turn_index": 20, "evidence": "£95"}],
        "unintended_target": [{"audit_seq": 9, "type": "datetime", "turn_index": 25,
                               "evidence": "next Tuesday"}],
        "ambiguous": [],
    })


def candidate(verdict, total, label="initial"):
    return Candidate(GenOutput({}, {}), audit_doc(verdict, total), clean_crosscheck(), label)


class Harness:
    """Records what the Loop called, so branch decisions can be asserted directly."""

    def __init__(self, material, blueprint):
        self.material, self.blueprint = material, blueprint
        self.generate_calls = []
        self.audit_calls = []
        self.revise_calls = []
        self.validate_results = []
        self.audit_results = []
        self.revise_output = None
        self.metrics_calls = 0
        self.crosscheck_results = []
        self.generate_error = None
        self.generate_error_times = 0
        self.stages = []
        self._runner = None
        self.runner_closed = False

    async def generate(self, scenario, attempt=0, feedback=None):
        self.generate_calls.append({"attempt": attempt, "feedback": feedback})
        if self.generate_error and len(self.generate_calls) <= self.generate_error_times:
            raise self.generate_error
        return GenOutput(copy.deepcopy(self.material), copy.deepcopy(self.blueprint))

    async def validate(self, material, blueprint):
        return self.validate_results.pop(0)

    async def run_metrics(self, material, runner=None):
        # `runner` is the remote sandbox handle the Loop threads through. Accepted and ignored: the
        # tests are about orchestration, and a real Code Interpreter session per test would make the
        # suite depend on AWS.
        self.metrics_calls += 1

        class M:
            @staticmethod
            def audit_metrics():
                return {"dialogue_words": 618}

        return M()

    async def audit_blind(self, material, metrics):
        self.audit_calls.append({"material": material, "metrics": metrics})
        return self.audit_results.pop(0)

    async def revise(self, material, blueprint, instruction):
        self.revise_calls.append(instruction)
        return self.revise_output or GenOutput(
            copy.deepcopy(self.material), copy.deepcopy(self.blueprint)
        )

    def crosscheck(self, blueprint, audit):
        return self.crosscheck_results.pop(0)

    @property
    def metrics_runner(self):
        """Stands in for the remote metrics session.

        Records whether it was closed, so `test_the_session_is_always_released` can assert the
        wrapper releases what it opened on every exit path -- including the twelve early returns.
        """
        harness = self

        class _Runner:
            closed = False

            async def run(self, material):
                return {"assessable": True,
                        "parts": [{"dialogue_words": 618, "dialogue_turns": 34}]}

            async def close(self):
                harness.runner_closed = True

        if self._runner is None:
            self._runner = _Runner()
        return self._runner

    async def emit(self, stage, detail=None):
        self.stages.append(stage)


@pytest.fixture
def harness(material, blueprint, monkeypatch):
    h = Harness(material, blueprint)
    monkeypatch.setattr(loop_module.agent_steps, "generate", h.generate)
    monkeypatch.setattr(loop_module, "validate", h.validate)
    monkeypatch.setattr(loop_module, "run_metrics_remote", h.run_metrics)
    monkeypatch.setattr(loop_module.agent_steps, "audit_blind", h.audit_blind)
    monkeypatch.setattr(loop_module.agent_steps, "revise", h.revise)
    # The Loop builds a SandboxedMetrics when the caller supplies none, and that would reach AWS.
    # Every test drives `run_one` directly, so a stub with the two methods the wrapper calls is
    # enough -- and it also asserts the wrapper really does close what it opened.
    monkeypatch.setattr(loop_module, "_build_metrics_runner", lambda *a, **k: h.metrics_runner)
    monkeypatch.setattr(loop_module, "crosscheck", h.crosscheck)
    monkeypatch.setattr(loop_module.asyncio, "sleep", lambda *a, **k: _noop())
    return h


async def _noop():
    return None


def ok_validation(warnings=None):
    return ValidationResult([], warnings or [], {"dialogue_words": 618})


def bad_validation(errors):
    return ValidationResult(errors, [], {})


class TestGenerationBudget:
    @pytest.mark.asyncio
    async def test_three_validation_failures_still_deliver_the_last_attempt(self, harness):
        """Validation is a report, not a gate.

        This test used to assert the opposite -- ok=False, reason="validation_exhausted", and
        `audit_calls == []` on the reasoning that "a rejected material must never reach the audit".
        The product owner overruled it, and the diagnosis was right: "模型正常返回了内容，是校验规则
        把它判死了 -- 这不是'生成异常'". Five of the validator's rules were then measured rejecting
        real exam papers, which is what "the validator is sometimes wrong" looks like in numbers.

        So the material is delivered, and it goes through the REST of the pipeline unchanged -- it
        is audited and cross-checked like any other, which is why `audit_calls` is now expected to
        be non-empty. The retries are untouched (still three attempts, still accumulating
        feedback); only the give-up changed from discard to deliver.
        """
        harness.validate_results = [bad_validation(["e1"]), bad_validation(["e2"]),
                                    bad_validation(["e3"])]
        harness.audit_results = [audit_doc("PASS", 84)]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)

        assert result.ok, "a material the model returned must not be swallowed"
        assert result.candidate is not None
        assert len(harness.generate_calls) == 3, "all three attempts are still spent"
        assert len(harness.audit_calls) == 1, "the delivered material is still audited"
        # The findings travel with it, so the reader page can state them as reference.
        assert result.validation_findings == ["e3"], "the LAST attempt's findings, not all three"
        # Not `degraded`: that flag means "skipped part of the pipeline", and this material did not.
        assert result.degraded is False
        assert "validation_reported" in harness.stages

    @pytest.mark.asyncio
    async def test_a_revision_that_fixes_validation_clears_the_findings(self, harness):
        """The reason the retries and the revision are still worth their cost.

        Three generations failed validation, the material was delivered anyway, and then the
        revision produced a version that validates. The delivered artifact is the revision, so it
        must NOT carry the initial version's findings -- reporting notes about a script that is not
        the one on screen would send a question-writer looking for a defect that no longer exists.
        """
        harness.validate_results = [bad_validation(["e1"]), bad_validation(["e2"]),
                                    bad_validation(["e3"]), ok_validation()]
        # A minor finding on the first audit, so `is_clean` is False and the revision actually runs.
        harness.audit_results = [audit_doc("PASS_WITH_MINOR_EDITS", 70, findings=[{"severity": "minor", "rule": "r",
                                                             "evidence": "e", "fix": "f"}]),
                                 audit_doc("PASS", 92)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)

        assert result.ok and result.selected_version == "revised"
        assert result.validation_findings == []

    @pytest.mark.asyncio
    async def test_findings_survive_when_the_initial_version_wins(self, harness):
        """The mirror case: the revision validates but scores worse, so `initial` ships.

        `initial` is the same script the validator reported on, so it keeps those findings. Dropping
        them because a revision happened would deliver a material whose notes were computed from a
        version nobody sees.
        """
        harness.validate_results = [bad_validation(["e1"]), bad_validation(["e2"]),
                                    bad_validation(["e3"]), ok_validation()]
        harness.audit_results = [audit_doc("PASS", 92, findings=[{"severity": "minor", "rule": "r",
                                                             "evidence": "e", "fix": "f"}]), audit_doc("PASS", 61)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)

        assert result.ok and result.selected_version == "initial"
        assert result.validation_findings == ["e3"]

    @pytest.mark.asyncio
    async def test_a_clean_material_carries_no_findings(self, harness):
        """The normal path must be untouched: an empty list, never a missing key."""
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("PASS", 91)]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)

        assert result.ok and result.validation_findings == []
        assert result.as_dict()["validation_findings"] == []

    @pytest.mark.asyncio
    async def test_third_attempt_passing_continues_normally(self, harness):
        harness.validate_results = [bad_validation(["e1"]), bad_validation(["e2"]),
                                    ok_validation()]
        harness.audit_results = [audit_doc("PASS", 88)]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert result.ok
        assert len(harness.generate_calls) == 3
        assert result.selected_version == "initial"

    @pytest.mark.asyncio
    async def test_validator_errors_are_fed_back_to_the_next_attempt(self, harness):
        harness.validate_results = [bad_validation(["dialogue words outside 450-750: 812"]),
                                    ok_validation()]
        harness.audit_results = [audit_doc("PASS", 90)]
        harness.crosscheck_results = [clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)
        assert harness.generate_calls[1]["feedback"] == ["dialogue words outside 450-750: 812"]

    @pytest.mark.asyncio
    async def test_feedback_accumulates_across_attempts(self, harness):
        """Regression from a live 3-slot batch that produced 0 materials.

        Passing only the latest attempt's errors made the model oscillate: attempt 2 fixed the
        reported problem and regressed on something attempt 1 had right, so three attempts and
        ~240s per slot yielded nothing. Every error seen so far must stay in the prompt.
        """
        harness.validate_results = [
            bad_validation(["full opening must include 'four different recordings'"]),
            bad_validation(["dialogue words outside 450-750: 448"]),
            ok_validation(),
        ]
        harness.audit_results = [audit_doc("PASS", 90)]
        harness.crosscheck_results = [clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)

        third = harness.generate_calls[2]["feedback"]
        assert "full opening must include 'four different recordings'" in third
        assert "dialogue words outside 450-750: 448" in third

    @pytest.mark.asyncio
    async def test_offset_anchors_are_repaired_before_validation(self, harness, clone, material,
                                                                 blueprint, monkeypatch):
        """Observed live: an attempt failed with six uniform off-by-one anchor errors.

        Those are computable exactly, so spending a 40s regeneration on them is waste. The rule
        is unchanged -- one unique match repairs, anything else does not.
        """
        shifted = clone(material)
        shifted["listening_material_parts"][0]["script"]["turns"].insert(
            2, {"speaker": "speaker3", "text": "Sorry, once more?"}
        )
        harness.material = shifted
        seen = {}

        async def spy_validate(mat, bp):
            seen["indices"] = [item["turn_index"] for item in bp["items"]]
            return ok_validation()

        monkeypatch.setattr(loop_module, "validate", spy_validate)
        harness.audit_results = [audit_doc("PASS", 90)]
        harness.crosscheck_results = [clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)

        original = [item["turn_index"] for item in blueprint["items"]]
        assert seen["indices"] == [i + 1 for i in original]
        assert "anchors_repaired" in harness.stages

    @pytest.mark.asyncio
    async def test_unrepairable_anchors_still_reach_the_validator(self, harness, clone,
                                                                  monkeypatch):
        """A failed repair must not short-circuit anything: the validator still runs every time."""
        broken = clone(harness.blueprint)
        broken["items"][0]["evidence"] = "text that appears nowhere"
        harness.blueprint = broken
        calls = []

        async def spy_validate(mat, bp):
            calls.append(bp)
            return bad_validation(["evidence not found"])

        monkeypatch.setattr(loop_module, "validate", spy_validate)
        harness.audit_results = [audit_doc("PASS", 80)]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert len(calls) == 3, "every attempt must still be validated"
        # Still delivered, still reported. The unrepairable anchor is now a note on a material the
        # user can read rather than the reason they got nothing.
        assert result.ok
        assert result.validation_findings == ["evidence not found"]

    @pytest.mark.asyncio
    async def test_repeated_errors_are_not_duplicated_in_feedback(self, harness):
        harness.validate_results = [bad_validation(["same error"]), bad_validation(["same error"]),
                                    ok_validation()]
        harness.audit_results = [audit_doc("PASS", 90)]
        harness.crosscheck_results = [clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)
        assert harness.generate_calls[2]["feedback"] == ["same error"]

    @pytest.mark.asyncio
    async def test_warnings_do_not_trigger_regeneration(self, harness):
        """The hard-won distinction: warnings advise, they never fail (prd.md R5)."""
        harness.validate_results = [ok_validation(["dialogue words outside preferred 600-650"]),
                                    ok_validation()]
        harness.audit_results = [audit_doc("PASS", 85), audit_doc("PASS", 85)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert len(harness.generate_calls) == 1
        assert result.ok

    @pytest.mark.asyncio
    async def test_warnings_reach_the_revise_instruction_as_advisory(self, harness):
        harness.validate_results = [ok_validation(["dialogue turns outside preferred 30-40"]),
                                    ok_validation()]
        harness.audit_results = [audit_doc("PASS", 85), audit_doc("PASS", 86)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)
        instruction = harness.revise_calls[0]
        assert instruction.must_fix == []
        assert any("outside preferred 30-40" in item for item in instruction.advisory)


class TestInfrastructureBudgetIsSeparate:
    @pytest.mark.asyncio
    async def test_throttling_does_not_consume_a_generation_attempt(self, harness):
        """One 429 must not cost a material one of its three quality attempts."""
        harness.generate_error = ModelCallError("429 throttled")
        harness.generate_error_times = 2
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("PASS", 90)]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert result.ok
        # Three generate() calls, but all on the infra budget: only one validation was consumed.
        assert len(harness.generate_calls) == 3
        assert harness.generate_calls[2]["attempt"] == 0
        assert "infra_retry" in harness.stages

    @pytest.mark.asyncio
    async def test_infra_budget_exhaustion_is_reported_as_a_model_error(self, harness):
        harness.generate_error = ModelCallError("503")
        harness.generate_error_times = 99
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert not result.ok
        assert result.reason == "model_error"


class TestCrossCheckDefects:
    @pytest.mark.asyncio
    async def test_hard_defects_enter_must_fix_without_regenerating(self, harness):
        """prd.md R5: these two classes are hard defects but never cost a generation attempt."""
        harness.validate_results = [ok_validation(), ok_validation()]
        harness.audit_results = [audit_doc("PASS", 80), audit_doc("PASS", 84)]
        harness.crosscheck_results = [defective_crosscheck(), clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert len(harness.generate_calls) == 1, "cross-check must not trigger regeneration"
        assert len(harness.revise_calls) == 1
        must_fix = harness.revise_calls[0].must_fix
        assert any("unrecoverable" in item for item in must_fix)
        assert any("unintended" in item for item in must_fix)
        assert result.ok


class TestMemorylessReAudit:
    @pytest.mark.asyncio
    async def test_re_audit_message_structure_matches_the_first_audit(self, harness):
        """prd.md R3: the re-audit must be a fresh call, not a continuation."""
        harness.validate_results = [ok_validation(), ok_validation()]
        first = audit_doc("FAIL", 55, findings=[{"severity": "major", "rule": "r",
                                                 "evidence": "e", "fix": "f"}])
        harness.audit_results = [first, audit_doc("PASS", 82)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)

        assert len(harness.audit_calls) == 2
        assert set(harness.audit_calls[0]) == set(harness.audit_calls[1]) == {"material", "metrics"}
        assert sorted(harness.audit_calls[0]["metrics"]) == sorted(
            harness.audit_calls[1]["metrics"]
        )

    @pytest.mark.asyncio
    async def test_re_audit_receives_neither_prior_verdict_nor_instructions(self, harness):
        harness.validate_results = [ok_validation(), ok_validation()]
        harness.audit_results = [audit_doc("FAIL", 40, findings=[{"severity": "critical",
                                                                  "rule": "narrator answers",
                                                                  "evidence": "e", "fix": "f"}]),
                                 audit_doc("PASS", 80)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        await run_one(FakeScenario(), "slot-1", harness.emit)
        second = harness.audit_calls[1]
        serialised = repr(second)
        for leaked in ("narrator answers", "must_fix", "FAIL", "critical"):
            assert leaked not in serialised


class TestAnchorDesyncRollsBack:
    @pytest.mark.asyncio
    async def test_revision_with_unlocatable_anchor_falls_back(self, harness, clone):
        broken = clone(harness.blueprint)
        broken["items"][2]["evidence"] = "a sentence that appears nowhere"
        harness.revise_output = GenOutput(clone(harness.material), broken)
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("PASS_WITH_MINOR_EDITS", 74,
                                           findings=[{"severity": "minor", "rule": "r",
                                                      "evidence": "e", "fix": "f"}])]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert result.ok
        assert result.selected_version == "initial"
        assert result.note == "revise_rejected_anchor_desync"
        assert len(harness.audit_calls) == 1, "a rejected revision must not be re-audited"

    @pytest.mark.asyncio
    async def test_shifted_anchors_are_repaired_and_the_revision_proceeds(
        self, harness, clone
    ):
        shifted = clone(harness.material)
        turns = shifted["listening_material_parts"][0]["script"]["turns"]
        turns.insert(2, {"speaker": "speaker3", "text": "Sorry, once more please?"})
        harness.revise_output = GenOutput(shifted, clone(harness.blueprint))
        harness.validate_results = [ok_validation(), ok_validation()]
        harness.audit_results = [audit_doc("PASS_WITH_MINOR_EDITS", 74,
                                           findings=[{"severity": "minor", "rule": "r",
                                                      "evidence": "e", "fix": "f"}]),
                                 audit_doc("PASS", 84)]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert result.selected_version == "revised"
        assert result.anchor_repairs
        assert "anchors_repaired" in harness.stages


class TestRevisionRejectedByValidator:
    @pytest.mark.asyncio
    async def test_rollback_with_a_recorded_reason(self, harness):
        harness.validate_results = [ok_validation(), bad_validation(["speaker_count must be 3"])]
        harness.audit_results = [audit_doc("PASS_WITH_MINOR_EDITS", 72,
                                           findings=[{"severity": "minor", "rule": "r",
                                                      "evidence": "e", "fix": "f"}])]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert result.ok
        assert result.selected_version == "initial"
        assert result.note == "revise_rejected_by_validate"
        assert result.detail["errors"] == ["speaker_count must be 3"]


class TestPickBetter:
    def test_verdict_outranks_score(self):
        """PASS 78 beats PASS_WITH_MINOR_EDITS 82: verdict reflects hard defects."""
        before = candidate("PASS", 78, "initial")
        after = candidate("PASS_WITH_MINOR_EDITS", 82, "revised")
        assert pick_better(before, after) is before

    def test_higher_score_within_the_same_verdict_wins(self):
        before = candidate("PASS", 78, "initial")
        after = candidate("PASS", 84, "revised")
        assert pick_better(before, after) is after

    def test_tie_goes_to_the_revision(self):
        before = candidate("PASS", 80, "initial")
        after = candidate("PASS", 80, "revised")
        assert pick_better(before, after) is after

    def test_fail_and_not_assessable_rank_equally(self):
        before = candidate("FAIL", 40, "initial")
        after = candidate("NOT_ASSESSABLE", 0, "revised")
        # Same rank class, lower score: the initial version is kept.
        assert pick_better(before, after) is before

    def test_revision_upgrading_a_fail_is_selected(self):
        before = candidate("FAIL", 45, "initial")
        after = candidate("PASS_WITH_MINOR_EDITS", 68, "revised")
        assert pick_better(before, after) is after

    def test_missing_score_is_treated_as_zero_not_an_error(self):
        broken = Candidate(GenOutput({}, {}), {"verdict": "PASS"}, clean_crosscheck(), "revised")
        assert pick_better(candidate("PASS", 70), broken).label == "initial"


class TestSameSourceGuarantee:
    @pytest.mark.asyncio
    async def test_delivered_artifacts_all_come_from_the_selected_version(self, harness, clone):
        """prd.md R6: never a revised script beside the original's score."""
        revised_material = clone(harness.material)
        revised_material["listening_material_parts"][0]["scenario"] = "REVISED MARKER"
        harness.revise_output = GenOutput(revised_material, clone(harness.blueprint))
        harness.validate_results = [ok_validation(), ok_validation()]
        harness.audit_results = [
            audit_doc("PASS_WITH_MINOR_EDITS", 70,
                      findings=[{"severity": "minor", "rule": "r", "evidence": "e", "fix": "f"}]),
            audit_doc("PASS", 91),
        ]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]

        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        payload = result.as_dict()
        assert payload["selected_version"] == "revised"
        assert payload["material"]["listening_material_parts"][0]["scenario"] == "REVISED MARKER"
        assert payload["audit"]["score"]["total"] == 91

    @pytest.mark.asyncio
    async def test_rollback_delivers_the_initial_audit_with_the_initial_script(
        self, harness, clone
    ):
        revised_material = clone(harness.material)
        revised_material["listening_material_parts"][0]["scenario"] = "REVISED MARKER"
        harness.revise_output = GenOutput(revised_material, clone(harness.blueprint))
        harness.validate_results = [ok_validation(), ok_validation()]
        harness.audit_results = [
            audit_doc("PASS", 88, warnings=["band deviation"]),
            audit_doc("FAIL", 44, findings=[{"severity": "major", "rule": "r",
                                             "evidence": "e", "fix": "f"}]),
        ]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]

        payload = (await run_one(FakeScenario(), "slot-1", harness.emit)).as_dict()
        assert payload["selected_version"] == "initial"
        assert payload["audit"]["score"]["total"] == 88
        assert payload["material"]["listening_material_parts"][0]["scenario"] != "REVISED MARKER"


class TestRouting:
    def test_pass_routes_to_pending(self):
        assert route_for(candidate("PASS", 90)) == "pending"
        assert route_for(candidate("PASS_WITH_MINOR_EDITS", 70)) == "pending"

    def test_fail_also_routes_to_pending(self):
        """Was "FAIL routes to quarantine". The client's rule replaces it: a flawed material is
        returned to the user with its shortcomings stated, and the user decides."""
        assert route_for(candidate("FAIL", 40)) == "pending"
        assert route_for(candidate("NOT_ASSESSABLE", 0)) == "pending"

    @pytest.mark.asyncio
    async def test_a_selected_fail_is_delivered_to_pending_not_withheld(self, harness):
        harness.validate_results = [ok_validation(), ok_validation()]
        harness.audit_results = [audit_doc("FAIL", 45, findings=[{"severity": "major",
                                                                  "rule": "r", "evidence": "e",
                                                                  "fix": "f"}]),
                                 audit_doc("FAIL", 47, findings=[{"severity": "major",
                                                                  "rule": "r", "evidence": "e",
                                                                  "fix": "f"}])]
        harness.crosscheck_results = [clean_crosscheck(), clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert result.ok and result.route == "pending"
        # The verdict is not softened on the way out: the card needs it to state the shortcomings.
        assert result.candidate.verdict == "FAIL"


class TestAssessability:
    """`is_assessable` is what batch.py refills on. It must separate "flawed" from "unreadable"."""

    def test_a_fail_is_assessable_and_therefore_never_refilled(self):
        result = MaterialResult("slot-1", "s", True, candidate("FAIL", 40), "initial", "pending")
        assert is_assessable(result)

    def test_pass_verdicts_are_assessable(self):
        for verdict in ("PASS", "PASS_WITH_MINOR_EDITS"):
            result = MaterialResult("slot-1", "s", True, candidate(verdict, 80), "initial",
                                    "pending")
            assert is_assessable(result), verdict

    def test_not_assessable_is_not(self):
        result = MaterialResult("slot-1", "s", True, candidate("NOT_ASSESSABLE", 0), "initial",
                                "pending")
        assert not is_assessable(result)

    def test_an_unreadable_verdict_is_not_assessable_either(self):
        """Matches state_store.verdict_of: an audit nobody can parse is not evidence of quality."""
        result = MaterialResult("slot-1", "s", True, candidate("SOMETHING_NEW", 80), "initial",
                                "pending")
        assert not is_assessable(result)

    def test_a_failed_slot_is_not_assessable(self):
        assert not is_assessable(MaterialResult("slot-1", "s", False, reason="model_error"))


class TestTimeBudgetDegradation:
    @pytest.mark.asyncio
    async def test_revision_is_skipped_and_labelled_when_the_budget_is_short(self, harness):
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("PASS_WITH_MINOR_EDITS", 72,
                                           findings=[{"severity": "minor", "rule": "r",
                                                      "evidence": "e", "fix": "f"}])]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit,
                              allow_revision=lambda: False)
        assert result.ok
        assert result.degraded and result.degraded_reason == "time_budget"
        assert result.selected_version == "initial"
        assert harness.revise_calls == []

    @pytest.mark.asyncio
    async def test_degraded_material_is_routed_on_its_own_verdict(self, harness):
        """design.md §9: degrading skips an optimisation; it is not a quality penalty."""
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("PASS_WITH_MINOR_EDITS", 72,
                                           findings=[{"severity": "minor", "rule": "r",
                                                      "evidence": "e", "fix": "f"}])]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit,
                              allow_revision=lambda: False)
        assert result.route == "pending"

    @pytest.mark.asyncio
    async def test_a_degraded_failing_material_is_still_delivered(self, harness):
        """Was "still quarantines". Degrading is not a quality penalty and neither is FAIL a
        reason to withhold, so the material comes back carrying both flags."""
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("FAIL", 40, findings=[{"severity": "critical",
                                                                  "rule": "r", "evidence": "e",
                                                                  "fix": "f"}])]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit,
                              allow_revision=lambda: False)
        assert result.ok and result.route == "pending" and result.degraded
        assert result.candidate.verdict == "FAIL"


class TestIsClean:
    def test_clean_material_skips_the_revision(self):
        assert is_clean(audit_doc("PASS", 95), clean_crosscheck(), [])

    def test_minor_finding_still_triggers_a_revision(self):
        audit = audit_doc("PASS_WITH_MINOR_EDITS", 78,
                          findings=[{"severity": "minor", "rule": "r", "evidence": "e",
                                     "fix": "f"}])
        assert not is_clean(audit, clean_crosscheck(), [])

    def test_validator_warning_still_triggers_a_revision(self):
        assert not is_clean(audit_doc("PASS", 90), clean_crosscheck(), ["words outside band"])

    def test_cross_check_defect_blocks_cleanliness(self):
        assert not is_clean(audit_doc("PASS", 90), defective_crosscheck(), [])

    @pytest.mark.asyncio
    async def test_clean_path_makes_exactly_two_model_calls(self, harness):
        harness.validate_results = [ok_validation()]
        harness.audit_results = [audit_doc("PASS", 95)]
        harness.crosscheck_results = [clean_crosscheck()]
        result = await run_one(FakeScenario(), "slot-1", harness.emit)
        assert len(harness.generate_calls) == 1 and len(harness.audit_calls) == 1
        assert harness.revise_calls == []
        assert result.note == "clean_on_first_pass"


class TestReviseInstructionSeparation:
    def test_severities_are_sorted_into_the_right_sections(self):
        audit = audit_doc("FAIL", 50, findings=[
            {"severity": "critical", "rule": "narrator answers", "evidence": "e",
             "turn_index": 1, "fix": "f"},
            {"severity": "major", "rule": "words too high", "evidence": "e", "fix": "f"},
            {"severity": "minor", "rule": "weak cue", "evidence": "e", "fix": "f"},
        ], warnings=["band deviation"])
        instruction = build_revise_instruction(audit, defective_crosscheck(), ["turn deviation"])

        assert len(instruction.must_fix) == 4  # critical + major + unrecoverable + unintended
        assert any("critical" in item for item in instruction.must_fix)
        assert any("weak cue" in item for item in instruction.advisory)
        assert any("turn deviation" in item for item in instruction.advisory)
        assert any("band deviation" in item for item in instruction.advisory)
        # Never mixed: an advisory item in the must-fix list would provoke rewrites of compliant
        # content, which is the failure skill-contract already measured.
        assert not any("weak cue" in item for item in instruction.must_fix)

    def test_message_labels_the_advisory_section_as_non_binding(self):
        from backend.steps.agent_steps import build_revise_message

        instruction = build_revise_instruction(audit_doc("PASS", 80), clean_crosscheck(), ["w"])
        message = build_revise_message({}, {}, instruction)
        assert "Advisory only" in message
        assert "do NOT rewrite compliant content" in message
        assert "Must fix" in message


def compliance_review(*items, summary="s"):
    return {"items": list(items), "summary": summary}


def breach(code="C2", severity="critical", turn_index=5):
    return {"code": code, "compliant": False, "severity": severity,
            "turn_index": turn_index, "evidence": "e", "fix": "f"}


class TestComplianceReviewIsConsumed:
    """The C1-C6 review reaches every consumer of audit severity.

    Written after measuring that it reached none of them. The auditor's SKILL.md tells it to keep the
    review out of ``findings``, so every consumer that read only ``findings`` was silently reading
    half the audit -- and a perfectly obedient auditor reporting a critical register breach produced
    ``is_clean=True``, ``verdict=PASS``, score 88, and an empty revision instruction. No laziness, no
    error, no signal: the highest-severity semantic defect the system can detect, discarded.
    """

    def _audit_with(self, *items, verdict="PASS", total=88):
        audit = audit_doc(verdict, total)
        audit["compliance_review"] = compliance_review(*items)
        return audit

    def test_a_critical_compliance_item_blocks_cleanliness(self):
        assert not is_clean(self._audit_with(breach()), clean_crosscheck(), [])

    def test_a_critical_compliance_item_forces_a_fail_verdict(self):
        """The auditor states PASS honestly -- its verdict is defined over `findings`. Python has to
        reconcile the two blocks, because `pick_better` ranks on verdict first."""
        candidate = Candidate(GenOutput({}, {}), self._audit_with(breach()), clean_crosscheck(), "initial")
        assert candidate.verdict == "FAIL"

    def test_severity_caps_apply_to_compliance_items(self):
        critical = Candidate(GenOutput({}, {}), self._audit_with(breach(severity="critical"), total=95),
                             clean_crosscheck(), "initial")
        major = Candidate(GenOutput({}, {}), self._audit_with(breach(severity="major"), total=95),
                          clean_crosscheck(), "initial")
        assert critical.score == 49
        assert major.score == 69

    def test_a_minor_compliance_item_only_lowers_the_verdict_one_step(self):
        candidate = Candidate(GenOutput({}, {}), self._audit_with(breach(severity="minor"), total=90),
                             clean_crosscheck(), "initial")
        assert candidate.verdict == "PASS_WITH_MINOR_EDITS"
        assert candidate.score == 90

    def test_compliance_items_are_graded_into_the_revision_instruction(self):
        audit = self._audit_with(breach("C2", "critical"), breach("C5", "major"),
                                 breach("C3", "minor"), {"code": "C1", "compliant": True})
        instruction = build_revise_instruction(audit, clean_crosscheck(), [])

        assert sum("C2" in item for item in instruction.must_fix) == 1
        assert sum("C5" in item for item in instruction.must_fix) == 1
        assert sum("C3" in item for item in instruction.advisory) == 1
        # A compliant item is not a defect, and reporting it as one costs a pointless rewrite.
        assert not any("C1" in item for item in instruction.must_fix + instruction.advisory)

    def test_a_clean_review_changes_nothing(self):
        """The control. Without it, every test above would pass on a rule that always fires."""
        audit = self._audit_with({"code": "C1", "compliant": True},
                                 {"code": "C2", "compliant": True})
        candidate = Candidate(GenOutput({}, {}), audit, clean_crosscheck(), "initial")
        assert (candidate.verdict, candidate.score) == ("PASS", 88)
        assert is_clean(audit, clean_crosscheck(), [])

    def test_an_audit_without_the_block_is_unaffected(self):
        """`compliance_review` is optional in the schema, so old audits must behave as before."""
        audit = audit_doc("PASS", 88)
        assert "compliance_review" not in audit
        candidate = Candidate(GenOutput({}, {}), audit, clean_crosscheck(), "initial")
        assert (candidate.verdict, candidate.score) == ("PASS", 88)
        assert is_clean(audit, clean_crosscheck(), [])

    def test_not_assessable_is_never_promoted_or_demoted(self):
        audit = audit_doc("NOT_ASSESSABLE", 0)
        audit["compliance_review"] = compliance_review(breach())
        assert Candidate(GenOutput({}, {}), audit, clean_crosscheck(), "initial").verdict == "NOT_ASSESSABLE"


class TestAuditReplyShape:
    """What `audit_blind` accepts. Every case here was measured passing before the fix."""

    def _envelope(self, reply):
        from backend.steps.agent_steps import _audit_envelope
        from backend.steps.call import ModelCallError

        return _audit_envelope, ModelCallError, reply

    def test_a_decoy_object_cannot_win(self):
        """`extract_json` returns the first balanced object, which is right for prose-wrapped
        replies and wrong when there are two. Measured: a reply opening with
        `{"verdict": "PASS", "note": "let me reconsider"}` and then giving a real FAIL with critical
        findings was delivered as a clean PASS, since the only check was `"verdict" in audit`."""
        envelope, error, _ = self._envelope(None)
        with pytest.raises(error):
            envelope('{"verdict": "PASS", "note": "let me reconsider"}\n\n'
                     '{"verdict": "FAIL", "score": {"total": 42}, '
                     '"findings": [{"severity": "critical"}], "blind_information_map": []}',
                     "audit")

    @pytest.mark.parametrize("missing", ["score", "findings", "blind_information_map"])
    def test_each_load_bearing_key_is_required(self, missing):
        """Absence of any of these reads as good news. No `findings` means "no defects"; no
        `blind_information_map` leaves the cross-check nothing to compare, which it reports clean."""
        envelope, error, _ = self._envelope(None)
        audit = audit_doc("PASS", 95)
        audit.pop(missing)
        with pytest.raises(error):
            envelope(json.dumps(audit), "audit")

    def test_a_complete_audit_passes(self):
        envelope, _, _ = self._envelope(None)
        assert envelope(json.dumps(audit_doc("PASS", 88)), "audit")["verdict"] == "PASS"


class TestProviderErrorsUseTheInfraBudget:
    @pytest.mark.asyncio
    async def test_a_provider_exception_becomes_a_model_call_error(self):
        """Otherwise it escapes `run_one` and `batch.py` charges it a whole refill round.

        Measured before the fix: `_with_infra_retries` catches `ModelCallError`/`ScriptError` only, so
        a throttling error made one call, propagated out of the Loop, and was recorded as
        `unhandled_error` -- which is refillable, meaning a 429 bought a full regeneration instead of
        a two-second backoff.
        """
        from backend.steps.agent_steps import _invoke
        from backend.steps.call import ModelCallError

        class Throttled:
            async def invoke_async(self, message):
                raise RuntimeError("ThrottlingException: rate exceeded")

        with pytest.raises(ModelCallError) as excinfo:
            await _invoke(Throttled(), "m", "generation")
        assert "ThrottlingException" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_model_call_error_is_not_double_wrapped(self):
        from backend.steps.agent_steps import _invoke
        from backend.steps.call import ModelCallError

        class Already:
            async def invoke_async(self, message):
                raise ModelCallError("original text")

        with pytest.raises(ModelCallError) as excinfo:
            await _invoke(Already(), "m", "audit")
        assert str(excinfo.value) == "original text"


class TestSlotIsolation:
    @pytest.mark.asyncio
    async def test_an_unexpected_exception_is_contained_by_the_batch_layer(self, harness):
        from backend.orchestration.batch import Budget, _run_slot
        import asyncio

        async def explode(*args, **kwargs):
            raise RuntimeError("boom")

        harness_queue = asyncio.Queue()
        import backend.orchestration.batch as batch_module

        original = batch_module.run_one
        batch_module.run_one = explode
        try:
            result = await _run_slot(FakeScenario(), "slot-1", asyncio.Semaphore(1),
                                     Budget(), harness_queue)
        finally:
            batch_module.run_one = original
        assert not result.ok and result.reason == "unhandled_error"
        assert "boom" in result.detail
