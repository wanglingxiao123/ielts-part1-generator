"""Model provider configuration tests.

No live calls: these assert the wiring the design depends on, all of which was verified against
the real SDK on 2026-07-28 (see docs/model-access.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from strands.models.openai_responses import OpenAIResponsesModel

from backend.model import provider

BACKEND = Path(__file__).resolve().parents[1]


class TestResponsesApiIsUsed:
    def test_provider_builds_a_responses_model(self, monkeypatch):
        """GPT-5.6 is Responses-API only; a Chat Completions provider cannot reach it."""
        monkeypatch.setattr(provider, "AUTH_MODE", "bearer")
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-token")
        model = provider.build_model(max_output_tokens=1000)
        assert isinstance(model, OpenAIResponsesModel)

    def test_chat_completions_model_is_never_imported(self):
        source = (BACKEND / "model" / "provider.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name != "OpenAIModel"


class TestNoTokenLifecycleLogic:
    def test_provider_has_no_cache_or_expiry_handling(self):
        """Strands mints a bearer token per call; duplicating that would add a staleness bug."""
        source = (BACKEND / "model" / "provider.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for banned in ("refresh_token", "token_cache", "_expires_at", "is_expired"):
            assert banned not in names


class TestMantleAndClientArgsAreExclusive:
    def test_sdk_rejects_both_at_construction(self):
        """Verified against the installed SDK, not assumed from the design document."""
        with pytest.raises(ValueError) as exc:
            OpenAIResponsesModel(
                model_id="openai.gpt-5.6-terra",
                bedrock_mantle_config={"region": "us-east-1"},
                client_args={"api_key": "x"},
            )
        assert "api_key" in str(exc.value)

    def test_mantle_mode_passes_no_credentials_in_client_args(self, monkeypatch):
        captured = {}

        class Spy(OpenAIResponsesModel):
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(provider, "OpenAIResponsesModel", Spy)
        monkeypatch.setattr(provider, "AUTH_MODE", "mantle")
        provider.build_model(max_output_tokens=1000)
        assert "client_args" not in captured
        assert captured["bedrock_mantle_config"] == {"region": provider.REGION}


class TestParameters:
    def test_temperature_is_never_sent(self, monkeypatch):
        """Live endpoint rejects it: 400 unsupported_parameter for this model family.

        The design specified per-step temperatures; that turned out to be wrong, so step
        behaviour is tuned with reasoning.effort instead.
        """
        captured = {}

        class Spy(OpenAIResponsesModel):
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(provider, "OpenAIResponsesModel", Spy)
        monkeypatch.setattr(provider, "AUTH_MODE", "mantle")
        provider.build_model(max_output_tokens=1000, reasoning_effort="high")
        assert "temperature" not in captured["params"]
        assert captured["params"]["max_output_tokens"] == 1000
        assert captured["params"]["reasoning"] == {"effort": "high"}

    def test_no_step_sets_a_temperature(self):
        for name in ("generate.py", "audit.py", "revise.py"):
            tree = ast.parse((BACKEND / "steps" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "temperature":
                    pytest.fail("%s sets a temperature the model rejects" % name)


class TestRegionGuard:
    def test_supported_regions_pass(self):
        provider.assert_region_supported("us-east-1")
        provider.assert_region_supported("us-east-2")

    def test_other_regions_are_rejected(self):
        """GPT-5.6 has no cross-region inference: Runtime and model must share a region."""
        with pytest.raises(ValueError) as exc:
            provider.assert_region_supported("eu-west-1")
        assert "cross-region" in str(exc.value)

    def test_region_check_is_not_run_at_import_time(self):
        """An import-time check would make the container fail /ping over a config problem."""
        tree = ast.parse((BACKEND / "model" / "provider.py").read_text(encoding="utf-8"))
        for node in tree.body:
            assert not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call)
