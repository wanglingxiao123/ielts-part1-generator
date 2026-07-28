"""Filesystem locations for skill assets and configuration.

The skill files (SKILL.md, references, scripts) are maintained by the skill-contract task and
are the single source of truth. The backend only reads them, and resolves them through this
module so the container layout and the repo layout differ in exactly one place.
"""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent


def _first_existing(candidates, label: str) -> Path:
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    raise FileNotFoundError(
        "could not locate %s; tried %s" % (label, [str(c) for c in candidates if c])
    )


def skills_root() -> Path:
    """Directory holding the two skills plus shared/."""
    override = os.environ.get("IELTS_SKILLS_ROOT")
    return _first_existing(
        [
            Path(override) if override else None,
            REPO_ROOT / "skills" / "ielts-listening-skills",
            BACKEND_DIR / "skills",
        ],
        "skills root",
    )


def scenarios_path() -> Path:
    """The scenario catalogue. One file, shared with the audio-storage task.

    Deliberately not copied into backend/: two catalogues drift, and a drifted scenario id
    means the frontend offers a scenario the backend does not recognise.
    """
    override = os.environ.get("IELTS_SCENARIOS_PATH")
    return _first_existing(
        [
            Path(override) if override else None,
            REPO_ROOT / "config" / "scenarios.yaml",
            BACKEND_DIR / "config" / "scenarios.yaml",
        ],
        "scenarios.yaml",
    )


def generate_skill_dir() -> Path:
    return skills_root() / "generate-ielts-listening-part1"


def audit_skill_dir() -> Path:
    return skills_root() / "audit-ielts-listening-part1"


def validate_script() -> Path:
    return generate_skill_dir() / "scripts" / "validate_part1.py"


def metrics_script() -> Path:
    return audit_skill_dir() / "scripts" / "audit_metrics.py"
