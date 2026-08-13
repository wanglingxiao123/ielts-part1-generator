from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from audio_storage.object_store import InMemoryObjectStore
from backend.orchestration import manual_question_revision as subject
from backend.orchestration.slot_store import SlotPersistenceError, SlotStore


async def collect(stream):
    return [event async for event in stream]


@pytest.fixture(autouse=True)
def question_only_classification(monkeypatch):
    async def classify(_material, _blueprint, _package, comments):
        return {
            "outcome": "question_only",
            "reasons": [
                {"comment_id": row["id"], "question_number": 1,
                 "reason": "question-only", "references": []}
                for row in comments
            ],
        }

    monkeypatch.setattr(subject.agent_steps, "classify_question_revision", classify)


@pytest.mark.asyncio
async def test_material_required_outcome_stores_no_version(monkeypatch):
    async def classify(*_args):
        return {
            "outcome": "revise_material",
            "reasons": [{"comment_id": "c1", "question_number": 2,
                         "reason": "the material states two equal answers",
                         "references": ["material turn 2"]}],
        }

    monkeypatch.setattr(subject.agent_steps, "classify_question_revision", classify)
    backing = InMemoryObjectStore()
    store = SlotStore(backing)
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        base_version={"package": {}},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert events[-1]["type"] == "question_revision_needs_material"
    assert store.load_question_version("mat-1", "req-1") is None
    request = store._read("_question_revisions/mat-1/req-1.json")
    assert request["status"] == "needs_material_revision"


@pytest.mark.asyncio
async def test_no_change_stores_reason_without_creating_version(monkeypatch):
    model_reasons = [{
        "comment_id": "c1",
        "question_number": 2,
        "reason": "The existing item already has one supported answer.",
        "references": ["hallucinated face", "hallucinated answer", "hallucinated evidence"],
    }]

    async def classify(*_args):
        return {"outcome": "no_change", "reasons": model_reasons}

    monkeypatch.setattr(subject.agent_steps, "classify_question_revision", classify)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={
            "question_face": {"questions": [
                {"number": 2, "carrier_before": "Postcode", "carrier_after": ""},
            ]},
            "answer_key": [{"number": 2, "canonical": "BT14 9BJ"}],
            "evidence": [{"number": 2, "turn_index": 3, "quote": "That's BT14 9BJ."}],
        },
        base_version={"package": {}}, comments=[{"id": "c1"}], actor="reviewer"))

    references = events[-1]["reasons"][0]["references"]
    assert references == [
        "题面：Postcode [Q2]",
        "标准答案：BT14 9BJ",
        "材料证据（Turn 3）：That's BT14 9BJ.",
    ]
    assert all("hallucinated" not in value for value in references)
    assert store.load_question_version("mat-1", "req-1") is None
    stored = store._read("_question_revisions/mat-1/req-1.json")
    assert stored["status"] == "no_change"
    assert stored["reasons"][0]["references"] == references


@pytest.mark.asyncio
async def test_replan_questions_is_distinct_from_material_revision(monkeypatch):
    async def classify(*_args):
        return {
            "outcome": "replan_questions",
            "reasons": [{"comment_id": "c1", "question_number": 2,
                         "replan_scope": "retarget",
                         "reason": "A different information point is required",
                         "references": ["blueprint item 2"]}],
        }

    monkeypatch.setattr(subject.agent_steps, "classify_question_revision", classify)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        base_version={"package": {}}, comments=[{"id": "c1"}], actor="reviewer"))

    assert events[-1]["type"] == "question_revision_needs_replan"
    record = store._read("_question_revisions/mat-1/req-1.json")
    assert record["status"] == "replan_questions"
    assert record["comment_outcomes"][0]["replan_scope"] == "retarget"
    assert store.load_question_version("mat-1", "req-1") is None


@pytest.mark.asyncio
async def test_mixed_snapshot_routes_high_without_running_partial_question_revision(monkeypatch):
    async def classify(*_args):
        return {
            "outcome": "replan_questions",
            "reasons": [
                {
                    "comment_id": "c1", "question_number": 1,
                    "outcome": "question_only", "reason": "local carrier fix",
                },
                {
                    "comment_id": "c2", "question_number": 2,
                    "outcome": "replan_questions", "reason": "new target required",
                },
            ],
        }

    async def revise(*_args):
        raise AssertionError("a mixed higher-layer snapshot must not partially revise questions")

    monkeypatch.setattr(subject.agent_steps, "classify_question_revision", classify)
    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        base_version={"package": {}}, comments=[{"id": "c1"}, {"id": "c2"}],
        actor="reviewer"))

    assert events[-1]["type"] == "question_revision_needs_replan"
    record = store._read("_question_revisions/mat-1/req-1.json")
    assert record["status"] == "replan_questions"
    assert {row["comment_id"]: row["outcome"] for row in record["comment_outcomes"]} == {
        "c1": "question_only",
        "c2": "replan_questions",
    }


@pytest.mark.asyncio
async def test_agent_failure_stores_failed_request_without_version(monkeypatch):
    async def revise(*_args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        base_version={"package": {}},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert events[-1]["type"] == "question_revision_failed"
    assert store.load_question_version("mat-1", "req-1") is None
    record = store._read("_question_revisions/mat-1/req-1.json")
    assert record["status"] == "failed"
    assert record["comment_outcomes"] == [{
        "comment_id": "c1",
        "question_number": 1,
        "outcome": "question_only",
        "reason": "question-only",
    }]


@pytest.mark.asyncio
async def test_byte_identical_revision_fails_without_creating_noop_version(monkeypatch):
    package = {
        "question_face": {"questions": []},
        "answer_key": [],
        "evidence": [],
    }

    async def revise(_material, _blueprint, current, _comments):
        return {"outcome": "revised", "package": copy.deepcopy(current)}

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-noop",
        base_version_id="original", material={}, blueprint={}, package=package,
        base_version={"package": package},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert events[-1]["type"] == "question_revision_failed"
    assert store.load_question_version("mat-1", "req-noop") is None
    record = store._read("_question_revisions/mat-1/req-noop.json")
    assert record["status"] == "failed"
    assert record["comment_outcomes"][0]["outcome"] == "question_only"


@pytest.mark.asyncio
async def test_only_a_fully_checked_package_becomes_an_immutable_version(monkeypatch):
    package = _package()
    package["material_id"] = "mat-1"
    revised_comment_ids = []

    async def classify(*_args):
        return {
            "outcome": "question_only",
            "reasons": [
                {
                    "comment_id": "c1", "question_number": 1,
                    "outcome": "question_only", "reason": "local fix",
                },
                {
                    "comment_id": "c2", "question_number": 2,
                    "outcome": "no_change", "reason": "already correct",
                },
            ],
        }

    async def revise(_material, _blueprint, _package, comments):
        revised_comment_ids.extend(row["id"] for row in comments)
        revised = copy.deepcopy(package)
        revised["question_face"]["questions"][0]["carrier_after"] = " revised"
        return {"outcome": "revised", "package": revised}

    class Validation:
        errors = []
        warnings = []

        def as_dict(self):
            return {"ok": True, "errors": [], "warnings": [], "metrics": {}}

    class Cross:
        consistency = {"computed": {
            "question_qc_status": "PASS",
            "counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0},
            "reviewed_question_ids": list(range(1, 11)),
        }}
        hard_defects = []
        needs_review = []
        leakage = []
        equally_supported_rivals = []
        items = []
        compared = agreed = 10

        def as_dict(self):
            return {"ok": True, "compared": 10, "agreed": 10}

    async def validate(*_args):
        return Validation()

    async def audit(*_args):
        return {"question_qc_status": "PASS"}

    monkeypatch.setattr(subject.agent_steps, "classify_question_revision", classify)
    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    monkeypatch.setattr(subject, "validate_questions", validate)
    monkeypatch.setattr(subject, "question_metrics", lambda *_args: {})
    monkeypatch.setattr(subject.agent_steps, "audit_questions_blind", audit)
    monkeypatch.setattr(subject, "crosscheck_questions", lambda *_args: Cross())
    monkeypatch.setattr(subject, "hard_blockers", lambda _candidate: [])

    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package=package,
        base_version={"package": package},
        comments=[
            {"id": "c1", "anchor": {"type": "question", "index": 1}},
            {"id": "c2", "anchor": {"type": "question", "index": 2}},
        ], actor="reviewer"))

    assert [event["type"] for event in events] == [
        "question_revision_started",
        "question_revision_revising",
        "question_revision_validating",
        "question_revision_auditing",
        "question_revision_storing",
        "question_revision_completed",
    ]
    version = store.load_question_version("mat-1", "req-1")
    assert version["package"]["question_face"]["questions"][0]["carrier_after"] == " revised"
    assert version["package"]["question_face"]["questions"][1:] == (
        package["question_face"]["questions"][1:])
    assert version["blueprint"] == {}
    assert version["based_on_version_id"] == "original"
    assert version["source_comment_ids"] == ["c1"]
    assert revised_comment_ids == ["c1"]
    record = store._read("_question_revisions/mat-1/req-1.json")
    assert {row["comment_id"]: row["outcome"] for row in record["comment_outcomes"]} == {
        "c1": "question_only",
        "c2": "no_change",
    }


def test_question_version_is_create_only_and_same_payload_is_idempotent():
    store = SlotStore(InMemoryObjectStore())
    first = {"id": "req-1", "package": {"question_face": {"title": "first"}}}
    store.save_question_version("mat-1", "req-1", first)
    store.save_question_version("mat-1", "req-1", first)

    with pytest.raises(SlotPersistenceError):
        store.save_question_version(
            "mat-1", "req-1",
            {"id": "req-1", "package": {"question_face": {"title": "changed"}}},
        )
    assert store.load_question_version("mat-1", "req-1") == first


def _package():
    return {
        "question_face": {
            "instructions": [
                {"group_id": "A", "question_range": "1-5"},
                {"group_id": "B", "question_range": "6-10"},
            ],
            "groups": [
                {"group_id": "A", "layout": "form"},
                {"group_id": "B", "layout": "note"},
            ],
            "questions": [
                {"number": number, "group_id": "A" if number <= 5 else "B",
                 "carrier_before": "£" if number == 5 else ""}
                for number in range(1, 11)
            ],
        },
        "answer_key": [
            {"number": number, "canonical": "£128" if number == 5 else "answer-%d" % number}
            for number in range(1, 11)
        ],
        "evidence": [
            {"number": number, "turn_index": number}
            for number in range(1, 11)
        ],
    }


def _candidate(package, *, rival_number=9, validator_errors=None):
    rival = {"number": rival_number, "text": "passport"}
    cross = SimpleNamespace(
        hard_defects=[],
        leakage=[],
        equally_supported_rivals=[rival],
        needs_review=[],
        compared=10,
        agreed=10,
        consistency={"computed": {"reviewed_question_ids": list(range(1, 11))},
                     "errors": []},
        items=[],
    )
    review = {
        "per_question_findings": [
            {"number": rival_number, "rule_id": "AR-012", "severity": "MAJOR",
             "state": "open"}
        ],
        "group_findings": [],
    }
    return SimpleNamespace(
        package=package,
        review=review,
        cross_check=cross,
        validation=SimpleNamespace(errors=validator_errors or []),
    )


def _base_version(package):
    return {
        "package": package,
        "quality": {
            "review": {
                "per_question_findings": [],
                "group_findings": [],
                "question_qc_status": "PASS",
            },
            "cross_check": {"compared": 10, "agreed": 10},
            "validation": {"ok": True, "errors": []},
        },
    }


def test_unchanged_q9_audit_variance_does_not_block_a_q5_only_revision():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][4]["carrier_before"] = ""

    blockers, advisories, changed = subject._revision_gate(
        _candidate(revised), _base_version(base))

    assert blockers == []
    assert changed == [5]
    assert any("Q9" in advisory and "passport" in advisory for advisory in advisories)


def test_audit_issue_on_an_actually_changed_question_still_blocks():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][8]["carrier_before"] = "Required ID: "

    blockers, advisories, changed = subject._revision_gate(
        _candidate(revised), _base_version(base))

    assert changed == [9]
    assert advisories == []
    assert any("Q9" in blocker and "passport" in blocker for blocker in blockers)


@pytest.mark.parametrize(
    ("collection", "row", "message"),
    [
        ("hard_defects", {"number": 6, "outcome": "answer_mismatch"}, "answer_mismatch"),
        ("leakage", {"number": 6}, "printed page"),
        ("needs_review", {"number": 6}, "evidence anchor"),
    ],
)
def test_changed_question_crosscheck_issues_still_block(collection, row, message):
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][5]["carrier_after"] = " per visit"
    candidate = _candidate(revised, rival_number=10)
    candidate.cross_check.equally_supported_rivals = []
    candidate.review["per_question_findings"] = []
    setattr(candidate.cross_check, collection, [row])

    blockers, advisories, changed = subject._revision_gate(
        candidate, _base_version(base))

    assert changed == [6]
    assert advisories == []
    assert any(message in blocker for blocker in blockers)


def test_group_finding_for_a_changed_question_group_still_blocks():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][5]["carrier_after"] = " per visit"
    candidate = _candidate(revised, rival_number=10)
    candidate.cross_check.equally_supported_rivals = []
    candidate.review["per_question_findings"] = []
    candidate.review["group_findings"] = [{
        "group_id": "B",
        "rule_id": "QR-099",
        "severity": "MAJOR",
        "state": "open",
    }]

    blockers, advisories, changed = subject._revision_gate(
        candidate, _base_version(base))

    assert changed == [6]
    assert advisories == []
    assert any("group B" in blocker for blocker in blockers)


def test_validator_errors_always_block_even_when_the_question_is_unchanged():
    base = _package()
    blockers, _, changed = subject._revision_gate(
        _candidate(copy.deepcopy(base), validator_errors=["bad rectangle"]),
        _base_version(base),
    )

    assert changed == []
    assert blockers == ["validator error: bad rectangle"]


def test_missing_baseline_quality_uses_the_existing_strict_gate(monkeypatch):
    monkeypatch.setattr(subject, "hard_blockers", lambda _candidate: ["strict blocker"])

    blockers, advisories, changed = subject._revision_gate(
        _candidate(_package()), {"package": _package()})

    assert blockers == ["strict blocker"]
    assert advisories == []
    assert changed == []


def test_crosscheck_hard_defect_downgrades_on_an_unchanged_question():
    base = _package()
    candidate = _candidate(copy.deepcopy(base))
    candidate.cross_check.hard_defects = [{"number": 9, "outcome": "answer_mismatch"}]

    blockers, advisories, _ = subject._revision_gate(
        candidate, _base_version(base))

    assert blockers == []
    assert any("answer_mismatch" in advisory for advisory in advisories)


def test_unexplained_crosscheck_shortfall_always_blocks():
    base = _package()
    candidate = _candidate(copy.deepcopy(base))
    candidate.cross_check.equally_supported_rivals = []
    candidate.review["per_question_findings"] = []
    candidate.cross_check.agreed = 9

    blockers, _, _ = subject._revision_gate(candidate, _base_version(base))

    assert any("agrees on 9 of 10" in blocker for blocker in blockers)


def test_agreed_rival_does_not_double_count_a_separate_crosscheck_shortfall():
    base = _package()
    candidate = _candidate(copy.deepcopy(base), rival_number=9)
    candidate.review["per_question_findings"] = []
    candidate.cross_check.needs_review = [{"number": 10}]
    candidate.cross_check.items = [
        {"number": number, "outcome": "anchor_adjacent" if number == 10 else "agree"}
        for number in range(1, 11)
    ]
    candidate.cross_check.agreed = 9

    blockers, advisories, _ = subject._revision_gate(
        candidate, _base_version(base))

    assert blockers == []
    assert any("Q9" in advisory and "passport" in advisory for advisory in advisories)
    assert any("Q10" in advisory and "evidence anchor" in advisory for advisory in advisories)


def test_question_only_boundary_rejects_group_or_answer_replanning():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["groups"][0]["layout"] = "note"
    revised["answer_key"][1]["canonical"] = "different"
    revised["evidence"][1]["turn_index"] = 8
    blueprint = {"items": [{"number": 2, "target": "answer-2", "turn_index": 2}]}

    errors = subject._question_only_boundary_errors(base, revised, blueprint)

    assert "question groups or layouts changed" in errors
    assert "Q2 changed its blueprint answer target" in errors
    assert "Q2 changed its blueprint evidence turn" in errors


def test_question_only_boundary_allows_canonical_case_formatting():
    base = _package()
    revised = copy.deepcopy(base)
    revised["answer_key"][1]["canonical"] = "ANSWER-2"
    blueprint = {"items": [{"number": 2, "target": "answer-2"}]}

    assert subject._question_only_boundary_errors(base, revised, blueprint) == []


def test_question_only_normalization_keeps_only_the_anchored_question_patch():
    base = _package()
    model = copy.deepcopy(base)
    for question in model["question_face"]["questions"]:
        question["carrier_after"] = "model rewrite"
    for answer in model["answer_key"]:
        answer["canonical"] = "rewritten-%d" % answer["number"]
    for evidence in model["evidence"]:
        evidence["quote"] = "rewritten evidence"
    model["question_face"]["groups"][1]["layout"] = "form"
    model["question_face"]["instructions"][1]["word_limit"] = 3

    revised = subject._normalize_question_only_package(base, model, {6})

    changed, changed_groups = subject._changed_scope(base, revised)
    assert changed == {6}
    assert changed_groups == {"B"}
    assert revised["question_face"]["questions"][5]["carrier_after"] == "model rewrite"
    assert revised["answer_key"][5]["canonical"] == "rewritten-6"
    assert revised["evidence"][5]["quote"] == "rewritten evidence"
    assert revised["question_face"]["questions"][8] == base["question_face"]["questions"][8]
    assert revised["answer_key"][8] == base["answer_key"][8]
    assert revised["evidence"][8] == base["evidence"][8]
    assert revised["question_face"]["groups"] == base["question_face"]["groups"]
    assert revised["question_face"]["instructions"] == base["question_face"]["instructions"]


def test_question_only_normalization_keeps_baseline_order_and_drops_extra_rows():
    base = _package()
    model = copy.deepcopy(base)
    model["question_face"]["questions"].reverse()
    model["answer_key"].reverse()
    model["evidence"].reverse()
    for key in ("questions",):
        model["question_face"][key].append({"number": 11})
    model["answer_key"].append({"number": 11})
    model["evidence"].append({"number": 11})
    model["question_face"]["questions"][4]["carrier_after"] = " per visit"

    revised = subject._normalize_question_only_package(base, model, {6})

    for rows in (
        revised["question_face"]["questions"],
        revised["answer_key"],
        revised["evidence"],
    ):
        assert [row["number"] for row in rows] == list(range(1, 11))


def test_question_only_scope_comes_from_comment_anchors():
    comments = [
        {"id": "c1", "anchor": {"type": "question", "index": 6}},
        {"id": "c2", "anchor": {"type": "question", "index": 9}},
    ]

    assert subject._anchored_question_numbers(comments) == {6, 9}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda model: model["question_face"]["questions"].pop(5),
        lambda model: model["answer_key"].pop(5),
        lambda model: model["evidence"].pop(5),
        lambda model: model["question_face"]["questions"].append(
            copy.deepcopy(model["question_face"]["questions"][5])),
    ],
)
def test_question_only_normalization_rejects_missing_or_duplicate_anchored_rows(mutate):
    base = _package()
    model = copy.deepcopy(base)
    mutate(model)

    with pytest.raises(ValueError, match="invalid anchored patch"):
        subject._normalize_question_only_package(base, model, {6})


@pytest.mark.parametrize(
    ("collection", "row", "message"),
    [
        ("hard_defects", {"number": 9, "outcome": "answer_mismatch"}, "answer_mismatch"),
        ("leakage", {"number": 9}, "printed page"),
        ("needs_review", {"number": 9}, "evidence anchor"),
        ("items", {"number": 9, "outcome": "disagree"}, "does not agree"),
    ],
)
def test_unchanged_question_crosscheck_issues_become_advisories(
    collection, row, message,
):
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][5]["carrier_after"] = " per visit"
    candidate = _candidate(revised, rival_number=10)
    candidate.cross_check.equally_supported_rivals = []
    setattr(candidate.cross_check, collection, [row])
    if collection == "items":
        candidate.cross_check.agreed = 9

    blockers, advisories, changed = subject._revision_gate(
        candidate, _base_version(base))

    assert changed == [6]
    assert blockers == []
    assert any("Q9" in advisory and message in advisory for advisory in advisories)


@pytest.mark.asyncio
async def test_q6_only_revision_ignores_model_rewrites_and_advises_q9_baseline_findings(
    monkeypatch,
):
    base = _package()
    model = copy.deepcopy(base)
    for question in model["question_face"]["questions"]:
        question["carrier_after"] = "model rewrite"
    model["question_face"]["questions"][5]["carrier_after"] = " per visit"

    async def revise(*_args):
        return {"outcome": "revised", "package": model}

    class Validation:
        errors = []

        def as_dict(self):
            return {"ok": True, "errors": [], "warnings": [], "metrics": {}}

    class Cross:
        hard_defects = []
        leakage = []
        equally_supported_rivals = [{"number": 9, "text": "14 May"}]
        needs_review = [{"number": 9}]
        compared = 10
        agreed = 9
        consistency = {
            "computed": {
                "reviewed_question_ids": list(range(1, 11)),
                "counts": {"CRITICAL": 0, "MAJOR": 1, "MINOR": 0},
                "question_qc_status": "FAIL",
            },
            "errors": [],
        }
        items = [
            {"number": number, "outcome": "anchor_adjacent" if number == 9 else "agree"}
            for number in range(1, 11)
        ]

        def as_dict(self):
            return {
                "compared": self.compared,
                "agreed": self.agreed,
                "hard_defects": self.hard_defects,
                "leakage": self.leakage,
                "equally_supported_rivals": self.equally_supported_rivals,
                "needs_review": self.needs_review,
                "items": self.items,
                "consistency": self.consistency,
            }

    async def audit(*_args):
        return {
            "question_qc_status": "FAIL",
            "per_question_findings": [
                {
                    "number": 9,
                    "rule_id": "AR-012",
                    "severity": "MAJOR",
                    "state": "open",
                }
            ],
            "group_findings": [],
        }

    async def validate(*_args):
        return Validation()

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    monkeypatch.setattr(subject, "validate_questions", validate)
    monkeypatch.setattr(subject, "question_metrics", lambda *_args: {})
    monkeypatch.setattr(subject.agent_steps, "audit_questions_blind", audit)
    monkeypatch.setattr(subject, "crosscheck_questions", lambda *_args: Cross())

    store = SlotStore(InMemoryObjectStore())
    events = await collect(subject.revise_from_comments(
        store=store,
        material_id="20260809-service-cleaning-5c201dd2",
        request_id="req-q6-local",
        base_version_id="original",
        material={},
        blueprint={"items": [
            {
                "number": number,
                "target": base["answer_key"][number - 1]["canonical"],
                "turn_index": number,
            }
            for number in range(1, 11)
        ]},
        package=base,
        base_version=_base_version(base),
        comments=[{
            "id": "c-q6",
            "anchor": {"type": "question", "index": 6},
            "content": "在Q6空格后补上 per visit，明确3 hours是每次服务时长",
        }],
        actor="reviewer",
    ))

    assert events[-1]["type"] == "question_revision_completed"
    version = store.load_question_version(
        "20260809-service-cleaning-5c201dd2", "req-q6-local")
    assert version["changed_questions"] == [6]
    assert version["package"]["question_face"]["questions"][5]["carrier_after"] == " per visit"
    assert version["package"]["question_face"]["questions"][8] == (
        base["question_face"]["questions"][8])
    assert len(version["baseline_advisories"]) == 3
    assert all("Q9" in advisory for advisory in version["baseline_advisories"])


def test_field_changes_identify_dependent_question_fields():
    base = _package()
    revised = copy.deepcopy(base)
    revised["question_face"]["questions"][4]["carrier_before"] = ""
    revised["evidence"][4]["turn_index"] = 8

    changes = subject._field_changes(base, revised)

    assert {
        (row["question_number"], row["section"], row["field"])
        for row in changes
    } == {(5, "question", "carrier_before"), (5, "evidence", "turn_index")}


def test_field_changes_include_group_instruction_fields():
    base = _package()
    revised = copy.deepcopy(base)
    base["question_face"]["instructions"][0]["word_limit"] = 2
    revised["question_face"]["instructions"][0]["word_limit"] = 1

    changes = subject._field_changes(base, revised)

    assert any(
        row["question_number"] == 1
        and row["section"] == "instruction"
        and row["field"] == "word_limit"
        and row["before"] == 2
        and row["after"] == 1
        for row in changes
    )


@pytest.mark.asyncio
async def test_completed_request_replay_does_not_call_agent(monkeypatch):
    calls = 0

    async def revise(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore())
    store.save_question_version("mat-1", "req-1", {
        "id": "req-1", "package": {"question_face": {}},
    })
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        base_version={"package": {}},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert calls == 0
    assert events[-1]["type"] == "question_revision_completed"


@pytest.mark.asyncio
async def test_non_durable_runtime_refuses_before_agent_call(monkeypatch):
    calls = 0

    async def revise(*_args):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(subject.agent_steps, "revise_questions_from_comments", revise)
    store = SlotStore(InMemoryObjectStore(), persistent=False)
    events = await collect(subject.revise_from_comments(
        store=store, material_id="mat-1", request_id="req-1",
        base_version_id="original", material={}, blueprint={}, package={},
        base_version={"package": {}},
        comments=[{"id": "c1"}], actor="reviewer"))

    assert calls == 0
    assert events[-1]["type"] == "question_revision_failed"
    assert "存储未配置" in events[-1]["message"]
