"""Generation step: one model call producing material + blueprint.

Contains no branching. Whether the output is acceptable, and whether to try again, is decided
in orchestration/loop.py -- see design.md §1's responsibility line.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..model import provider
from .call import ModelCallError, call_json
from .skill_prompts import generate_system_prompt

__all__ = ["GenOutput", "generate"]

GENERATE_MAX_TOKENS = 32000
GENERATE_EFFORT = "medium"


class GenOutput(object):
    """A material/blueprint pair from one generation or revision.

    Kept as one object so the two artifacts cannot be separated by accident: the whole point of
    design.md §6's same-source rule is that a delivered material and its blueprint always come
    from the same model call.
    """

    __slots__ = ("material", "blueprint")

    def __init__(self, material: Dict[str, Any], blueprint: Dict[str, Any]) -> None:
        self.material = material
        self.blueprint = blueprint


def _stamp(material: Dict[str, Any], scenario_text: str) -> Dict[str, Any]:
    """Fill the metadata the model cannot know: real model id and real UTC timestamp.

    Both are contract-validated fields the model can only guess at. ``extracted_at`` from a
    model is a hallucinated clock reading, and ``model`` would name whatever the model believes
    it is called. Neither guess should reach a delivered artifact.
    """
    if not isinstance(material, dict):
        return material
    material["model"] = provider.MODEL_ID
    material["extracted_at"] = datetime.now(timezone.utc).isoformat()
    parts = material.get("listening_material_parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], dict):
        if not str(parts[0].get("scenario") or "").strip():
            parts[0]["scenario"] = scenario_text
    return material


def build_generate_message(scenario: Any, attempt: int, feedback: Optional[list] = None) -> str:
    """Assemble the user message for one generation attempt.

    ``feedback`` carries the previous attempt's validator errors. Telling the model what the
    authoritative validator rejected is far more effective than re-stating the specification it
    already has, and it is not a decision the model makes -- the orchestrator has already
    decided to retry.
    """
    sections = [
        "Generate one IELTS Listening Part 1 material for the scenario below.",
        "## Scenario\n\nid: %s\ncategory: %s\ntitle: %s\n\n%s"
        % (scenario.id, scenario.category, scenario.title_zh, scenario.prompt_hint),
        (
            "## Output\n\n"
            "Return ONE JSON object with exactly two top-level keys:\n"
            '  "material"  -> conforms to material.schema.json\n'
            '  "blueprint" -> conforms to blueprint.schema.json\n\n'
            "No Markdown fences, no commentary before or after the JSON.\n"
            "Set every turn_index to the index of the turn in material.listening_material_parts[0]"
            ".script.turns whose text literally contains that item's evidence string.\n"
            "The evidence string must be an exact substring of that turn's text."
        ),
    ]
    if feedback:
        sections.append(
            "## Deterministic validation failures from earlier attempts\n\n"
            "Fix every point below and keep every one fixed. These come from the authoritative "
            "validator, not a suggestion, and the list is cumulative: an earlier attempt "
            "already satisfied some of these, so re-check all of them.\n"
            + "\n".join("- %s" % item for item in feedback)
        )
    return "\n\n".join(sections)


async def generate(
    scenario: Any, attempt: int = 0, feedback: Optional[list] = None
) -> GenOutput:
    """Produce one material + blueprint. Raises ModelCallError on unusable output."""
    model = provider.build_model(
        max_output_tokens=GENERATE_MAX_TOKENS, reasoning_effort=GENERATE_EFFORT
    )
    payload = await call_json(
        model, generate_system_prompt(), build_generate_message(scenario, attempt, feedback)
    )
    material, blueprint = payload.get("material"), payload.get("blueprint")
    if not isinstance(material, dict) or not isinstance(blueprint, dict):
        raise ModelCallError(
            "generation response lacked material/blueprint objects; keys=%s"
            % sorted(payload.keys())[:8]
        )
    return GenOutput(_stamp(material, scenario.prompt_hint), blueprint)


def dump(output: GenOutput) -> str:  # pragma: no cover - debugging aid
    return json.dumps(
        {"material": output.material, "blueprint": output.blueprint},
        ensure_ascii=False,
        indent=2,
    )
