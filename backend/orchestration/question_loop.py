"""The deterministic loop for one material's ten questions (the question-stage twin of :mod:`loop`).

    generate -> validate -> blind audit -> cross-check (pure Python)
      -> revise -> validate -> blind re-audit (memoryless) -> cross-check -> pick_better -> deliver

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

**No regeneration path.** The material loop retries generation when the validator reports errors,
because a script with structural errors is worth replacing. A question set with validator errors is
not in the same position: the ten information points are fixed by the blueprint, so a second attempt
draws from exactly the same well, and the defects this stage finds (an answer available on the page, a
rival reading) are editing problems with a known fix -- the auditor supplies one. So errors flow into
the revision instruction and the loop runs once. A set that is still defective after revision is
delivered with its findings attached, which is the same choice the material loop made for the same
reason: withholding it makes the defect unappealable and unseen, while delivering it with the review
makes it a note a reviewer can weigh.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..deterministic.question_crosscheck import crosscheck_questions
from ..deterministic.question_metrics import question_metrics
from ..deterministic.validate_questions import validate_questions
from ..steps import agent_steps
from .loop import _noop_emit, _with_infra_retries
from .question_revision_plan import build_question_revise_instruction

__all__ = [
    "QuestionCandidate",
    "QuestionResult",
    "SEVERITY_ORDER",
    "ScriptWasEdited",
    "is_clean_questions",
    "pick_better_questions",
    "run_questions",
]

# Most severe first. The order IS the ranking, which is why it is a tuple and not a set.
SEVERITY_ORDER = ("CRITICAL", "MAJOR", "MINOR")


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


def is_clean_questions(candidate: QuestionCandidate) -> bool:
    """Can this set be delivered without a revision pass?

    Requires: no graded finding at any severity, no cross-check hard defect, no leakage, no
    equally-supported rival, and no validator error. Note that MINOR findings block, matching the
    material loop: a revision that fixes them costs one call, and if it makes things worse
    ``pick_better_questions`` discards it at no cost to quality.

    Leakage and rivals are checked separately from the findings rather than assumed to imply one. They
    are produced by Python from the auditor's own reconstruction, so they hold even when the auditor
    reported nothing -- which is exactly the case where reading only the findings would call a broken
    set clean.
    """
    counts = candidate.counts
    if any(int(counts.get(name, 0) or 0) for name in SEVERITY_ORDER):
        return False
    cross = candidate.cross_check
    if cross.hard_defects or cross.leakage or cross.equally_supported_rivals:
        return False
    if cross.needs_review:
        return False
    return not (getattr(candidate.validation, "errors", None)
                or getattr(candidate.validation, "warnings", None))


def pick_better_questions(
    before: QuestionCandidate, after: QuestionCandidate
) -> QuestionCandidate:
    """Choose between the pre- and post-revision question sets.

    Ties go to the revision, exactly as on the material side: where the measured quality is identical
    the revised set has at least absorbed the defect list, so it is the better artifact to hand a
    reviewer.
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
    """Outcome for one material's question set: the delivered candidate plus how it was reached."""

    __slots__ = ("ok", "candidate", "selected_version", "reason", "detail", "timings",
                 "script_unchanged")

    def __init__(
        self,
        ok: bool,
        candidate: Optional[QuestionCandidate] = None,
        selected_version: Optional[str] = None,
        reason: Optional[str] = None,
        detail: Any = None,
        timings: Optional[Dict[str, float]] = None,
        script_unchanged: bool = True,
    ) -> None:
        if ok and candidate is None:
            raise ValueError("a successful QuestionResult must carry a candidate")
        self.ok = ok
        self.candidate = candidate
        self.selected_version = selected_version
        self.reason = reason
        self.detail = detail
        self.timings = timings or {}
        self.script_unchanged = script_unchanged

    def as_dict(self) -> Dict[str, Any]:
        if not self.ok or self.candidate is None:
            return {"ok": False, "reason": self.reason, "detail": self.detail,
                    "timings": self.timings}
        payload = self.candidate.as_dict()
        payload.update({
            "ok": True,
            "selected_version": self.selected_version,
            # Stated on every successful result, not only when it was violated. A reader must be able
            # to see that the script was checked, because "no warning" and "not checked" look identical.
            "script_unchanged": self.script_unchanged,
            "timings": self.timings,
        })
        return payload


async def run_questions(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    emit: Optional[Callable] = None,
) -> QuestionResult:
    """Produce one question set for a finalised material.

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

    initial = QuestionCandidate(package, review, cross, validation, "initial")
    if is_clean_questions(initial):
        await emit("question_set_clean", {"status": initial.status})
        return QuestionResult(True, initial, "initial", timings=timings)

    instruction = build_question_revise_instruction(review, cross, validation.warnings)
    if instruction.empty:
        # Not clean, yet nothing to instruct. Possible when the only signal is a validator error the
        # instruction builder does not translate into prose. Revising against an empty defect list
        # would spend a call asking for an unspecified change, so the set is delivered with its review.
        await emit("question_revision_skipped", {"reason": "no actionable defects"})
        return QuestionResult(True, initial, "initial", timings=timings)

    started = time.time()
    await emit("question_revision_started", {"must_fix": len(instruction.must_fix),
                                             "advisory": len(instruction.advisory)})
    revised = await _with_infra_retries(
        lambda: agent_steps.revise_questions(material, blueprint, package, instruction),
        "question revision", emit)
    timings["revise"] = round(time.time() - started, 1)

    # Before anything else is done with the revision.
    _assert_script_untouched(revised)

    started = time.time()
    revised_validation = await _with_infra_retries(
        lambda: validate_questions(material, blueprint, revised), "question revalidation", emit)
    timings["revalidate"] = round(time.time() - started, 1)
    await emit("question_validated", {"version": "revised",
                                      "errors": len(revised_validation.errors),
                                      "warnings": len(revised_validation.warnings)})

    started = time.time()
    # A fresh agent per call inside `audit_questions_blind`, which is what makes this re-audit
    # memoryless: it cannot inherit the first review's conclusions or the defect list it was asked to
    # fix, because there is no shared session to inherit them from.
    revised_review, revised_cross = await _audit_and_crosscheck(
        material, revised, "revised", emit)
    timings["reaudit"] = round(time.time() - started, 1)

    after = QuestionCandidate(revised, revised_review, revised_cross, revised_validation, "revised")
    chosen = pick_better_questions(initial, after)
    await emit("question_version_selected", {
        "selected": chosen.label,
        "initial_key": list(initial.key()),
        "revised_key": list(after.key()),
    })
    return QuestionResult(True, chosen, chosen.label, timings=timings)
