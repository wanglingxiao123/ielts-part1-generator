"""Incident regressions: identify a report without accepting fragments or weaker audits."""
import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.steps.agent_steps import _invoke, _question_audit_envelope
from backend.steps.call import ModelCallError


@pytest.fixture
def report():
    return {
        "reconstructed_answers": [
            {"number": number, "answer": f"answer {number}", "turn_index": number,
             "quote": f"quote {number}", "confidence": "high", "competing_candidates": [],
             "derivable_without_recording": False} for number in range(1, 11)],
        "per_question_findings": [], "group_findings": [],
        "coverage": {"reviewed_question_ids": list(range(1, 11)), "unreviewed": []},
        "summary": {"counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "INFO": 0,
                               "ADVISORY_WARNING": 0}},
        "question_qc_status": "PASS",
    }


# These are precisely the field names in the saved slot-3 failure, with synthetic values.
ROW = {"answer_category": "place", "blank": "1", "blank_position": "middle",
       "carrier_after": "room", "carrier_before": "the", "group_id": "g1",
       "number": 1, "response_form": "word"}


@pytest.mark.parametrize("prefix,suffix", [
    ("", ""), ("Final report:\n", "\nEnd of report."),
    (json.dumps(ROW) + "\n", ""),
    ("```json\n" + json.dumps(ROW) + "\n```\n```json\n", "\n```"),
    (json.dumps({"example": ROW}) + "\n", ""),
])
def test_unique_report_survives_leading_question_object(report, prefix, suffix):
    assert _question_audit_envelope(prefix + json.dumps(report) + suffix, "question audit") == report


@pytest.mark.parametrize("wrapper", [
    lambda value: json.dumps(ROW),
    lambda value: '{"question_face":{"questions":[' + json.dumps(ROW) + ',',
    lambda value: '{"draft":' + value,
    lambda value: value + '\n{"unfinished":',
    lambda value: '{"unfinished":\n' + value,
    lambda value: json.dumps({"example": json.loads(value)}),
    lambda value: '[' + value + ']',
    lambda value: json.dumps(value),
    lambda value: "nothing to report",
])
def test_fragments_and_nested_reports_fail_closed(report, wrapper):
    with pytest.raises(ModelCallError):
        _question_audit_envelope(wrapper(json.dumps(report)), "question audit")


@pytest.mark.parametrize("different", [False, True])
def test_multiple_reports_are_ambiguous_even_when_one_would_pass(report, different):
    other = deepcopy(report)
    if different:
        other["question_qc_status"] = "FAIL"
    with pytest.raises(ModelCallError, match="ambiguous"):
        _question_audit_envelope(json.dumps(other) + '\n```json\n' + json.dumps(report) + '\n```',
                                 "question audit")


@pytest.mark.parametrize("defect", ["empty", "coverage", "status", "counts"])
def test_selecting_a_report_never_bypasses_quality_checks(report, defect):
    if defect == "empty":
        report["reconstructed_answers"] = []
    elif defect == "coverage":
        report["reconstructed_answers"].pop()
        report["coverage"]["reviewed_question_ids"].pop()
    elif defect == "status":
        report["question_qc_status"] = "FAIL"
    else:
        report["summary"]["counts"]["MAJOR"] = 1
    with pytest.raises(ModelCallError):
        _question_audit_envelope(json.dumps(ROW) + "\n" + json.dumps(report), "question audit")


def test_selected_report_still_checks_quotes_against_material(report):
    # A repeated quote cannot be normalised to a unique turn, and neither
    # occurrence is at the declared index. The existing AL-007 gate must reject it.
    report["reconstructed_answers"][0]["quote"] = "Repeated quote."
    material = {"listening_material_parts": [{"script": {"turns": [
        {"speaker": "speaker1", "text": "No cited quotes occur here."} for _ in range(12)
    ]}}]}
    material["listening_material_parts"][0]["script"]["turns"][0]["text"] = "Repeated quote."
    material["listening_material_parts"][0]["script"]["turns"][2]["text"] = "Repeated quote."
    with pytest.raises(ModelCallError, match="anchors quotes"):
        _question_audit_envelope(json.dumps(ROW) + "\n" + json.dumps(report), "question audit", material)


@pytest.mark.asyncio
async def test_complete_report_after_decoy_does_not_spend_infrastructure_retries(report):
    from backend.orchestration.loop import _with_infra_retries

    agent = SimpleNamespace(invoke_async=AsyncMock(
        return_value=json.dumps(ROW) + "\n" + json.dumps(report)))
    emit = AsyncMock()

    async def audit():
        reply = await _invoke(agent, "synthetic blind input", "question audit")
        return _question_audit_envelope(reply, "question audit")

    assert await _with_infra_retries(audit, "initial question audit", emit) == report
    agent.invoke_async.assert_awaited_once()
    emit.assert_not_awaited()


def test_extraction_failure_does_not_echo_reply_content():
    with pytest.raises(ModelCallError) as exc:
        _question_audit_envelope('{"private-token": "private-reasoning",', "question audit")
    assert "private-" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["end_turn", "private-stop-token"])
async def test_audit_logs_only_safe_response_metadata(caplog, stop):
    from strands.agent.agent_result import AgentResult
    from strands.telemetry.metrics import EventLoopMetrics

    metrics = EventLoopMetrics()
    metrics.accumulated_usage = {"inputTokens": 123, "outputTokens": 45, "totalTokens": 168,
                                 "private_extra": "private-usage-token"}
    result = AgentResult(stop_reason=stop, message={"role": "assistant", "content": [
        {"text": "private-response-and-reasoning"}]}, metrics=metrics, state={"secret": "private-state"})
    agent = SimpleNamespace(invoke_async=AsyncMock(return_value=result))
    with caplog.at_level(logging.INFO, logger="backend.steps.agent_steps"):
        reply = await _invoke(agent, "private-prompt-token", "question audit")
    assert "private-response-and-reasoning" in reply
    assert "question_audit_response" in caplog.text
    assert "stop_reason=" + (stop if stop == "end_turn" else "unknown") in caplog.text
    assert "inputTokens" in caplog.text and "123" in caplog.text
    assert "reply_chars=" in caplog.text and "elapsed_seconds=" in caplog.text
    assert "private-" not in caplog.text
