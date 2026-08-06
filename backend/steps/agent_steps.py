"""The four model steps, each one call to a pre-defined agent.

Replaces ``generate.py`` / ``audit.py`` / ``revise.py``, which between them assembled prompts from
skill files, named which skill to use, and told the model exactly what to produce. The agents do
that themselves now: they pick a skill from their pool, read the references it names, run the scripts
it names, and fix what those scripts report.

What is left here is the part Python still owns:

* **the envelope.** An agent's reply is text; somebody has to insist it contains the two artifacts
  and raise when it does not. That is ``ModelCallError``, which the Loop retries on the
  infrastructure budget.
* **the information flow.** ``audit`` builds the auditor's message, and the blueprint is not in it;
  ``feasibility_audit`` builds a message that must carry it. This module is where both halves of that
  boundary are visible in one place, which is the point of keeping them adjacent: each one's guard
  reads as the other's mirror image.
* **the fields the model cannot know.** Real model id, real UTC timestamp. A model's
  ``extracted_at`` is a hallucinated clock reading.

No branching. Whether output is acceptable, whether to retry, and which version ships are decided
in ``orchestration/loop.py``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..agents import build_audit_agent, build_feasibility_agent, build_generate_agent
from ..deterministic.guards import assert_blind, assert_carries_plan, assert_no_plan_on_disk
from ..model import provider
from .call import ModelCallError, extract_json

__all__ = [
    "BlindAuditInput",
    "FeasibilityInput",
    "GenOutput",
    "audit_blind",
    "build_audit_message",
    "build_audit_payload",
    "build_feasibility_message",
    "build_feasibility_payload",
    "build_revise_message",
    "feasibility_audit",
    "generate",
    "GenerationWorkspace",
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


# Keys the audit schema requires, checked here because nothing else validates the audit reply and it
# drives both the revision instructions and `pick_better`. Not the full schema: `jsonschema` in the
# request path would reject an otherwise usable audit over a nested detail, and the Loop has no way to
# repair one. These four are the ones whose absence is silently benign -- an audit missing `findings`
# reads as "no defects", and one missing `blind_information_map` defeats the cross-check by having
# nothing to compare, which `crosscheck` reports as clean.
_AUDIT_REQUIRED_KEYS = ("verdict", "score", "findings", "blind_information_map")


def _audit_envelope(reply: str, label: str) -> Dict[str, Any]:
    """Parse and shape-check an audit reply.

    ``extract_json`` scans for the first balanced object, which is the right behaviour for a model
    that wraps its answer in prose -- but it means a reply containing two objects delivers the first.
    Measured: a reply whose first object was ``{"verdict": "PASS", "note": "let me reconsider"}``
    followed by a real ``FAIL`` with critical findings was accepted as a clean PASS, because the only
    check was ``"verdict" in audit``. Requiring the four load-bearing keys makes a decoy object fail
    to qualify, and ``ModelCallError`` puts the call back on the infrastructure budget.
    """
    audit = extract_json(reply)
    missing = [key for key in _AUDIT_REQUIRED_KEYS if key not in audit]
    if missing:
        raise ModelCallError(
            "%s reply is missing required keys %s; keys present=%s"
            % (label, missing, sorted(audit.keys())[:10])
        )
    return audit


class GenerationWorkspace(object):
    """A private scratch directory for one generation, removed when the call returns.

    **Why the directory rather than the two filenames.** The skill used to name ``/tmp/material.json``
    and ``/tmp/blueprint.json``, and every concurrent generation therefore wrote the same two paths.
    Measured on interleaved slots: slot A read back slot B's plan from ``/tmp/blueprint.json`` and
    validated its own script against it; A's purge deleted B's files mid-run; and B's fresh write made
    A's ``assert_no_plan_on_disk`` raise and kill A. One CLI batch with the default concurrency of 3
    produced zero materials from nine generation attempts.

    A per-call directory removes the shared name, and deleting the tree removes the need to reconstruct
    which files were written -- the earlier purge parsed the agent's own shell commands with a regex,
    which missed anything built by interpolation (``D=/tmp; cat > $D/blueprint.json``).

    The plan must not outlive the generation regardless of how it was written, because
    ``strands_tools.file_read`` resolves paths against the process working directory and consults no
    sandbox: the audit agent built moments later can read any absolute path. The other two legs of the
    boundary do not help -- the audit pool holding no plan schema is about *shapes*, and withholding
    ``shell`` is about *commands*.
    """

    __slots__ = ("_dir",)

    def __init__(self) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="ielts-gen-"))

    @property
    def path(self) -> Path:
        return self._dir

    def instructions(self) -> str:
        """The one paragraph telling the agent where to work. Included in every generation request."""
        return (
            "\n\n## Working directory\n\n"
            "Write every scratch file under `%s`, which is yours alone and is deleted when this "
            "request finishes. Use it for the material and blueprint JSON files the skill tells you "
            "to validate. Do not write to `/tmp` directly: other work runs concurrently there, and a "
            "shared filename means reading back someone else's file.\n\n"
            "Write the path out in full in every command. Do not assign it to a shell variable -- "
            "each `shell` call is its own process, and a variable set in one command is not visible "
            "in the next." % self._dir
        )

    def remove(self) -> List[str]:
        """Delete the directory. Returns the files that were in it, for logging."""
        contents: List[str] = []
        try:
            contents = [str(p) for p in sorted(self._dir.rglob("*")) if p.is_file()]
        except OSError:
            pass
        shutil.rmtree(self._dir, ignore_errors=True)
        return contents


async def _invoke(agent: Any, message: str, label: str) -> str:
    """One agent call, with provider exceptions translated to ``ModelCallError``.

    Without this the exception type is whatever the SDK raises, and ``loop.py``'s
    ``_with_infra_retries`` catches only ``ModelCallError``/``ScriptError``. Measured: a throttling
    error escaped ``run_one`` entirely, was caught by ``batch.py`` as ``unhandled_error``, and --
    because that reason is refillable -- burned a whole refill round regenerating a material that had
    nothing wrong with it, instead of waiting two seconds and retrying the one call.

    The wrapper used to live in ``call.py``, which the pipeline no longer uses. Moving the model calls
    to pre-defined agents left the translation behind, and nothing failed loudly enough to notice: a
    throttle still produced a material, just via the most expensive path available.
    """
    try:
        return str(await agent.invoke_async(message))
    except ModelCallError:
        raise
    except Exception as exc:  # noqa: BLE001 - SDK raises provider-specific errors
        raise ModelCallError("%s call failed: %s: %s" % (label, type(exc).__name__, str(exc)[:300]))


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
    workspace = GenerationWorkspace()
    message = (
        "Generate one listening material for the scenario below.\n\n"
        "## Scenario\n\nid: %s\ncategory: %s\ntitle: %s\n\n%s"
        % (scenario.id, scenario.category, scenario.title_zh, scenario.prompt_hint)
        + workspace.instructions()
        + _feedback_block(feedback)
    )
    try:
        reply = await _invoke(agent, message, "generation")
    finally:
        # In `finally` because a failed generation leaves scratch files too, and the next step after a
        # failure is a retry or an audit either way.
        workspace.remove()
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
    return _audit_envelope(await _invoke(agent, payload, "audit"), "audit")


class FeasibilityInput(object):
    """Everything the non-blind feasibility judge is given. Immutable, and exactly three fields.

    Shaped like :class:`BlindAuditInput` on purpose, and it is the same argument read the other way
    round: there the class exists so that *adding* a field is a visible act, here so that *losing*
    one is. ``blueprint`` is the field this whole step depends on, and the failure mode of dropping it
    is silent (see :func:`assert_carries_plan`), so it is named in a type rather than being the second
    of three positional arguments.

    Frozen for the same reason as the blind input: nothing can rearrange the inputs after
    construction, so what the guard checked is what gets sent.
    """

    __slots__ = ("material", "blueprint", "qr027")

    def __init__(
        self, material: Dict[str, Any], blueprint: Dict[str, Any], qr027: Dict[str, Any]
    ) -> None:
        object.__setattr__(self, "material", material)
        object.__setattr__(self, "blueprint", blueprint)
        object.__setattr__(self, "qr027", qr027)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("FeasibilityInput is frozen")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("FeasibilityInput is frozen")


def build_feasibility_payload(data: FeasibilityInput) -> str:
    """Serialise the non-blind input into the judge's message.

    **Not** built on :func:`build_audit_payload`. That function's only caller follows it with
    ``assert_blind``, and its docstring says the blindness there is an omission rather than a filter;
    giving it a blueprint parameter would turn "blind" into an option, which is the shape
    ``guards.assert_blind`` exists to refuse. Two payload builders whose guards contradict each other
    is the honest arrangement.

    The plan is checked before assembly, on the object, because ten items can be counted structurally
    there and only textually here.

    ``qr027`` counts come from the validator and are handed over rather than described, for the same
    reason ``metrics`` are handed to the auditor: a count asserted without calculation is the one most
    likely to be wrong. The judge is told not to recount them; the thresholds are applied by
    ``question_feasibility_preflight``, not by either side of this call.
    """
    assert_carries_plan(data.blueprint)
    return "\n\n".join([
        "Judge whether ten reliable IELTS Listening Part 1 items can be written from the plan below, "
        "for the material below. You are given both deliberately: the question is about these "
        "specific ten information points.",
        "## material.json\n\n%s" % json.dumps(data.material, ensure_ascii=False, indent=2),
        "## blueprint.json\n\n%s" % json.dumps(data.blueprint, ensure_ascii=False, indent=2),
        "## Answer-variety counts (already calculated; do not recount, and do not apply thresholds)"
        "\n\n%s" % json.dumps(data.qr027, ensure_ascii=False, indent=2),
    ])


def build_feasibility_message(
    material: Dict[str, Any], blueprint: Dict[str, Any], qr027: Dict[str, Any]
) -> str:
    """Convenience wrapper over :class:`FeasibilityInput` + :func:`build_feasibility_payload`."""
    return build_feasibility_payload(FeasibilityInput(material, blueprint, qr027))


# The three keys `question_feasibility_preflight.REQUIRED_SEMANTICS` reads, verbatim. A reply missing
# any of them cannot be turned into a verdict, so the preflight would answer SEMANTICS_MISSING -- and
# a retry here is far cheaper than delivering a material nobody can write questions for.
_FEASIBILITY_REQUIRED_KEYS = ("feasible", "reasons", "category_semantics_ok")
_FEASIBILITY_BOOL_KEYS = ("feasible", "category_semantics_ok")
_FEASIBILITY_ALLOWED_KEYS = frozenset(_FEASIBILITY_REQUIRED_KEYS) | {"qr027_exception"}
_QR027_EXCEPTION_KEYS = frozenset({"requested", "justification"})


def _feasibility_envelope(reply: str, label: str) -> Dict[str, Any]:
    """Parse a feasibility reply and check it against the contract, in values as well as keys.

    **Why this checks values and not only keys, given that the preflight checks them too.** The two
    layers are deliberately overlapping, because they stand in different places. Here the call has
    only just returned and the infrastructure retry budget is still intact, so a reply that wrote
    ``"false"`` where ``false`` belonged is very likely correct on the next attempt -- that is this
    call's slip, not a property of the material. ``ModelCallError`` therefore puts it back on the
    budget. ``question_feasibility_preflight._semantics_problem`` stands at the *verdict*, with no
    retries left, and can only report honestly that it cannot decide. Keeping just this layer would
    assume the preflight is never called by anything else (it is a skill script, so it is); keeping
    just that one would upgrade a repairable formatting wobble into "this material cannot be judged".

    The full contract lives in ``schemas/feasibility.schema.json`` and is enforced against positive
    and negative cases by ``skills/shared/tests``. This is its values-and-types subset restated in
    plain Python: ``jsonschema`` is a dev dependency, and importing a new third-party package into
    ``backend/`` fails ci_gates gate 10 (measured) because the container never installs it.

    One intended divergence from the schema, asserted by the tests rather than left to be discovered:
    a whitespace-only ``justification`` passes the schema (``minLength: 1`` counts characters) and
    raises here (``strip()``). Expressing "not blank" in JSON Schema needs a pattern whose semantics
    are harder to keep aligned with Python's, so each layer uses the tool it is good at.
    """
    data = extract_json(reply)

    # 1. The three keys the verdict is built from. `extract_json` returns the first balanced object,
    #    so a reply that opens with a decoy summary object arrives here instead of the real answer --
    #    measured on the audit side, where a `{"verdict": "PASS", "note": ...}` decoy was accepted as
    #    a clean pass. Requiring all three makes a decoy fail to qualify.
    missing = [key for key in _FEASIBILITY_REQUIRED_KEYS if key not in data]
    if missing:
        raise ModelCallError(
            "%s reply is missing required keys %s; keys present=%s"
            % (label, missing, sorted(data.keys())[:10])
        )

    # 2. Both flags must be real booleans. `isinstance(x, bool)` rather than a truth test, because
    #    the string `"false"` is truthy: a `feasible: "false"` read by truthiness becomes a PASS, and
    #    the material ships as feasible while the model said the opposite.
    for key in _FEASIBILITY_BOOL_KEYS:
        if not isinstance(data[key], bool):
            raise ModelCallError(
                "%s reply has %s=%r (%s); it must be a JSON boolean"
                % (label, key, data[key], type(data[key]).__name__)
            )

    # 3. `reasons` is a list of strings. A bare string is iterable, so without the type check a
    #    reply of `"reasons": "item 6 is ambiguous"` would satisfy every check below character by
    #    character.
    reasons = data["reasons"]
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise ModelCallError(
            "%s reply has reasons=%s; it must be a list of strings"
            % (label, type(reasons).__name__ if not isinstance(reasons, list)
               else [type(item).__name__ for item in reasons][:5])
        )

    # 4. A rejection has to be actionable. `feasible: false` costs a full material regeneration, and
    #    a rejection with no stated cause gives the next attempt nothing to avoid. Blank entries
    #    count as no reason: `[""]` and `["   "]` are non-empty lists carrying zero information.
    if not all(data[key] for key in _FEASIBILITY_BOOL_KEYS):
        if not any(item.strip() for item in reasons):
            raise ModelCallError(
                "%s reply rejects the plan (feasible=%r, category_semantics_ok=%r) with no usable "
                "reason: %r" % (label, data["feasible"], data["category_semantics_ok"], reasons[:5])
            )

    # 5. `qr027_exception` is optional as a whole and strict once written -- see below.
    if "qr027_exception" in data:
        _check_qr027_exception(data["qr027_exception"], label)

    # 6. No unknown top-level keys, matching the schema's `additionalProperties: false`. A stray
    #    `"confidence": 0.4` means the model is answering against some other contract, in which case
    #    `feasible` may not mean what this side reads it to mean either.
    unknown = sorted(set(data) - _FEASIBILITY_ALLOWED_KEYS)
    if unknown:
        raise ModelCallError(
            "%s reply carries keys outside the contract: %s" % (label, unknown[:5])
        )
    return data


def _check_qr027_exception(exception: Any, label: str) -> None:
    """The five sub-rules for ``qr027_exception``, each mirroring one schema rule.

    ==  ============================================  ===================================
    a   is an object                                  ``"type": "object"``
    b   ``requested`` present and a real boolean      ``required: ["requested"]`` + type
    c   ``requested: true`` needs a non-blank string  ``if/then`` + ``minLength: 1``
    d   ``requested: false`` needs no justification   not in an unconditional ``required``
    e   no keys beyond the two                        ``additionalProperties: false``
    ==  ============================================  ===================================

    Rule (d) is the one worth spelling out, because getting it wrong is easy in the strict direction:
    an unconditional ``required: ["requested", "justification"]`` rejects the entirely legal
    ``{"requested": false}``, where declining to request an exception leaves nothing to justify.

    Rule (b) exists because ``requested`` is the whole meaning of the object. A
    ``{"justification": "..."}`` reads like a request, while ``preflight._justification_of`` requires
    ``requested is True`` and reads it as no request at all -- so it would arrive as a silent semantic
    downgrade rather than as an error.

    **How this relates to the preflight's reading of the same value, since they differ.** The
    preflight interprets a malformed exception as "no exception requested" (a request that says
    nothing is not a request). This layer calls it a contract violation and retries. They are not in
    conflict: with retries left, the model gets a chance to say what it meant; with none left, the
    conservative reading stands.
    """
    if not isinstance(exception, dict):
        raise ModelCallError(
            "%s reply has qr027_exception=%r (%s); it must be an object"
            % (label, exception, type(exception).__name__)
        )
    requested = exception.get("requested")
    if "requested" not in exception or not isinstance(requested, bool):
        raise ModelCallError(
            "%s reply has a qr027_exception whose `requested` is %s; it must be a JSON boolean, "
            "because it is what makes the object mean anything"
            % (label, "absent" if "requested" not in exception else repr(requested))
        )
    justification = exception.get("justification")
    if requested:
        if not isinstance(justification, str) or not justification.strip():
            raise ModelCallError(
                "%s reply requests a QR-027 exception with justification=%r; an exception granted "
                "without a stated cause is a permanently lowered limit"
                % (label, justification)
            )
    elif "justification" in exception and not isinstance(justification, str):
        # Not required when `requested` is false, but a value of the wrong type still says the model
        # is working from a different contract.
        raise ModelCallError(
            "%s reply has a qr027_exception whose justification is %s, not a string"
            % (label, type(justification).__name__)
        )
    unknown = sorted(set(exception) - _QR027_EXCEPTION_KEYS)
    if unknown:
        raise ModelCallError(
            "%s reply has a qr027_exception carrying keys outside the contract: %s. A misspelled "
            "`justifcation` is caught here rather than becoming a silent no-reason request."
            % (label, unknown[:5])
        )


async def feasibility_audit(
    material: Dict[str, Any], blueprint: Dict[str, Any], qr027: Dict[str, Any]
) -> Dict[str, Any]:
    """Judge question feasibility for a finalised material. Three parameters, all required.

    The mirror of :func:`audit_blind`: that one asserts the plan is absent, this one asserts it is
    present. Neither check is redundant with the other and neither can be written as an option on a
    shared function.

    No ``assert_no_plan_on_disk`` here. That guard protects a *blind* agent from reading the plan off
    the filesystem, and this agent is being handed the plan on the wire. Sweeping scratch files here
    would guard nothing while suggesting to the next reader that this call is blind.

    A fresh agent per call, like the other two, so no material's judgment inherits another's.
    """
    payload = build_feasibility_payload(FeasibilityInput(material, blueprint, qr027))
    agent = build_feasibility_agent()
    return _feasibility_envelope(await _invoke(agent, payload, "feasibility"), "feasibility")


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
    workspace = GenerationWorkspace()
    try:
        reply = await _invoke(
            agent,
            build_revise_message(material, blueprint, instruction) + workspace.instructions(),
            "revision")
    finally:
        workspace.remove()
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
