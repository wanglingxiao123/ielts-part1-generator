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
    BlindnessViolation,
    assert_blind,
    assert_reference_text_blind,
    blueprint_key_hits,
)
from backend.steps.agent_steps import BlindAuditInput, build_audit_payload


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
