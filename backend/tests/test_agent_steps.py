"""Envelope tests for the feasibility step -- AC2's second layer (design.md D2/D7).

The contract itself lives in ``skills/feasibility/feasibility-listening-part1/schemas/
feasibility.schema.json`` and is asserted against positive and negative cases by
``skills/shared/tests/run_tests.py``. This file covers the backend's restatement of its
values-and-types subset, which exists because ``jsonschema`` is a dev dependency that the runtime
image never installs (ci_gates gate 10 rejects a new third-party import into ``backend/``, measured).

Two layers checking the same values is deliberate, not duplication -- they stand in different places:

* here the call has only just returned and the three-attempt infrastructure budget is intact, so a
  reply that wrote ``"false"`` where ``false`` belonged is this call's slip and very likely correct on
  the next attempt. ``ModelCallError`` puts it back on the budget.
* ``question_feasibility_preflight._semantics_problem`` stands at the verdict with no retries left and
  can only report honestly that it cannot decide (``SEMANTICS_MISSING``).

The overlap is only safe while the two agree, which is what
``TestTheTwoLayersAgree`` at the bottom of this file is for.
"""

from __future__ import annotations

import json
import sys

import pytest

from backend import paths
from backend.steps.agent_steps import (
    FeasibilityInput,
    _check_qr027_exception,
    _feasibility_envelope,
    build_feasibility_message,
    build_feasibility_payload,
    classify_question_revision,
)
from backend.steps.call import ModelCallError


def _reply(**over) -> str:
    """A well-formed reply with the named keys overridden. ``None`` as a value deletes the key."""
    data = {"feasible": True, "reasons": ["item 4 reads cleanly"], "category_semantics_ok": True}
    for key, value in over.items():
        if value is _ABSENT:
            data.pop(key, None)
        else:
            data[key] = value
    return json.dumps(data)


_ABSENT = object()


@pytest.mark.asyncio
async def test_question_revision_classification_requires_all_comment_reasons(monkeypatch):
    async def invoke(*_args):
        return json.dumps({
            "outcome": "question_only",
            "reasons": [{
                "comment_id": "c1", "question_number": 1,
                "reason": "local fix", "references": [],
            }],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    with pytest.raises(ModelCallError, match="every comment"):
        await classify_question_revision(
            {}, {}, {}, [{"id": "c1"}, {"id": "c2"}])


@pytest.mark.asyncio
async def test_classification_discards_model_supplied_references(monkeypatch):
    async def invoke(*_args):
        return json.dumps({
            "outcome": "no_change",
            "reasons": [{
                "comment_id": "c1", "question_number": 1,
                "reason": "already correct",
                "references": ["hallucinated question", "hallucinated answer"],
            }],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    result = await classify_question_revision({}, {}, {}, [{"id": "c1"}])
    assert result == {
        "outcome": "no_change",
        "reasons": [{
            "comment_id": "c1",
            "question_number": 1,
            "outcome": "no_change",
            "reason": "already correct",
        }],
    }


@pytest.mark.asyncio
async def test_classification_preserves_each_comment_outcome_and_uses_highest_route(monkeypatch):
    async def invoke(*_args):
        return json.dumps({
            "reasons": [
                {
                    "comment_id": "c1", "question_number": 1,
                    "outcome": "question_only", "reason": "fix the carrier",
                },
                {
                    "comment_id": "c2", "question_number": 2,
                    "outcome": "replan_questions", "reason": "choose another target",
                },
                {
                    "comment_id": "c3", "question_number": 3,
                    "outcome": "no_change", "reason": "already correct",
                },
            ],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    result = await classify_question_revision(
        {}, {}, {}, [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}])

    assert result["outcome"] == "replan_questions"
    assert {
        row["comment_id"]: row["outcome"] for row in result["reasons"]
    } == {
        "c1": "question_only",
        "c2": "replan_questions",
        "c3": "no_change",
    }


@pytest.mark.asyncio
async def test_classification_projects_anchored_question_over_model_number(monkeypatch):
    async def invoke(*_args):
        return json.dumps({
            "reasons": [{
                "comment_id": "c1", "question_number": 2,
                "outcome": "no_change", "reason": "already correct",
            }],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    result = await classify_question_revision(
        {}, {}, {}, [{
            "id": "c1", "anchor": {"type": "question", "index": 1},
        }])
    assert result["reasons"][0]["question_number"] == 1


@pytest.mark.asyncio
async def test_classification_projects_anchor_when_model_omits_question_number(monkeypatch):
    async def invoke(*_args):
        return json.dumps({
            "reasons": [{
                "comment_id": "c1",
                "outcome": "replan_questions",
                "reason": "change the group layout",
            }],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    result = await classify_question_revision(
        {}, {}, {}, [{
            "id": "c1", "anchor": {"type": "question", "index": 3},
        }])
    assert result["reasons"] == [{
        "comment_id": "c1",
        "question_number": 3,
        "outcome": "replan_questions",
        "reason": "change the group layout",
    }]


@pytest.mark.asyncio
async def test_classification_requires_question_number_without_anchor(monkeypatch):
    async def invoke(*_args):
        return json.dumps({
            "reasons": [{
                "comment_id": "c1",
                "outcome": "question_only",
                "reason": "fix the wording",
            }],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    with pytest.raises(ModelCallError, match="incomplete"):
        await classify_question_revision({}, {}, {}, [{"id": "c1"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor_index", [True, 0, 11, "3"])
async def test_classification_rejects_malformed_question_anchor(
    monkeypatch, anchor_index,
):
    async def invoke(*_args):
        return json.dumps({
            "reasons": [{
                "comment_id": "c1",
                "question_number": 3,
                "outcome": "question_only",
                "reason": "fix the wording",
            }],
        })

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    with pytest.raises(ModelCallError, match="incomplete"):
        await classify_question_revision(
            {}, {}, {}, [{
                "id": "c1",
                "anchor": {"type": "question", "index": anchor_index},
            }])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_overrides", "error"),
    [
        ({"outcome": "unsupported"}, "incomplete"),
        ({"reason": "   "}, "incomplete"),
        ({"comment_id": "other"}, "incomplete"),
    ],
)
async def test_anchor_projection_does_not_weaken_reason_validation(
    monkeypatch, reason_overrides, error,
):
    reason = {
        "comment_id": "c1",
        "question_number": 9,
        "outcome": "question_only",
        "reason": "fix the wording",
    }
    reason.update(reason_overrides)

    async def invoke(*_args):
        return json.dumps({"reasons": [reason]})

    monkeypatch.setattr("backend.steps.agent_steps._invoke", invoke)
    with pytest.raises(ModelCallError, match=error):
        await classify_question_revision(
            {}, {}, {}, [{
                "id": "c1", "anchor": {"type": "question", "index": 3},
            }])


class TestRequiredKeys:
    """Check 1: the three keys the verdict is built from."""

    @pytest.mark.parametrize("missing", ["feasible", "reasons", "category_semantics_ok"])
    def test_each_required_key_is_required(self, missing):
        with pytest.raises(ModelCallError) as exc:
            _feasibility_envelope(_reply(**{missing: _ABSENT}), "feasibility")
        assert missing in str(exc.value)

    def test_a_decoy_object_does_not_qualify(self):
        """``extract_json`` returns the FIRST balanced object, so a reply can open with a summary.

        Measured on the audit side, where a leading ``{"verdict": "PASS", "note": ...}`` decoy was
        accepted as a clean pass. Requiring all three keys is what stops a decoy from qualifying.
        """
        with pytest.raises(ModelCallError):
            _feasibility_envelope('{"note": "looks fine"}\n' + _reply(), "feasibility")

    def test_a_well_formed_reply_is_returned_unchanged(self):
        """The anti-tightening assertion. Without it the envelope could raise unconditionally."""
        data = _feasibility_envelope(_reply(), "feasibility")
        assert data == {"feasible": True, "reasons": ["item 4 reads cleanly"],
                        "category_semantics_ok": True}

    def test_a_rejection_with_reasons_is_returned_not_raised(self):
        """``feasible: false`` is a legitimate answer, not a malformed reply.

        This is the one most at risk of being broken by a stricter check: an envelope that rejected
        rejections would turn every unfeasible plan into a retry loop and then a SEMANTICS_MISSING,
        so a plan that genuinely cannot carry ten items would never be reported as such.
        """
        data = _feasibility_envelope(
            _reply(feasible=False, reasons=["item 6's price is never stated"]), "feasibility")
        assert data["feasible"] is False


class TestBooleanTypes:
    """Check 2: both flags must be real JSON booleans."""

    @pytest.mark.parametrize("key", ["feasible", "category_semantics_ok"])
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
    def test_non_booleans_are_rejected(self, key, value):
        with pytest.raises(ModelCallError) as exc:
            _feasibility_envelope(_reply(**{key: value}), "feasibility")
        assert key in str(exc.value)

    def test_the_string_false_is_the_case_this_check_exists_for(self):
        """``"false"`` is truthy in Python, so a truth test would read it as a PASS.

        The material would then ship as feasible while the model said the opposite -- a measured
        false-green shape from stage 3A, restated here because this layer is the one that can retry.
        """
        with pytest.raises(ModelCallError):
            _feasibility_envelope(_reply(feasible="false"), "feasibility")


class TestReasonsShape:
    """Check 3: ``reasons`` is a list of strings."""

    def test_a_bare_string_is_rejected(self):
        """A string is iterable, so without the type check every check below would pass per-character."""
        with pytest.raises(ModelCallError):
            _feasibility_envelope(_reply(reasons="item 6 is ambiguous"), "feasibility")

    @pytest.mark.parametrize("value", [None, 5, {"item": 6}, [{"item": 6}], ["ok", 7]])
    def test_non_string_lists_are_rejected(self, value):
        with pytest.raises(ModelCallError):
            _feasibility_envelope(_reply(reasons=value), "feasibility")

    def test_an_empty_list_is_fine_when_nothing_is_wrong(self):
        assert _feasibility_envelope(_reply(reasons=[]), "feasibility")["reasons"] == []


class TestRejectionsMustBeActionable:
    """Check 4: a ``false`` has to carry a usable reason."""

    @pytest.mark.parametrize("flags", [
        {"feasible": False},
        {"category_semantics_ok": False},
        {"feasible": False, "category_semantics_ok": False},
    ])
    @pytest.mark.parametrize("reasons", [[], [""], ["   "], ["", "  "]])
    def test_a_rejection_without_a_reason_is_rejected(self, flags, reasons):
        """``[""]`` and ``["   "]`` explicitly: non-empty lists carrying zero information.

        A rejection costs a full material regeneration, and one with no stated cause gives the next
        attempt nothing to avoid.
        """
        with pytest.raises(ModelCallError):
            _feasibility_envelope(_reply(reasons=reasons, **flags), "feasibility")

    def test_one_usable_reason_among_blanks_is_enough(self):
        data = _feasibility_envelope(
            _reply(feasible=False, reasons=["", "item 6's price is never stated"]), "feasibility")
        assert data["feasible"] is False


class TestUnknownKeys:
    """Check 6: no top-level keys outside the contract."""

    @pytest.mark.parametrize("extra", [{"confidence": 0.4}, {"verdict": "PASS"},
                                       {"score": {"total": 80}}, {"reasoning": "..."}])
    def test_unknown_keys_are_rejected(self, extra):
        """A stray key means the model is answering against some other contract.

        In which case ``feasible`` may not mean what this side reads it to mean either. ``verdict``
        and ``score`` are in the list for a second reason: they are the audit's shape, and a reply
        carrying them is one where the judge did the job it was told not to do.
        """
        with pytest.raises(ModelCallError):
            _feasibility_envelope(_reply(**extra), "feasibility")

    def test_qr027_exception_is_not_an_unknown_key(self):
        data = _feasibility_envelope(
            _reply(qr027_exception={"requested": False}), "feasibility")
        assert data["qr027_exception"] == {"requested": False}


# design.md D2's table, verbatim. The same rows are asserted against the schema by
# `skills/shared/tests/run_tests.py::test_feasibility_schema_qr027_exception`; keeping both means a
# drift between the layers shows up as a failing test rather than as one layer quietly waving through
# what the other rejects.
_QR027_POSITIVES = [
    ("2: {'requested': false}", {"requested": False}),
    ("3: requested with a justification",
     {"requested": True, "justification": "three items share one category inherently"}),
    # 4 is an anti-tightening row: an earlier draft required `justification` unconditionally, which
    # rejects the entirely legal `{"requested": false}`. Every negative row below passed against it.
    ("4: not requested, justification present anyway",
     {"requested": False, "justification": "n/a"}),
]

_QR027_NEGATIVES = [
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
    # 7 is the one row where the two layers disagree ON PURPOSE: the schema accepts a whitespace-only
    # justification (`minLength` counts characters) and this layer rejects it (`strip()`). Expressing
    # "not blank" in JSON Schema needs a pattern kept aligned with Python's semantics; each layer uses
    # the tool it is good at. Do not "fix" the schema to match.
    ("7: a whitespace-only justification", {"requested": True, "justification": "   "}),
    ("8: a numeric justification", {"requested": True, "justification": 5}),
    ("9: an unknown key alongside",
     {"requested": True, "justification": "inherent", "confidence": 0.4}),
    ("10: justification misspelled", {"requested": True, "justifcation": "inherent"}),
]


class TestQr027Exception:
    """Check 5's five sub-rules, as the fourteen cases from design.md D2."""

    def test_the_key_may_be_absent(self):
        """Positive 1, and the anti-tightening assertion for the whole object being optional."""
        assert "qr027_exception" not in _feasibility_envelope(_reply(), "feasibility")

    @pytest.mark.parametrize("label,exception", _QR027_POSITIVES,
                             ids=[label for label, _ in _QR027_POSITIVES])
    def test_positives_pass(self, label, exception):
        data = _feasibility_envelope(_reply(qr027_exception=exception), "feasibility")
        assert data["qr027_exception"] == exception

    @pytest.mark.parametrize("label,exception", _QR027_NEGATIVES,
                             ids=[label for label, _ in _QR027_NEGATIVES])
    def test_negatives_raise(self, label, exception):
        with pytest.raises(ModelCallError):
            _feasibility_envelope(_reply(qr027_exception=exception), "feasibility")

    @pytest.mark.parametrize("label,exception", _QR027_NEGATIVES,
                             ids=[label for label, _ in _QR027_NEGATIVES])
    def test_the_sub_check_raises_on_its_own_too(self, label, exception):
        """Called directly, so a future envelope that stopped invoking check 5 fails here.

        Without this, deleting the `if "qr027_exception" in data:` branch would leave every case above
        green -- the negatives would still be caught by check 6's unknown-key scan for some rows and
        by nothing at all for the rest.
        """
        with pytest.raises(ModelCallError):
            _check_qr027_exception(exception, "feasibility")

    def test_a_misspelled_justification_is_caught_here_not_downstream(self):
        """Negative 10, spelled out because of what it becomes if it gets through.

        ``preflight._justification_of`` requires ``requested is True`` AND a non-blank
        ``justification``, so ``{"requested": true, "justifcation": "..."}`` reads downstream as a
        request with no reason -- which is a REGENERATE_MATERIAL over a typo. Caught here, it is one
        retry.

        Which sub-rule catches it depends on ``requested``, and both are correct: with ``true`` the
        justification is simply missing (rule c, checked first), and with ``false`` there is nothing
        missing so the unknown-key scan (rule e) is what fires. Asserting only "it raises" rather than
        the message, because pinning either message would make the test fail if the rule order changed
        without the contract changing.
        """
        with pytest.raises(ModelCallError):
            _feasibility_envelope(
                _reply(qr027_exception={"requested": True, "justifcation": "inherent"}),
                "feasibility")
        with pytest.raises(ModelCallError) as exc:
            _feasibility_envelope(
                _reply(qr027_exception={"requested": False, "justifcation": "inherent"}),
                "feasibility")
        assert "justifcation" in str(exc.value), "rule e must name the key it did not recognise"


class TestPayloadCarriesThePlan:
    def test_the_payload_carries_material_plan_and_counts(self, material, blueprint):
        payload = build_feasibility_message(material, blueprint, {"qr027_numeric_answers": 1})
        assert "## material.json" in payload
        assert "## blueprint.json" in payload
        assert "qr027_numeric_answers" in payload
        # The plan's own CONTENT, not just the heading -- see `assert_carries_plan`'s docstring on why
        # a check that the word "blueprint" appears is not a check. `target` is the field that makes
        # this a non-blind call: it is the answer key, and it is exactly what `assert_blind` refuses to
        # let near the audit payload.
        assert '"target": "%s"' % blueprint["items"][0]["target"] in payload
        assert str(blueprint["items"][9]["turn_index"]) in payload

    def test_the_judge_is_told_not_to_recount(self, material, blueprint):
        """The counts arrive calculated. A judge that recounts them is a second source of truth."""
        payload = build_feasibility_message(material, blueprint, {"qr027_numeric_answers": 1})
        assert "do not recount" in payload

    def test_the_input_is_frozen(self, material, blueprint):
        data = FeasibilityInput(material, blueprint, {})
        with pytest.raises(AttributeError):
            data.blueprint = {}
        with pytest.raises(AttributeError):
            del data.blueprint

    @pytest.mark.parametrize("bad", [None, {}, {"blueprint_schema_version": 1, "items": []}])
    def test_assembly_refuses_a_plan_that_is_not_there(self, material, bad):
        """The guard runs before assembly, so a blind judgment cannot be built at all."""
        from backend.deterministic.guards import MissingPlanViolation

        with pytest.raises(MissingPlanViolation):
            build_feasibility_payload(FeasibilityInput(material, bad, {}))


# The negative fixtures both layers must agree on.
#
# TWO SETS OF CASES ARE DELIBERATELY EXCLUDED, and both exclusions are decisions rather than gaps:
#
# 1. Check 6's unknown-key cases. Rejecting an unrecognised key is this layer's own strictness; the
#    preflight ignores unknown keys on purpose, because it is a skill script other callers may reuse
#    and a key it does not read cannot mislead it.
# 2. A non-string INSIDE `reasons` on the accept path (`{"feasible": true, "reasons": [7], ...}`).
#    Measured: layer 3 answers PASS there. It checks `reasons` is a list, and checks the item types
#    only where it reads them -- on a rejection, where `[7]` does become SEMANTICS_MISSING (also
#    measured, and asserted below). That is defensible at the verdict: with both booleans true, nobody
#    downstream reads `reasons`, so a junk entry beside an approval changes no decision. This layer
#    still rejects it, because a reply whose types are wrong is a reply to retry while retries exist.
#
# Do not "complete" this list with either set. The divergences are asserted directly instead.
_BAD_FOR_BOTH_LAYERS = [
    {"reasons": [], "category_semantics_ok": True},                      # feasible absent
    {"feasible": True, "category_semantics_ok": True},                   # reasons absent
    {"feasible": True, "reasons": []},                                   # category_semantics_ok absent
    {"feasible": "false", "reasons": [], "category_semantics_ok": True},
    {"feasible": True, "reasons": [], "category_semantics_ok": 1},
    {"feasible": True, "reasons": "not a list", "category_semantics_ok": True},
    {"feasible": False, "reasons": [7], "category_semantics_ok": True},
    {"feasible": False, "reasons": [], "category_semantics_ok": True},
    {"feasible": False, "reasons": [""], "category_semantics_ok": True},
    {"feasible": True, "reasons": [], "category_semantics_ok": False},
]

# The clean deterministic half, so the semantic half is the only thing under test. Hand-built rather
# than produced by running the validator: what is being checked is the aggregator's reading of a
# malformed *semantic* reply, and a real validator cannot produce one at all.
def _clean_validation() -> dict:
    return {
        "ok": True, "errors": [], "warnings": [],
        "metrics": {"blueprint_schema_version": 2, "qr027_numeric_answers": 1,
                    "qr027_spelled_answers": 9, "qr027_largest_category": 2},
    }


def _preflight():
    scripts = str(paths.validate_script().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import question_feasibility_preflight as pf

    return pf


class TestTheTwoLayersAgree:
    """The precondition for letting layers 2 and 3 overlap at all.

    Two hand-written checks of the same contract drift, and the drift directions are both bad: relax
    layer 2 and bad output reaches layer 3; relax layer 3 and bad output collects a
    REGENERATE_MATERIAL, burning a whole regeneration over a formatting slip. So the same fixtures are
    run through both.
    """

    @pytest.mark.parametrize("bad", _BAD_FOR_BOTH_LAYERS,
                             ids=[str(sorted(b)) + "/" + str(list(b.values()))[:30]
                                  for b in _BAD_FOR_BOTH_LAYERS])
    def test_layer_two_raises_and_layer_three_cannot_decide(self, bad):
        pf = _preflight()
        with pytest.raises(ModelCallError):
            _feasibility_envelope(json.dumps(bad), "feasibility")

        verdict = pf.preflight(_clean_validation(), bad)
        # SEMANTICS_MISSING and never REGENERATE_MATERIAL: a malformed reply is not evidence the
        # material is unfit, and treating it as such spends a generation to fix nothing.
        assert verdict.outcome == pf.SEMANTICS_MISSING, (bad, verdict.outcome, verdict.reasons)

    def test_the_two_known_divergences_are_what_the_comment_says(self):
        """Pins both exclusions above as measured facts, so neither can rot into a real drift.

        Written as an assertion rather than left in the comment because a comment describing behaviour
        stops being true silently. If the preflight is later tightened to check item types everywhere,
        this test fails and the fixture list is where the answer goes.
        """
        pf = _preflight()

        # Divergence 1: an unknown key. This layer refuses; the preflight ignores it and decides.
        unknown = {"feasible": True, "reasons": ["fine"], "category_semantics_ok": True,
                   "confidence": 0.4}
        with pytest.raises(ModelCallError):
            _feasibility_envelope(json.dumps(unknown), "feasibility")
        assert pf.preflight(_clean_validation(), unknown).outcome == pf.PASS

        # Divergence 2: a non-string in `reasons` beside an approval.
        junk_ok = {"feasible": True, "reasons": [7], "category_semantics_ok": True}
        with pytest.raises(ModelCallError):
            _feasibility_envelope(json.dumps(junk_ok), "feasibility")
        assert pf.preflight(_clean_validation(), junk_ok).outcome == pf.PASS

        # ...but the same junk beside a REJECTION is caught by both, which is the case that matters:
        # there the reasons are what the next generation is supposed to act on.
        junk_reject = {"feasible": False, "reasons": [7], "category_semantics_ok": True}
        assert pf.preflight(_clean_validation(), junk_reject).outcome == pf.SEMANTICS_MISSING

    def test_the_backend_constants_match_the_scripts(self):
        """A copied string that drifts is worse than the lazy import it saves."""
        from backend.deterministic import feasibility as backend_side

        pf = _preflight()
        for name in ("PASS", "PASS_WITH_JUSTIFICATION", "REGENERATE_MATERIAL",
                     "SEMANTICS_MISSING", "VALIDATION_INCOMPLETE", "UNSUPPORTED_VERSION"):
            assert getattr(backend_side, name) == getattr(pf, name), name

    def test_cannot_decide_covers_exactly_the_three_non_exits(self):
        from backend.deterministic import feasibility as backend_side

        assert backend_side.CANNOT_DECIDE == frozenset({
            backend_side.SEMANTICS_MISSING,
            backend_side.VALIDATION_INCOMPLETE,
            backend_side.UNSUPPORTED_VERSION,
        })
        # None of the three client-named exits may leak into it: folding one in would assert a
        # material is unfit in order to report that a system-side problem occurred.
        for exit_name in (backend_side.PASS, backend_side.PASS_WITH_JUSTIFICATION,
                          backend_side.REGENERATE_MATERIAL):
            assert exit_name not in backend_side.CANNOT_DECIDE
