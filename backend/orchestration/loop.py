"""The deterministic Loop for one material (design.md §3) -- the core of this task.

Every branch in the pipeline is a Python ``if`` in this file. The model is never asked whether
to iterate again, whether validation passed, or which version to deliver. It cannot be: the
scripts are not registered as tools, so it has no way to observe or influence those decisions.

The cost is real -- no model-driven self-correction, and each step re-sends the specification --
and it is accepted deliberately. The value of a material carrying a score depends entirely on
that score being produced by a process that is reproducible and unit-testable. A model that can
decide it has passed will eventually decide it has passed.

    generate -> validate -> [errors: regenerate, at most 2 retries; then deliver anyway]
      -> metrics -> blind audit -> cross-check (pure Python)
      -> revise -> anchor repair -> validate
      -> blind re-audit (memoryless) -> pick_better -> deliver

Validation is a REPORT, not a gate. The retries stay -- a retry that fixes the material is worth
two minutes -- but the final give-up delivers the last attempt instead of discarding it. The
product owner's diagnosis, and it was correct: "模型正常返回了内容，是校验规则把它判死了 --
这不是'生成异常'". A validator that rejects a script the model wrote is making a claim about that
script, and the claim is sometimes wrong (five rules were measured rejecting real exam papers).
Withholding the material makes the validator's mistake unappealable and unseen; delivering it with
the findings attached makes it a note a question-writer can weigh.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..deterministic.anchors import repair_anchors
from ..deterministic.crosscheck import crosscheck
from ..deterministic.metrics import run_metrics_remote
from ..deterministic.runner import ScriptError
from ..deterministic.validate import validate
from ..steps import agent_steps
from .revision_plan import build_revise_instruction, compliance_severities
from ..steps.call import ModelCallError

__all__ = [
    "MaterialResult",
    "VERDICT_RANK",
    "Candidate",
    "is_assessable",
    "is_clean",
    "pick_better",
    "route_for",
    "run_one",
]

# Generation attempts: 1 initial + 2 retries (prd.md R5, "validate errors -> regenerate").
MAX_GENERATION_ATTEMPTS = 3
# Infrastructure retries, counted separately. Throttling or a truncated response says nothing
# about material quality; letting it eat a generation attempt would condemn a salvageable
# material because the transport hiccupped.
MAX_INFRA_RETRIES = 3
INFRA_BACKOFF_BASE = 2.0

VERDICT_RANK = {
    "FAIL": 0,
    "NOT_ASSESSABLE": 0,
    "PASS_WITH_MINOR_EDITS": 1,
    "PASS": 2,
}


class Candidate(object):
    """One complete, internally consistent version of a material.

    material / blueprint / audit / cross_check travel together. design.md §6 forbids delivering
    a revised script beside the original's score, and the only way to guarantee that is to make
    the four inseparable rather than to remember to keep them aligned.
    """

    __slots__ = ("gen", "audit", "cross_check", "label", "validation")

    def __init__(
        self,
        gen: Any,
        audit: Dict[str, Any],
        cross_check: Any,
        label: str,
        validation: Any = None,
    ) -> None:
        self.gen = gen
        self.audit = audit
        self.cross_check = cross_check
        self.label = label
        self.validation = validation

    @property
    def verdict(self) -> str:
        """The audit verdict, lowered when the compliance review contradicts it.

        The auditor derives its own verdict from ``findings`` alone -- its rubric says so, and its
        SKILL.md tells it to keep the C1-C6 review in a separate block. So an audit reporting a
        critical register breach in ``compliance_review`` and nothing in ``findings`` arrives as
        ``PASS``. Taking that at face value delivered such a material as clean, at its full score.

        Applied here rather than asking the auditor to combine them: this is the same ranking
        judgement as ``pick_better``, and it has to be reproducible in a unit test.
        """
        stated = str(self.audit.get("verdict", "NOT_ASSESSABLE"))
        if stated == "NOT_ASSESSABLE":
            return stated
        severities = compliance_severities(self.audit)
        if "critical" in severities or "major" in severities:
            return "FAIL"
        if severities and VERDICT_RANK.get(stated, 0) > VERDICT_RANK["PASS_WITH_MINOR_EDITS"]:
            return "PASS_WITH_MINOR_EDITS"
        return stated

    @property
    def score(self) -> int:
        """The audit score, capped by severity the way the rubric requires.

        The rubric asks the auditor to apply these caps itself (critical -> at most 49, major -> at
        most 69), and for findings it does. It has no reason to apply them for compliance items,
        since its own verdict does not account for them either. So the cap is enforced here, over
        whatever number arrived.
        """
        score = self.audit.get("score")
        total = score.get("total") if isinstance(score, dict) else None
        if not isinstance(total, int):
            return 0
        severities = compliance_severities(self.audit)
        if "critical" in severities:
            return min(total, 49)
        if "major" in severities:
            return min(total, 69)
        return total

    def key(self) -> Tuple[int, int]:
        """Ranking key: verdict class first, score second.

        Verdict dominates because it reports the presence or absence of hard defects, while the
        score is a continuous editorial judgement. A PASS at 78 is a better thing to ship than a
        PASS_WITH_MINOR_EDITS at 82.
        """
        return (VERDICT_RANK.get(self.verdict, 0), self.score)


class MaterialResult(object):
    """Outcome for one slot: either a delivered material or a recorded failure."""

    __slots__ = ("slot_id", "scenario_id", "ok", "candidate", "selected_version", "route",
                 "reason", "detail", "note", "degraded", "degraded_reason", "timings",
                 "anchor_repairs", "warnings", "validation_findings",
                 # Assigned by batch.py once the material is offered for selection. They are not
                 # constructor arguments because the Loop does not know about S3 or scenario keys
                 # -- it produces a material, and publication is somebody else's decision.
                 "material_id", "scenario_key", "group_key",
                 # How many NOT_ASSESSABLE attempts batch.py discarded before this one. Not a
                 # constructor argument for the same reason as the three above: whether a retry is
                 # affordable is a scheduling question the Loop has no way to answer.
                 "refill_rounds")

    def __init__(
        self,
        slot_id: str,
        scenario_id: str,
        ok: bool,
        candidate: Optional[Candidate] = None,
        selected_version: Optional[str] = None,
        route: Optional[str] = None,
        reason: Optional[str] = None,
        detail: Any = None,
        note: Optional[str] = None,
        degraded: bool = False,
        degraded_reason: Optional[str] = None,
        timings: Optional[Dict[str, float]] = None,
        anchor_repairs: Optional[List[Dict[str, Any]]] = None,
        warnings: Optional[List[str]] = None,
        validation_findings: Optional[List[str]] = None,
    ) -> None:
        if ok and candidate is None:
            # Enforced here rather than discovered during serialisation. A success without a
            # candidate would otherwise crash while building the batch summary, i.e. after five
            # other materials had already been produced. Slot tasks are exception-isolated, so
            # raising at the point of the mistake costs one slot instead of the whole batch.
            raise ValueError("a successful MaterialResult must carry a candidate")
        self.slot_id = slot_id
        self.scenario_id = scenario_id
        self.ok = ok
        self.candidate = candidate
        self.selected_version = selected_version
        self.route = route
        self.reason = reason
        self.detail = detail
        self.note = note
        self.degraded = degraded
        self.degraded_reason = degraded_reason
        self.timings = timings or {}
        self.anchor_repairs = anchor_repairs or []
        self.warnings = warnings or []
        # Structural checks the validator still reports on the DELIVERED material. Separate from
        # `warnings` on purpose: a warning is an advisory band deviation the spec never required,
        # while these were errors the pipeline could not clear in three attempts. Conflating them
        # would either hide these behind copy that says "does not affect use", or make every
        # word-count deviation read as a defect. The reader page renders them as reference notes.
        self.validation_findings = validation_findings or []
        self.material_id: Optional[str] = None
        self.scenario_key: Optional[str] = None
        self.group_key: Optional[str] = None
        self.refill_rounds = 0

    def as_dict(self) -> Dict[str, Any]:
        if not self.ok:
            return {
                "slot_id": self.slot_id,
                "scenario": self.scenario_id,
                "ok": False,
                "reason": self.reason,
                "detail": self.detail,
                "refill_rounds": self.refill_rounds,
                "timings": self.timings,
            }
        candidate = self.candidate
        return {
            "slot_id": self.slot_id,
            "scenario": self.scenario_id,
            "ok": True,
            # The join key for every later call: select, audio status, presigned URLs. Present in
            # the material_completed event so the frontend never has to invent one.
            "material_id": self.material_id,
            "scenario_key": self.scenario_key,
            "group_key": self.group_key,
            "material": candidate.gen.material,
            "blueprint": candidate.gen.blueprint,
            "audit": candidate.audit,
            "cross_check": candidate.cross_check.as_dict(),
            "selected_version": self.selected_version,
            "route": self.route,
            "note": self.note,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            # How many unassessable attempts were discarded before this material. Reported for
            # observability -- the user is not shown it, but a batch quietly spending its budget
            # on refills is something an operator has to be able to see.
            "refill_rounds": self.refill_rounds,
            "anchor_repairs": self.anchor_repairs,
            "warnings": self.warnings,
            # Present but empty on the normal path. Always emitted rather than conditionally added,
            # so the frontend reads one shape and an absent key cannot be mistaken for "clean".
            "validation_findings": self.validation_findings,
            "timings": self.timings,
        }


def is_clean(audit: Dict[str, Any], cross_check: Any, validate_warnings: List[str]) -> bool:
    """Can this version be delivered without a revision pass?

    Requires: no graded finding in either of the auditor's two output blocks, both classes of
    cross-check defect empty, and no advisory items at all. Minor findings and warnings still
    trigger a revision -- the parent task requires the revision step to do real work, and if the
    worst happens the revision is rejected by validation and we fall back at no cost to quality.

    Both blocks, because the auditor is instructed to keep the C1-C6 compliance review out of
    ``findings``. Reading only ``findings`` made a material with a critical register breach come back
    "clean on first pass".
    """
    findings = audit.get("findings") if isinstance(audit, dict) else None
    for finding in findings or []:
        if isinstance(finding, dict) and finding.get("severity") in ("critical", "major", "minor"):
            return False
    if compliance_severities(audit):
        return False
    if getattr(cross_check, "hard_defects", None) or getattr(cross_check, "ambiguous", None):
        return False
    if validate_warnings:
        return False
    audit_warnings = audit.get("warnings") if isinstance(audit, dict) else None
    return not audit_warnings


def pick_better(before: Candidate, after: Candidate) -> Candidate:
    """Choose between the pre- and post-revision versions.

    Ties go to the revision: it has at least absorbed the defect list, so where the measured
    quality is identical the edited script is the better artifact to hand a reviewer.
    """
    return after if after.key() >= before.key() else before


def route_for(candidate: Candidate) -> str:
    """Routing advice for the audio-storage task. This task never writes S3.

    Always ``pending``, whatever the verdict. A FAIL material is returned to the user like any
    other: the frontend states its shortcomings and the user decides whether to use it. Withholding
    it would mean a user who asked for two materials received one, which is the outcome the product
    owner ruled out.

    Degraded materials are likewise unpenalised (design.md §9): a degraded material only skipped
    one optimisation pass, and applying a second standard would mean the same script is treated
    differently for being scheduled last. That is scheduling noise, not a quality signal.
    ``degraded: true`` keeps the reviewer informed.
    """
    return "pending"


def is_assessable(result: "MaterialResult") -> bool:
    """Did this slot produce something a user can actually judge?

    False only for NOT_ASSESSABLE, which means the audit found no usable script: nothing to read,
    nothing to weigh, nothing to listen to. batch.py re-runs such a slot rather than returning a
    blank card.

    Note what this is NOT: a quality gate. FAIL is assessable -- the user is shown the defects and
    chooses. Widening this predicate to cover FAIL would silently reintroduce quarantine, this
    time as an invisible regeneration loop that spends the user's whole time budget hiding
    materials they asked to see.
    """
    if not result.ok or result.candidate is None:
        return False
    verdict = result.candidate.verdict
    # An unrecognised verdict counts as unassessable, matching state_store.verdict_of: an audit
    # nobody can read is not evidence that the material is fine.
    return verdict in VERDICT_RANK and verdict != "NOT_ASSESSABLE"


async def _with_infra_retries(operation: Callable, label: str, emit: Callable) -> Any:
    """Run a model/script call on the infrastructure budget, separate from generation attempts."""
    last: Optional[Exception] = None
    for attempt in range(MAX_INFRA_RETRIES):
        try:
            return await operation()
        except (ModelCallError, ScriptError) as exc:
            last = exc
            if attempt == MAX_INFRA_RETRIES - 1:
                break
            # Jitter so concurrent slots hitting the same throttle do not retry in lockstep.
            delay = INFRA_BACKOFF_BASE ** attempt + random.random()
            await emit("infra_retry", {"step": label, "attempt": attempt + 1,
                                       "error": str(exc)[:200], "retry_in": round(delay, 2)})
            await asyncio.sleep(delay)
    raise last if last else RuntimeError("infra retry loop exited without an error")


async def _noop_emit(stage: str, detail: Optional[Dict[str, Any]] = None) -> None:
    return None


def _build_metrics_runner(scenario_id: str, slot_id: str) -> Any:
    """A remote metrics session for one material.

    A named function rather than an inline construction so tests can substitute a stub: the real one
    opens an AgentCore session, and a unit suite that reached AWS would be neither fast nor reliable.

    Imports are local because ``sandboxed_metrics`` pulls in the tools package, and importing this
    module must stay cheap for the deterministic layer.
    """
    from .. import paths
    from ..sandboxed_metrics import SandboxedMetrics

    return SandboxedMetrics("%s-%s" % (scenario_id, slot_id), paths.metrics_script())


async def run_one(
    scenario: Any,
    slot_id: str = "slot-1",
    emit: Optional[Callable] = None,
    allow_revision: Callable[[], bool] = lambda: True,
    metrics_runner: Optional[Any] = None,
) -> MaterialResult:
    """Run the Loop for one material, releasing the remote metrics session afterwards.

    A wrapper purely so the cleanup is written once. ``_run_one`` has twelve return points -- every
    branch that decides a material is finished is one -- and a session released at each of them is a
    session someone eventually forgets to release, which leaks a remote environment per material
    until the platform's 900s timeout reclaims it.

    When a caller supplies its own runner it also owns its lifetime; that is how a batch can share
    one session across a material's audit and re-audit.
    """
    owns_runner = metrics_runner is None
    if owns_runner:
        metrics_runner = _build_metrics_runner(scenario.id, slot_id)
    try:
        return await _run_one(scenario, slot_id, emit, allow_revision, metrics_runner)
    finally:
        if owns_runner:
            await metrics_runner.close()


async def _run_one(
    scenario: Any,
    slot_id: str,
    emit: Optional[Callable],
    allow_revision: Callable[[], bool],
    metrics_runner: Any,
) -> MaterialResult:
    """Run the full Loop for one material.

    ``allow_revision`` is the time-budget hook. When the batch is close to the platform's
    15-minute hard limit it returns False and the Loop delivers the first audited version
    instead of spending two more model calls. That is a deliberate degradation: an honest
    material carrying its defect list beats the whole batch being cut off by a 504.

    ``metrics_runner`` runs the audit metrics script somewhere the generator's plan does not exist,
    and is always supplied by ``run_one`` above. The audit and the re-audit share one session:
    creating it costs 2.5s, and paying that twice per material is waste.
    """
    emit = emit or _noop_emit
    timings: Dict[str, float] = {}
    started = time.monotonic()



    def mark(stage: str, since: float) -> None:
        timings[stage] = round(time.monotonic() - since, 2)

    gen = None
    gen_warnings: List[str] = []
    last_errors: List[str] = []
    # The last attempt's output, kept whether or not it validated. This is the difference between
    # "3 attempts produced nothing" and "3 attempts produced a material the validator still has
    # notes about": the model returned a full script every time, and the script is what the user
    # asked for. Held separately from `gen` so the clean path is unaffected -- `gen is None` still
    # means "no attempt cleared validation" and still selects the deliver-with-findings branch.
    last_gen = None
    last_warnings: List[str] = []
    # Feedback accumulates across attempts instead of being replaced. Measured on a live 3-slot
    # batch: passing only the latest attempt's errors made all three materials oscillate --
    # attempt 2 fixed the reported error and regressed on a phrase attempt 1 had got right, so
    # three attempts burned ~240s each and produced nothing. Every error seen so far stays in
    # front of the model.
    seen_errors: List[str] = []

    # ---- generation, retried on validation errors only -------------------------------------
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        await emit("generating", {"attempt": attempt + 1})
        step_started = time.monotonic()
        try:
            candidate_gen = await _with_infra_retries(
                lambda: agent_steps.generate(scenario, attempt, seen_errors or None),
                "generate", emit,
            )
        except (ModelCallError, ScriptError) as exc:
            mark("generate", step_started)
            return MaterialResult(slot_id, scenario.id, False, reason="model_error",
                                 detail=str(exc)[:500], timings=timings)
        mark("generate_%d" % (attempt + 1), step_started)

        # Same deterministic anchor repair as the revise path, applied to fresh output too.
        # Measured: a generation attempt failed with six "turn_index N does not carry its
        # evidence (found at turn N+1)" errors -- a uniform off-by-one that the repair resolves
        # with certainty. Spending a 40s model call to re-derive an index we can compute exactly
        # is waste, and the rule is identical: one unique match repairs, anything else does not.
        # A failed repair changes nothing here; the validator still has the final say below.
        gen_repair = repair_anchors(candidate_gen.material, candidate_gen.blueprint)
        if gen_repair.ok and gen_repair.repaired:
            candidate_gen.blueprint = gen_repair.blueprint
            await emit("anchors_repaired", {"count": len(gen_repair.repaired),
                                            "phase": "generate"})

        await emit("validating", {"attempt": attempt + 1})
        step_started = time.monotonic()
        try:
            result = await _with_infra_retries(
                lambda: validate(candidate_gen.material, candidate_gen.blueprint),
                "validate", emit,
            )
        except ScriptError as exc:
            mark("validate", step_started)
            return MaterialResult(slot_id, scenario.id, False, reason="validator_unavailable",
                                 detail=str(exc)[:500], timings=timings)
        mark("validate_%d" % (attempt + 1), step_started)

        # Retained on every attempt, passing or not: the last one is what gets delivered if none
        # of the three clears validation.
        last_gen, last_warnings = candidate_gen, result.warnings

        if result.ok:
            # Warnings do NOT fail. They become advisory input to the revision.
            gen, gen_warnings = candidate_gen, result.warnings
            break
        last_errors = result.errors
        for message in result.errors:
            if message not in seen_errors:
                seen_errors.append(message)
        await emit("regenerating", {"attempt": attempt + 1, "errors": result.errors[:3]})

    # ---- validation is a report, not a gate -------------------------------------------------
    #
    # Three attempts all carrying validator errors used to return ok=False with
    # reason="validation_exhausted", and the frontend rendered an empty 生成异常 card. That was the
    # swallow: the model had returned a complete script every time. The material is delivered now,
    # carrying the findings, and the rest of the Loop runs over it unchanged -- it is still audited,
    # still cross-checked, still revised, so the delivered artifact is measured exactly like any
    # other and the findings are one more thing a question-writer reads.
    #
    # `validation_findings` and not `degraded`: `degraded` already means "delivered without the full
    # pipeline" (the time budget skipped revision), and this material went through the whole
    # pipeline. Reusing the flag would make one boolean answer two different questions, and the copy
    # each needs is different -- "未经修改环节" versus "校验还有几处提示". They can also co-occur.
    validation_findings: List[str] = []
    if gen is None:
        if last_gen is None:
            # Genuinely nothing: every attempt raised before producing output. That is the only
            # remaining no-content case, and it is what `model_error` above already reports.
            return MaterialResult(slot_id, scenario.id, False, reason="no_material_generated",
                                  detail={"errors": last_errors}, timings=timings)
        gen, gen_warnings = last_gen, last_warnings
        validation_findings = list(last_errors)
        await emit("validation_reported", {"findings": last_errors[:3],
                                           "count": len(last_errors)})

    # ---- blind audit: material + metrics only ----------------------------------------------
    await emit("auditing", {})
    step_started = time.monotonic()
    try:
        metrics_a = await _with_infra_retries(
            lambda: run_metrics_remote(gen.material, metrics_runner), "metrics", emit)
        audit_a = await _with_infra_retries(
            lambda: agent_steps.audit_blind(gen.material, metrics_a.audit_metrics()),
            "audit", emit,
        )
    except (ModelCallError, ScriptError) as exc:
        mark("audit", step_started)
        return MaterialResult(slot_id, scenario.id, False, reason="audit_failed",
                              detail=str(exc)[:500], timings=timings)
    mark("audit", step_started)

    # The one place the plan and the audit meet: pure Python, no model, no token cost.
    cross_a = crosscheck(gen.blueprint, audit_a)
    initial = Candidate(gen, audit_a, cross_a, "initial")
    await emit("audited", {"verdict": initial.verdict, "score": initial.score,
                           "cross_check_ok": cross_a.ok})

    if is_clean(audit_a, cross_a, gen_warnings):
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="clean_on_first_pass",
                              timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)

    if not allow_revision():
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="revision_skipped_time_budget",
                              degraded=True, degraded_reason="time_budget",
                              timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)

    # ---- revision ---------------------------------------------------------------------------
    instruction = build_revise_instruction(audit_a, cross_a, gen_warnings)
    await emit("revising", {"must_fix": len(instruction.must_fix),
                            "advisory": len(instruction.advisory)})
    step_started = time.monotonic()
    try:
        revised = await _with_infra_retries(
            lambda: agent_steps.revise(gen.material, gen.blueprint, instruction), "revise", emit
        )
    except (ModelCallError, ScriptError) as exc:
        # A failed revision is never fatal: the initial version is already audited and valid.
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="revise_call_failed",
                              detail=str(exc)[:300], timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)
    mark("revise", step_started)

    # Anchor sync. 1 unique hit repairs the index; 0 or >=2 hits fail the revision outright.
    repair = repair_anchors(revised.material, revised.blueprint)
    if not repair.ok:
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="revise_rejected_anchor_desync",
                              detail={"failures": repair.failures[:5]},
                              timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)
    revised.blueprint = repair.blueprint
    if repair.repaired:
        await emit("anchors_repaired", {"count": len(repair.repaired)})

    step_started = time.monotonic()
    try:
        result_b = await _with_infra_retries(
            lambda: validate(revised.material, revised.blueprint), "validate_revised", emit
        )
    except ScriptError as exc:
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="revise_validation_unavailable",
                              detail=str(exc)[:300], timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)
    mark("validate_revised", step_started)

    if not result_b.ok:
        # Roll back and say why. The revision consumed no quality budget, so this is a clean
        # outcome rather than a failure -- the initial version is still fully audited.
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="revise_rejected_by_validate",
                              detail={"errors": result_b.errors[:5]},
                              timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)

    # ---- re-audit: a brand-new memoryless call ---------------------------------------------
    # It receives neither the first audit's conclusions nor the revision instructions. The map
    # is rebuilt from scratch, which is the only way the comparison stays independent.
    await emit("re_auditing", {})
    step_started = time.monotonic()
    try:
        metrics_b = await _with_infra_retries(
            lambda: run_metrics_remote(revised.material, metrics_runner), "metrics_revised", emit
        )
        audit_b = await _with_infra_retries(
            lambda: agent_steps.audit_blind(revised.material, metrics_b.audit_metrics()),
            "re_audit", emit,
        )
    except (ModelCallError, ScriptError) as exc:
        timings["total"] = round(time.monotonic() - started, 2)
        return MaterialResult(slot_id, scenario.id, True, initial, "initial",
                              route_for(initial), note="re_audit_failed",
                              detail=str(exc)[:300], timings=timings, warnings=gen_warnings,
                              validation_findings=validation_findings)
    mark("re_audit", step_started)

    cross_b = crosscheck(revised.blueprint, audit_b)
    revised_candidate = Candidate(revised, audit_b, cross_b, "revised", result_b)

    best = pick_better(initial, revised_candidate)
    timings["total"] = round(time.monotonic() - started, 2)
    return MaterialResult(
        slot_id, scenario.id, True, best, best.label, route_for(best),
        note="selected_%s" % best.label,
        timings=timings,
        anchor_repairs=repair.repaired if best.label == "revised" else [],
        warnings=result_b.warnings if best.label == "revised" else gen_warnings,
        # The findings belong to the material actually delivered. Reaching here means the revision
        # passed validation (`result_b.ok`), so if the revision is the one shipped its findings are
        # empty -- the rewrite fixed what three generations could not, which is exactly the value of
        # keeping the retries. If the initial version still wins on audit score, it is the same
        # script the validator reported on and it keeps those findings.
        validation_findings=[] if best.label == "revised" else validation_findings,
    )
