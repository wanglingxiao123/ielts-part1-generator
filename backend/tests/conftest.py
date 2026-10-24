"""Shared fixtures. Uses the skill's own test fixtures rather than inventing material.

The skill fixtures are real, validator-passing documents kept in sync with the frozen contract
by the 51-check suite. Hand-written material here would drift from that contract and the tests
would start asserting a shape nothing else agrees with.
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

FIXTURES = REPO_ROOT / "skills" / "shared" / "tests" / "fixtures"


@pytest.fixture
def offline_agent_models(monkeypatch):
    """Opt-in setup for Agent structure/envelope tests, not provider auth tests.

    Construct real Agents and their tools without operator credentials. Model
    execution is forbidden: these tests inspect Agents or replace `_invoke`.
    """
    from backend.model import provider
    from strands.models.openai import OpenAIModel
    from strands.models.openai_responses import OpenAIResponsesModel

    monkeypatch.setattr(provider, "AUTH_MODE", "bearer")
    monkeypatch.setattr(provider, "MODEL_ID", "moonshotai.kimi-k2.5")
    monkeypatch.setattr(provider, "REGION", "us-east-1")
    monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", "moonshotai.kimi-k2.5")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "offline-agent-test-token")

    async def no_model_calls(*args, **kwargs):
        pytest.fail("Agent structure/envelope tests must not invoke a model")
        yield  # Keep the model's async-generator interface.

    monkeypatch.setattr(OpenAIModel, "stream", no_model_calls)
    monkeypatch.setattr(OpenAIResponsesModel, "stream", no_model_calls)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def material() -> dict:
    return _load("material_valid.json")


@pytest.fixture
def blueprint() -> dict:
    return _load("blueprint_valid.json")


@pytest.fixture
def audit_aligned() -> dict:
    return _load("audit_aligned.json")


@pytest.fixture
def audit_diverged() -> dict:
    return _load("audit_valid.json")


@pytest.fixture
def clone():
    return copy.deepcopy


def _question_package_document() -> dict:
    """The validator-clean question package, built by the skill suite's own helper.

    Imported rather than reproduced, for the same reason this file loads the skill fixtures instead of
    writing material inline. There is no committed question-package fixture -- ``build_fixtures.py``
    owns ``fixtures/`` and the package is assembled in memory from the material and the plan -- so the
    only alternative was a hand-written face here, which would drift from the contract the 60-check
    suite keeps and leave these tests asserting a shape nothing else agrees with.

    ``run_tests`` imports only the standard library at module scope, so this costs nothing but a
    directory glob.
    """
    tests_dir = str(FIXTURES.parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import run_tests  # noqa: PLC0415 - path must be set up first

    return run_tests._question_package()


@pytest.fixture
def question_package() -> dict:
    return _question_package_document()


@pytest.fixture
def question_face() -> dict:
    """Block A alone -- the only block a question auditor may ever receive."""
    return _question_package_document()["question_face"]
