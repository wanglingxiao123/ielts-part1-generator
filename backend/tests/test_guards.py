"""Blindness guard tests -- the highest-priority correctness property (prd.md R3).

These tests exist because a blindness leak has no symptom. It does not raise, it does not
produce malformed output; the score simply comes out higher than it should, and the artifact
looks fine. So the guard has to be tested as carefully as the thing it guards.
"""

from __future__ import annotations

import json

import pytest

from backend.deterministic.guards import (
    BLUEPRINT_ONLY_KEYS,
    FEASIBILITY_ITEM_COUNT,
    FEASIBILITY_PLAN_VERSION,
    BlindnessViolation,
    MissingPlanViolation,
    assert_blind,
    assert_carries_plan,
    assert_reference_text_blind,
    blueprint_key_hits,
)
from backend.steps.agent_steps import (
    BlindAuditInput,
    build_audit_payload,
    build_feasibility_message,
)


class TestPromptGuard:
    def test_form_group_is_rejected(self):
        with pytest.raises(BlindnessViolation) as exc:
            assert_blind('audit this: {"form_group": "A"}')
        assert "form_group" in str(exc.value)

    @pytest.mark.parametrize("key", [k for k in BLUEPRINT_ONLY_KEYS])
    def test_every_declared_key_is_actually_enforced(self, key):
        """A key listed but not enforced is worse than no list: it reads as protection."""
        with pytest.raises(BlindnessViolation):
            assert_blind("prompt fragment %s trailing text" % key)

    def test_case_insensitive(self):
        with pytest.raises(BlindnessViolation):
            assert_blind('{"Form_Group": "A"}')

    def test_serialised_blueprint_is_rejected(self, blueprint):
        with pytest.raises(BlindnessViolation):
            assert_blind(json.dumps(blueprint))

    def test_clean_prompt_passes(self, material):
        assert_blind(json.dumps(material))

    def test_guard_raises_rather_than_stripping(self):
        """Silent sanitising would keep the batch green while changing what was audited."""
        leaked = '{"item_form": "table"}'
        with pytest.raises(BlindnessViolation):
            assert_blind(leaked)

    def test_natural_language_target_is_not_a_false_positive(self):
        """A guard that blocks valid materials gets switched off."""
        assert_blind("speaker3: Our target date is the fifth, and we booked a group room.")

    def test_hits_are_reported_sorted_and_deduplicated(self):
        hits = blueprint_key_hits('{"item_form": 1, "form_group": 2, "item_form": 3}')
        assert hits == ["form_group", "item_form"]


class TestAuditPayloadIsBlind:
    def test_real_audit_payload_passes_its_own_guard(self, material):
        payload = build_audit_payload(BlindAuditInput(material, {"dialogue_words": 618}))
        assert_blind(payload)

    def test_payload_carries_material_and_metrics_only(self, material):
        payload = build_audit_payload(BlindAuditInput(material, {"dialogue_words": 618}))
        assert "618" in payload and "listening_material_parts" in payload

    def test_input_type_has_exactly_two_fields(self):
        """Type isolation: no third field for planning data to arrive through."""
        assert BlindAuditInput.__slots__ == ("material", "metrics")

    def test_input_is_frozen(self, material, blueprint):
        data = BlindAuditInput(material, {})
        with pytest.raises(AttributeError):
            data.blueprint = blueprint

    def test_audit_blind_signature_has_no_third_parameter(self, material, blueprint):
        """Structural proof for prd.md's "cannot be called with a blueprint" criterion."""
        import inspect

        from backend.steps.agent_steps import audit_blind

        parameters = list(inspect.signature(audit_blind).parameters)
        assert parameters == ["material", "metrics"]
        with pytest.raises(TypeError):
            inspect.signature(audit_blind).bind(material, {}, blueprint)

    def test_audit_module_does_not_import_generation_types(self):
        """Defence 2, as an import check: planning types must be unreachable from here.

        Parses the AST rather than grepping the text, so the module's own docstring explaining
        the isolation cannot satisfy -- or break -- the assertion.
        """
        import ast

        import backend.steps.agent_steps as module

        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any("generate" in name or "revise" in name for name in imported), imported

    def test_planning_identifiers_absent_from_the_audit_functions(self):
        """The CI grep from implement.md phase 3, narrowed to the functions it is about.

        The grep used to cover a whole module that did nothing but audit. Now generation, revision
        and audit share one module -- and generation legitimately handles the blueprint, because
        producing it is its job. So the check is scoped to the audit functions' own ASTs instead of
        the file, which keeps it meaningful: a blueprint reference inside `audit_blind` or
        `build_audit_payload` is a leak, one inside `revise` is the feature.

        Scoped by AST rather than by line range so it cannot be defeated by moving code around.
        """
        import ast

        import backend.steps.agent_steps as module

        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        audit_functions = {"audit_blind", "build_audit_payload", "build_audit_message"}
        checked = set()
        for node in ast.walk(tree):
            name = getattr(node, "name", None)
            if name not in audit_functions:
                continue
            checked.add(name)
            # Skip the docstring: it explains the boundary, and explaining it is not violating it.
            body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value, ast.Constant)) else node.body
            for inner in [n for stmt in body for n in ast.walk(stmt)]:
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    for banned in ("blueprint", "form_group", "question_type_coverage",
                                   "item_form"):
                        assert banned not in inner.value, "%s: %r" % (name, inner.value[:60])
                if isinstance(inner, ast.Name):
                    assert "blueprint" not in inner.id, "%s references %s" % (name, inner.id)
                if isinstance(inner, ast.Attribute):
                    assert "blueprint" not in inner.attr, "%s reads .%s" % (name, inner.attr)
        assert checked == audit_functions, "missed: %s" % (audit_functions - checked)


class TestReferenceTextTier:
    def test_prose_mention_is_allowed(self):
        """The audit skill must be able to say "do not read the blueprint"."""
        assert_reference_text_blind("Never read a generator blueprint, even if offered.")

    def test_serialised_field_is_still_rejected(self):
        with pytest.raises(BlindnessViolation):
            assert_reference_text_blind('example: {"question_type_coverage": {"form": [1]}}')

    def test_real_audit_system_prompt_is_clean(self):
        # The audit agent's system prompt is procedural; the rubric reaches it by activating a
        # skill. So what must be clean is the pool's own text -- the rubric plus the schema the agent
        # will read -- which is what an assembled prompt used to consist of.
        from backend import agents as agents_module

        pool = agents_module.pool_dir("audit")
        for path in sorted(pool.rglob("*.md")):
            assert_reference_text_blind(path.read_text(encoding="utf-8"), str(path.name))
        assert (pool / "audit-listening-part1" / "references" / "audit-rubric.md").is_file()

    def test_audit_prompt_excludes_the_planning_schema(self):
        from backend import agents as agents_module

        pool = agents_module.pool_dir("audit")
        assert not list(pool.rglob("blueprint*.json"))
        for path in sorted(pool.rglob("*")):
            if path.is_file():
                assert "blueprint.schema.json" not in path.read_text(
                    encoding="utf-8", errors="ignore"), path.name


class TestCarriesPlanGuard:
    """AC8: the mirror of :func:`assert_blind`, and the reason it needs its own tests.

    A blindness leak at least changes the payload. A *missing* plan changes nothing observable: the
    judge sees only the script, answers anyway with exactly the confidence it would have had, and the
    reply is the same shape. So the guard has to be tested for the one property that is easy to lose --
    that it can actually fail.
    """

    def test_a_real_v2_plan_passes(self, blueprint):
        """The anti-tightening assertion, and it is not a formality.

        Without it the guard could degrade to `raise` on everything and every negative case below
        would still be green -- the same shape as stage 3A's "feasible:true + empty reasons must PASS".
        """
        assert_carries_plan(blueprint)
        assert len(blueprint["items"]) == FEASIBILITY_ITEM_COUNT
        assert blueprint["blueprint_schema_version"] == FEASIBILITY_PLAN_VERSION

    @pytest.mark.parametrize("bad", [None, "x", [], 0, 2, True, ("a",), object()])
    def test_a_non_dict_is_refused(self, bad):
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(bad)

    def test_an_empty_dict_is_refused(self):
        """The most important row in this class: the keyword guard this replaced waved it through.

        `BLUEPRINT_ONLY_KEYS` contains the bare word `blueprint`, which the payload's own
        `## blueprint.json` heading always matches -- so a keyword-based version of this guard passed
        `{}`, an empty `items` list and a v1 plan alike. An assertion that cannot fail is worse than no
        assertion, because it is read as coverage.

        The message is asserted, and that is not pinning prose. Measured by mutation: deleting the
        `not blueprint` criterion leaves `{}` refused anyway, because an empty dict also has no
        version -- so the version gate masks it and a raises-only assertion stays green while the
        criterion is gone. What the two messages distinguish is which bug to look for: "nothing was
        handed over" (a default argument survived, an upstream field was read under the wrong key)
        versus "this plan has the wrong number of items" (a truncated blueprint). Reporting the
        second for an empty payload sends the next reader to count items that do not exist.
        """
        with pytest.raises(MissingPlanViolation) as exc:
            assert_carries_plan({})
        assert "no plan to judge" in str(exc.value), str(exc.value)

    @pytest.mark.parametrize("version", [1, 3, "2", 2.0, None, True, False, [2], {"v": 2}])
    def test_only_integer_version_two_is_accepted(self, version, blueprint, clone):
        """`True` and `2.0` are in the list for specific reasons.

        `isinstance(True, int)` holds and `True == 1`, so a bare int check accepts `True` as a version
        number. `2.0 == 2` is also true, so an equality-only check accepts a float that no validator
        ever writes. Both are caught by testing the type before the value.
        """
        plan = clone(blueprint)
        plan["blueprint_schema_version"] = version
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(plan)

    def test_a_missing_version_key_is_refused(self, blueprint, clone):
        """v1 records have no version key at all, and they are display-only by product decision."""
        plan = clone(blueprint)
        del plan["blueprint_schema_version"]
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(plan)

    @pytest.mark.parametrize("count", [0, 1, 9, 11, 20])
    def test_the_item_count_must_be_exactly_ten(self, count, blueprint, clone):
        plan = clone(blueprint)
        plan["items"] = plan["items"][:count] if count <= 10 else plan["items"] * 2
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(plan)

    @pytest.mark.parametrize("items", [None, "ten", {}, 10, [None] * 10])
    def test_items_must_be_a_list_of_objects(self, items, blueprint, clone):
        plan = clone(blueprint)
        plan["items"] = items
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(plan)

    def test_a_missing_items_key_is_refused(self, blueprint, clone):
        plan = clone(blueprint)
        del plan["items"]
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(plan)

    def test_one_non_object_item_among_ten_is_refused(self, blueprint, clone):
        """Ten entries of the right count but the wrong kind. The count check alone would pass this."""
        plan = clone(blueprint)
        plan["items"][6] = "an item"
        with pytest.raises(MissingPlanViolation) as exc:
            assert_carries_plan(plan)
        assert "7" in str(exc.value), "the position must be named, so the report is actionable"

    def test_item_contents_are_not_re_checked(self, blueprint, clone):
        """An item missing its fields still passes: that is `validate_part1.py`'s question.

        Re-checking it here would be a second implementation of the item contract, and the two would
        disagree eventually -- with this one silently blocking calls the validator considers fine.
        """
        plan = clone(blueprint)
        plan["items"] = [{} for _ in range(FEASIBILITY_ITEM_COUNT)]
        assert_carries_plan(plan)

    def test_the_label_reaches_the_message(self):
        with pytest.raises(MissingPlanViolation) as exc:
            assert_carries_plan({}, "some other caller")
        assert "some other caller" in str(exc.value)


class TestTheTwoPayloadsCannotBeSwapped:
    """AC3 / AC4: the blind and non-blind paths are mutually exclusive by construction.

    Neither guard is redundant with the other, and neither can be an option on a shared function --
    which is the property these two tests pin. If one payload builder ever grew a `blind=` parameter,
    both assertions below would have to be deleted to make it pass.
    """

    def test_the_feasibility_payload_would_fail_the_blindness_guard(self, material, blueprint):
        """Proof by contradiction that the two paths are not interchangeable."""
        payload = build_feasibility_message(material, blueprint, {"qr027_numeric_answers": 1})
        with pytest.raises(BlindnessViolation):
            assert_blind(payload, "feasibility payload sent down the audit path")

    def test_the_audit_payload_would_fail_the_carries_plan_guard(self, material):
        """And the other direction: the blind payload has no plan to judge.

        `assert_carries_plan` takes the blueprint OBJECT rather than the assembled string, so the
        argument here is what the audit path actually has -- nothing.
        """
        from backend.steps.agent_steps import BlindAuditInput, build_audit_payload

        blind = build_audit_payload(BlindAuditInput(material, {"dialogue_words": 618}))
        assert_blind(blind)
        with pytest.raises(MissingPlanViolation):
            assert_carries_plan(None, "audit payload sent down the feasibility path")

    def test_the_audit_payload_is_byte_identical_to_before(self, material):
        """AC3: adding a third path must not have changed the second one by a character.

        Pinned as a fixed string rather than by re-deriving it from the module, which would pass no
        matter what the module produced. The metrics dict is one key so the expected value stays
        readable; the material is the shared fixture.
        """
        from backend.steps.agent_steps import BlindAuditInput, build_audit_payload

        payload = build_audit_payload(BlindAuditInput(material, {"dialogue_words": 618}))
        expected = "\n\n".join([
            "Audit the listening material below.",
            "## material.json\n\n%s" % json.dumps(material, ensure_ascii=False, indent=2),
            "## Deterministic metrics (already calculated; do not recount)\n\n%s"
            % json.dumps({"dialogue_words": 618}, ensure_ascii=False, indent=2),
        ])
        assert payload == expected
