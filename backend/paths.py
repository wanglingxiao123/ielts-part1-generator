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
    """Directory holding the skill pools (``generate/`` and ``audit/``).

    Resolution order matters. ``REPO_ROOT / "skills"`` comes first because that is where the pools
    live in the repository; ``BACKEND_DIR / "skills"`` is the container layout, where the Dockerfile
    copies them. The legacy ``skills/ielts-listening-skills`` path is gone: it held one directory per
    skill with a shared ``schemas/``, which is the shape the pools replaced.
    """
    override = os.environ.get("IELTS_SKILLS_ROOT")
    return _first_existing(
        [
            Path(override) if override else None,
            REPO_ROOT / "skills",
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


# Skills live in pools now (``skills/generate/``, ``skills/audit/``), one directory per subject
# inside each. These helpers find a script by name across a pool rather than naming a subject
# directory, so a second subject does not need an edit here.


def _script_in_pool(pool: str, filename: str) -> Path:
    """The one script called ``filename`` inside ``pool``.

    Ambiguity is an error rather than a first-match: two subjects both shipping
    ``validate_part1.py`` would make the winner depend on directory order, and the loser would be
    validated by the wrong rules. A caller that needs a specific subject's script should go through
    the capability that declares it.
    """
    root = skills_root() / pool
    matches = sorted(root.glob("*/scripts/%s" % filename))
    if not matches:
        raise FileNotFoundError("no %s under %s" % (filename, root))
    if len(matches) > 1:
        raise FileNotFoundError(
            "%s is ambiguous in %s: %s" % (filename, root, [str(m) for m in matches])
        )
    return matches[0]


def validate_script() -> Path:
    return _script_in_pool("generate", "validate_part1.py")


def metrics_script() -> Path:
    return _script_in_pool("audit", "audit_metrics.py")
