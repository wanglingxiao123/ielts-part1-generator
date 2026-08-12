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

from ..agents import (
    build_audit_agent,
    build_feasibility_agent,
    build_generate_agent,
    build_question_audit_agent,
)
from ..deterministic.guards import (
    assert_answer_blind,
    assert_blind,
    assert_carries_plan,
    assert_no_answers_on_disk,
    assert_no_plan_on_disk,
)
from ..deterministic.question_crosscheck import quote_anchor_errors, review_consistency
from ..model import provider
from .call import ModelCallError, extract_json

__all__ = [
    "BlindAuditInput",
    "BlindQuestionAuditInput",
    "FeasibilityInput",
    "GenOutput",
    "QUESTION_COUNT",
    "audit_blind",
    "audit_questions_blind",
    "build_audit_message",
    "build_audit_payload",
    "build_feasibility_message",
    "build_feasibility_payload",
    "build_question_audit_message",
    "build_question_audit_payload",
    "build_revise_message",
    "build_revise_questions_message",
    "classify_question_revision",
    "feasibility_audit",
    "generate",
    "generate_questions",
    "GenerationWorkspace",
    "replan_blueprint",
    "revise",
    "revise_questions",
    "revise_questions_from_comments",
]

# Part 1 is ten items. Named here because the question envelope enforces it, and duplicated from
# `guards.FEASIBILITY_ITEM_COUNT` rather than imported from it on purpose: that constant is about the
# plan a feasibility judge must be handed, and one name serving both would make a change to either
# meaning look like a change to both.
QUESTION_COUNT = 10


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


class BlindQuestionAuditInput(object):
    """Everything the question auditor is allowed to see. Immutable, and exactly three fields.

    A separate type from :class:`BlindAuditInput` rather than a third slot on it, and the reason is not
    tidiness. The two audits are blind to *different things*: the material auditor must not see the
    plan, while this one must not see the answers or the evidence they were anchored to -- and a
    question face legitimately carries fields (``response_form``, ``answer_category``,
    ``narrator_window_id``) that are on the material auditor's forbidden list. One type would need one
    guard, and any guard broad enough to pass both payloads is too narrow to catch either leak.

    ``material`` is the COMPLETE script INCLUDING its narrator turns. Withholding narration would look
    like extra caution and would remove the auditor's ability to decide window membership at all
    (SC-019 / AL-017), which is one of the judgements this step exists to obtain.

    ``question_face`` is block A of the question package and nothing else. ``answer_key`` and
    ``evidence`` are the other two blocks, physically separate in the package for exactly this moment.

    Frozen so nothing can attach the key after construction either.
    """

    __slots__ = ("material", "question_face", "question_metrics")

    def __init__(
        self,
        material: Dict[str, Any],
        question_face: Dict[str, Any],
        question_metrics: Dict[str, Any],
    ) -> None:
        object.__setattr__(self, "material", material)
        object.__setattr__(self, "question_face", question_face)
        object.__setattr__(self, "question_metrics", question_metrics)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("BlindQuestionAuditInput is frozen")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("BlindQuestionAuditInput is frozen")


def build_question_audit_payload(data: BlindQuestionAuditInput) -> str:
    """Serialise the permitted input into the question auditor's message.

    **Not built on :func:`build_audit_payload`, deliberately and permanently.** That function's output
    string is pinned by a test because the material audit must stay byte-identical, and its caller
    follows it with ``assert_blind`` -- a guard this payload cannot pass, because a question face
    carries ``response_form`` and ``answer_category`` and both are blueprint-only keys on that side.
    Giving it a question-face parameter would either loosen that guard, reopening the material-audit
    leak it exists for, or leave this payload unguarded. Two builders whose guards reject each other's
    output is the honest arrangement, the same conclusion :func:`build_feasibility_payload` reached
    from the opposite direction.

    Blindness here is an omission rather than a filter, as on the material side: the only data reaching
    this function is a ``BlindQuestionAuditInput``, which has three fields, and the answer is not one
    of them. There is nothing to strip because there is nothing to strip out.

    The instruction names what is withheld, which the material payload does not need to. Its auditor
    reconstructs a map from a script and could not be handed an "answer" even in principle; this one is
    looking at ten gaps that each have a right answer somewhere, and a model that assumes the key was
    lost in transit tends to ask for it or hedge around not having it. Saying that the omission is the
    design turns that into the instruction it is.
    """
    return "\n\n".join([
        "Audit the ten Part 1 questions below against the material below. Rebuild each answer, its "
        "decisive evidence and its same-level rivals yourself: you are not given the answers, the "
        "evidence table or the item plan, and that omission is deliberate rather than an accident of "
        "packaging. If any of them appears anywhere in this request, report the leak and audit "
        "nothing.",
        "## material.json (complete script, narration included)\n\n%s"
        % json.dumps(data.material, ensure_ascii=False, indent=2),
        "## question_face.json (everything the candidate sees)\n\n%s"
        % json.dumps(data.question_face, ensure_ascii=False, indent=2),
        "## Deterministic question metrics (already calculated; do not recount)\n\n%s"
        % json.dumps(data.question_metrics, ensure_ascii=False, indent=2),
    ])


def build_question_audit_message(
    material: Dict[str, Any],
    question_face: Dict[str, Any],
    question_metrics: Dict[str, Any],
) -> str:
    """Convenience wrapper over the frozen input plus its payload builder."""
    return build_question_audit_payload(
        BlindQuestionAuditInput(material, question_face, question_metrics))


# The keys the question audit schema requires and whose absence is silently benign, which is the same
# selection principle as `_AUDIT_REQUIRED_KEYS`. A reply without `reconstructed_answers` has no product
# at all yet reads as a review; one without `coverage` cannot be checked for the nine-of-ten case; one
# without `per_question_findings` reads as "no defects"; one without `question_qc_status` leaves the
# caller to infer a verdict it was supposed to be told.
_QUESTION_AUDIT_REQUIRED_KEYS = (
    "reconstructed_answers",
    "per_question_findings",
    "coverage",
    "question_qc_status",
)


def _question_audit_envelope(reply: str, label: str,
                             material: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Parse and shape-check a question audit reply.

    Same decoy problem as ``_audit_envelope``: ``extract_json`` returns the first balanced object, so a
    reply that opens with a summary object delivers that instead of the review. Requiring the four
    load-bearing keys makes a decoy fail to qualify and puts the call back on the retry budget.

    ``reconstructed_answers`` is additionally checked for being a non-empty list, which the other
    envelopes have no equivalent of. It is the one field that is *worthless when well-formed but
    empty*: ``[]`` passes any key check, satisfies the schema's array type, and leaves the deterministic
    cross-check with nothing to compare -- which it then reports as agreement.

    Then two things the schema cannot check, both of which produce a review that *reads clean*:

    **Exactly Q1-Q10, and the two lists must agree.** The schema permits a partial review with a
    stated reason, deliberately: an auditor that could only settle nine items should say so rather than
    invent the tenth. This caller's requirement is stricter -- ten items were sent, so nine is a failed
    call to retry, not a result to deliver -- and the schema is the wrong place to encode it, because
    tightening it there would push an honest auditor into fabrication. Worse than either: nine rebuilt
    answers under a ``reviewed_question_ids`` of ten satisfies every rule in the schema, and the item
    with no rebuilt answer is then indistinguishable from an item with nothing wrong with it. Only a
    comparison of the two lists catches that, and only a caller can make it.

    **The counts and the status must follow from the findings.** ``review_consistency`` recomputes them
    from the open findings using the algorithm in the audit skill's own rules file, so this checks the
    auditor against its stated instructions rather than against a second standard invented here. The
    failure it catches is the one with no other symptom: two MAJOR findings above a ``counts`` block of
    zeros and a ``question_qc_status`` of ``PASS``. Every field is well-typed, the schema is satisfied,
    and the orchestrator routes on the status -- so it would ship the material.

    **And each quote must sit in the turn its own row names** (AL-007), when ``material`` is supplied.
    The writer's side of this has been strict since the beginning; the auditor's side was enforced
    nowhere, so a mistyped index travelled all the way to the delivery gate disguised as a semantic
    question -- "is the neighbouring turn the confirmation of the same fact?" -- about what was really
    one integer written down wrong. Checked here because here it is a cheap retry, and because a fresh
    agent quoting the script correctly is exactly what fixes it.

    ``material`` is optional in the signature so the shape checks above remain reachable without a
    script, and every production caller passes it. Without it the quote check simply does not run, which
    is honest: it is not the same as running and finding nothing.

    All of these raise ``ModelCallError`` rather than returning a flag, which puts the call back on the
    infrastructure retry budget with a fresh agent. That is the right remedy: an arithmetic slip in a
    long structured reply is exactly the kind of thing a second attempt does not repeat.
    """
    audit = extract_json(reply)
    missing = [key for key in _QUESTION_AUDIT_REQUIRED_KEYS if key not in audit]
    if missing:
        raise ModelCallError(
            "%s reply is missing required keys %s; keys present=%s"
            % (label, missing, sorted(audit.keys())[:10])
        )
    rebuilt = audit.get("reconstructed_answers")
    if not isinstance(rebuilt, list) or not rebuilt:
        raise ModelCallError(
            "%s reply carries no reconstructed answers (%r); an empty reconstruction leaves the "
            "cross-check nothing to compare and reads as agreement"
            % (label, type(rebuilt).__name__)
        )
    consistency = review_consistency(audit)
    if not consistency["ok"]:
        raise ModelCallError(
            "%s reply disagrees with itself: %s"
            % (label, "; ".join(consistency["errors"]))
        )
    if material is not None:
        drifted = quote_anchor_errors(audit, material)
        if drifted:
            raise ModelCallError(
                "%s reply anchors quotes on turns they are not in: %s"
                % (label, "; ".join(drifted))
            )
    return audit


async def audit_questions_blind(
    material: Dict[str, Any],
    question_face: Dict[str, Any],
    question_metrics: Dict[str, Any],
) -> Dict[str, Any]:
    """Audit ten questions. Three parameters, and there will never be a fourth.

    A fresh agent per call, like the other three steps, so a re-audit cannot inherit the first
    review's conclusions.

    ``assert_no_plan_on_disk`` is called here as well as on the material side, and it is doing more
    work at this point in the run: the generator has just written this material's questions, so a
    scratch file left behind holds this set's actual answers rather than an earlier material's plan.
    ``file_read`` resolves against the process working directory and consults no sandbox.

    **Both sweeps, because either one alone misses half the accident.** They look for different text and
    that difference was measured, not assumed: a scratch file holding only ``{"answer_key": [...]}``
    scores *zero* hits against ``BLUEPRINT_JSON_FIELDS``. The full question package is caught by the plan
    sweep only incidentally, because its ``evidence`` rows happen to carry ``narrator_window_id``. So an
    answer key written on its own -- which is exactly what a partial write or a hand-run validator
    leaves -- is invisible to ``assert_no_plan_on_disk``. Calling only one of these reads, at the call
    site, like protection against all of it.

    The two are separate because they must be able to disagree about what they delete.
    """
    payload = build_question_audit_payload(
        BlindQuestionAuditInput(material, question_face, question_metrics))
    assert_answer_blind(payload)
    assert_no_plan_on_disk()
    assert_no_answers_on_disk()
    agent = build_question_audit_agent()
    # `material` reaches the envelope for the quote check only. It is emphatically NOT added to the
    # payload: the auditor's blindness is what makes its reconstruction worth anything, and `payload`
    # has already passed `assert_answer_blind`. This argument is read after the reply comes back.
    return _question_audit_envelope(
        await _invoke(agent, payload, "question audit"), "question audit", material)


# The question package's own required blocks, from question_package.schema.json. Checked in the
# envelope for the same reason the audit keys are: nothing else validates the reply, and each absence
# here is silently benign further down. A package with no `answer_key` is an unmarked test; with no
# `evidence` it is a test nobody can check; with no `question_face` there is nothing to print. All
# three would pass a bare "is it a JSON object" test.
_QUESTION_PACKAGE_REQUIRED_KEYS = ("question_face", "answer_key", "evidence", "material_id")


def _question_envelope(reply: str, label: str) -> Dict[str, Any]:
    """Pull a complete question package out of an agent's reply.

    The decoy problem again, and the question stage is where it is most likely: the skill's workflow
    has the agent write the package to a file and run a validator over it, so its reply naturally
    contains the validator's own ``{"ok": ..., "errors": [...]}`` report. ``extract_json`` returns the
    first balanced object, so requiring the package's blocks is what keeps a validator report from
    being delivered as a question set.

    ``question_face`` is additionally required to hold ten questions. The count is not a schema detail
    at this layer -- it is the difference between a Part 1 paper and a fragment -- and a short set
    passes every structural check while failing silently downstream: the cross-check would compare the
    items that exist, agree on all of them, and report a clean set.
    """
    payload = extract_json(reply)
    missing = [key for key in _QUESTION_PACKAGE_REQUIRED_KEYS if key not in payload]
    if missing:
        raise ModelCallError(
            "%s reply is missing required question-package blocks %s; keys present=%s"
            % (label, missing, sorted(payload.keys())[:10])
        )
    face = payload.get("question_face")
    questions = face.get("questions") if isinstance(face, dict) else None
    if not isinstance(questions, list) or len(questions) != QUESTION_COUNT:
        raise ModelCallError(
            "%s reply carries %s questions, not %d; a short set passes every structural check and "
            "then reads as a clean review of the items that happen to exist"
            % (label, len(questions) if isinstance(questions, list) else "no",
               QUESTION_COUNT)
        )
    return payload


async def generate_questions(material: Dict[str, Any], blueprint: Dict[str, Any]) -> Dict[str, Any]:
    """Write the ten questions for a finalised material. Returns the complete package.

    The generate agent and the generate pool, not a fourth agent: writing questions is a generation
    task over the same plan the script was written from, and it needs ``shell`` for the same reason the
    material generation does -- the skill's workflow ends in running its own validator until it reports
    no errors, which is what keeps deterministic defects out of the model's revision budget.

    A fresh agent per call, so a revision cannot inherit the review it was asked to fix, and a second
    material's questions cannot echo the first's.

    The request names the artifacts and the working directory and nothing about question design. The
    layouts, the word limits and the answer-variety rules live in the skill, which is what lets the
    rules be edited without touching this function.
    """
    agent = build_generate_agent()
    workspace = GenerationWorkspace()
    try:
        material_path = workspace.path / "material.json"
        blueprint_path = workspace.path / "blueprint.json"
        material_path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")
        blueprint_path.write_text(
            json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        message = (
            "Write the ten IELTS Listening Part 1 questions for the finalised material below.\n\n"
            "The material is final and its blueprint's ten information points are fixed. Activate the "
            "skill that covers question writing, follow its workflow completely including running its "
            "validator until it reports no errors, and reply with the single question-package JSON "
            "object that skill specifies.\n\n"
            "material.json:  %s\nblueprint.json: %s"
            % (material_path, blueprint_path)
            + workspace.instructions()
        )
        reply = await _invoke(agent, message, "question generation")
    finally:
        # In `finally` for a reason sharper than the material side's: this tree holds a complete answer
        # key, and the next step is a blind audit by an agent that can read any absolute path.
        workspace.remove()
    return _question_envelope(reply, "question generation")


async def replan_blueprint(
    material: Dict[str, Any],
    current_blueprint: Dict[str, Any],
    comments: List[Dict[str, Any]],
    feedback: Optional[List[str]] = None,
) -> GenOutput:
    """Rebuild all ten information points while treating the material as immutable input."""
    agent = build_generate_agent()
    workspace = GenerationWorkspace()
    try:
        material_path = workspace.path / "material.json"
        blueprint_path = workspace.path / "current-blueprint.json"
        comments_path = workspace.path / "comments.json"
        material_path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")
        blueprint_path.write_text(
            json.dumps(current_blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        comments_path.write_text(
            json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
        message = "\n\n".join([
            "Replan the complete information-point blueprint for this existing IELTS Listening "
            "Part 1 material. The listening material is immutable: do not edit or rewrite it. "
            "Return that complete material unchanged beside the replacement blueprint so Python can "
            "verify the boundary. Use the reviewer comments as requirements, and rebuild all ten points and all group "
            "boundaries rather than patching the current blueprint.",
            "Activate the listening-material generation skill and follow its blueprint rules. "
            "The new blueprint may change targets, answer categories, item_form, form_group, and "
            "group boundaries. It must still contain exactly ten fair points supported by the "
            "unchanged material. Run the material+blueprint validator until it reports no errors.",
            "Each comment may carry replan_scope. For layout_only, preserve every existing item's "
            "target, evidence, and turn_index exactly; change only layout/group fields and any "
            "derived semantic metadata needed to make that unchanged point valid. For retarget, "
            "different information points are allowed. Python enforces this boundary.",
            "## Files\n\nmaterial.json: %s\ncurrent-blueprint.json: %s\ncomments.json: %s"
            % (material_path, blueprint_path, comments_path),
            "Reply with exactly one JSON object shaped as "
            '{"material": <the complete unchanged material>, '
            '"blueprint": <the complete replacement blueprint>}. '
            "Do not include questions, an answer key, or commentary.",
        ]) + workspace.instructions() + _feedback_block(feedback)
        reply = await _invoke(agent, message, "question replanning")
    finally:
        workspace.remove()

    output = _envelope(reply, "question replanning")
    expected = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    returned = json.dumps(
        output.material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if returned != expected:
        raise ModelCallError(
            "question replanning changed the immutable listening material")
    return output


async def revise_questions(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
    instruction: Any,
) -> Dict[str, Any]:
    """One question revision: a complete replacement package, not a patch.

    Same argument as :func:`revise` -- re-emitting the whole package keeps the evidence anchors the
    agent's own responsibility rather than making the orchestrator reconcile edited carriers against
    turn indices by diffing.

    The script is passed in and must come back untouched, which is the one hard constraint this step
    has that no other revision does. It is stated in the prompt as a prohibition rather than left
    implicit, because every plausible fix for the defects this loop finds -- a leaked answer, a second
    defensible answer -- is *easier* to make in the script than in the question. The audio for this
    material may already exist; a script edit does not fix the item, it silently invalidates the
    recording (SR-021).
    """
    agent = build_generate_agent()
    workspace = GenerationWorkspace()
    try:
        material_path = workspace.path / "material.json"
        blueprint_path = workspace.path / "blueprint.json"
        package_path = workspace.path / "questions.json"
        material_path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")
        blueprint_path.write_text(
            json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        package_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        reply = await _invoke(
            agent,
            build_revise_questions_message(
                material_path, blueprint_path, package_path, instruction)
            + workspace.instructions(),
            "question revision")
    finally:
        workspace.remove()
    return _question_envelope(reply, "question revision")


async def classify_question_revision(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
    comments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Classify reviewer comments before any question package is rewritten."""
    agent = build_generate_agent()
    workspace = GenerationWorkspace()
    try:
        material_path = workspace.path / "material.json"
        blueprint_path = workspace.path / "blueprint.json"
        package_path = workspace.path / "questions.json"
        comments_path = workspace.path / "comments.json"
        material_path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")
        blueprint_path.write_text(
            json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        package_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        comments_path.write_text(
            json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
        message = "\n\n".join([
            "Classify the requested changes to this IELTS Listening Part 1 question package. "
            "Do not modify or regenerate any artifact.",
            "## Files\n\nmaterial.json: %s\nblueprint.json: %s\nquestions.json: %s\ncomments.json: %s"
            % (material_path, blueprint_path, package_path, comments_path),
            "Classify every comment independently with exactly one outcome:\n"
            "- question_only: all comments can be solved by editing the question face, answer key, "
            "accepted variants, word limit, evidence anchor, or related question metadata, without "
            "changing the material, the ten blueprint information points, item_form, form_group, or "
            "group boundaries.\n"
            "- no_change: this comment is unfounded because the existing package is already correct.\n"
            "- replan_questions: at least one comment requires choosing a different information "
            "point or changing item_form, form_group, or group boundaries, but not the material.\n"
            "- revise_material: at least one comment requires changing the listening material.\n\n"
            "For every replan_questions result also return replan_scope. Use layout_only when the "
            "reviewer requests only item_form, form_group, group-boundary, or printed-layout changes "
            "and does not request different information points. Use retarget when satisfying the "
            "comment requires replacing any target answer or evidence point. For every other outcome "
            "use replan_scope none.\n\n"
            "Return exactly: "
            '{"reasons":[{"comment_id":"...",'
            '"outcome":"question_only|no_change|replan_questions|revise_material",'
            '"replan_scope":"none|layout_only|retarget",'
            '"reason":"specific explanation"}]}. '
            "Runtime derives question_number from question-anchored comments; include "
            "question_number only for comments without a question anchor. "
            "Do not provide evidence references: Runtime derives those from the stored package. "
            "Include one reason for every input comment.",
        ]) + workspace.instructions()
        reply = await _invoke(agent, message, "reviewer-comment question classification")
    finally:
        workspace.remove()

    payload = extract_json(reply)
    forbidden = [
        key for key in (
            "material", "blueprint", "script", "listening_material_parts", "package",
        )
        if key in payload
    ]
    if forbidden:
        raise ModelCallError(
            "reviewer-comment classification returned artifact(s): %s"
            % ", ".join(forbidden))
    allowed_outcomes = {"question_only", "no_change", "replan_questions", "revise_material"}
    legacy_outcome = payload.get("outcome")
    if legacy_outcome is not None and legacy_outcome not in allowed_outcomes:
        raise ModelCallError(
            "reviewer-comment classification returned unknown outcome %r" % legacy_outcome)
    reasons = payload.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        raise ModelCallError("reviewer-comment classification returned no reasons")
    comment_ids = {str(row.get("id") or "") for row in comments if isinstance(row, dict)}
    anchored_numbers = {
        str(row.get("id") or ""): (row.get("anchor") or {}).get("index")
        for row in comments
        if isinstance(row, dict)
        and isinstance(row.get("anchor"), dict)
        and (row.get("anchor") or {}).get("type") == "question"
    }
    cleaned = []
    covered = set()
    for reason in reasons:
        comment_id = str(reason.get("comment_id") or "") if isinstance(reason, dict) else ""
        outcome = reason.get("outcome", legacy_outcome) if isinstance(reason, dict) else None
        replan_scope = (
            reason.get("replan_scope", "none") if isinstance(reason, dict) else None
        )
        model_number = reason.get("question_number") if isinstance(reason, dict) else None
        number = anchored_numbers.get(comment_id, model_number)
        if (not isinstance(reason, dict) or comment_id not in comment_ids
                or comment_id in covered
                or outcome not in allowed_outcomes
                or replan_scope not in {"none", "layout_only", "retarget"}
                or (
                    outcome == "replan_questions"
                    and replan_scope not in {"layout_only", "retarget"}
                )
                or (outcome != "replan_questions" and replan_scope != "none")
                or isinstance(number, bool) or not isinstance(number, int)
                or not 1 <= number <= QUESTION_COUNT
                or not str(reason.get("reason") or "").strip()):
            raise ModelCallError(
                "reviewer-comment classification reason is incomplete or does not match its input")
        covered.add(comment_id)
        cleaned_reason = {
            "comment_id": comment_id,
            "question_number": number,
            "outcome": outcome,
            "reason": str(reason["reason"]).strip(),
        }
        if outcome == "replan_questions":
            cleaned_reason["replan_scope"] = replan_scope
        cleaned.append(cleaned_reason)
    if covered != comment_ids:
        raise ModelCallError("reviewer-comment classification did not cover every comment")
    priority = {
        "no_change": 0,
        "question_only": 1,
        "replan_questions": 2,
        "revise_material": 3,
    }
    route = max((row["outcome"] for row in cleaned), key=priority.__getitem__)
    return {"outcome": route, "reasons": cleaned}


async def revise_questions_from_comments(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    package: Dict[str, Any],
    comments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply comments already classified as question-only."""
    agent = build_generate_agent()
    workspace = GenerationWorkspace()
    try:
        material_path = workspace.path / "material.json"
        blueprint_path = workspace.path / "blueprint.json"
        package_path = workspace.path / "questions.json"
        comments_path = workspace.path / "comments.json"
        for path, value in (
            (material_path, material), (blueprint_path, blueprint),
            (package_path, package), (comments_path, comments),
        ):
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        message = "\n\n".join([
            "Revise the complete ten-question package for these comments. They have already been "
            "classified as question-only. Activate the question-writing skill.",
            "material.json: %s\nblueprint.json: %s\nquestions.json: %s\ncomments.json: %s"
            % (material_path, blueprint_path, package_path, comments_path),
            "The material, all ten blueprint information points, item_form, form_group, and group "
            "boundaries are immutable. Do not retarget any answer. Make the smallest sufficient "
            "change and synchronise every dependent question-face, answer-key, accepted-answer, "
            "word-limit, evidence, and metadata field.",
            "Run the validator until it reports no errors. Return exactly "
            '{"outcome":"revised","package":{COMPLETE QUESTION PACKAGE}}.',
        ]) + workspace.instructions()
        reply = await _invoke(agent, message, "reviewer-comment question revision")
    finally:
        workspace.remove()

    payload = extract_json(reply)
    outcome = payload.get("outcome")
    if outcome != "revised":
        raise ModelCallError("reviewer-comment revision returned unknown outcome %r" % outcome)
    package_out = payload.get("package")
    if not isinstance(package_out, dict):
        raise ModelCallError("reviewer-comment revision returned no complete package")
    nested_forbidden = [
        key for key in ("material", "blueprint", "script", "listening_material_parts")
        if key in package_out
    ]
    if nested_forbidden:
        raise ModelCallError(
            "reviewer-comment package contains immutable artifact(s): %s"
            % ", ".join(nested_forbidden))
    # Reuse the exact package envelope checks without making extract_json parse a nested object.
    missing = [key for key in _QUESTION_PACKAGE_REQUIRED_KEYS if key not in package_out]
    face = package_out.get("question_face")
    questions = face.get("questions") if isinstance(face, dict) else None
    if missing or not isinstance(questions, list) or len(questions) != QUESTION_COUNT:
        raise ModelCallError(
            "reviewer-comment revision returned an incomplete package; missing=%s questions=%s"
            % (missing, len(questions) if isinstance(questions, list) else "none"))
    return {"outcome": outcome, "package": package_out}


def build_revise_questions_message(
    material_path: Any, blueprint_path: Any, package_path: Any, instruction: Any
) -> str:
    """The question revision request.

    A named function rather than inline in :func:`revise_questions`, like ``build_revise_message``, so
    the must-fix / advisory split and the script prohibition are testable without a model call. Both
    are load-bearing and both fail silently: an advisory item presented as an obligation provokes
    rewrites of sound items, and a missing prohibition produces a package that validates perfectly
    against a script the recording no longer matches.

    Paths rather than inlined JSON, unlike the material revision. The three documents together are
    large, the agent has ``shell`` and ``file_read`` and its skill's workflow already works from files,
    and its validator takes paths -- so inlining them would spend the prompt budget on text the agent
    then writes back to disk to use.
    """
    return "\n\n".join([
        "Revise the ten questions below against the defect list. Return the COMPLETE revised "
        "question package JSON -- every block -- not a patch or a diff.",
        "## Files\n\nmaterial.json:  %s\nblueprint.json: %s\nquestions.json: %s"
        % (material_path, blueprint_path, package_path),
        "## The script is FIXED and must not change\n\n"
        "Do not edit `material.json`. Not a word, not a turn, not the narration. The recording for "
        "this script may already exist, so an edit there does not fix a question -- it invalidates the "
        "audio while leaving the defect in place. Every fix below must be made in the question "
        "carrier, the layout, the answer key or the evidence anchors. If a defect looks unfixable "
        "without touching the script, target a different information point from the blueprint "
        "instead.",
        "## Must fix\n\n" + ("\n".join("- %s" % item for item in instruction.must_fix)
                             if instruction.must_fix else "- (none)"),
        "## Advisory only — do NOT rewrite compliant questions to satisfy these\n\n"
        "These are notes and observed-typical deviations. The set already satisfies the hard limits. "
        "Address them only where it costs nothing.\n\n"
        + ("\n".join("- %s" % item for item in instruction.advisory)
           if instruction.advisory else "- (none)"),
        "Make the smallest change that resolves every must-fix item, then run the question validator "
        "again until it reports no errors.\n"
        "CRITICAL: re-check every evidence row's turn_index against the script's turns array. The "
        "script has not moved, so an anchor that was right stays right -- but an item you retarget "
        "needs its anchor, its narrator_window_id and its answer key entry moved with it.",
    ])


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
