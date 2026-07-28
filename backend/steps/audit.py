"""Blind audit step (design.md §4 defence 1: type isolation).

The auditor reads the script and the deterministic metrics. Nothing else. It never receives the
generator's private plan of information points, in any form, at any point.

Why this is structural rather than a convention: comparing two independently produced
information maps is the strongest quality signal in the system, and it collapses into mere
agreement the moment the auditor can see what was planned. The failure is silent -- the score
simply comes out too high, no error is raised, and the resulting artifact looks entirely normal.
Nobody would find it by reading the output.

So the isolation is enforced four ways, and this module carries three of them:

1. ``BlindAuditInput`` has two fields, ``material`` and ``metrics``. ``audit_blind`` takes
   exactly those two arguments. There is no third parameter for a caller to pass planning data
   into, so the mistake cannot be made by extending an argument list.
2. This module imports nothing from ``generate``. Its planning types are not reachable here.
3. ``assert_blind`` scans the assembled payload before the request is sent, and raises.

The fourth is the memoryless re-audit: every call builds a new client with a fresh empty
session, so a second audit of a revised script cannot inherit the first audit's conclusions or
the revision instructions. Verified by a unit test asserting both calls have identical message
structure.

A CI grep over this file asserts the planning-side identifiers never appear in it. That grep is
why this docstring words things the way it does -- a check that a comment can defeat is not a
check.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from ..deterministic.guards import assert_blind
from ..model import provider
from .call import call_json
from .skill_prompts import audit_system_prompt

__all__ = ["BlindAuditInput", "audit_blind", "build_audit_payload"]

AUDIT_MAX_TOKENS = 32000
# Higher effort than generation: a verdict that drifts between runs is worse than a slow one,
# because every downstream routing decision is made from it.
AUDIT_EFFORT = "high"


class BlindAuditInput(object):
    """Everything the auditor is allowed to see. Immutable, and exactly two fields.

    ``material`` is material.json verbatim; ``metrics`` is the deterministic script output.
    Adding a field to this class is the one change that could break the isolation, which is
    precisely why the permitted input is named here in one visible place instead of being
    implied by a call site.
    """

    __slots__ = ("material", "metrics")

    def __init__(self, material: Dict[str, Any], metrics: Dict[str, Any]) -> None:
        object.__setattr__(self, "material", material)
        object.__setattr__(self, "metrics", metrics)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("BlindAuditInput is frozen")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("BlindAuditInput is frozen")


def build_audit_payload(data: BlindAuditInput) -> str:
    """Serialise the permitted input into the user message.

    Instructions are intentionally minimal: the rubric already lives in the system prompt read
    from the skill file. Restating it here would create a second source of truth.
    """
    return "\n\n".join([
        "Audit the listening material below and return one JSON object conforming to "
        "audit.schema.json.",
        "## material.json\n\n%s" % json.dumps(data.material, ensure_ascii=False, indent=2),
        "## Deterministic metrics (already calculated; do not recount)\n\n%s"
        % json.dumps(data.metrics, ensure_ascii=False, indent=2),
        "Build the information map by reading the script only. Return JSON with no Markdown "
        "fences and no commentary.",
    ])


async def audit_blind(material: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Audit a material. Two parameters, and there will never be a third.

    Each call constructs its own client and its own single-turn session, so consecutive audits
    of an original and a revised script share no state whatsoever.
    """
    data = BlindAuditInput(material=material, metrics=metrics)
    payload = build_audit_payload(data)
    assert_blind(payload)
    model = provider.build_model(
        max_output_tokens=AUDIT_MAX_TOKENS, reasoning_effort=AUDIT_EFFORT
    )
    return await call_json(model, audit_system_prompt(), payload)
