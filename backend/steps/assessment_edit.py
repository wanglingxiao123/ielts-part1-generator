"""Model steps for changes to a teacher-selected assessment, not fresh generation."""
from __future__ import annotations

import json

from ..agents import build_audit_agent, build_generate_agent
from .agent_steps import _invoke
from .call import ModelCallError, extract_unique_envelope


async def propose(material, blueprint, package, comments, feedback):
    prompt = """Edit the teacher-selected assessment below. It is an accepted baseline.
Do NOT run a generation workflow, re-audit its overall quality, fix old problems, remove
background details, or regenerate the question set. Fulfil ONLY the comments.
Return a minimal patch. Every turn edit replaces an exact unique substring in that turn,
not necessarily its whole text. Include ALL affected repetitions/confirmations so the new
fact is consistent. Preserve unrelated numbers, distractors, phrasing, turns and layout.
Question numbers that may change are only the question anchors in comments.
For other questions, preserve their answers, wording and evidence verbatim.
If the request cannot be fulfilled within that scope, return outcome="needs_scope",
with a reason explaining which additional questions/structure need the teacher's decision.
Do not silently broaden the request.

Return exactly one JSON object:
{"outcome":"patch"|"needs_scope","reason":"...",
 "turn_edits":[{"turn_index":16,"before":"95","after":"105","reason":"requested price"}],
 "question_updates":[{"number":5,
   "answer":{...complete replacement answer_key row...},
   "evidence":{...complete replacement evidence row...},
   "blueprint_item":{...complete replacement blueprint item...},
   "question":{...optional complete replacement question_face.questions row...}}],
 "blueprint_updates":{...only correction and/or indirect_confirmation if affected...}}
Copy unchanged row fields exactly. Do not change group, type, item_form, answer_category,
response_form, form_group, narrator_window_id, word_limit or numeral_allowance.
Omit question when its printed wording need not change (e.g. a price blank).
Dates, names and currencies must agree with the retained printed question.
Do not return a complete replacement material. No other top-level keys.
"""
    payload = dict(material=material, blueprint=blueprint, package=package,
                   comments=comments, feedback=feedback)
    reply = await _invoke(build_generate_agent(), prompt + "\n" + json.dumps(
        payload, ensure_ascii=False), "assessment patch")
    result = extract_unique_envelope(reply, ("outcome", "reason"))
    if result.get("outcome") not in {"patch", "needs_scope"}:
        raise ModelCallError("assessment patch has an invalid outcome")
    return result


async def review(base, candidate, comments, edits):
    """Independent impact review; intentionally not called a blind material audit.

    The reviewer knows the requested change. It has no authority to reject the baseline.
    A malformed response retries this step, never restarts the material rewrite.
    """
    prompt = """Review ONLY the effect of these edits to a teacher-accepted assessment.
This is a change-impact review, NOT an IELTS material generation/compliance audit.
The base is authoritative. Do not question existing background details, quality findings,
answer categories or whether an old information point was in an earlier blind map.
Check: does the patch fulfil the teacher's comments; are repetitions/corrections and
dependent facts consistent; do updated answers/evidence match the printed questions;
did it introduce a contradiction, damage another question, or change unrelated content?
Only a NEW defect caused by the edits, an unfulfilled request, or an unauthorised edit
may block. Do not require broad cleanup before accepting a narrow change.
Return exactly one object:
{"request_satisfied":true,"scope_preserved":true,"dependencies_consistent":true,
 "blocking_findings":[{"edit_index":0,"message":"new problem caused by this edit"}],
 "notes":[]}
edit_index refers to the zero-based edits list. Explain the causal link to that edit.
Empty blocking_findings means none. All three booleans must be explicit.
"""
    payload = json.dumps(dict(base=base, candidate=candidate, comments=comments,
                              edits=edits), ensure_ascii=False)
    required = ("request_satisfied", "scope_preserved", "dependencies_consistent",
                "blocking_findings", "notes")
    failure = None
    for attempt in range(2):
        try:
            reply = await _invoke(build_audit_agent(), prompt + "\n" + payload,
                                  "assessment impact review")
            result = extract_unique_envelope(reply, required)
            if any(type(result[k]) is not bool for k in required[:3]):
                raise ModelCallError("impact review booleans are missing or invalid")
            if not isinstance(result["blocking_findings"], list) or not isinstance(result["notes"], list):
                raise ModelCallError("impact review findings/notes must be lists")
            for row in result["blocking_findings"]:
                if (not isinstance(row, dict) or type(row.get("edit_index")) is not int
                        or not 0 <= row["edit_index"] < len(edits)
                        or not isinstance(row.get("message"), str) or not row["message"].strip()):
                    raise ModelCallError("impact review finding must identify a changed edit")
            return result
        except ModelCallError as exc:
            failure = exc
            prompt += "\nPrevious response was malformed. Return the entire required JSON object."
    raise failure
