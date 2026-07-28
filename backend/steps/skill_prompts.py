"""Load prompts from the human-maintained skill files.

The constraint from prd.md: SKILL.md, references and scripts are the single source of truth.
No prompt text is transcribed into the backend. If the specification changes, the behaviour
changes with it -- a copied prompt here would become a second source of truth that silently
diverges from the file reviewers actually edit.

Cached per process. These files are immutable for a container's lifetime, and re-reading them
for all ~5 calls per material would add pointless I/O to the event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from .. import paths
from ..deterministic.guards import assert_reference_text_blind

__all__ = ["generate_system_prompt", "audit_system_prompt", "revise_system_prompt"]

_cache: Dict[str, str] = {}


def _read(path: Path) -> str:
    key = str(path)
    if key not in _cache:
        _cache[key] = path.read_text(encoding="utf-8")
    return _cache[key]


def _schema(name: str) -> str:
    return _read(paths.skills_root() / "shared" / "schemas" / name)


def generate_system_prompt() -> str:
    """generate skill: SKILL.md + specification.md + both output schemas."""
    skill = paths.generate_skill_dir()
    return "\n\n".join([
        _read(skill / "SKILL.md"),
        "# Reference: specification.md\n\n" + _read(skill / "references" / "specification.md"),
        "# Schema: material.schema.json\n\n" + _schema("material.schema.json"),
        "# Schema: blueprint.schema.json\n\n" + _schema("blueprint.schema.json"),
    ])


def audit_system_prompt() -> str:
    """audit skill: SKILL.md + audit-rubric.md + audit schema only.

    Note what is absent: blueprint.schema.json. The auditor is not told the shape of the
    generator's plan, so it cannot pattern-match its reconstruction onto that shape even
    accidentally. The assembled text is scanned for serialised blueprint fields before it is
    returned -- defence 3 of design.md §4, applied at the source as well as at the wire.
    """
    skill = paths.audit_skill_dir()
    prompt = "\n\n".join([
        _read(skill / "SKILL.md"),
        "# Reference: audit-rubric.md\n\n" + _read(skill / "references" / "audit-rubric.md"),
        "# Schema: audit.schema.json\n\n" + _schema("audit.schema.json"),
    ])
    assert_reference_text_blind(prompt, "system prompt")
    return prompt


def revise_system_prompt() -> str:
    """revise reuses the generate skill: a revision must satisfy the same contract.

    Giving the reviser its own condensed rules would create a second specification that drifts
    from the one the validator enforces.
    """
    return generate_system_prompt()
