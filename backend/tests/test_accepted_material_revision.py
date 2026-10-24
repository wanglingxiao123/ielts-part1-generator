import asyncio
import copy
import json
from pathlib import Path

import pytest

from audio_storage.object_store import InMemoryObjectStore
from backend.deterministic.validate import ValidationResult
from backend.orchestration import accepted_material_revision as subject
from backend.orchestration.assessment_patch import apply_patch, turns
from backend.orchestration.slot_store import SlotStore
from backend.steps import assessment_edit
from backend.steps.call import ModelCallError
from qti_export.service import export_document, ExportRejected

FIXTURE = Path(__file__).parent / "fixtures" / "accepted_hotel.json"
COMMENTS = [{"id": "c", "anchor": {"type": "question", "index": 5},
             "text": "将Q5的正确每晚房价从95英镑改为105英镑。"}]


def hotel():
    return json.loads(FIXTURE.read_text())


def price_patch(base):
    answer = copy.deepcopy(base["package"]["answer_key"][4])
    answer.update(canonical="105", alternatives=["£105", "one hundred and five"])
    # Keep accepted variants within the original word limit.
    answer["alternatives"] = ["£105"]
    evidence = copy.deepcopy(base["package"]["evidence"][4])
    evidence["quote"] = evidence["quote"].replace("95", "105")
    evidence["evidence_entity"] = evidence["evidence_entity"].replace("95", "105")
    item = copy.deepcopy(base["blueprint"]["items"][4])
    item["target"] = "105"
    item["evidence"] = item["evidence"].replace("95", "105")
    correction = copy.deepcopy(base["blueprint"]["correction"])
    correction["final"] = correction["final"].replace("95", "105")
    return {"outcome": "patch", "reason": "Requested price update",
            "turn_edits": [{"turn_index": i, "before": "95", "after": "105",
                            "reason": "price or confirmation"} for i in (16, 17)],
            "question_updates": [{"number": 5, "answer": answer, "evidence": evidence,
                                  "blueprint_item": item}],
            "blueprint_updates": {"correction": correction}}


def passed():
    return {"request_satisfied": True, "scope_preserved": True,
            "dependencies_consistent": True, "blocking_findings": [], "notes": []}


def test_price_edit_preserves_v2_face_and_all_unrelated_content():
    base = hotel()
    result = apply_patch(**base, comments=COMMENTS, patch=price_patch(base))
    material, blueprint, package, changed = result
    assert changed == [5]
    assert package["question_face"] == base["package"]["question_face"]
    for i, (before, after) in enumerate(zip(turns(base["material"]), turns(material))):
        assert after == ({**before, "text": before["text"].replace("95", "105")}
                         if i in (16, 17) else before)
    for key in ("answer_key", "evidence"):
        assert package[key][:4] + package[key][5:] == base["package"][key][:4] + base["package"][key][5:]
    assert blueprint["items"][:4] + blueprint["items"][5:] == base["blueprint"]["items"][:4] + base["blueprint"]["items"][5:]
    assert package["answer_key"][4]["canonical"] == "105"
    assert base["package"]["answer_key"][4]["canonical"] == "95"


@pytest.mark.parametrize("case", ["other_question", "layout", "bad_quote", "other_evidence", "narrator"])
def test_patch_rejects_scope_or_evidence_damage(case):
    base = hotel()
    patch = price_patch(base)
    if case == "other_question":
        patch["question_updates"][0]["number"] = 3
    elif case == "layout":
        patch["question_updates"][0]["blueprint_item"]["item_form"] = "table"
    elif case == "bad_quote":
        patch["question_updates"][0]["evidence"]["quote"] = "The cost is 105 dollars."
    elif case == "other_evidence":
        patch["turn_edits"].append({"turn_index": 4, "before": "Carter", "after": "Miller"})
    else:
        patch["turn_edits"].append({"turn_index": 0, "before": "Part 1", "after": "Part 2"})
    with pytest.raises(ValueError):
        apply_patch(**base, comments=COMMENTS, patch=patch)


@pytest.mark.asyncio
async def test_accepted_baseline_issues_do_not_trigger_full_audit_or_regeneration(monkeypatch):
    base = hotel()
    store = SlotStore(InMemoryObjectStore())
    calls = []
    async def propose(*args):
        calls.append(copy.deepcopy(args[:3]))
        return price_patch(base)
    async def validate(*args):
        return ValidationResult(["unchanged pre-existing issue"], [], {})
    async def review(*args):
        return passed()
    monkeypatch.setattr(assessment_edit, "propose", propose)
    monkeypatch.setattr(assessment_edit, "review", review)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject, "validate_questions", validate)
    events = [e async for e in subject.revise_material_from_comments(
        store=store, material_id="hotel", request_id="r", source_request_id="source",
        base_version_id="v2", **base, comments=COMMENTS, actor="teacher")]
    assert events[-1]["type"] == "question_revision_completed"
    version = store.load_question_version("hotel", "r")
    assert version["package"]["question_face"] == base["package"]["question_face"]
    assert version["quality"]["validation_delta"]["introduced_errors"] == []
    assert version["audio"]["status"] == "needs_synthesis"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_impact_never_saves_version_and_retries_from_selected_base(monkeypatch):
    base = hotel()
    store = SlotStore(InMemoryObjectStore())
    calls = []
    async def propose(*args):
        calls.append(copy.deepcopy(args[:3]))
        return price_patch(base)
    async def validate(*args):
        return ValidationResult([], [], {})
    async def review(*args):
        return {**passed(), "dependencies_consistent": False,
                "blocking_findings": [{"edit_index": 0, "message": "new contradiction"}]}
    monkeypatch.setattr(assessment_edit, "propose", propose)
    monkeypatch.setattr(assessment_edit, "review", review)
    monkeypatch.setattr(subject, "validate", validate)
    monkeypatch.setattr(subject, "validate_questions", validate)
    events = [e async for e in subject.revise_material_from_comments(
        store=store, material_id="hotel", request_id="r", source_request_id="source",
        base_version_id="v2", **base, comments=COMMENTS, actor="teacher")]
    assert events[-1]["type"] == "question_revision_failed"
    assert store.load_question_version("hotel", "r") is None
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]


@pytest.mark.asyncio
async def test_malformed_review_retries_review_only_and_requires_complete_envelope(monkeypatch):
    replies = iter(['{"total": 95, "dimensions": {}}', json.dumps(passed())])
    calls = []
    async def invoke(*args):
        calls.append(args)
        return next(replies)
    monkeypatch.setattr(assessment_edit, "build_audit_agent", lambda: object())
    monkeypatch.setattr(assessment_edit, "_invoke", invoke)
    assert await assessment_edit.review({}, {}, COMMENTS, [{}]) == passed()
    assert len(calls) == 2


def test_export_selected_version_ignores_old_audit_and_preserves_marking_scheme():
    base = hotel()
    doc = {"status": "ready", "package": base["package"],
           "quality": {"review": {"question_qc_status": "FAIL"}, "cross_check": {"ok": False}}}
    bundle = export_document(doc, material_id="hotel", version_ordinal=5)
    assert bundle.zip_bytes().startswith(b"PK")
    for gap, answer in zip(bundle.material.gaps, base["package"]["answer_key"]):
        assert gap.target == answer["canonical"]
        assert set(gap.accept) == {answer["canonical"], *answer.get("alternatives", [])}
        assert gap.qti_added_alternatives == []
    assert "12 March" in bundle.material.gaps[1].accept


def test_export_still_rejects_incomplete_question_answer_mapping():
    doc = {"package": hotel()["package"]}
    doc["package"]["answer_key"].pop()
    with pytest.raises(ExportRejected, match="answer_key"):
        export_document(doc, material_id="hotel", version_ordinal=5)


@pytest.mark.asyncio
async def test_local_turn_edit_keeps_accepted_questions_despite_baseline_errors(monkeypatch):
    from backend.orchestration import manual_material_local_revision as local
    base = hotel()
    modified = copy.deepcopy(base["material"])
    turns(modified)[35]["text"] = "Thank you for your help. Goodbye."
    async def propose(*args):
        return {"outcome": "local_material_edit", "reason": "Requested wording",
                "material": modified}
    async def validation(*args):
        return ValidationResult(["pre-existing material issue"], [], {})
    async def review(*args):
        return passed()
    monkeypatch.setattr(local.agent_steps, "revise_material_turn", propose)
    monkeypatch.setattr(local, "validate", validation)
    monkeypatch.setattr(local, "validate_questions", validation)
    monkeypatch.setattr(assessment_edit, "review", review)
    store = SlotStore(InMemoryObjectStore())
    events = [e async for e in local.revise_material_local(
        store=store, material_id="hotel", request_id="local", base_version_id="v2",
        **base, base_version={}, actor="teacher",
        comments=[{"id": "c", "anchor": {"type": "turn", "index": 35},
                   "text": "Change goodbye wording"}])]
    assert events[-1]["type"] == "question_revision_completed"
    version = store.load_question_version("hotel", "local")
    assert version["package"] == base["package"]
    assert version["material"] == modified
    assert version["quality"]["material"]["introduced_errors"] == []


def test_scope_never_hides_new_target_evidence_error_as_an_old_issue():
    base = hotel()
    patch = price_patch(base)
    patch["question_updates"][0]["blueprint_item"]["target"] = "999"
    with pytest.raises(ValueError, match="match the planned target"):
        apply_patch(**base, comments=COMMENTS, patch=patch)
