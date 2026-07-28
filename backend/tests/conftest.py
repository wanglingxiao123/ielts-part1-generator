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

FIXTURES = REPO_ROOT / "skills" / "ielts-listening-skills" / "shared" / "tests" / "fixtures"


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
