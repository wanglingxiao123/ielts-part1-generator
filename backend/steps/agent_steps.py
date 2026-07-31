"""The three model steps, each one call to a pre-defined agent.

Replaces ``generate.py`` / ``audit.py`` / ``revise.py``, which between them assembled prompts from
skill files, named which skill to use, and told the model exactly what to produce. The agents do
that themselves now: they pick a skill from their pool, read the references it names, run the scripts
it names, and fix what those scripts report.

What is left here is the part Python still owns:

* **the envelope.** An agent's reply is text; somebody has to insist it contains the two artifacts
  and raise when it does not. That is ``ModelCallError``, which the Loop retries on the
  infrastructure budget.
* **the information flow.** ``audit`` builds the auditor's message, and the blueprint is not in it.
  This module is where that boundary is visible in one place.
* **the fields the model cannot know.** Real model id, real UTC timestamp. A model's
  ``extracted_at`` is a hallucinated clock reading.

No branching. Whether output is acceptable, whether to retry, and which version ships are decided
in ``orchestration/loop.py``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..agents import build_audit_agent, build_generate_agent
from ..deterministic.guards import assert_blind, assert_no_plan_on_disk
from ..model import provider
from .call import ModelCallError, extract_json

__all__ = [
    "BlindAuditInput",
    "GenOutput",
    "audit_blind",
    "build_audit_message",
    "build_audit_payload",
    "build_revise_message",
    "generate",
    "purge_plan_scratch",
    "revise",
]


class GenOutput(object):
    """A material/blueprint pair from one generation or revision.

    One object so the two cannot be separated by accident: a delivered material and its blueprint
    must come from the same model call, or the annotation describes a script that no longer exists.
    """

    __slots__ = ("material", "blueprint")

    def __init__(self, material: Dict[str, Any], blueprint: Dict[str, Any]) -> None:
        self.material = material
        self.blueprint = blueprint


def _stamp(material: Dict[str, Any], scenario_text: str) -> Dict[str, Any]:
    """Fill the metadata the model cannot know, and must not guess.

    ``model`` would otherwise name whatever the model believes it is called, and ``extracted_at``
    would be a hallucinated clock reading. Both are contract-validated fields, so a guess passes
    validation while being false.
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


def _envelope(reply: str, label: str) -> GenOutput:
    """Pull ``{"material": ..., "blueprint": ...}`` out of an agent's reply.

    Both keys are required. Measured: told to "return the material JSON and the blueprint JSON"
    without a named container, the agent wrote both to files and replied with one of them -- so a
    missing key here usually means the artifact exists in a container that is about to be discarded,
    which is indistinguishable from never having been produced.
    """
    payload = extract_json(reply)
    material, blueprint = payload.get("material"), payload.get("blueprint")
    if not isinstance(material, dict) or not isinstance(blueprint, dict):
        raise ModelCallError(
            "%s reply lacked the material/blueprint envelope; top-level keys=%s"
            % (label, sorted(payload.keys())[:8])
        )
    return GenOutput(material, blueprint)


# Where a generation agent's scratch files may live. Deletion is confined to these roots: a rule
# derived purely from the agent's own commands would follow a path anywhere on the filesystem, and a
# malformed command could then name something that is not scratch at all.
_SCRATCH_ROOTS = (Path("/tmp"), Path("/var/tmp"))

# Paths appearing in a shell command. Deliberately broad on the left (any redirect or bare argument)
# and narrow on the right (a .json file under a scratch root), because a missed file is a leak while
# a spurious match is refused by the root check below.
_PATH_IN_COMMAND = re.compile(r"(/(?:var/)?tmp/[\w.\-/]+\.json)")


def purge_plan_scratch(agent: Any) -> List[str]:
    """Delete the scratch JSON files a generation agent wrote. Returns what was removed.

    **Why this exists, and it is the blindness boundary's third leg.** The skill tells the generator
    to write the material and the blueprint to files so it can run its own validator, and that
    blueprint file holds the answers for *this* material -- which turn carries which target, what the
    self-correction replaces. Measured: the agent writes them to ``/tmp/material.json`` and
    ``/tmp/blueprint.json``.

    ``strands_tools.file_read`` resolves paths against the process working directory and never
    consults ``agent.sandbox`` -- measured, its signature carries no ``agent`` at all. So the audit
    agent, running in the same container moments later, could read that file by absolute path and the
    isolation would be over. Not because anything was passed to it: because it was left lying around.

    The two other legs stop different things and neither stops this one. The audit pool holding no
    plan schema is about *shapes*; giving the auditor no ``shell`` is about *commands*. A plan file at
    a fixed, guessable path defeats both.

    Paths come from the agent's own commands rather than from a glob, so this deletes what this
    generation produced and not a concurrent slot's files. Restricted to scratch roots regardless: an
    agent that wrote ``> /etc/hosts`` should not have that honoured here.
    """
    removed: List[str] = []
    for message in getattr(agent, "messages", None) or []:
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            use = block.get("toolUse") if isinstance(block, dict) else None
            if not use or use.get("name") != "shell":
                continue
            command = use.get("input", {}).get("command")
            # `shell` accepts a string, a list of strings, or a list of dicts.
            texts: List[str] = []
            for item in command if isinstance(command, list) else [command]:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("command"), str):
                    texts.append(item["command"])
            for text in texts:
                for candidate in _PATH_IN_COMMAND.findall(text):
                    path = Path(candidate)
                    if not any(root in path.parents for root in _SCRATCH_ROOTS):
                        continue
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        # A file we cannot remove is worth reporting, not worth failing the
                        # generation over -- the material is already produced and valid.
                        continue
                    removed.append(str(path))
    return removed


def _feedback_block(feedback: Optional[List[str]]) -> str:
    """Validator errors from earlier attempts, cumulative.

    Cumulative rather than latest-only, and this was measured on a live 3-slot batch: passing only
    the newest attempt's errors made all three materials oscillate -- attempt 2 fixed the reported
    error and regressed on something attempt 1 had right, so three attempts burned ~240s each and
    produced nothing.
    """
    if not feedback:
        return ""
    return (
        "\n\n## Deterministic validation failures from earlier attempts\n\n"
        "Fix every point below and keep every one fixed. These come from the authoritative "
        "validator, not a suggestion, and the list is cumulative: an earlier attempt already "
        "satisfied some of these, so re-check all of them.\n"
        + "\n".join("- %s" % item for item in feedback)
    )


async def generate(
    scenario: Any, attempt: int = 0, feedback: Optional[List[str]] = None
) -> GenOutput:
    """One generation. The agent chooses its skill and runs its own validator.

    The request names the scenario and nothing about how to write it -- no schema, no word count, no
    turn structure. Those live in the skill, which is what lets a second subject work without
    touching this function.
    """
    agent = build_generate_agent()
    message = (
        "Generate one listening material for the scenario below.\n\n"
        "## Scenario\n\nid: %s\ncategory: %s\ntitle: %s\n\n%s"
        % (scenario.id, scenario.category, scenario.title_zh, scenario.prompt_hint)
        + _feedback_block(feedback)
    )
    try:
        reply = str(await agent.invoke_async(message))
    finally:
        # In `finally` because a failed generation leaves scratch files too, and the next step after a
        # failure is a retry or an audit either way.
        purge_plan_scratch(agent)
    output = _envelope(reply, "generation")
    return GenOutput(_stamp(output.material, scenario.prompt_hint), output.blueprint)


class BlindAuditInput(object):
    """Everything the auditor is allowed to see. Immutable, and exactly two fields.

    The permitted input is named here, in one visible place, instead of being implied by a call site.
    Adding a field to this class is the one change that could break the isolation, which is precisely
    why it is a class rather than two loose arguments: extending an argument list is a small edit
    that reads as harmless, while adding a slot to a type called ``BlindAuditInput`` does not.

    Frozen so nothing can attach the plan after construction either.
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
    """Serialise the permitted input into the auditor's message.

    **The blindness boundary, and it is an omission rather than a filter.** The only data reaching
    this function is a ``BlindAuditInput``, which has two fields; there is nothing to strip, because
    there is nothing to strip out.

    ``metrics`` comes from the sandboxed run and carries counts only. Handing them over stops the
    auditor recounting, and a metric asserted without calculation is the one most likely to be wrong.

    Instructions are minimal on purpose: the rubric lives in the skill the agent activates, and
    restating it here would create a second source of truth that drifts from the file reviewers edit.
    """
    return "\n\n".join([
        "Audit the listening material below.",
        "## material.json\n\n%s" % json.dumps(data.material, ensure_ascii=False, indent=2),
        "## Deterministic metrics (already calculated; do not recount)\n\n%s"
        % json.dumps(data.metrics, ensure_ascii=False, indent=2),
    ])


def build_audit_message(material: Dict[str, Any], metrics: Dict[str, Any]) -> str:
    """Convenience wrapper over :class:`BlindAuditInput` + :func:`build_audit_payload`."""
    return build_audit_payload(BlindAuditInput(material=material, metrics=metrics))


async def audit_blind(material: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Audit a material. Two parameters, and there will never be a third.

    A fresh agent per call, which is what makes the re-audit memoryless: it cannot inherit the first
    audit's conclusions or the revision instructions, because there is no shared session to inherit
    them from.
    """
    payload = build_audit_payload(BlindAuditInput(material=material, metrics=metrics))
    assert_blind(payload)
    # The wire is clean; the disk has to be too. `file_read` resolves against the process working
    # directory and consults no sandbox, so a plan file the generator left behind is readable by the
    # agent built on the next line.
    assert_no_plan_on_disk()
    agent = build_audit_agent()
    audit = extract_json(str(await agent.invoke_async(payload)))
    if "verdict" not in audit:
        raise ModelCallError(
            "audit reply carried no verdict; keys=%s" % sorted(audit.keys())[:8]
        )
    return audit


async def revise(
    material: Dict[str, Any], blueprint: Dict[str, Any], instruction: Any
) -> GenOutput:
    """One revision: a complete replacement, not a patch.

    A patch would need the orchestrator to reconcile edited text against turn anchors by diffing,
    and anchor drift under diffing is not controllable. Re-emitting both artifacts keeps the anchors
    the agent's own responsibility, and ``deterministic/anchors.py`` then verifies rather than trusts
    the result.
    """
    agent = build_generate_agent()
    try:
        reply = str(await agent.invoke_async(
            build_revise_message(material, blueprint, instruction)))
    finally:
        purge_plan_scratch(agent)
    output = _envelope(reply, "revision")

    parts = material.get("listening_material_parts")
    scenario_text = ""
    if isinstance(parts, list) and parts and isinstance(parts[0], dict):
        scenario_text = str(parts[0].get("scenario") or "")
    return GenOutput(_stamp(output.material, scenario_text), output.blueprint)


def build_revise_message(
    material: Dict[str, Any], blueprint: Dict[str, Any], instruction: Any
) -> str:
    """The revision request.

    A named function rather than inline in ``revise`` so the must-fix / advisory split is testable
    without a model call. That split is load-bearing -- an advisory item presented as an obligation
    provokes rewrites of compliant scripts -- and a property worth having is a property worth
    asserting.
    """
    return "\n\n".join([
        "Revise the listening material below against the defect list. Return the COMPLETE revised "
        "material and blueprint, not a patch or a diff.",
        "## Current material.json\n\n%s" % json.dumps(material, ensure_ascii=False, indent=2),
        "## Current blueprint.json\n\n%s" % json.dumps(blueprint, ensure_ascii=False, indent=2),
        "## Must fix\n\n" + ("\n".join("- %s" % item for item in instruction.must_fix)
                             if instruction.must_fix else "- (none)"),
        "## Advisory only — do NOT rewrite compliant content to satisfy these\n\n"
        "These are observed-typical deviations and minor notes. The script already satisfies the "
        "hard limits. Address them only where it costs nothing.\n\n"
        + ("\n".join("- %s" % item for item in instruction.advisory)
           if instruction.advisory else "- (none)"),
        "Make the smallest change that resolves every must-fix item.\n"
        "CRITICAL: re-check every blueprint item's turn_index against the REVISED turns array. "
        "Editing the script shifts turn positions, and a stale anchor puts a reviewer's annotation "
        "beside the wrong sentence.",
    ])
