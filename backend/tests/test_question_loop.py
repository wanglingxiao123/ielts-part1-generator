"""The question cross-check, the review recompute, and the loop's ranking.

These test the three things a question set can be wrong about in a way that produces no error and no
visible symptom -- which is the whole reason they exist rather than being covered by the schema:

1. **A review that agrees with the key on every item it looked at, having looked at nine.** Passes the
   schema, reads as clean, and the tenth item is indistinguishable from an item with nothing wrong.
2. **A review whose stated counts and status do not follow from its own findings.** Two MAJOR findings
   above a ``PASS``. Every field is well-typed; the orchestrator routes on the status and ships it.
3. **A ranking that lets three MINOR findings outvote one MAJOR.** Any weighted score does this, and
   the result is a revision that traded a real defect for cosmetics being chosen as the better set.

The comparison direction matters throughout: several tests assert that something does NOT fire. A
cross-check tightened until it reports every item as divergent would pass every "catches the defect"
test and be useless, and the anti-false-positive tests are what make that fail.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.deterministic.question_crosscheck import (  # noqa: E402
    crosscheck_questions,
    review_consistency,
)
from backend.orchestration.question_loop import (  # noqa: E402
    QuestionCandidate,
    QuestionResult,
    ScriptWasEdited,
    SEVERITY_ORDER,
    _assert_script_untouched,
    delivery_blockers,
    is_clean_questions,
    pick_better_questions,
)
from backend.orchestration.question_revision_plan import (  # noqa: E402
    build_question_revise_instruction,
)
from backend.steps.agent_steps import _question_audit_envelope, _question_envelope  # noqa: E402
from backend.steps.call import ModelCallError  # noqa: E402

NUMBERS = list(range(1, 11))


def _review(package: dict, findings=None, status=None, counts=None, answers=None) -> dict:
    """A schema-shaped review that agrees with the key, so each test perturbs one thing.

    Built from the package's own answer key on purpose: a review whose reconstruction is *correct* is
    the harder baseline, because then anything a test detects is what the test introduced rather than
    an artefact of hand-written fixture answers.
    """
    evidence = {row["number"]: row for row in package["evidence"]}
    rebuilt = answers if answers is not None else [
        {
            "number": entry["number"],
            "answer": entry["canonical"],
            "confidence": "high",
            "turn_index": evidence[entry["number"]]["turn_index"],
            "quote": evidence[entry["number"]]["quote"],
            "narrator_window_id": evidence[entry["number"]]["narrator_window_id"],
            "derivable_without_recording": False,
            "competing_candidates": [],
        }
        for entry in package["answer_key"]
    ]
    findings = findings or []
    if counts is None:
        counts = {name: 0 for name in ("CRITICAL", "MAJOR", "MINOR", "INFO", "ADVISORY_WARNING")}
        for finding in findings:
            if finding.get("state", "open") == "open":
                counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    if status is None:
        if counts.get("CRITICAL") or counts.get("MAJOR"):
            status = "FAIL"
        elif counts.get("MINOR"):
            status = "WARNING"
        else:
            status = "PASS"
    return {
        "reconstructed_answers": rebuilt,
        "per_question_findings": findings,
        "group_findings": [],
        "coverage": {"reviewed_question_ids": [a["number"] for a in rebuilt], "unreviewed": []},
        "question_qc_status": status,
        "summary": {"counts": counts,
                    "visual_counts": {name: 0 for name in
                                      ("CRITICAL", "MAJOR", "MINOR", "INFO", "ADVISORY_WARNING")}},
        "visual_qc_status": "NOT_RUN",
        "visual_findings": [],
        "content_review_readiness": {"ready": True},
    }


def _finding(number: int, severity: str, rule: str = "QR-040", state: str = "open") -> dict:
    return {"number": number, "rule_id": rule, "severity": severity,
            "evidence": "e", "fix": "f", "state": state}


class TestTheReviewMustCoverExactlyTenItems:
    """Gate 1. The schema permits a partial review; this caller does not."""

    def test_a_clean_ten_item_review_passes(self, question_package):
        result = review_consistency(_review(question_package))
        assert result["ok"], result["errors"]
        assert result["computed"]["reviewed_question_ids"] == NUMBERS

    def test_nine_reviewed_items_are_rejected(self, question_package):
        review = _review(question_package)
        review["reconstructed_answers"] = review["reconstructed_answers"][:9]
        review["coverage"]["reviewed_question_ids"] = NUMBERS[:9]
        review["coverage"]["unreviewed"] = [10]
        result = review_consistency(review)
        assert not result["ok"]
        # Both the short coverage and the non-empty `unreviewed` are named: an operator reading one
        # error should not have to guess that the other is also true.
        assert any("Q1-Q10" in e for e in result["errors"])
        assert any("unreviewed" in e for e in result["errors"])

    def test_the_self_hiding_case_is_caught(self, question_package):
        """Nine rebuilt answers under a coverage claim of ten -- the case the schema cannot see.

        This is the one that matters most. The short-coverage case above at least *says* it is short.
        Here every schema rule is satisfied, the count of reviewed ids is ten, and Q10 simply has no
        rebuilt answer -- which downstream is indistinguishable from Q10 being fine.
        """
        review = _review(question_package)
        review["reconstructed_answers"] = review["reconstructed_answers"][:9]
        # coverage still claims all ten, and `unreviewed` is still empty
        result = review_consistency(review)
        assert not result["ok"]
        assert any("indistinguishable" in e for e in result["errors"])

    def test_a_duplicated_number_cannot_pad_the_count(self, question_package):
        """Ten entries covering nine items. The total is right and the coverage is wrong."""
        review = _review(question_package)
        review["reconstructed_answers"][9] = copy.deepcopy(review["reconstructed_answers"][8])
        result = review_consistency(review)
        assert not result["ok"]
        assert any("duplicated number" in e for e in result["errors"])
        assert len(review["reconstructed_answers"]) == 10  # the count really is ten

    def test_eleven_items_are_rejected_too(self, question_package):
        """The over-coverage direction, so the check is an equality and not a minimum."""
        review = _review(question_package)
        extra = copy.deepcopy(review["reconstructed_answers"][0])
        extra["number"] = 11
        review["reconstructed_answers"].append(extra)
        review["coverage"]["reviewed_question_ids"] = NUMBERS + [11]
        result = review_consistency(review)
        assert not result["ok"]
        assert any("unexpected [11]" in e for e in result["errors"])


class TestTheCountsAndStatusMustFollowFromTheFindings:
    """Gate 3. Recomputed with the algorithm from the audit skill's own rules file."""

    @pytest.mark.parametrize("severities,expected", [
        ([], "PASS"),
        (["MINOR"], "WARNING"),
        (["MINOR", "MINOR"], "WARNING"),
        (["MAJOR"], "FAIL"),
        (["CRITICAL"], "FAIL"),
        (["MINOR", "MAJOR"], "FAIL"),
    ])
    def test_the_status_is_derived_not_read(self, question_package, severities, expected):
        findings = [_finding(i + 1, s) for i, s in enumerate(severities)]
        result = review_consistency(_review(question_package, findings))
        assert result["ok"], result["errors"]
        assert result["computed"]["question_qc_status"] == expected

    def test_info_and_advisory_are_counted_but_never_change_the_status(self, question_package):
        """Per the rules file: both appear in the counts and neither moves the verdict."""
        findings = [_finding(1, "INFO"), _finding(2, "ADVISORY_WARNING")]
        result = review_consistency(_review(question_package, findings))
        assert result["ok"], result["errors"]
        assert result["computed"]["question_qc_status"] == "PASS"
        assert result["computed"]["counts"]["INFO"] == 1
        assert result["computed"]["counts"]["ADVISORY_WARNING"] == 1

    @pytest.mark.parametrize("state", ["resolved", "waived", "not_applicable"])
    def test_only_open_findings_count(self, question_package, state):
        findings = [_finding(1, "MAJOR", state=state)]
        result = review_consistency(_review(question_package, findings))
        assert result["ok"], result["errors"]
        assert result["computed"]["counts"]["MAJOR"] == 0
        assert result["computed"]["question_qc_status"] == "PASS"

    def test_two_majors_above_a_pass_are_caught(self, question_package):
        """The failure with no other symptom. Well-typed, schema-clean, and routed on."""
        review = _review(question_package, [_finding(1, "MAJOR"), _finding(8, "MAJOR")],
                         status="PASS")
        result = review_consistency(review)
        assert not result["ok"]
        assert any("question_qc_status is 'PASS'" in e and "'FAIL'" in e for e in result["errors"])

    def test_a_miscounted_summary_is_caught(self, question_package):
        review = _review(question_package, [_finding(1, "MAJOR")])
        review["summary"]["counts"]["MAJOR"] = 0
        result = review_consistency(review)
        assert not result["ok"]
        assert any("summary.counts.MAJOR is 0 but 1 open" in e for e in result["errors"])

    def test_group_findings_are_counted_with_item_findings(self, question_package):
        """The schema says the counts cover both blocks; counting only one halves the audit."""
        review = _review(question_package)
        review["group_findings"] = [
            {"group_id": "A", "rule_id": "SC-012", "severity": "MAJOR",
             "evidence": "e", "fix": "f", "state": "open"}
        ]
        review["summary"]["counts"]["MAJOR"] = 1
        review["question_qc_status"] = "FAIL"
        result = review_consistency(review)
        assert result["ok"], result["errors"]
        assert result["computed"]["counts"]["MAJOR"] == 1


class TestTheEnvelopeTurnsInconsistencyIntoARetry:
    """The two gates are only worth having if the pipeline acts on them."""

    def test_a_self_consistent_review_is_accepted(self, question_package):
        review = _review(question_package, [_finding(1, "MAJOR")])
        parsed = _question_audit_envelope(json.dumps(review), "question audit")
        assert parsed["question_qc_status"] == "FAIL"

    def test_a_nine_of_ten_review_is_a_model_call_error(self, question_package):
        review = _review(question_package)
        review["reconstructed_answers"] = review["reconstructed_answers"][:9]
        with pytest.raises(ModelCallError) as exc:
            _question_audit_envelope(json.dumps(review), "question audit")
        assert "disagrees with itself" in str(exc.value)

    def test_a_miscounted_review_is_a_model_call_error(self, question_package):
        review = _review(question_package, [_finding(1, "MAJOR"), _finding(8, "MAJOR")],
                         status="PASS")
        with pytest.raises(ModelCallError):
            _question_audit_envelope(json.dumps(review), "question audit")


class TestTheCrossCheckFindsWhatTheAuditorMightNot:
    """Python comparing the writer's key against the blind reconstruction."""

    def test_an_agreeing_review_is_clean(self, question_package, material):
        result = crosscheck_questions(question_package, _review(question_package), material)
        assert result.ok, result.hard_defects
        assert result.agreed == 10
        assert result.quotes_checked

    def test_a_different_answer_is_a_hard_defect(self, question_package, material):
        review = _review(question_package)
        review["reconstructed_answers"][0]["answer"] = "name"
        result = crosscheck_questions(question_package, review, material)
        assert not result.ok
        assert [r["number"] for r in result.hard_defects] == [1]
        assert result.hard_defects[0]["outcome"] == "answer_divergence"

    def test_an_accepted_alternative_is_not_a_divergence(self, question_package, material):
        """The anti-false-positive that keeps the check trustworthy.

        An auditor writing an answer the key itself accepts has not diverged. Reporting it would
        manufacture a defect out of the writer's own allowance, and the revision would narrow a
        correct item.
        """
        package = copy.deepcopy(question_package)
        package["answer_key"][0]["alternatives"] = ["A. Woods"]
        review = _review(package)
        review["reconstructed_answers"][0]["answer"] = "A. Woods"
        result = crosscheck_questions(package, review, material)
        assert result.ok, result.hard_defects

    def test_punctuation_and_case_are_not_divergences(self, question_package, material):
        review = _review(question_package)
        review["reconstructed_answers"][0]["answer"] = "anna  woods!"
        result = crosscheck_questions(question_package, review, material)
        assert result.ok, result.hard_defects

    def test_a_plural_still_diverges(self, question_package, material):
        """Normalisation must not swallow the Q8 case: `two-bedroom` != `two bedrooms`."""
        package = copy.deepcopy(question_package)
        package["answer_key"][7]["canonical"] = "two-bedroom"
        review = _review(package)
        review["reconstructed_answers"][7]["answer"] = "two bedrooms"
        result = crosscheck_questions(package, review, material)
        assert [r["number"] for r in result.hard_defects] == [8]

    def test_a_hyphen_alone_is_not_a_divergence(self, question_package, material):
        """The other side of the same rule -- the key's own counting rule treats these as one word."""
        package = copy.deepcopy(question_package)
        package["answer_key"][7]["canonical"] = "two-bedroom"
        review = _review(package)
        review["reconstructed_answers"][7]["answer"] = "two bedroom"
        result = crosscheck_questions(package, review, material)
        assert result.ok, result.hard_defects

    def test_an_empty_answer_is_reported_as_unanswerable_not_as_missing(
            self, question_package, material):
        review = _review(question_package)
        review["reconstructed_answers"][3]["answer"] = ""
        result = crosscheck_questions(question_package, review, material)
        assert [r["outcome"] for r in result.hard_defects] == ["no_answer_found"]

    def test_leakage_is_reported_even_when_the_answer_agrees(self, question_package, material):
        """The auditor had no key, so anything it derived from the page a candidate derives too."""
        review = _review(question_package)
        review["reconstructed_answers"][0]["derivable_without_recording"] = True
        result = crosscheck_questions(question_package, review, material)
        assert not result.ok
        assert [r["number"] for r in result.leakage] == [1]
        # The answers still agree, so this must NOT arrive as a hard defect.
        assert result.hard_defects == []

    def test_an_equally_supported_rival_is_reported(self, question_package, material):
        review = _review(question_package)
        review["reconstructed_answers"][7]["competing_candidates"] = [
            {"text": "three bedrooms", "equally_supported": True, "reason": "also fits"}
        ]
        result = crosscheck_questions(question_package, review, material)
        assert not result.ok
        assert [r["number"] for r in result.equally_supported_rivals] == [8]

    def test_a_non_equal_rival_is_not_reported(self, question_package, material):
        """`equally_supported: false` is the auditor saying it considered and rejected a rival."""
        review = _review(question_package)
        review["reconstructed_answers"][7]["competing_candidates"] = [
            {"text": "three bedrooms", "equally_supported": False, "reason": "weaker"}
        ]
        result = crosscheck_questions(question_package, review, material)
        assert result.ok, result.as_dict()

    def test_a_far_anchor_is_a_hard_defect(self, question_package, material):
        review = _review(question_package)
        row = review["reconstructed_answers"][2]
        row["turn_index"] = 0
        row["quote"] = material["listening_material_parts"][0]["script"]["turns"][0]["text"][:20]
        result = crosscheck_questions(question_package, review, material)
        assert [r["outcome"] for r in result.hard_defects] == ["anchor_divergence"]

    def test_an_adjacent_anchor_is_neither_agreement_nor_a_hard_defect(
            self, question_package, material):
        """+-1 is permitted only when the neighbouring turn confirms the same fact.

        That is a reading of two sentences, so the deterministic layer reports it for a human and
        refuses to decide -- which is why it is in ``needs_review`` and in neither of the other two
        buckets.
        """
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package)
        row = review["reconstructed_answers"][0]
        row["turn_index"] = row["turn_index"] + 1
        row["quote"] = turns[row["turn_index"]]["text"][:15]
        result = crosscheck_questions(question_package, review, material)
        assert [r["number"] for r in result.needs_review] == [1]
        assert result.hard_defects == []
        assert 1 not in result.by_outcome.get("agree", [])

    def test_a_quote_from_nowhere_is_a_hard_defect(self, question_package, material):
        """A right answer with an unverifiable anchor is not evidence the item is sound."""
        review = _review(question_package)
        review["reconstructed_answers"][4]["quote"] = "this sentence is in no turn of the script"
        result = crosscheck_questions(question_package, review, material)
        assert [r["outcome"] for r in result.hard_defects] == ["quote_unverifiable"]

    def test_without_the_script_the_quote_check_says_it_did_not_run(self, question_package):
        review = _review(question_package)
        review["reconstructed_answers"][4]["quote"] = "this sentence is in no turn of the script"
        result = crosscheck_questions(question_package, review, None)
        assert result.ok
        # The distinction that must survive: not checked, rather than checked and fine.
        assert result.quotes_checked is False


class TestTheRealQ1AndQ8Divergences:
    """The two MAJORs from the one real audit, reproduced structurally.

    Not a duplicate of the tests above. Those introduce one defect into a clean review; this asserts
    that the two defects a real model actually produced arrive as the right kinds of problem and reach
    the revision instruction, which is the closed loop the whole stage exists to run.
    """

    @staticmethod
    def _real_review(package):
        review = _review(
            package,
            [_finding(1, "MAJOR", "QR-040"), _finding(8, "MAJOR", "AR-012")])
        # Q1: the auditor recovered "name" off the printed form row, without the recording.
        review["reconstructed_answers"][0]["answer"] = "name"
        review["reconstructed_answers"][0]["derivable_without_recording"] = True
        # Q8: it read the confirmed ideal rather than the stated minimum, and named the rival.
        review["reconstructed_answers"][7]["answer"] = "two bedrooms"
        review["reconstructed_answers"][7]["confidence"] = "medium"
        review["reconstructed_answers"][7]["competing_candidates"] = [
            {"text": "three bedrooms", "equally_supported": True,
             "reason": "the carrier does not distinguish the minimum from the ideal"}
        ]
        return review

    def test_both_arrive_as_the_right_kind_of_defect(self, question_package, material):
        package = copy.deepcopy(question_package)
        package["answer_key"][7]["canonical"] = "two-bedroom"
        result = crosscheck_questions(package, self._real_review(package), material)
        assert not result.ok
        assert [r["number"] for r in result.hard_defects] == [1, 8]
        assert [r["number"] for r in result.leakage] == [1]
        assert [r["number"] for r in result.equally_supported_rivals] == [8]

    def test_every_signal_reaches_the_must_fix_list(self, question_package, material):
        package = copy.deepcopy(question_package)
        package["answer_key"][7]["canonical"] = "two-bedroom"
        review = self._real_review(package)
        result = crosscheck_questions(package, review, material)
        instruction = build_question_revise_instruction(review, result, [])
        text = "\n".join(instruction.must_fix)
        assert "QR-040" in text and "AR-012" in text
        assert "leakage" in text and "rival" in text
        # The auditor's own proposed fixes travel with the findings: it is the only party that
        # reconstructed the items without the key.
        assert instruction.must_fix and all("—" in line for line in instruction.must_fix)

    def test_no_must_fix_line_says_the_same_thing_twice(self, question_package, material):
        """Both of these were found on the real Q1/Q8 instruction, not imagined.

        A doubled sentence in a defect list reads like two defects, and a defect list is the one
        document in this pipeline whose length the reader is expected to take literally.
        """
        package = copy.deepcopy(question_package)
        package["answer_key"][7]["canonical"] = "two-bedroom"
        review = self._real_review(package)
        result = crosscheck_questions(package, review, material)
        lines = build_question_revise_instruction(review, result, []).must_fix
        divergence = next(line for line in lines if "answer_divergence" in line)
        # The row's own reason names both answers once. A prefix restating it doubled the sentence.
        assert divergence.count("while the key accepts") == 1
        assert "while the answer key accepts" not in divergence
        rival = next(line for line in lines if "rival" in line)
        assert ".." not in rival

    def test_a_resolved_finding_does_not_demand_a_second_fix(self, question_package, material):
        review = _review(question_package, [_finding(1, "MAJOR", state="resolved")])
        result = crosscheck_questions(question_package, review, material)
        instruction = build_question_revise_instruction(review, result, [])
        assert instruction.must_fix == []

    def test_minor_findings_and_adjacent_anchors_stay_advisory(self, question_package, material):
        """An advisory item presented as an obligation provokes rewrites of compliant questions."""
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package, [_finding(2, "MINOR")])
        row = review["reconstructed_answers"][0]
        row["turn_index"] = row["turn_index"] + 1
        row["quote"] = turns[row["turn_index"]]["text"][:15]
        result = crosscheck_questions(question_package, review, material)
        instruction = build_question_revise_instruction(review, result, ["a band deviation"])
        assert instruction.must_fix == []
        assert len(instruction.advisory) == 3


class TestTheRankingIsLexicographicNotWeighted:
    """One MAJOR must never be outvoted by any number of MINORs."""

    @staticmethod
    def _candidate(package, material, findings, label, errors=()):
        review = _review(package, findings)
        cross = crosscheck_questions(package, review, material)
        review_result = review_consistency(review)
        cross_dict = cross.as_dict()
        cross_dict["consistency"] = review_result
        from backend.deterministic.question_crosscheck import QuestionCrossCheckResult
        from backend.deterministic.validate import ValidationResult
        return QuestionCandidate(package, review, QuestionCrossCheckResult(cross_dict),
                                 ValidationResult(list(errors), [], {}), label)

    def test_one_major_loses_to_three_minors(self, question_package, material):
        major = self._candidate(question_package, material, [_finding(1, "MAJOR")], "initial")
        minors = self._candidate(
            question_package, material,
            [_finding(1, "MINOR"), _finding(2, "MINOR"), _finding(3, "MINOR")], "revised")
        assert pick_better_questions(major, minors) is minors
        # And the asymmetry is real: a weighted sum with any positive MINOR weight would flip this.
        assert minors.key() > major.key()

    def test_a_critical_loses_to_any_number_of_majors(self, question_package, material):
        critical = self._candidate(question_package, material, [_finding(1, "CRITICAL")], "initial")
        majors = self._candidate(
            question_package, material,
            [_finding(i, "MAJOR") for i in range(1, 6)], "revised")
        assert pick_better_questions(critical, majors) is majors

    def test_a_tie_goes_to_the_revision(self, question_package, material):
        before = self._candidate(question_package, material, [_finding(1, "MINOR")], "initial")
        after = self._candidate(question_package, material, [_finding(4, "MINOR")], "revised")
        assert before.key() == after.key()
        assert pick_better_questions(before, after) is after

    def test_a_worse_revision_is_discarded(self, question_package, material):
        before = self._candidate(question_package, material, [], "initial")
        after = self._candidate(question_package, material, [_finding(1, "MAJOR")], "revised")
        assert pick_better_questions(before, after) is before

    def test_cross_check_defects_break_a_findings_tie(self, question_package, material):
        """A silent review whose own reconstruction diverges must not rank as clean."""
        clean = self._candidate(question_package, material, [], "initial")
        silent_review = _review(question_package)
        silent_review["reconstructed_answers"][0]["answer"] = "name"
        cross = crosscheck_questions(question_package, silent_review, material)
        cross_dict = cross.as_dict()
        cross_dict["consistency"] = review_consistency(silent_review)
        from backend.deterministic.question_crosscheck import QuestionCrossCheckResult
        from backend.deterministic.validate import ValidationResult
        silent = QuestionCandidate(question_package, silent_review,
                                   QuestionCrossCheckResult(cross_dict),
                                   ValidationResult([], [], {}), "revised")
        assert silent_review["question_qc_status"] == "PASS"  # it really does claim to be clean
        assert pick_better_questions(clean, silent) is clean

    def test_validator_errors_break_a_remaining_tie(self, question_package, material):
        before = self._candidate(question_package, material, [], "initial")
        after = self._candidate(question_package, material, [], "revised", errors=["QR-021"])
        assert pick_better_questions(before, after) is before

    def test_a_fully_clean_set_needs_no_revision(self, question_package, material):
        assert is_clean_questions(self._candidate(question_package, material, [], "initial"))

    @pytest.mark.parametrize("severity", SEVERITY_ORDER)
    def test_any_graded_finding_triggers_a_revision(self, question_package, material, severity):
        candidate = self._candidate(question_package, material, [_finding(1, severity)], "initial")
        assert not is_clean_questions(candidate)

    def test_leakage_alone_triggers_a_revision(self, question_package, material):
        """Even with a silent auditor -- which is precisely when it matters."""
        review = _review(question_package)
        review["reconstructed_answers"][0]["derivable_without_recording"] = True
        cross = crosscheck_questions(question_package, review, material)
        cross_dict = cross.as_dict()
        cross_dict["consistency"] = review_consistency(review)
        from backend.deterministic.question_crosscheck import QuestionCrossCheckResult
        from backend.deterministic.validate import ValidationResult
        candidate = QuestionCandidate(question_package, review,
                                      QuestionCrossCheckResult(cross_dict),
                                      ValidationResult([], [], {}), "initial")
        assert not is_clean_questions(candidate)


class TestTheDeliveryGate:
    """``question_qc_status`` is not a delivery gate. All four conditions are required together.

    The class exists because the dangerous case is a review that says PASS. Every test here builds a
    set the status alone would wave through, and asserts that the gate does not.
    """

    @staticmethod
    def _candidate(package, material, review, errors=(), warnings=(), label="initial"):
        from backend.deterministic.question_crosscheck import (
            QuestionCrossCheckResult, crosscheck_questions)
        from backend.deterministic.validate import ValidationResult
        cross = crosscheck_questions(package, review, material)
        return QuestionCandidate(package, review, cross,
                                 ValidationResult(list(errors), list(warnings), {}), label)

    def test_a_fully_clean_set_is_deliverable(self, question_package, material):
        candidate = self._candidate(question_package, material, _review(question_package))
        assert delivery_blockers(candidate) == []
        assert is_clean_questions(candidate)

    def test_a_pass_status_does_not_clear_a_cross_check_divergence(
            self, question_package, material):
        """The headline requirement: status PASS, and the set is still not deliverable."""
        review = _review(question_package)
        review["reconstructed_answers"][0]["answer"] = "name"
        candidate = self._candidate(question_package, material, review)
        assert candidate.status == "PASS"
        assert not is_clean_questions(candidate)
        assert any("answer_divergence" in line for line in delivery_blockers(candidate))

    def test_a_validator_error_blocks_a_passing_review(self, question_package, material):
        candidate = self._candidate(question_package, material, _review(question_package),
                                    errors=["QR-021: two answers share a carrier"])
        assert candidate.status == "PASS"
        assert any("validator error" in line for line in delivery_blockers(candidate))

    def test_a_nine_of_ten_review_cannot_be_delivered(self, question_package, material):
        """Coverage is read from the recompute, not from the review's claim about itself."""
        review = _review(question_package)
        review["reconstructed_answers"] = review["reconstructed_answers"][:9]
        review["coverage"]["reviewed_question_ids"] = list(range(1, 10))
        candidate = self._candidate(question_package, material, review)
        blockers = delivery_blockers(candidate)
        assert any("not all ten items" in line for line in blockers)

    def test_nine_of_ten_agreement_blocks_even_with_no_hard_defect(
            self, question_package, material):
        """An adjacent anchor is not agreement, so 9/10 agreed is not a deliverable 10/10."""
        review = _review(question_package)
        row = review["reconstructed_answers"][2]
        row["turn_index"] = row["turn_index"] + 1
        turns = material["listening_material_parts"][0]["script"]["turns"]
        row["quote"] = turns[row["turn_index"]]["text"][:15]
        candidate = self._candidate(question_package, material, review)
        assert candidate.cross_check.hard_defects == []      # nothing hard...
        assert candidate.cross_check.needs_review            # ...but not agreement either
        blockers = delivery_blockers(candidate)
        assert any("agrees on 9 of 10" in line for line in blockers)
        assert not is_clean_questions(candidate)

    def test_a_validator_warning_blocks_and_says_so_verbatim(self, question_package, material):
        """The deliberate strictness, pinned so a future relaxation is a visible decision.

        This is the condition that can send an otherwise clean set to REGENERATE_MATERIAL: the QR-026
        ceiling warning fires when a set is AT a legal limit, and the real material carries it. The test
        asserts the blocker quotes the warning, because the cost of this choice must be legible in the
        failure detail rather than looking like a real defect.
        """
        warning = "end-of-line blanks are at the QR-026 ceiling (7 of 10); one more would fail"
        candidate = self._candidate(question_package, material, _review(question_package),
                                    warnings=[warning])
        blockers = delivery_blockers(candidate)
        assert blockers == ["validator warning: %s" % warning]
        assert not is_clean_questions(candidate)

    def test_every_blocker_is_reported_not_just_the_first(self, question_package, material):
        review = _review(question_package, [_finding(2, "MAJOR")])
        review["reconstructed_answers"][0]["answer"] = "name"
        review["reconstructed_answers"][0]["derivable_without_recording"] = True
        candidate = self._candidate(question_package, material, review, errors=["QR-021"])
        blockers = delivery_blockers(candidate)
        assert len(blockers) >= 4
        assert any("MAJOR" in b for b in blockers)
        assert any("answer_divergence" in b for b in blockers)
        assert any("printed page" in b for b in blockers)
        assert any("validator error" in b for b in blockers)

    def test_a_failed_result_cannot_be_built_with_a_deliverable_candidate(
            self, question_package, material):
        candidate = self._candidate(question_package, material, _review(question_package))
        with pytest.raises(ValueError) as exc:
            QuestionResult(False, candidate=candidate)
        assert "rejected_candidate" in str(exc.value)


class TestTheScriptCannotBeRevised:
    """SR-021. The audio may already exist, so a script edit does not fix an item."""

    def test_a_package_alone_is_accepted(self, question_package):
        _assert_script_untouched(question_package)

    @pytest.mark.parametrize("key", ["material", "listening_material_parts", "script"])
    def test_a_returned_script_is_refused(self, question_package, key):
        package = copy.deepcopy(question_package)
        package[key] = {"anything": True}
        with pytest.raises(ScriptWasEdited) as exc:
            _assert_script_untouched(package)
        assert key in str(exc.value)
        assert "SR-021" in str(exc.value)


class TestTheLoopWiring:
    """``run_questions`` itself: which steps run, in what order, and what reaches the auditor.

    Every model call and the two script calls are stubbed. What is being tested is the orchestration --
    that a clean set skips the revision, that a defective one is revised and re-audited, and above all
    that the auditor is never handed the answer key. That last one is the property with no symptom: a
    loop that passed the whole package to the audit step would produce a review agreeing on every item
    and a cross-check reporting perfect agreement.
    """

    @pytest.fixture
    def harness(self, question_package, blueprint, material, monkeypatch):
        from backend.deterministic.validate import ValidationResult
        from backend.orchestration import question_loop as loop_module

        class Harness:
            def __init__(self):
                self.calls = []
                self.audit_inputs = []
                self.reviews = []
                self.revised_package = copy.deepcopy(question_package)
                self.validation = ValidationResult([], [], {})

            async def generate_questions(self, mat, bp):
                self.calls.append("generate")
                return copy.deepcopy(question_package)

            async def validate_questions(self, mat, bp, package):
                self.calls.append("validate")
                return self.validation

            async def audit_questions_blind(self, mat, face, metrics):
                self.calls.append("audit")
                self.audit_inputs.append((mat, face, metrics))
                return self.reviews.pop(0)

            async def revise_questions(self, mat, bp, package, instruction):
                self.calls.append("revise")
                self.instruction = instruction
                return self.revised_package

        h = Harness()
        monkeypatch.setattr(loop_module.agent_steps, "generate_questions", h.generate_questions)
        monkeypatch.setattr(loop_module, "validate_questions", h.validate_questions)
        monkeypatch.setattr(
            loop_module.agent_steps, "audit_questions_blind", h.audit_questions_blind)
        monkeypatch.setattr(loop_module.agent_steps, "revise_questions", h.revise_questions)
        h.material, h.blueprint = material, blueprint
        return h

    @pytest.mark.asyncio
    async def test_a_clean_set_skips_the_revision(self, harness, question_package):
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package)]
        result = await run_questions(harness.material, harness.blueprint)
        assert result.ok
        assert result.selected_version == "initial"
        assert harness.calls == ["generate", "validate", "audit"]

    @pytest.mark.asyncio
    async def test_a_defective_set_is_revised_and_re_audited(self, harness, question_package):
        from backend.orchestration.question_loop import run_questions

        defective = _review(question_package, [_finding(1, "MAJOR")])
        harness.reviews = [defective, _review(question_package)]
        result = await run_questions(harness.material, harness.blueprint)
        assert harness.calls == ["generate", "validate", "audit", "revise", "validate", "audit"]
        assert result.ok
        assert result.selected_version == "revised-1"
        assert result.rounds == 1
        assert harness.instruction.must_fix

    @pytest.mark.asyncio
    async def test_a_second_round_runs_when_the_first_revision_is_still_blocked(
            self, harness, question_package):
        """The change this contract is for: round one improves the set but does not clear it."""
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [
            _review(question_package, [_finding(1, "MAJOR"), _finding(8, "MAJOR")]),
            _review(question_package, [_finding(8, "MINOR")]),   # Q1 fixed, Q8 downgraded
            _review(question_package),                            # Q8 cleared
        ]
        result = await run_questions(harness.material, harness.blueprint)
        assert harness.calls == ["generate", "validate", "audit",
                                 "revise", "validate", "audit",
                                 "revise", "validate", "audit"]
        assert result.ok
        assert result.selected_version == "revised-2"
        assert result.rounds == 2

    @pytest.mark.asyncio
    async def test_the_loop_stops_at_two_revisions_and_asks_for_a_new_material(
            self, harness, question_package):
        from backend.orchestration.question_loop import (
            MAX_QUESTION_REVISIONS, QUESTIONS_NOT_DELIVERABLE, run_questions)
        from backend.deterministic.feasibility import REGENERATE_MATERIAL

        harness.reviews = [_review(question_package, [_finding(1, "MAJOR")]) for _ in range(3)]
        result = await run_questions(harness.material, harness.blueprint)
        assert harness.calls.count("revise") == MAX_QUESTION_REVISIONS
        assert harness.calls.count("audit") == MAX_QUESTION_REVISIONS + 1
        assert not result.ok
        assert result.reason == QUESTIONS_NOT_DELIVERABLE
        assert result.outcome == REGENERATE_MATERIAL
        assert result.rounds == 2

    @pytest.mark.asyncio
    async def test_a_rejected_set_is_never_reachable_as_a_delivered_one(
            self, harness, question_package):
        """The contract's core prohibition: no "current best but still defective" delivery.

        Asserted on the attribute a caller would actually read to ship, not on the flag it is supposed
        to check first -- because the failure being designed out is a forgotten ``if result.ok``.
        """
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package, [_finding(1, "CRITICAL")]) for _ in range(3)]
        result = await run_questions(harness.material, harness.blueprint)
        assert result.candidate is None
        assert result.rejected_candidate is not None
        assert result.blockers
        payload = result.as_dict()
        assert payload["ok"] is False
        assert "package" not in payload
        assert payload["rejected_candidate"]["package"] is not None

    @pytest.mark.asyncio
    async def test_a_revision_that_makes_things_worse_does_not_become_the_verdict(
            self, harness, question_package):
        """``pick_better_questions`` keeps the better DIAGNOSIS; neither version is delivered."""
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package, [_finding(1, "MINOR")]),
                           _review(question_package, [_finding(1, "CRITICAL")]),
                           _review(question_package, [_finding(1, "CRITICAL")])]
        result = await run_questions(harness.material, harness.blueprint)
        assert not result.ok
        # The initial WARNING set is the least defective of the three and is what the report carries.
        assert result.rejected_candidate.label == "initial"
        assert result.rejected_candidate.status == "WARNING"

    @pytest.mark.asyncio
    async def test_an_unactionable_blocker_stops_the_loop_without_delivering(
            self, harness, question_package):
        """Blocked with an empty instruction used to DELIVER. It must now refuse.

        A validator error the instruction builder does not translate into prose leaves nothing to ask
        for -- which is not evidence the defect is gone.
        """
        from backend.deterministic.validate import ValidationResult
        from backend.orchestration.question_loop import run_questions

        harness.validation = ValidationResult(["QR-021: two answers share a carrier"], [], {})
        harness.reviews = [_review(question_package)]
        result = await run_questions(harness.material, harness.blueprint)
        assert harness.calls == ["generate", "validate", "audit"]
        assert not result.ok
        assert result.rounds == 0
        assert any("QR-021" in line for line in result.blockers)

    @pytest.mark.asyncio
    async def test_the_auditor_never_receives_the_answer_key(self, harness, question_package):
        """The property the whole stage rests on, asserted on what the loop actually passes."""
        from backend.deterministic.guards import assert_answer_blind
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package, [_finding(1, "MAJOR")]),
                           _review(question_package)]
        await run_questions(harness.material, harness.blueprint)
        assert len(harness.audit_inputs) == 2
        for mat, face, metrics in harness.audit_inputs:
            assert "answer_key" not in face and "evidence" not in face
            assert "answer_key" not in metrics
            # And the real guard agrees, over the payload actually built from these three.
            assert_answer_blind(json.dumps([mat, face, metrics], ensure_ascii=False))

    @pytest.mark.asyncio
    async def test_a_revision_returning_a_script_aborts_the_loop(
            self, harness, question_package):
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package, [_finding(1, "MAJOR")])]
        harness.revised_package = copy.deepcopy(question_package)
        harness.revised_package["material"] = {"edited": True}
        with pytest.raises(ScriptWasEdited):
            await run_questions(harness.material, harness.blueprint)
        # The re-audit never ran: an edited script means the questions describe something nobody hears.
        assert harness.calls.count("audit") == 1

    @pytest.mark.asyncio
    async def test_validator_warnings_reach_the_instruction_as_advisory(
            self, harness, question_package):
        from backend.deterministic.validate import ValidationResult
        from backend.orchestration.question_loop import run_questions

        harness.validation = ValidationResult([], ["a band deviation"], {})
        # Three reviews, not two: the warning itself blocks delivery, so both rounds run even though
        # the audit comes back clean. That is the QR-026-ceiling behaviour pinned in
        # TestTheDeliveryGate, seen here from the loop's side.
        harness.reviews = [_review(question_package, [_finding(1, "MAJOR")]),
                           _review(question_package),
                           _review(question_package)]
        result = await run_questions(harness.material, harness.blueprint)
        assert any("band deviation" in line for line in harness.instruction.advisory)
        assert not any("band deviation" in line for line in harness.instruction.must_fix)
        assert not result.ok
        assert result.blockers == ["validator warning: a band deviation"]


class TestTheQuestionEnvelope:
    """A reply that is a validator report, or a short set, must not be delivered as questions."""

    def test_a_complete_package_is_accepted(self, question_package):
        parsed = _question_envelope(json.dumps(question_package), "question generation")
        assert len(parsed["question_face"]["questions"]) == 10

    def test_a_validator_report_first_is_rejected(self, question_package):
        """The skill's workflow ends in a validator run, so its report is in the reply by nature."""
        reply = ('{"ok": true, "errors": [], "warnings": []}\n\n'
                 + json.dumps(question_package))
        with pytest.raises(ModelCallError) as exc:
            _question_envelope(reply, "question generation")
        assert "question-package blocks" in str(exc.value)

    def test_a_short_set_is_rejected(self, question_package):
        package = copy.deepcopy(question_package)
        package["question_face"]["questions"] = package["question_face"]["questions"][:9]
        with pytest.raises(ModelCallError) as exc:
            _question_envelope(json.dumps(package), "question generation")
        assert "9 questions, not 10" in str(exc.value)

    @pytest.mark.parametrize("key", ["question_face", "answer_key", "evidence", "material_id"])
    def test_every_required_block_is_enforced(self, question_package, key):
        package = copy.deepcopy(question_package)
        del package[key]
        with pytest.raises(ModelCallError):
            _question_envelope(json.dumps(package), "question generation")
