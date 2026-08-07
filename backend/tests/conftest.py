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
