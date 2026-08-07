"""The deterministic loop for one material's ten questions (the question-stage twin of :mod:`loop`).

    generate -> validate -> blind audit -> cross-check (pure Python)
      -> [ revise -> validate -> blind re-audit (memoryless) -> cross-check ] x up to 2
      -> deliver the first round that clears every gate, or REGENERATE_MATERIAL

Every branch is a Python ``if`` here, for the reason the material loop states and this stage makes
sharper: the model is never asked whether its questions are answerable. It cannot be -- the auditor
that would answer is the one deliberately denied the answer key, and the generator that holds the key
is the one whose work is in question. Neither party can be asked to grade the pair.

**One structural difference from the material loop, and it is the whole point of this stage.** There,
a revision may rewrite the script. Here the script is immovable: it is the recorded artifact, or is
about to be, so every defect must be fixed in the carrier, the layout, the answer key or the evidence
anchors (SR-021). That constraint is stated in the revision prompt and enforced by comparing the
script before and after -- not because the generator is expected to disobey, but because every natural
fix for a leaked answer or a second defensible answer is easier to make in the script than in the
question, and a script edit here does not fail: it silently invalidates audio that already exists.

**This stage does not deliver its best effort, and that is the difference from the material loop.**
There, a material that still carries findings after revision is delivered with them attached, because
withholding it makes the defect unappealable while delivering it makes the finding a note a reviewer
can weigh. A question set has no equivalent reading. Its defects are not editorial opinions -- an
answer available on the printed page, a second equally-supported answer, a rebuilt answer the key
would mark wrong -- and every one of them is a candidate being graded against something the paper does
not support. There is no reviewer downstream who benefits from seeing that in a delivered set; the
grading has already happened by then. So the exits are: clear every gate and deliver, or return
:data:`REGENERATE_MATERIAL` and let the replacement slot draw a different material.

**Up to two targeted revisions, each judged on its own full evidence.** Every round runs the whole
deterministic chain -- validator, blind audit, cross-check -- so a round is never accepted on the
strength of the previous round's review, and a fix that repairs one item while breaking another is
seen. The first round that clears every gate is delivered immediately: continuing would spend a call
to look for defects that have already been shown absent, and would risk replacing a clean set.

**Two is a budget, not a convergence claim.** The ten information points are fixed by the blueprint,
so successive rounds redraw from the same well; a third round is not obviously better than a different
material, and the measured cost of a round is ~100-120s of model time. Where the second round still
leaves a hard defect, the honest answer is that this material's questions are hard to make fair, which
is what :data:`REGENERATE_MATERIAL` says.

**``pick_better_questions`` no longer chooses what to ship.** It survives to keep the best-diagnosed
candidate for the failure report, and nothing more. Ranking says which of two defective sets is less
defective; it has never said either is deliverable, and on the failure path the winner is reachable
only through ``rejected_candidate`` -- never through ``candidate``, which is the attribute a caller
reads to ship.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..deterministic.feasibility import REGENERATE_MATERIAL
from ..deterministic.question_crosscheck import crosscheck_questions
from ..deterministic.question_metrics import question_metrics
from ..deterministic.validate_questions import validate_questions
from ..steps import agent_steps
from .loop import _noop_emit, _with_infra_retries
from .question_revision_plan import build_question_revise_instruction

__all__ = [
    "AT_CEILING_WARNINGS",
    "MAX_QUESTION_REVISIONS",
    "QUESTION_NUMBERS",
    "QUESTIONS_NOT_DELIVERABLE",
    "QuestionCandidate",
    "QuestionResult",
    "SEVERITY_ORDER",
    "ScriptWasEdited",
    "delivery_blockers",
    "is_clean_questions",
    "pick_better_questions",
    "run_questions",
]

# Most severe first. The order IS the ranking, which is why it is a tuple and not a set.
SEVERITY_ORDER = ("CRITICAL", "MAJOR", "MINOR")

# Part 1 is exactly ten items, and the delivery gate requires the audit to have covered all ten.
# Duplicated from the shared cross-check's own ``NUMBERS`` rather than imported, for the reason
# ``QUESTION_COUNT`` in agent_steps states: that module is loaded off a runtime ``sys.path`` and
# importing it here to read one constant would make this module's import order load-bearing.
QUESTION_NUMBERS = tuple(range(1, 11))

# Targeted revision rounds after the initial generation. Two, per the product decision.
#
# Why a constant and not an environment knob: the number is a quality contract, not a tuning
# parameter. Raising it trades model time for the chance that a third redraw from the same fixed
# blueprint finds a fix the first two did not, and lowering it converts sets that the second round
# would have cleared -- measured: the first real case cleared Q1 in round one and needed a second round
# for Q8 -- into regenerated materials. Either change deserves the diff.
MAX_QUESTION_REVISIONS = 2

# Validator warnings that report a legal ceiling was REACHED, not exceeded. They do not block delivery.
#
# Matched on a substring of the validator's own message, which is the weak part of this and is why the
# list is one entry long and the marker is the rule id plus the word "ceiling" rather than a loose
# keyword. The validator emits warnings as free prose with no code, so there is nothing sturdier to
# match on; a rule-id field on validator warnings would replace this, and until then a reworded message
# fails safe -- the warning stops being recognised and starts blocking again, which is the old
# behaviour rather than a silent pass.
#
# Why these must not block: the QR-026 warning is emitted on the `counts["final"] == MAX_FINAL_BLANKS`
# branch, immediately after the `>` branch that is an error. The validator has already ruled the set
# legal, so there is no defect to revise -- and measured on the real material, blocking here spent two
# revision rounds and then discarded a compliant question set.
AT_CEILING_WARNINGS = ("QR-026 ceiling",)


def _is_at_ceiling_warning(warning: Any) -> bool:
    return any(marker in str(warning) for marker in AT_CEILING_WARNINGS)


# The failure reason this loop returns when no round produced a deliverable set. Named here rather than
# spelled at the raise site so a caller can match on it without repeating the string.
#
# It carries the material-stage vocabulary deliberately: the remedy is not another question attempt --
# two have already run against a blueprint that cannot change -- but a different material, which is
# what the replacement slot does with this verdict.
QUESTIONS_NOT_DELIVERABLE = "questions_not_deliverable"


class QuestionCandidate(object):
    """One complete, internally consistent version of a question set.

    package / review / cross_check / validation travel together for the reason the material loop's
    ``Candidate`` states: delivering a revised set beside the original's review is a lie about the
    artifact, and the only way to guarantee it cannot happen is to make them inseparable rather than to
    remember to keep them aligned.
    """

    __slots__ = ("package", "review", "cross_check", "validation", "label")

    def __init__(
        self,
        package: Dict[str, Any],
        review: Dict[str, Any],
        cross_check: Any,
        validation: Any,
        label: str,
    ) -> None:
        self.package = package
        self.review = review
        self.cross_check = cross_check
        self.validation = validation
        self.label = label

    @property
    def status(self) -> str:
        """The auditor's own ``question_qc_status``, recomputed rather than read.

        The stated value is not trusted here and does not need to be: ``review_consistency`` already
        ran inside the audit envelope and raised if the two disagreed, so by this point they agree.
        Reading the computed one keeps this property true even if that check is ever relaxed.
        """
        computed = (self.cross_check.consistency or {}).get("computed") or {}
        status = computed.get("question_qc_status")
        if isinstance(status, str):
            return status
        return str(self.review.get("question_qc_status", "FAIL"))

    @property
    def counts(self) -> Dict[str, int]:
        computed = (self.cross_check.consistency or {}).get("computed") or {}
        counts = computed.get("counts")
        return counts if isinstance(counts, dict) else {}

    def key(self) -> Tuple[int, ...]:
        """Ranking key: severity counts, lexicographically, most severe first. Bigger is better.

        **Why lexicographic rather than a score.** The material side ranks on ``(verdict, score)``,
        where the score is the auditor's continuous editorial judgement. A question review has no
        score, and inventing one -- a weighted sum of severities, say -- would let three MINOR findings
        outvote one MAJOR. They must not: a MAJOR here means a candidate can be marked wrong for
        reading the paper correctly, and no number of stylistic notes trades against that. Comparing
        counts in severity order gives exactly that: fewer CRITICAL wins outright, and MAJOR is
        consulted only when the CRITICAL counts are equal.

        Counts are negated because ``pick_better_questions`` takes the larger key and fewer defects is
        better. Negating rather than reversing the comparison keeps every ``key()`` here reading the
        same direction as the material loop's.

        ``question_qc_status`` is deliberately absent from the key, and its absence costs nothing: the
        status is *derived* from these three counts by the rules file's own algorithm, so ordering by
        the counts reproduces the status ordering exactly (all-zero → PASS, MINOR only → WARNING,
        CRITICAL or MAJOR → FAIL) and additionally breaks ties inside each status band. Including it
        would add a term that can never disagree with the terms after it.

        The cross-check's hard defects are the fourth term, and they belong in the key rather than
        being folded into the counts because they are an independent signal: they come from Python
        comparing the writer's key against the blind reconstruction, not from the auditor's judgement.
        A review that reported nothing while its own rebuilt answers diverged from the key on two items
        would otherwise rank as clean. They rank *after* the graded findings because a divergence is
        usually also reported as a finding -- so the two agree most of the time, and where they do not,
        the auditor's severity grading is the more informative of the two.

        Validator errors are last. They are the most mechanical signal and the most likely to be
        already covered above, but a set that cannot pass its own validator is never the better
        artifact when everything else is equal.
        """
        counts = self.counts
        return tuple(
            [-int(counts.get(name, 0) or 0) for name in SEVERITY_ORDER]
            + [-len(self.cross_check.hard_defects),
               -len(getattr(self.validation, "errors", []) or [])]
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "package": self.package,
            "review": self.review,
            "cross_check": self.cross_check.as_dict(),
            "validation": self.validation.as_dict() if self.validation is not None else None,
            "status": self.status,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "QuestionCandidate(%s, status=%s, key=%s)" % (self.label, self.status, self.key())


def delivery_blockers(candidate: QuestionCandidate) -> List[str]:
    """Every reason this set may not be delivered, as prose. Empty means deliverable.

    **A list rather than a bool, because this is now the gate and not a hint.** Under the old
    always-deliver loop the answer only chose whether to spend a revision call, so "not clean" needed
    no explanation. It now decides between shipping and :data:`REGENERATE_MATERIAL`, and a material
    rejected with no stated reason is a material nobody can argue with -- the reasons go into the
    failure detail, and each one names the check that produced it.

    The four conditions, all required together:

    * **the validator reports no hard error.** Warnings do not clear the gate either; see below.
    * **the audit covered exactly Q1-Q10.** Read from the cross-check's own recompute, not from the
      review's claim about itself.
    * **the cross-check agrees on all ten items.** Not merely "no hard defect": ``agreed`` counts only
      ``agree``, so an item parked in ``anchor_adjacent`` -- answers matching, anchors one turn apart --
      leaves the total at nine and blocks. That is the stricter reading of the instruction and it is
      the right one here, because adjacency is explicitly *not* agreement (the neighbouring turn has to
      confirm the same fact, which no integer comparison establishes) and this gate is the last place
      anyone looks.
    * **no graded finding, no hard defect, no leakage, no rival.**

    Leakage and rivals are checked separately from the findings rather than assumed to imply one. They
    are produced by Python from the auditor's own reconstruction, so they hold even when the auditor
    reported nothing -- which is exactly the case where reading only the findings would call a broken
    set clean.

    **Validator warnings block, except the ones that only say a legal limit was reached.** A warning
    normally describes something a reviser could improve, so it blocks for the reason MINOR findings do:
    a revision costs one call and a worse result is discarded. :data:`AT_CEILING_WARNINGS` is the
    exception, and it is a narrow one -- those warnings fire when a set is *at* a cap rather than over
    it, so the validator has already decided the set is legal and there is nothing to fix. Measured: the
    QR-026 end-of-line warning is emitted on the ``counts["final"] == MAX_FINAL_BLANKS`` branch, the
    branch above it being the error, and the real material this loop was built against carries it in
    every round. Blocking on it burned two revision rounds and then discarded a compliant material.
    """
    blockers: List[str] = []
    counts = candidate.counts
    graded = [(name, int(counts.get(name, 0) or 0)) for name in SEVERITY_ORDER]
    for name, count in graded:
        if count:
            blockers.append("%d open %s finding(s) in the blind audit" % (count, name))

    cross = candidate.cross_check
    for row in cross.hard_defects:
        blockers.append("cross-check %s on Q%s" % (row.get("outcome"), row.get("number")))
    for row in cross.leakage:
        blockers.append("Q%s is answerable from the printed page alone (QR-040)" % row.get("number"))
    for row in cross.equally_supported_rivals:
        blockers.append("Q%s has an equally-supported rival answer %r (AR-012)"
                        % (row.get("number"), row.get("text")))
    for row in cross.needs_review:
        blockers.append("Q%s's evidence anchor is one turn from the writer's and unconfirmed"
                        % row.get("number"))

    reviewed = ((cross.consistency or {}).get("computed") or {}).get("reviewed_question_ids") or []
    if sorted(reviewed) != list(QUESTION_NUMBERS):
        blockers.append("the blind audit covered %s, not all ten items" % sorted(reviewed))
    if cross.compared and cross.agreed != cross.compared:
        blockers.append("the cross-check agrees on %d of %d items"
                        % (cross.agreed, cross.compared))

    for message in (cross.consistency or {}).get("errors") or []:
        blockers.append("the review disagrees with itself: %s" % message)

    for error in getattr(candidate.validation, "errors", None) or []:
        blockers.append("validator error: %s" % error)
    for warning in getattr(candidate.validation, "warnings", None) or []:
        if _is_at_ceiling_warning(warning):
            continue
        blockers.append("validator warning: %s" % warning)

    return blockers


def is_clean_questions(candidate: QuestionCandidate) -> bool:
    """Is this set deliverable? True exactly when :func:`delivery_blockers` is empty.

    Kept as the name the loop and the tests read, and reduced to one line over ``delivery_blockers`` so
    the predicate and the explanation can never disagree about what blocks. Two independent copies of
    this rule is the failure mode worth designing out: the version that decides whether to ship and the
    version that lists why it did not must be the same code.
    """
    return not delivery_blockers(candidate)


def pick_better_questions(
    before: QuestionCandidate, after: QuestionCandidate
) -> QuestionCandidate:
    """Which of two question sets is *better diagnosed*. **Not** which one may be delivered.

    **This function no longer selects what ships, and the distinction is the whole point of the
    two-round contract.** Delivery is decided solely by :func:`delivery_blockers`, which is a
    conjunction of absolute conditions; ranking is a comparison, and a comparison between two defective
    sets has a winner. Reading that winner as an outcome is exactly the mistake the contract forbids --
    "current best but still carrying hard errors" is not a deliverable question set at any rank.

    What it is still for: when every round fails, the failure report should carry the least defective
    version rather than whichever happened to run last, because that is the one worth reading to see
    why the material resisted. It reaches the caller as ``rejected_candidate``, never as ``candidate``.

    Ties go to the later set, as on the material side: where the measured quality is identical the
    revision has at least absorbed the defect list.
    """
    return after if after.key() >= before.key() else before


class ScriptWasEdited(RuntimeError):
    """A question revision returned an edited script.

    Its own exception type rather than a logged warning. The material this stage was handed is either
    already recorded or about to be, so an edited script does not produce a worse question set -- it
    produces a question set that is correct about a script nobody will ever hear. That failure has no
    other symptom: the package validates, the audit passes, and the mismatch surfaces when a candidate
    listens to the audio.
    """


def _assert_script_untouched(reply_package: Dict[str, Any]) -> None:
    """Refuse a revision that hands back a script.

    **What this can and cannot detect, stated plainly, because a guard that cannot fire is worse than
    no guard.** The revision agent is given file *copies* in a workspace that is deleted when the call
    returns, so it has no way to mutate the caller's material -- comparing the script before and after
    would compare an object with itself and pass unconditionally. That check was written first here and
    removed for exactly that reason.

    What is real is the reply. A generate-pool agent asked to fix an unanswerable item will sometimes
    conclude the script is at fault, edit its copy, and return the edited material alongside the
    package -- which is a correct instinct about the defect and the wrong action, since the audio is
    fixed. That arrives as a ``material``, ``script`` or ``listening_material_parts`` block in the
    envelope, is silently dropped by :func:`_question_envelope`, and leaves a package whose evidence
    quotes cite a script that exists only in the deleted workspace. Raising here makes it loud.

    The complementary check is deterministic and already runs: the cross-check verifies every rebuilt
    answer's quote against the *caller's* turns, so an anchor pointing at text that was only ever in the
    edited copy comes back as ``quote_unverifiable``.
    """
    returned = [key for key in ("material", "listening_material_parts", "script")
                if key in reply_package]
    if returned:
        raise ScriptWasEdited(
            "the question revision returned %s alongside the package, which means it edited the "
            "recorded script; questions must be fixed in the carrier, layout, answer key or evidence "
            "anchors (SR-021)" % ", ".join(returned)
        )


async def _audit_and_crosscheck(
    material: Dict[str, Any],
    package: Dict[str, Any],
    label: str,
    emit: Callable,
) -> Tuple[Dict[str, Any], Any]:
    """One blind audit plus the deterministic comparison against the key.

    Kept together because they are never useful apart: the audit's reconstruction is only interpretable
    against the key, and the cross-check has nothing to compare without it. Keeping them in one
    function also makes it structurally impossible to cross-check the first review against the second
    package.

    The auditor is handed ``question_face`` and the locally computed metrics, and nothing else. Building
    the input here rather than passing the package through means the answer key is never in scope for
    the call.
    """
    face = package.get("question_face")
    if not isinstance(face, dict):
        raise ValueError("question package carries no question_face")
    metrics = question_metrics(material, face)
    review = await _with_infra_retries(
        lambda: agent_steps.audit_questions_blind(material, face, metrics),
        "%s question audit" % label, emit)
    cross = crosscheck_questions(package, review, material)
    await emit("question_cross_check", {
        "version": label,
        "agreed": cross.agreed,
        "by_outcome": cross.by_outcome,
        "hard_defects": len(cross.hard_defects),
        "leakage": len(cross.leakage),
        "rivals": len(cross.equally_supported_rivals),
        "status": review.get("question_qc_status"),
    })
    return review, cross


class QuestionResult(object):
    """Outcome for one material's question set: the delivered candidate plus how it was reached.

    **``candidate`` is populated only on success, and that is enforced rather than documented.** A
    failed result carries its best version as ``rejected_candidate``, a separate attribute, so that no
    caller can ship a rejected set by reading the field it would read on the happy path. Putting both
    in one attribute and asking callers to check ``ok`` first is the arrangement this contract exists to
    rule out: it makes delivering a defective set a missing ``if`` rather than an impossibility.
    """

    __slots__ = ("ok", "candidate", "selected_version", "reason", "detail", "timings",
                 "script_unchanged", "outcome", "rejected_candidate", "blockers", "rounds")

    def __init__(
        self,
        ok: bool,
        candidate: Optional[QuestionCandidate] = None,
        selected_version: Optional[str] = None,
        reason: Optional[str] = None,
        detail: Any = None,
        timings: Optional[Dict[str, float]] = None,
        script_unchanged: bool = True,
        outcome: Optional[str] = None,
        rejected_candidate: Optional[QuestionCandidate] = None,
        blockers: Optional[List[str]] = None,
        rounds: int = 0,
    ) -> None:
        if ok and candidate is None:
            raise ValueError("a successful QuestionResult must carry a candidate")
        if not ok and candidate is not None:
            # The invariant stated in the class docstring, checked. A failed result holding a candidate
            # in the deliverable slot would be indistinguishable from a success to every reader that
            # tests the attribute rather than the flag.
            raise ValueError(
                "a failed QuestionResult must not carry a deliverable candidate; "
                "pass it as rejected_candidate")
        self.ok = ok
        self.candidate = candidate
        self.selected_version = selected_version
        self.reason = reason
        self.detail = detail
        self.timings = timings or {}
        self.script_unchanged = script_unchanged
        # The feasibility-vocabulary verdict for the replacement slot. Present on both paths: on success
        # it is None because no verdict is needed, and a reader must not have to infer that from `ok`.
        self.outcome = outcome
        self.rejected_candidate = rejected_candidate
        self.blockers = blockers or []
        self.rounds = rounds

    def as_dict(self) -> Dict[str, Any]:
        if not self.ok or self.candidate is None:
            payload = {
                "ok": False,
                "reason": self.reason,
                "outcome": self.outcome,
                "detail": self.detail,
                "blockers": self.blockers,
                "rounds": self.rounds,
                "timings": self.timings,
            }
            if self.rejected_candidate is not None:
                # Under its own key, and never under "package". A serialised failure that carried the
                # rejected set where a delivered one goes would be shippable by accident downstream --
                # the same reason the two live in different attributes.
                payload["rejected_candidate"] = self.rejected_candidate.as_dict()
            return payload
        payload = self.candidate.as_dict()
        payload.update({
            "ok": True,
            "selected_version": self.selected_version,
            # Stated on every successful result, not only when it was violated. A reader must be able
            # to see that the script was checked, because "no warning" and "not checked" look identical.
            "script_unchanged": self.script_unchanged,
            "rounds": self.rounds,
            "timings": self.timings,
        })
        return payload


async def run_questions(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    emit: Optional[Callable] = None,
) -> QuestionResult:
    """Produce one deliverable question set for a finalised material, or refuse to deliver one.

    Returns ``ok=True`` only for a set that cleared every condition in :func:`delivery_blockers`. When
    the initial set and both revision rounds are blocked, returns ``ok=False`` with
    ``outcome=REGENERATE_MATERIAL`` and the best-diagnosed version under ``rejected_candidate``. There
    is no third exit: this function never returns a set it knows to be defective.

    ``emit`` is optional and defaults to a no-op, matching ``run_one``: a caller that wants progress
    events passes one, and a test that does not need them is not obliged to invent one.
    """
    emit = emit or _noop_emit
    timings: Dict[str, float] = {}

    started = time.time()
    await emit("question_generation_started", {})
    package = await _with_infra_retries(
        lambda: agent_steps.generate_questions(material, blueprint), "question generation", emit)
    timings["generate"] = round(time.time() - started, 1)

    started = time.time()
    validation = await _with_infra_retries(
        lambda: validate_questions(material, blueprint, package), "question validation", emit)
    timings["validate"] = round(time.time() - started, 1)
    await emit("question_validated", {"version": "initial", "errors": len(validation.errors),
                                      "warnings": len(validation.warnings)})

    started = time.time()
    review, cross = await _audit_and_crosscheck(material, package, "initial", emit)
    timings["audit"] = round(time.time() - started, 1)

    current = QuestionCandidate(package, review, cross, validation, "initial")
    best = current
    rounds = 0

    while True:
        blockers = delivery_blockers(current)
        if not blockers:
            # Deliver on the first round that clears every gate, without a further revision. A revision
            # from here could only make it worse, and `pick_better_questions` discarding the result
            # would not refund the call.
            await emit("question_set_clean", {"version": current.label, "status": current.status,
                                              "rounds": rounds})
            return QuestionResult(True, current, current.label, timings=timings, rounds=rounds)

        best = pick_better_questions(best, current)
        await emit("question_set_blocked", {"version": current.label, "rounds": rounds,
                                            "blockers": blockers[:8],
                                            "blocker_count": len(blockers)})

        if rounds >= MAX_QUESTION_REVISIONS:
            break

        instruction = build_question_revise_instruction(
            current.review, current.cross_check, current.validation.warnings)
        if instruction.empty:
            # Blocked with nothing to instruct. Possible when the only blocker is one the instruction
            # builder does not translate into prose -- a validator error, or a coverage shortfall the
            # generator cannot act on. Revising against an empty defect list would spend a call asking
            # for an unspecified change, so the loop stops here.
            #
            # Under the old contract this path DELIVERED the set. It must not: "we could not describe
            # the defect" is not evidence the defect is absent, and it is the one blocked state where a
            # deliver-anyway would look most reasonable in a log.
            await emit("question_revision_skipped", {"reason": "no actionable defects",
                                                     "rounds": rounds})
            break

        rounds += 1
        label = "revised-%d" % rounds
        started = time.time()
        await emit("question_revision_started", {"round": rounds, "label": label,
                                                 "must_fix": len(instruction.must_fix),
                                                 "advisory": len(instruction.advisory)})
        revised = await _with_infra_retries(
            lambda: agent_steps.revise_questions(
                material, blueprint, current.package, instruction),
            "question revision %d" % rounds, emit)
        timings["revise_%d" % rounds] = round(time.time() - started, 1)

        # Before anything else is done with the revision.
        _assert_script_untouched(revised)

        started = time.time()
        revised_validation = await _with_infra_retries(
            lambda: validate_questions(material, blueprint, revised),
            "question revalidation %d" % rounds, emit)
        timings["revalidate_%d" % rounds] = round(time.time() - started, 1)
        await emit("question_validated", {"version": label,
                                          "errors": len(revised_validation.errors),
                                          "warnings": len(revised_validation.warnings)})

        started = time.time()
        # A fresh agent per call inside `audit_questions_blind`, which is what makes every re-audit
        # memoryless: it cannot inherit the previous review's conclusions or the defect list it was
        # asked to fix, because there is no shared session to inherit them from. That matters more with
        # two rounds than with one -- round two's auditor must not be reading round one's verdict.
        revised_review, revised_cross = await _audit_and_crosscheck(
            material, revised, label, emit)
        timings["reaudit_%d" % rounds] = round(time.time() - started, 1)

        # Each round is judged on its own complete evidence: its own validator run, its own blind
        # review, its own cross-check. Nothing is carried forward from the round that produced it.
        current = QuestionCandidate(
            revised, revised_review, revised_cross, revised_validation, label)

    # Every round was blocked. The best-diagnosed version goes into the report and nowhere near the
    # deliverable slot; the verdict is REGENERATE_MATERIAL, which the replacement slot answers with a
    # different material rather than a third attempt at this blueprint.
    blockers = delivery_blockers(best)
    await emit("questions_rejected", {
        "outcome": REGENERATE_MATERIAL,
        "rounds": rounds,
        "best_version": best.label,
        "best_key": list(best.key()),
        "blockers": blockers[:8],
        "blocker_count": len(blockers),
    })
    return QuestionResult(
        False,
        reason=QUESTIONS_NOT_DELIVERABLE,
        outcome=REGENERATE_MATERIAL,
        detail=("%d revision round(s) did not produce a deliverable question set; best version %r "
                "still carries %d blocker(s): %s"
                % (rounds, best.label, len(blockers), "; ".join(blockers[:3]))),
        rejected_candidate=best,
        blockers=blockers,
        timings=timings,
        rounds=rounds,
    )
