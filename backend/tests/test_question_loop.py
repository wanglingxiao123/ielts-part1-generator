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
    WARNING_STATUS,
    _assert_script_untouched,
    advisory_notes,
    delivered_status,
    delivery_blockers,
    hard_blockers,
    sole_adjacency_release,
    is_clean_questions,
    is_deliverable,
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

    def test_an_off_by_one_index_with_a_real_quote_is_not_unverifiable(
            self, question_package, material):
        """The measured regression: an auditor that counts a narration turn shifts every later index.

        On the real re-audit this produced five ``quote_unverifiable`` rows whose answers matched the key
        exactly, dropped agreement to 4/10 and rejected a sound set -- because the unverifiable branch
        returned before the adjacency logic could see a one-turn gap.

        The quote here is verbatim from the writer's OWN anchor turn, so once it is resolved the two
        anchors coincide and the row is agreement, not merely advisory: the only thing that ever differed
        was the auditor's arithmetic. The shift is still recorded on the row, so a systematic mis-count
        stays legible instead of vanishing into a clean result.
        """
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package)
        row = review["reconstructed_answers"][0]
        writer_turn = row["turn_index"]
        # Quote the turn the writer anchored, but claim the next index -- exactly the observed shift.
        row["quote"] = turns[writer_turn]["text"][:15]
        row["turn_index"] = writer_turn + 1
        result = crosscheck_questions(question_package, review, material)
        assert result.by_outcome.get("quote_unverifiable") is None
        assert 1 in result.by_outcome["agree"]
        row_out = next(r for r in result.items if r["number"] == 1)
        assert row_out["stated_turn_shift"] == -1
        assert row_out["effective_auditor_turn"] == writer_turn

    def test_a_quote_in_neither_neighbour_is_still_unverifiable(
            self, question_package, material):
        """The window is +-1, not unbounded: a quote two turns away stays a hard defect."""
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package)
        row = review["reconstructed_answers"][0]
        row["quote"] = turns[row["turn_index"]]["text"][:15]
        row["turn_index"] = row["turn_index"] + 2
        result = crosscheck_questions(question_package, review, material)
        assert [r["outcome"] for r in result.hard_defects] == ["quote_unverifiable"]

    def test_a_one_turn_gap_across_a_narrator_window_is_a_hard_defect(
            self, question_package, material):
        """All three conditions are required. Different windows means no +-1 allowance.

        Turn 21 is the middle narration turn, so 21 and 22 are one apart and in different windows -- the
        case where "the neighbouring turn confirms the same fact" is least likely to hold, since a
        narration turn sits between them.
        """
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package)
        package = copy.deepcopy(question_package)
        evidence = next(e for e in package["evidence"] if e["number"] == 1)
        evidence["turn_index"] = 21
        row = review["reconstructed_answers"][0]
        row["turn_index"] = 22
        row["quote"] = turns[22]["text"][:15]
        result = crosscheck_questions(package, review, material)
        hard = [r for r in result.hard_defects if r["number"] == 1]
        assert [r["outcome"] for r in hard] == ["anchor_divergence"]
        assert "different narrator windows" in hard[0]["reason"]
        assert result.needs_review == []

    def test_a_one_turn_gap_without_proposition_alignment_is_a_hard_defect(
            self, question_package, material):
        """The third condition, and the one an auditor cannot grant itself.

        ``proposition_alignment_result`` is the writer's own claim that the quote and the carrier state
        the same fact. Without it there is nothing supporting the +-1 allowance's precondition, so the
        gap is hard rather than advisory -- an unknown must not buy a release.
        """
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package)
        package = copy.deepcopy(question_package)
        evidence = next(e for e in package["evidence"] if e["number"] == 1)
        evidence["proposition_alignment_result"] = "derived"
        row = review["reconstructed_answers"][0]
        row["turn_index"] = row["turn_index"] + 1
        row["quote"] = turns[row["turn_index"]]["text"][:15]
        result = crosscheck_questions(package, review, material)
        hard = [r for r in result.hard_defects if r["number"] == 1]
        assert [r["outcome"] for r in hard] == ["anchor_divergence"]
        assert "not marked proposition-aligned" in hard[0]["reason"]

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


class TestTheRealRevisedTwoAdjacencies:
    """The 2026-08-08 run's ``revised-2`` candidate, and the two things a one-turn gap can be.

    Fixture reconstructed from the wire frame of request ``web-1786...-slot-1`` (material
    ``harbour-view-hotel-reservation``), whose third question package reported::

        {"version": "revised-2", "agreed": 7, "hard_defects": 0, "leakage": 0, "rivals": 0,
         "status": "PASS", "by_outcome": {"agree": [3, 4, 6, 7, 8, 9, 10],
                                          "anchor_adjacent": [1, 2, 5]}}

    and was still withheld, on **four** blockers. Its own script was never persisted -- the auditor's
    review is written nowhere, which is why the exact turns Q1/Q2/Q5 named cannot be read back -- so the
    frame is replayed against the committed material: same three item numbers, same window, same
    ``aligned`` evidence, same 7/10, and the writer's anchors likewise every one ``aligned``.

    What the shape does not say is which of two situations produced it, and they get opposite treatment:

    * the auditor quoted the **writer's own sentence** under an index one out. One sentence, no reading
      to do, so it agrees -- and is recorded rather than absorbed, because five of them in a set is an
      auditor mis-counting the narration.
    * the auditor quoted the **neighbouring sentence**. Two sentences, and whether the neighbour
      confirms the same fact is a reading no integer performs. Stays hard, and a uniquely located quote
      does not change that: locating a quote establishes which sentence was read, never that two
      sentences state one fact.

    Both are asserted here together on purpose. A change that promoted the second to agreement would
    make the first test pass and this one fail, which is the only way to keep the release narrow.
    """

    @staticmethod
    def _drifted_index(review, turns, numbers):
        """The auditor quotes the writer's sentence verbatim, having written the index one too high."""
        for row in review["reconstructed_answers"]:
            if row["number"] in numbers:
                row["turn_index"] = row["turn_index"] + 1
        return review

    @staticmethod
    def _quoted_the_neighbour(review, turns, numbers):
        """The auditor read the *next* turn and anchored there -- a genuinely different sentence."""
        for row in review["reconstructed_answers"]:
            if row["number"] in numbers:
                row["turn_index"] = row["turn_index"] + 1
                row["quote"] = turns[row["turn_index"]]["text"]
        return review

    def test_a_mis_stated_index_is_agreement_and_says_so(self, question_package, material):
        """Q1/Q2/Q5 reach 10/10, and the normalisation is visible in the result rather than silent."""
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = self._drifted_index(_review(question_package), turns, (1, 2, 5))
        result = crosscheck_questions(question_package, review, material)
        assert result.agreed == 10
        assert result.by_outcome == {"agree": NUMBERS}
        assert [row["number"] for row in result.adjacency_normalised] == [1, 2, 5]
        for row in result.adjacency_normalised:
            evidence = {e["number"]: e for e in question_package["evidence"]}
            assert row["normalised_turn"] == evidence[row["number"]]["turn_index"]
            assert row["quote_pins_one_turn"] is True
            assert "mis-stated index rather than a second reading" in row["reason"]

    def test_quoting_the_neighbouring_sentence_stays_hard(self, question_package, material):
        """The residual on the real run, and it is not an arithmetic problem. It must not be waved on.

        Every release condition holds here -- the answers match, one window, the writer's evidence is
        ``aligned``, and the quote pins exactly one turn -- and it still does not agree, because the two
        anchors are two sentences.
        """
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = self._quoted_the_neighbour(_review(question_package), turns, (1, 2, 5))
        result = crosscheck_questions(question_package, review, material)
        assert result.agreed == 7
        assert result.by_outcome == {"agree": [3, 4, 6, 7, 8, 9, 10],
                                    "anchor_adjacent": [1, 2, 5]}
        assert result.hard_defects == []
        assert result.adjacency_normalised == []
        assert [row["number"] for row in result.needs_review] == [1, 2, 5]
        for row in result.needs_review:
            assert row["quote_pins_one_turn"] is True     # located, and still not agreement
            assert "two different sentences one turn apart" in row["reason"]

    def test_three_adjacencies_are_three_notes_not_four_blockers(self, question_package, material):
        """The fourth blocker on the real run was the first three added up.

        ``agrees on 7 of 10`` restates exactly the three lines above it. Charging it again inflates the
        count and makes a set look further from deliverable than it is -- and it is the line that would
        silently re-block a released set, because it is stated *about the set* rather than per item. So
        the count is asserted here on the release path too, not only on the hard one.

        The three lines themselves moved from hard to advisory on 2026-08-08 (see
        :func:`sole_adjacency_release`). The count did not.
        """
        from backend.deterministic.validate import ValidationResult
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = self._quoted_the_neighbour(_review(question_package), turns, (1, 2, 5))
        result = crosscheck_questions(question_package, review, material)
        candidate = QuestionCandidate(question_package, review, result,
                                      ValidationResult([], [], {}), "revised-2")
        assert hard_blockers(candidate) == []
        notes = advisory_notes(candidate)
        assert len(notes) == 3
        assert [line for line in notes if "agrees on" in line] == []
        assert all("evidence anchor is one turn" in line for line in notes)
        assert is_deliverable(candidate)
        assert delivered_status(candidate) == WARNING_STATUS

    def test_an_unnamed_shortfall_is_still_stated(self, question_package, material):
        """The suppression is narrow: it holds only while the shortfall is what was already named.

        Every outcome that is not ``agree`` currently produces a line of its own -- hard defects one by
        one, ``anchor_adjacent`` one by one -- so on today's classification the two totals always match
        and the restatement is always the arithmetic. That is what makes suppressing it safe, and also
        what makes it untestable from a review alone: no reconstruction can produce a shortfall with
        nothing named. So the condition is forced directly on the result. A new outcome that counted
        against agreement without being listed would be a hole in this report, and the guard is what
        keeps it from passing in silence.
        """
        from backend.deterministic.validate import ValidationResult
        review = _review(question_package)
        review["reconstructed_answers"][4]["answer"] = ""       # Q5: no_answer_found -- hard, named
        result = crosscheck_questions(question_package, review, material)
        candidate = QuestionCandidate(question_package, review, result,
                                      ValidationResult([], [], {}), "revised-2")
        assert result.agreed == 9
        assert len(result.hard_defects) == 1
        assert [line for line in hard_blockers(candidate) if "agrees on" in line] == []
        result.agreed = 7           # two more items short of agreement, neither of them listed
        assert any("agrees on 7 of 10 items beyond the 1 already listed" in line
                   for line in hard_blockers(candidate))

    def test_an_ambiguous_quote_pins_nothing_and_never_promotes(self, question_package, material):
        """A span in two turns of the neighbourhood resolves to the declared one and looks located.

        ``bedroom`` occurs in turns 35, 36 and 37 of this script -- the exact shape both the writer's
        validator and the auditor's instructions now reject, because it leaves the reconciliation with
        several candidates and no way to choose. It must not be read as evidence of anything.
        """
        review = _review(question_package)
        row = review["reconstructed_answers"][8]        # Q9, writer's anchor is turn 37
        row["turn_index"] = 36
        row["quote"] = "bedroom"
        result = crosscheck_questions(question_package, review, material)
        parked = next(item for item in result.items if item["number"] == 9)
        assert parked["outcome"] == "anchor_adjacent"
        assert parked["quote_pins_one_turn"] is False
        assert "does not even pin one of them" in parked["reason"]
        assert result.adjacency_normalised == []

    def test_the_auditors_unique_quote_repairs_a_drifted_anchor(
            self, question_package, material):
        """A unique verbatim quote proves which turn the auditor read despite a mistyped integer."""
        turns = material["listening_material_parts"][0]["script"]["turns"]
        review = _review(question_package)
        original_turns = {
            row["number"]: row["turn_index"] for row in review["reconstructed_answers"]
        }
        review = self._drifted_index(review, turns, (1, 2, 5))
        parsed = _question_audit_envelope(json.dumps(review), "question audit", material)
        for number in (1, 2, 5):
            assert (
                parsed["reconstructed_answers"][number - 1]["turn_index"]
                == original_turns[number]
            )
        # Without the script the check cannot run, and not running is not the same as passing.
        assert _question_audit_envelope(json.dumps(review), "question audit")

    def test_an_ambiguous_quote_anchor_is_still_rejected(
            self, question_package, material):
        turns = material["listening_material_parts"][0]["script"]["turns"]
        turns[1]["text"] = "The repeated evidence phrase."
        turns[2]["text"] = "The repeated evidence phrase."
        review = _review(question_package)
        row = review["reconstructed_answers"][0]
        row["turn_index"] = 0
        row["quote"] = "repeated evidence phrase"
        with pytest.raises(ModelCallError) as exc:
            _question_audit_envelope(json.dumps(review), "question audit", material)
        assert "anchors quotes on turns they are not in" in str(exc.value)
        assert row["turn_index"] == 0

    def test_a_clean_review_passes_the_new_anchor_check(self, question_package, material):
        """The anti-false-positive half. Every quote in the turn it names, and nothing fires."""
        assert _question_audit_envelope(json.dumps(_review(question_package)),
                                        "question audit", material)


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


class TestTheInstructionNeverOffersTheAnswerKeyAsAnEscape:
    """A two-fact conflict must be fixed in the carrier. ``alternatives`` is for one fact, spelled twice.

    The prohibition exists because the wrong fix is the cheap one and is undetectable afterwards: one
    array entry silences the cross-check for good, and a key accepting both Q8's ``two bedrooms``
    minimum and its ``three-bedroom`` ideal marks a candidate correct for answering a question the
    carrier did not ask.
    """

    @staticmethod
    def _cross(package, material, review):
        return crosscheck_questions(package, review, material)

    def test_a_rival_instruction_forbids_widening_the_key(self, question_package, material):
        review = _review(question_package)
        review["reconstructed_answers"][7]["competing_candidates"] = [
            {"text": "three bedrooms", "equally_supported": True,
             "reason": "the script also states a three-bedroom property would be ideal."}]
        cross = self._cross(question_package, material, review)
        instruction = build_question_revise_instruction(review, cross)
        line = next(l for l in instruction.must_fix if "cross-check rival" in l)
        assert "narrow the carrier" in line
        assert "Do NOT add the rival to `alternatives`" in line
        # And the old wording is gone: it invited exactly the edit now prohibited.
        assert "accept both in the answer key" not in line

    def test_a_divergence_instruction_carries_the_same_prohibition(
            self, question_package, material):
        """Where the real Q8 actually surfaced, in both rounds."""
        review = _review(question_package)
        review["reconstructed_answers"][7]["answer"] = "two bedrooms"
        cross = self._cross(question_package, material, review)
        instruction = build_question_revise_instruction(review, cross)
        line = next(l for l in instruction.must_fix if "answer_divergence" in l)
        assert "Do NOT add the rival to `alternatives`" in line
        assert "SAME fact written differently" in line

    def test_no_must_fix_line_anywhere_suggests_widening_the_key(
            self, question_package, material):
        """Asserted over the whole instruction, not one line, so a future addition cannot reintroduce it."""
        review = _review(question_package, [_finding(8, "MAJOR")])
        review["reconstructed_answers"][7]["answer"] = "two bedrooms"
        review["reconstructed_answers"][7]["derivable_without_recording"] = True
        review["reconstructed_answers"][7]["competing_candidates"] = [
            {"text": "three bedrooms", "equally_supported": True, "reason": "also fits."}]
        cross = self._cross(question_package, material, review)
        instruction = build_question_revise_instruction(review, cross, ["a warning"])
        blob = " ".join(instruction.must_fix + instruction.advisory)
        assert "accept both" not in blob
        for phrase in ("add both to the answer key", "widen the answer key"):
            assert phrase not in blob


class TestTheDeliveryGate:
    """``question_qc_status`` is not a delivery gate, and neither is "anything is open".

    The dangerous case in one direction is a review that says PASS: every test that perturbs one
    deterministic signal builds a set the status alone would wave through, and asserts the gate does
    not. The dangerous case in the *other* direction is the one production found -- a fair set held back
    over a cosmetic note -- so the tests here assert both, and the pairs are deliberately adjacent so a
    change that satisfies one by breaking the other cannot pass.
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
        assert is_deliverable(candidate)
        assert advisory_notes(candidate) == []

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

    def test_nine_of_ten_agreement_is_not_clean_but_is_now_deliverable(
            self, question_package, material):
        """An adjacent anchor is still not agreement -- and on its own it no longer withholds the set.

        This test asserted ``not is_deliverable`` until 2026-08-08, and the change of that one line is
        the whole of the adjacency release. What it protected was correct and is unchanged: the anchor
        gap is real, the set is NOT clean, and the gap is named. What it also did was destroy fair
        materials over it -- seven of the eight rejections on batch ``web-1786166271869-1`` were this
        exact shape and nothing else (see :class:`TestSoleAdjacencyShipsAsWarning`).

        The shortfall total is still *not* restated: one non-agreeing item that already has its own line
        makes "agrees on 9 of 10" that same line arithmetic, and charging it twice is what turned three
        real adjacencies into four blockers. That the line moved from hard to advisory must not
        resurrect it.
        """
        review = _review(question_package)
        row = review["reconstructed_answers"][2]
        row["turn_index"] = row["turn_index"] + 1
        turns = material["listening_material_parts"][0]["script"]["turns"]
        row["quote"] = turns[row["turn_index"]]["text"][:15]
        candidate = self._candidate(question_package, material, review)
        assert candidate.cross_check.hard_defects == []      # nothing hard...
        assert candidate.cross_check.needs_review            # ...but not agreement either
        blockers = delivery_blockers(candidate)
        assert any("Q3's evidence anchor is one turn" in line for line in blockers)
        assert not any("agrees on" in line for line in blockers)
        assert len(blockers) == 1
        assert not is_clean_questions(candidate)             # the gap is on the record...
        assert is_deliverable(candidate)                     # ...and no longer costs the material
        assert hard_blockers(candidate) == []
        assert delivered_status(candidate) == WARNING_STATUS

    def test_the_qr026_ceiling_warning_does_not_block(self, question_package, material):
        """AT a legal cap is not a defect: the validator already ruled the set legal.

        This warning is emitted on the ``counts["final"] == MAX_FINAL_BLANKS`` branch, the branch above
        it being the error. Blocking on it cost the real material two revision rounds and a discarded
        compliant question set.
        """
        warning = "end-of-line blanks are at the QR-026 ceiling (7 of 10); one more would fail"
        candidate = self._candidate(question_package, material, _review(question_package),
                                    warnings=[warning])
        assert delivery_blockers(candidate) == []
        assert is_clean_questions(candidate)

    def test_exceeding_the_ceiling_is_an_error_and_still_blocks(self, question_package, material):
        """The other side of the same rule: over the cap is a validator ERROR, which blocks.

        Pinned with the validator's own wording so the two branches cannot be confused: only the
        at-ceiling *warning* is waved through, and nothing about this change touches the error path.
        """
        error = ("8 of 10 blanks sit at the end of their line; QR-026 caps end-of-line blanking at 7")
        candidate = self._candidate(question_package, material, _review(question_package),
                                    errors=[error])
        assert any("validator error" in line for line in delivery_blockers(candidate))
        assert not is_clean_questions(candidate)

    def test_any_other_validator_warning_still_blocks(self, question_package, material):
        """The exemption is narrow. A warning that describes something fixable keeps blocking."""
        warning = ("part of Q9's answer 'guest room' appears in group 'D''s visible text (['guest']); "
                   "check it does not narrow the answer to one candidate")
        candidate = self._candidate(question_package, material, _review(question_package),
                                    warnings=[warning])
        assert delivery_blockers(candidate) == ["validator warning: %s" % warning]
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


class TestMinorFindingsShipAsWarning:
    """The production regression: a fair set must not be withheld over an improvable note.

    Fixture taken from the real 2026-08-07 acceptance run, invocation 4 of request
    ``web-1786110313583-1-slot-1`` (scenario ``accommodation-rental``, material
    ``20260807-accommodation-rental-f6e76f11``). Its ``initial`` candidate reported, on the wire::

        {"version": "initial", "agreed": 10, "by_outcome": {"agree": [1..10]},
         "hard_defects": 0, "leakage": 0, "rivals": 0, "status": "WARNING"}

    and was blocked by exactly ``["1 open MINOR finding(s) in the blind audit"]``. Both revision rounds
    then made it strictly worse (round 1: a MAJOR, two ``answer_divergence``, one rival; round 2: two
    MAJORs and leakage), so the run ended ``REGENERATE_MATERIAL`` -- and across five invocations on two
    scenarios nothing was ever delivered.

    The numbers above are what these tests reconstruct: everything deterministic clean, one MINOR open.
    """

    @staticmethod
    def _candidate(package, material, review, errors=(), warnings=(), label="initial"):
        from backend.deterministic.question_crosscheck import crosscheck_questions
        from backend.deterministic.validate import ValidationResult
        cross = crosscheck_questions(package, review, material)
        return QuestionCandidate(package, review, cross,
                                 ValidationResult(list(errors), list(warnings), {}), label)

    @pytest.fixture
    def production_initial(self, question_package, material):
        """The measured candidate: 10/10 agreed, 0 hard defects, 0 leakage, 0 rivals, one MINOR."""
        return self._candidate(question_package, material,
                               _review(question_package, [_finding(7, "MINOR")]))

    def test_the_measured_candidate_matches_the_wire(self, production_initial):
        """Pin the fixture against the recorded frame before asserting anything about the gate.

        Without this the class could drift into testing a set that never existed, and the whole point of
        the fixture is that this exact shape reached production.
        """
        cross = production_initial.cross_check
        assert cross.agreed == 10 and cross.compared == 10
        assert cross.hard_defects == [] and cross.leakage == []
        assert cross.equally_supported_rivals == [] and cross.needs_review == []
        assert production_initial.status == WARNING_STATUS
        assert production_initial.counts["MINOR"] == 1

    def test_it_is_deliverable(self, production_initial):
        """The one assertion the whole change exists for."""
        assert hard_blockers(production_initial) == []
        assert is_deliverable(production_initial)

    def test_the_minor_is_reported_as_an_advisory_not_a_blocker(self, production_initial):
        assert advisory_notes(production_initial) == ["1 open MINOR finding(s) in the blind audit"]
        # The exact string the production run printed as its blocker is now an advisory, and
        # `delivery_blockers` still reports it -- it moved category, it did not disappear.
        assert delivery_blockers(production_initial) == [
            "1 open MINOR finding(s) in the blind audit"]

    def test_it_is_not_called_clean(self, production_initial):
        """Deliverable and clean are now different questions, and the weaker one must not claim both."""
        assert not is_clean_questions(production_initial)

    @pytest.mark.parametrize("severity", ("CRITICAL", "MAJOR"))
    def test_the_same_finding_at_a_blocking_severity_still_blocks(
            self, question_package, material, severity):
        """The boundary, from the same fixture: only the severity changes and the verdict flips."""
        candidate = self._candidate(question_package, material,
                                    _review(question_package, [_finding(7, severity)]))
        assert hard_blockers(candidate) == ["1 open %s finding(s) in the blind audit" % severity]
        assert not is_deliverable(candidate)
        assert advisory_notes(candidate) == []

    def test_a_minor_does_not_rescue_a_set_with_a_hard_defect(self, question_package, material):
        """A MINOR alone ships; a MINOR next to a deterministic defect does not.

        The deterministic signals come from Python comparing the key against the blind reconstruction,
        so they are independent of the auditor's severity grading -- which is exactly why relaxing the
        findings threshold cannot weaken them.
        """
        review = _review(question_package, [_finding(7, "MINOR")])
        review["reconstructed_answers"][0]["answer"] = "name"
        candidate = self._candidate(question_package, material, review)
        assert not is_deliverable(candidate)
        assert any("answer_divergence" in line for line in hard_blockers(candidate))
        # Both lists are populated, and the hard one comes first so a truncated log shows the real cause.
        assert advisory_notes(candidate)
        assert "answer_divergence" in delivery_blockers(candidate)[0]

    @pytest.mark.parametrize("field,marker", [
        ("derivable_without_recording", "printed page"),
    ])
    def test_a_minor_does_not_rescue_leakage(self, question_package, material, field, marker):
        review = _review(question_package, [_finding(7, "MINOR")])
        review["reconstructed_answers"][0][field] = True
        candidate = self._candidate(question_package, material, review)
        assert not is_deliverable(candidate)
        assert any(marker in line for line in hard_blockers(candidate))

    def test_many_minors_never_add_up_to_a_blocker(self, question_package, material):
        """No threshold on advisory count, deliberately.

        A count-based cutoff would reintroduce exactly the weighted-sum error that
        ``TestTheRankingIsLexicographicNotWeighted`` exists to prevent: N cosmetic notes standing in for
        one unfairness. Severity decides; volume does not.
        """
        findings = [_finding(n, "MINOR") for n in range(1, 8)]
        candidate = self._candidate(question_package, material,
                                    _review(question_package, findings))
        assert candidate.counts["MINOR"] == 7
        assert hard_blockers(candidate) == []
        assert is_deliverable(candidate)


class TestSoleAdjacencyShipsAsWarning:
    """The 2026-08-08 regression: adjacency alone destroyed seven materials, and now ships as a note.

    Every fixture here replays a shape measured on batch ``web-1786166271869-1``. Its four child
    requests spent three resume rounds and 2360s to deliver **one** set out of four; of the 42 blockers
    the run reported, 26 were ``anchor_adjacent``, and of the eight ``questions_rejected`` verdicts --
    each one a regenerated material -- **seven** listed nothing but adjacency::

        slot-1r1 revised-1 hard=1 ["Q5's evidence anchor is one turn ..."]
        slot-1r1 revised-2 hard=1 ["Q2's evidence anchor is one turn ..."]
        slot-1   revised-2 hard=1 ["Q3's evidence anchor is one turn ..."]
        slot-1r1 revised-1 hard=3 [Q2, Q5, Q7 -- all three the same row]
        slot-1r1 revised-2 hard=2 [Q4, Q9 -- likewise]
        ...

    against cross-check frames that were otherwise spotless -- e.g. ``slot-1r1 revised-1``::

        {"agreed": 9, "by_outcome": {"agree": [1,2,3,4,6,7,8,9,10], "anchor_adjacent": [5]},
         "hard_defects": 0, "leakage": 0, "rivals": 0, "status": "PASS"}

    The reviews themselves are written nowhere (nothing persists a rejected candidate's audit), so the
    turn indices those items named cannot be read back. What is replayed is therefore the *frame*: the
    same item numbers, the same 9/10 or 7/10, everything deterministic clean, driven against the
    committed material by moving the auditor's anchor to the genuinely neighbouring sentence -- which is
    the harder of the two readings that produce this outcome (see
    :class:`TestTheRealRevisedTwoAdjacencies`).

    **The release is narrow, and half of these tests are what keeps it narrow.** Each "still blocks"
    case is also drawn from the same run: 18 of the 26 adjacency blockers sat *beside* another defect,
    and those must be untouched.
    """

    @staticmethod
    def _candidate(package, material, review, errors=(), warnings=(), label="revised-1"):
        from backend.deterministic.question_crosscheck import crosscheck_questions
        from backend.deterministic.validate import ValidationResult
        cross = crosscheck_questions(package, review, material)
        return QuestionCandidate(package, review, cross,
                                 ValidationResult(list(errors), list(warnings), {}), label)

    @staticmethod
    def _neighbour_anchors(review, turns, numbers):
        """Move the auditor's anchor to the next turn and quote *that* sentence.

        The neighbouring-sentence reading rather than the mis-stated-index one: a drifted index that
        still quotes the writer's own sentence normalises to ``agree`` and never reaches the release, so
        testing the release against it would test nothing.
        """
        for row in review["reconstructed_answers"]:
            if row["number"] in numbers:
                row["turn_index"] = row["turn_index"] + 1
                row["quote"] = turns[row["turn_index"]]["text"]
        return review

    @pytest.fixture
    def turns(self, material):
        return material["listening_material_parts"][0]["script"]["turns"]

    @pytest.fixture
    def slot1r1_revised1(self, question_package, material, turns):
        """``slot-1r1 revised-1``: 9/10 agreed, Q5 adjacent, one hard blocker, material destroyed."""
        return self._candidate(question_package, material,
                               self._neighbour_anchors(_review(question_package), turns, (5,)))

    # ── the shape that was rejected, and now is not ───────────────────────────

    def test_the_measured_frame_is_reproduced_before_anything_is_asserted(self, slot1r1_revised1):
        """Pin the fixture against the recorded frame first.

        Without this the class could drift into testing a set that never existed, and the entire
        argument for the release is that this exact shape reached production eight times.
        """
        cross = slot1r1_revised1.cross_check
        assert cross.compared == 10 and cross.agreed == 9
        assert cross.by_outcome == {"agree": [1, 2, 3, 4, 6, 7, 8, 9, 10], "anchor_adjacent": [5]}
        assert cross.hard_defects == [] and cross.leakage == []
        assert cross.equally_supported_rivals == []
        assert [row["number"] for row in cross.needs_review] == [5]
        assert slot1r1_revised1.status == "PASS"          # the auditor found nothing at all
        assert set(slot1r1_revised1.counts.values()) == {0}
        assert not any(slot1r1_revised1.counts.get(name) for name in SEVERITY_ORDER)

    def test_it_is_deliverable(self, slot1r1_revised1):
        """The assertion the change exists for: this material is no longer regenerated."""
        assert sole_adjacency_release(slot1r1_revised1)
        assert hard_blockers(slot1r1_revised1) == []
        assert is_deliverable(slot1r1_revised1)

    def test_the_gap_is_reported_as_an_advisory_not_a_blocker(self, slot1r1_revised1):
        """It moved category; it did not disappear. The reviewer still reads the gap."""
        notes = advisory_notes(slot1r1_revised1)
        assert len(notes) == 1
        assert notes[0].startswith("Q5's evidence anchor is one turn from the writer's within "
                                   "narrator window ")
        assert "confirm the neighbouring turn states the same fact" in notes[0]
        assert delivery_blockers(slot1r1_revised1) == notes
        assert not is_clean_questions(slot1r1_revised1)

    def test_it_ships_as_warning_and_not_as_pass(self, slot1r1_revised1):
        """The status the user's requirement names, and the one case the auditor cannot supply.

        Nothing was found, so the rules file computes ``PASS`` -- correctly, as a statement about the
        audit. The delivered record must not repeat it: a set with an open note recorded as PASS is a
        clean set as far as every later reader is concerned.
        """
        assert slot1r1_revised1.status == "PASS"
        assert delivered_status(slot1r1_revised1) == WARNING_STATUS
        result = QuestionResult(True, slot1r1_revised1, "revised-1",
                                advisories=advisory_notes(slot1r1_revised1))
        payload = result.as_dict()
        assert payload["status"] == WARNING_STATUS
        assert len(payload["advisories"]) == 1
        # And the auditor's own verdict is still in the record, unrewritten.
        assert payload["review"]["question_qc_status"] == "PASS"

    @pytest.mark.parametrize("numbers,agreed", [
        ((5,), 9),                  # slot-1r1 revised-1 / slot-1 revised-2, hard=1
        ((2,), 9),                  # slot-1r1 revised-2, hard=1
        ((4, 9), 8),                # slot-3 slot-1r1 revised-2, hard=2
        ((2, 5, 7), 7),             # slot-2 slot-1r1 revised-1, hard=3
        ((3, 6, 7, 8, 9), 5),       # slot-1 initial: five adjacent, nothing else open
    ])
    def test_every_adjacency_only_rejection_of_the_run_now_delivers(
            self, question_package, material, turns, numbers, agreed):
        """All five distinct adjacency-only shapes the run produced, by item set.

        Volume is deliberately not a threshold: the 5/10 case is the same defect five times, not a
        different kind of defect, and a count-based cutoff here would be the weighted-sum error that
        ``TestTheRankingIsLexicographicNotWeighted`` exists to prevent.
        """
        candidate = self._candidate(
            question_package, material,
            self._neighbour_anchors(_review(question_package), turns, numbers))
        assert candidate.cross_check.agreed == agreed
        assert [row["number"] for row in candidate.cross_check.needs_review] == sorted(numbers)
        assert hard_blockers(candidate) == []
        assert len(advisory_notes(candidate)) == len(numbers)
        assert is_deliverable(candidate)
        assert delivered_status(candidate) == WARNING_STATUS

    # ── and every shape from the same run that must STILL block ──────────────

    def test_adjacency_beside_an_answer_divergence_still_blocks(
            self, question_package, material, turns):
        """``slot-1r1 initial``: ``{"anchor_adjacent": [5], "answer_divergence": [3]}``, 2 blockers.

        The commonest shape of the run and the reason the release is all-or-nothing: with a divergence
        open, "the answers agree" is false *about the set*, and the adjacency is no longer the only
        question. Releasing per-row would have shipped this.
        """
        review = self._neighbour_anchors(_review(question_package), turns, (5,))
        review["reconstructed_answers"][2]["answer"] = "name"        # Q3 diverges
        candidate = self._candidate(question_package, material, review)
        assert sole_adjacency_release(candidate) == []
        blockers = hard_blockers(candidate)
        assert any("answer_divergence on Q3" in line for line in blockers)
        assert any("Q5's evidence anchor is one turn" in line for line in blockers)
        assert not is_deliverable(candidate)
        assert advisory_notes(candidate) == []

    def test_adjacency_beside_leakage_still_blocks(self, question_package, material, turns):
        """``slot-1r1 revised-2``: adjacency plus ``Q6 answerable from the printed page`` (QR-040)."""
        review = self._neighbour_anchors(_review(question_package), turns, (5,))
        review["reconstructed_answers"][5]["derivable_without_recording"] = True
        candidate = self._candidate(question_package, material, review)
        assert sole_adjacency_release(candidate) == []
        assert any("printed page" in line for line in hard_blockers(candidate))
        assert not is_deliverable(candidate)

    def test_adjacency_beside_a_major_still_blocks(self, question_package, material, turns):
        """``slot-2 slot-1 revised-2``: one MAJOR, a divergence, an adjacency and two validator errors.

        A MAJOR means a candidate can be marked wrong for reading the paper correctly, which is the one
        thing no note trades against.
        """
        review = self._neighbour_anchors(
            _review(question_package, [_finding(7, "MAJOR")]), turns, (7,))
        candidate = self._candidate(question_package, material, review)
        assert sole_adjacency_release(candidate) == []
        assert any("MAJOR" in line for line in hard_blockers(candidate))
        assert not is_deliverable(candidate)

    def test_adjacency_beside_a_validator_error_still_blocks(
            self, question_package, material, turns):
        """The AR-002/QR-017 error the run's ``slot-2`` hit, beside an adjacency."""
        error = ("Q2 canonical 'marina dot hale at example dot com' is 7 word(s) and 0 number(s), "
                 "which its group's rubric does not permit (AR-002/QR-017)")
        candidate = self._candidate(
            question_package, material,
            self._neighbour_anchors(_review(question_package), turns, (7,)), errors=[error])
        assert sole_adjacency_release(candidate) == []
        assert any("validator error" in line for line in hard_blockers(candidate))
        assert not is_deliverable(candidate)

    def test_adjacency_beside_a_validator_warning_is_released_but_still_warns(
            self, question_package, material, turns):
        """``slot-1r1 revised-2``, hard=1 + one warning: the boundary between the two categories.

        The run reported this as ``["Q3's evidence anchor ...", "validator warning: part of Q5's answer
        'garden room' appears in group 'A''s visible text"]``. A validator *warning* was already an
        advisory before this change, so it does not close the release -- both notes ship, and the set is
        still not called clean. Asserted because "no hard blocker" and "nothing else open" are different
        conditions and this is the case that distinguishes them.
        """
        warning = ("part of Q5's answer 'garden room' appears in group 'A''s visible text (['room']); "
                   "check it does not narrow the answer to one candidate")
        candidate = self._candidate(
            question_package, material,
            self._neighbour_anchors(_review(question_package), turns, (3,)), warnings=[warning])
        assert [row["number"] for row in sole_adjacency_release(candidate)] == [3]
        assert hard_blockers(candidate) == []
        notes = advisory_notes(candidate)
        assert len(notes) == 2
        assert any("validator warning" in line for line in notes)
        assert any("Q3's evidence anchor" in line for line in notes)
        assert is_deliverable(candidate) and not is_clean_questions(candidate)
        assert delivered_status(candidate) == WARNING_STATUS

    def test_a_different_window_is_anchor_divergence_and_never_reaches_the_release(
            self, question_package, material, turns):
        """The other adjacency the user's rule keeps hard, and it is hard one level down.

        A one-turn gap that crosses a narration boundary is classified ``anchor_divergence``, not
        ``anchor_adjacent``, so it lands in ``hard_defects`` and the release never sees it. Asserted
        rather than assumed: the release's own window check would be unreachable dead code if this
        classification ever changed, and this is the test that would fail first.

        Built by moving the writer's anchor to the turn immediately *after* the middle narration -- no
        anchor in the committed fixture sits beside it, so the gap has to be constructed. The auditor
        then reads the narration turn itself, one turn back and one window back.
        """
        narrator = [i for i, turn in enumerate(turns) if turn.get("speaker") == "speaker1"]
        boundary = narrator[1]
        package = copy.deepcopy(question_package)
        for row in package["evidence"]:
            if row["number"] == 5:
                row["turn_index"] = boundary + 1
                row["quote"] = turns[boundary + 1]["text"][:40]
        review = _review(package)
        for row in review["reconstructed_answers"]:
            if row["number"] == 5:
                row["turn_index"] = boundary
                row["quote"] = turns[boundary]["text"]
        candidate = self._candidate(package, material, review)
        assert candidate.cross_check.needs_review == []
        assert [row["number"] for row in candidate.cross_check.hard_defects] == [5]
        defect = candidate.cross_check.hard_defects[0]
        assert defect["outcome"] == "anchor_divergence"
        assert "different narrator windows" in defect["reason"]
        assert sole_adjacency_release(candidate) == []
        assert not is_deliverable(candidate)

    def test_an_unaligned_proposition_is_anchor_divergence_and_never_reaches_the_release(
            self, question_package, material, turns):
        """The third condition, likewise hard one level down: the writer never claimed alignment."""
        package = copy.deepcopy(question_package)
        for row in package["evidence"]:
            if row["number"] == 5:
                row["proposition_alignment_result"] = "carrier_broader"
        review = self._neighbour_anchors(_review(package), turns, (5,))
        candidate = self._candidate(package, material, review)
        assert candidate.cross_check.needs_review == []
        assert [row["number"] for row in candidate.cross_check.hard_defects] == [5]
        assert "not marked proposition-aligned" in candidate.cross_check.hard_defects[0]["reason"]
        assert sole_adjacency_release(candidate) == []
        assert not is_deliverable(candidate)

    # ── the release re-derives its preconditions rather than trusting the row ──

    @pytest.mark.parametrize("key,value", [
        ("same_narrator_window", False),
        ("same_narrator_window", None),
        ("proposition_aligned", False),
        ("proposition_aligned", None),
        ("writer_window", None),
        ("auditor_window", 2),
        ("writer_answer", "something else"),
        ("effective_auditor_turn", None),
        ("writer_turn", None),
    ])
    def test_a_row_missing_any_precondition_is_not_released(
            self, question_package, material, turns, key, value):
        """Every unknown falls to the hard side, which is what makes this a release and not a default.

        These are forced on the row rather than produced from a review, deliberately: today's ``compare``
        cannot emit an ``anchor_adjacent`` row with these fields unset, and that is exactly the property
        under test -- if a future change (or a row written before these keys existed) delivers one, the
        gate must refuse it rather than read the absence as agreement.
        """
        candidate = self._candidate(question_package, material,
                                    self._neighbour_anchors(_review(question_package), turns, (5,)))
        assert sole_adjacency_release(candidate)            # released before the field is disturbed
        row = candidate.cross_check.needs_review[0]
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value
        assert sole_adjacency_release(candidate) == []
        assert any("Q5's evidence anchor is one turn" in line
                   for line in hard_blockers(candidate))
        assert advisory_notes(candidate) == []

    def test_a_two_turn_gap_on_the_row_is_not_released(self, question_package, material, turns):
        """The gap is recomputed from the two anchors, not taken from the outcome label."""
        candidate = self._candidate(question_package, material,
                                    self._neighbour_anchors(_review(question_package), turns, (5,)))
        row = candidate.cross_check.needs_review[0]
        row["effective_auditor_turn"] = row["writer_turn"] + 2
        assert sole_adjacency_release(candidate) == []
        assert not is_deliverable(candidate)

    def test_a_nine_item_audit_is_not_released(self, question_package, material, turns):
        """Coverage is a precondition of its own: a shortfall is not something adjacency explains."""
        review = self._neighbour_anchors(_review(question_package), turns, (5,))
        review["reconstructed_answers"] = review["reconstructed_answers"][:9]
        review["coverage"]["reviewed_question_ids"] = list(range(1, 10))
        candidate = self._candidate(question_package, material, review)
        assert sole_adjacency_release(candidate) == []
        assert not is_deliverable(candidate)

    def test_a_clean_set_is_still_pass_with_no_notes(self, question_package, material):
        """The other direction: nothing open must not acquire a WARNING from this change."""
        candidate = self._candidate(question_package, material, _review(question_package))
        assert sole_adjacency_release(candidate) == []
        assert advisory_notes(candidate) == []
        assert delivered_status(candidate) == "PASS"
        assert QuestionResult(True, candidate, "initial").as_dict()["status"] == "PASS"
        assert is_clean_questions(candidate)


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
        """Both rounds get worse -> the best version is kept, and it SHIPS because it is fair.

        This is the exact shape of the production failure, and it used to return
        ``REGENERATE_MATERIAL``: initial carries one MINOR, both revisions turn it into a CRITICAL, and
        a perfectly fair set was destroyed along with its material. The MINOR-only initial is the least
        defective of the three, carries no hard blocker, and is therefore deliverable as ``WARNING``.
        """
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package, [_finding(1, "MINOR")]),
                           _review(question_package, [_finding(1, "CRITICAL")]),
                           _review(question_package, [_finding(1, "CRITICAL")])]
        result = await run_questions(harness.material, harness.blueprint)
        assert result.ok
        # Judged on the BEST version, not the last: deciding on `current` would grade the material by
        # the worst thing the reviser did to it.
        assert result.candidate.label == "initial"
        assert result.selected_version == "initial"
        assert result.candidate.status == "WARNING"
        # The note it shipped with is recorded, not silently dropped.
        assert result.advisories == ["1 open MINOR finding(s) in the blind audit"]
        assert result.as_dict()["advisories"] == result.advisories
        # And the budget really was spent trying to clear it first.
        assert result.rounds == 2

    @pytest.mark.asyncio
    async def test_the_worse_revision_is_never_the_one_delivered(
            self, harness, question_package):
        """Retention asserted on the shipped artifact, not only on the label.

        The label could be right while the payload was ``current`` -- the two are set from separate
        expressions at the exit -- and that failure has no symptom: a set delivered under the best
        version's name carrying the worst version's questions would validate, audit and ship. So the
        package identity is what is checked, against a revision made distinguishable on purpose.
        """
        from backend.orchestration.question_loop import run_questions

        harness.revised_package = copy.deepcopy(question_package)
        harness.revised_package["question_face"]["questions"][0]["carrier_before"] = "WORSE:"
        harness.reviews = [_review(question_package, [_finding(1, "MINOR")]),
                           _review(question_package, [_finding(1, "MAJOR")]),
                           _review(question_package, [_finding(1, "MAJOR")])]
        result = await run_questions(harness.material, harness.blueprint)
        assert result.ok
        delivered = result.candidate.package["question_face"]["questions"][0]["carrier_before"]
        assert delivered != "WORSE:"
        assert result.candidate.package is not harness.revised_package
        # The review travelling with it is the best version's own, not the last round's: shipping a set
        # beside another version's audit is a lie about the artifact, which is why they are inseparable.
        assert [f["severity"] for f in result.candidate.review["per_question_findings"]] == ["MINOR"]
        assert result.as_dict()["review"]["question_qc_status"] == "WARNING"

    @pytest.mark.asyncio
    async def test_an_advisory_only_best_ships_as_warning_when_the_audit_says_pass(
            self, harness, question_package, material):
        """Item 3 for the shape the auditor cannot label: adjacency, through the whole loop.

        The MINOR case above ships as ``WARNING`` because the rules file already computes ``WARNING``
        from a MINOR. Here the audit finds *nothing* -- computed status ``PASS`` -- while an adjacency
        note stays open, so ``WARNING`` can only come from :func:`delivered_status`. Asserted end to end
        rather than on the function, because the wire status is read off ``QuestionResult.as_dict``, and
        a loop that returned the candidate's own status would ship this as unqualified-clean.
        """
        from backend.orchestration.question_loop import run_questions

        turns = material["listening_material_parts"][0]["script"]["turns"]
        adjacent = TestSoleAdjacencyShipsAsWarning._neighbour_anchors(
            _review(question_package), turns, (5,))
        # Every round returns the same adjacency-only review, so the budget is spent and the exit
        # decides on a best candidate whose only open entry is the released note.
        harness.reviews = [adjacent, copy.deepcopy(adjacent), copy.deepcopy(adjacent)]
        result = await run_questions(harness.material, harness.blueprint)

        assert result.ok, "an adjacency-only set must no longer regenerate its material"
        assert result.candidate.status == "PASS"
        assert result.as_dict()["status"] == WARNING_STATUS
        assert len(result.advisories) == 1
        assert "one turn from the writer's" in result.advisories[0]
        assert result.as_dict()["review"]["question_qc_status"] == "PASS"

    @pytest.mark.asyncio
    async def test_a_surviving_major_still_regenerates_the_material(
            self, harness, question_package):
        """The other side of the same rule. Relaxing MINOR must not relax MAJOR.

        Paired with the test above deliberately: the two differ only in the severity of the finding on
        the best candidate, and they must reach opposite verdicts. A change that made the test above
        pass by delivering everything would fail here.
        """
        from backend.orchestration.question_loop import run_questions

        harness.reviews = [_review(question_package, [_finding(1, "MAJOR")]) for _ in range(3)]
        result = await run_questions(harness.material, harness.blueprint)
        assert not result.ok
        assert result.outcome == "REGENERATE_MATERIAL"
        assert result.candidate is None
        assert any("MAJOR" in line for line in result.blockers)

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
        # Three reviews, not two: the warning is an advisory, which still buys a revision round while
        # the budget lasts, so both rounds run even though the audit comes back clean after the first.
        harness.reviews = [_review(question_package, [_finding(1, "MAJOR")]),
                           _review(question_package),
                           _review(question_package)]
        result = await run_questions(harness.material, harness.blueprint)
        assert any("band deviation" in line for line in harness.instruction.advisory)
        assert not any("band deviation" in line for line in harness.instruction.must_fix)
        # A warning is improvable, not unfair: the rounds are spent trying to clear it, and when they
        # do not, the set ships with the warning recorded rather than being destroyed.
        assert result.ok
        assert result.advisories == ["validator warning: a band deviation"]
        assert result.rounds == 2


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
