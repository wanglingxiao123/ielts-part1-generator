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

**Two classes of defect, and only one of them withholds the set.** The paragraph that used to stand
here argued that a question set has no deliverable-with-findings reading, and applied that to every
finding at every severity. The first half is right and the generalisation was wrong, and production
measured the difference: five real invocations, two scenarios, **zero** delivered sets, and the
best-diagnosed candidate of the whole run was one MINOR finding away from the gate with 10/10
cross-check agreement, no hard defect, no leakage and no rival.

The distinction that matters is whether the finding establishes that the set is *unfair*:

* :func:`hard_blockers` -- an answer available on the printed page, a second equally-supported
  answer, a rebuilt answer the key would mark wrong, a CRITICAL or MAJOR finding, a validator error,
  an audit that did not cover ten items, a cross-check that does not agree. Each of these means a
  candidate can be marked wrong for reading the paper correctly. No number of them is deliverable and
  no reviewer downstream benefits from seeing them, because the grading has already happened.
* :func:`advisory_notes` -- a MINOR finding, a validator warning, or the one narrow adjacency shape
  :func:`sole_adjacency_release` describes. The rules file's own algorithm grades a MINOR-only set
  ``WARNING`` rather than ``FAIL``, which is the audit saying the set is usable and improvable.
  Withholding it makes the note unappealable; delivering it with the note attached is exactly the
  material loop's rationale, and it applies here for the same reason.

So the exits are: deliver (``PASS`` when nothing at all is open, ``WARNING`` when only advisories
are -- see :func:`delivered_status`, which is what makes that true rather than coincidental), or
return :data:`REGENERATE_MATERIAL` because a **hard** blocker survived two revisions.

**One adjacency shape is an advisory, every other one still blocks.** ``anchor_adjacent`` is a
one-turn gap between where the writer and the auditor anchored their evidence for an answer they
*agree* on. Measured on batch ``web-1786166271869-1`` it was 26 of 42 blockers, and one instance of it
destroyed an entire material by itself. It is released only when it is the sole open objection in the
whole set, all-or-nothing, with every precondition re-derived rather than trusted; adjacency sitting
beside any other defect is untouched. :func:`sole_adjacency_release` is the whole of that rule.

**A set with no hard blocker is never regenerated.** That is the invariant this module now owes its
caller, and it holds at both decision points -- before the first revision and after the last.

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

**Revision can make a set worse, and measured, it usually did.** On the production run the initial
candidate carried one MINOR; round one turned that into a MAJOR plus two ``answer_divergence`` items
plus a rival; round two into two MAJORs plus leakage. So the ranking's winner is not a consolation
prize to put in a report -- it is what gets delivered when it is deliverable. :func:`pick_better_questions`
chooses among *candidates*; :func:`hard_blockers` decides whether the winner may ship. Ranking still
never authorises delivery on its own, which is the property that made the old split worth having.
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
    "BLOCKING_SEVERITIES",
    "MAX_QUESTION_REVISIONS",
    "QUESTION_NUMBERS",
    "QUESTIONS_NOT_DELIVERABLE",
    "QuestionCandidate",
    "QuestionResult",
    "SEVERITY_ORDER",
    "ScriptWasEdited",
    "WARNING_STATUS",
    "advisory_notes",
    "delivered_status",
    "delivery_blockers",
    "hard_blockers",
    "sole_adjacency_release",
    "is_clean_questions",
    "is_deliverable",
    "pick_better_questions",
    "run_questions",
]

# Most severe first. The order IS the ranking, which is why it is a tuple and not a set.
SEVERITY_ORDER = ("CRITICAL", "MAJOR", "MINOR")

# The severities that withhold a question set. MINOR is deliberately absent, and it is the only one.
#
# Read this against the rules file's own status algorithm rather than as a policy choice: it grades a
# set FAIL for CRITICAL or MAJOR, and WARNING for MINOR alone. So a MINOR-only set is one the audit
# itself calls usable. Blocking on it asserted the opposite of what the auditor concluded, and the
# production cost of that assertion is recorded in this module's header.
#
# Why a MAJOR can never join it: a MAJOR here means a candidate can be marked wrong for reading the
# paper correctly. That is not an improvable note, it is an unfair item.
BLOCKING_SEVERITIES = ("CRITICAL", "MAJOR")

# The status a set carries when it ships with advisory notes attached. Not a fourth outcome -- the
# rules file already computes exactly this value from the counts, so this constant names what the
# audit said rather than inventing a delivery state.
WARNING_STATUS = "WARNING"

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


def sole_adjacency_release(candidate: "QuestionCandidate") -> List[Dict[str, Any]]:
    """The ``anchor_adjacent`` rows that may ship as advisories, or ``[]`` when none may.

    **All-or-nothing, and only when adjacency is the ONLY thing open.** The release exists for one
    measured shape: a set where the writer and the auditor agree on all ten answers, nothing
    deterministic is wrong, and the single remaining objection is that on some items the two anchored
    the same agreed fact one turn apart. Measured on batch ``web-1786166271869-1``: 26 of 42 blockers
    across the whole run were this row, one of them destroyed an entire material on its own, and three
    resume rounds delivered one set in 2360s.

    Why the release is safe *here* and not inside the cross-check: `anchor_adjacent` is reached only
    after the shared comparison has established that the answers agree, that both anchors sit in one
    narrator window, and that the writer's evidence is proposition-aligned (see
    ``cross_check_questions.compare``). What it cannot establish is that the neighbouring sentence
    confirms the same fact -- a reading. This function does not claim to settle that reading either. It
    says something narrower and checkable: when nothing else at all is open, a one-turn gap on an
    otherwise-agreed item is not worth destroying a fair material over, and the note ships attached to
    the set so the reader sees it.

    Every precondition is re-derived from the row and the candidate rather than trusted:

    * the answers on the row agree -- re-checked, not inferred from the outcome label;
    * the gap is exactly one turn, recomputed from the writer's anchor and the auditor's *effective*
      one -- where the quote was found, never the index it stated;
    * ``same_narrator_window`` -- the row's own recorded windows must be present and equal;
    * no ``answer_divergence`` or any other hard defect, no leakage, no equally-supported rival,
      nothing at CRITICAL or MAJOR, no validator error, full ten-item coverage;
    * ``anchor_adjacent`` is the only non-agreeing outcome present.

    A missing or unparseable field fails the check. That direction is deliberate: the release is a
    release, so every unknown has to fall to the hard side -- the same rule the shared module applies
    one level down. Returns the rows so the caller can name them in the advisory text; an empty list
    means "no release", never "released nothing".
    """
    cross = candidate.cross_check
    rows = list(cross.needs_review)
    if not rows:
        return []

    # Anything else open at all -- deterministic or graded -- and this is not the sole-adjacency shape.
    if cross.hard_defects or cross.leakage or cross.equally_supported_rivals:
        return []
    if any(int(candidate.counts.get(name, 0) or 0) for name in BLOCKING_SEVERITIES):
        return []
    if getattr(candidate.validation, "errors", None):
        return []
    if (cross.consistency or {}).get("errors"):
        return []

    # The audit must have covered exactly Q1-Q10; a shortfall is not a thing adjacency explains.
    reviewed = ((cross.consistency or {}).get("computed") or {}).get("reviewed_question_ids") or []
    if sorted(reviewed) != list(QUESTION_NUMBERS):
        return []

    # Every item must be either agreed or one of these adjacency rows. `agreed` counts only `agree`,
    # so this is the arithmetic that rules out a third outcome hiding in the item list.
    if cross.compared != cross.agreed + len(rows):
        return []

    for row in rows:
        if str(row.get("outcome")) != "anchor_adjacent":
            return []
        # The answers agreeing is what makes the anchor gap the *only* question. Re-checked here
        # because a released row must not depend on the outcome label alone.
        writer, auditor = row.get("writer_answer"), row.get("auditor_answer")
        if not isinstance(writer, str) or not isinstance(auditor, str):
            return []
        if writer.strip().casefold() != auditor.strip().casefold():
            return []
        # Same narrator window, from the row's own record of both windows. `compare` only reaches
        # `anchor_adjacent` when they matched, but an absent value here must not read as a match --
        # a row produced before those keys existed has to fail, not pass by default.
        window = row.get("writer_window")
        if window is None or row.get("auditor_window") != window:
            return []
        if row.get("same_narrator_window") is not True:
            return []
        # The writer's evidence claiming its quote and carrier state one fact is the third condition
        # the audit rules attach to +-1. Required explicitly here for the same reason.
        if row.get("proposition_aligned") is not True:
            return []
        # And the gap really is one turn, recomputed from the two anchors on the row.
        #
        # ``effective_auditor_turn`` and NOT ``auditor_turn``, with no fallback between them. The
        # effective turn is where the quote was actually located; the stated one is the value `compare`
        # distrusted enough to relocate, and reading it when the effective turn is missing would
        # re-import exactly the off-by-one the relocation removed. Absent means unknown, and unknown
        # fails.
        writer_turn, auditor_turn = row.get("writer_turn"), row.get("effective_auditor_turn")
        if not isinstance(writer_turn, int) or not isinstance(auditor_turn, int):
            return []
        if abs(writer_turn - auditor_turn) != 1:
            return []
    return rows


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


def hard_blockers(candidate: QuestionCandidate) -> List[str]:
    """Every reason this set may **not** be delivered at all, as prose. Empty means deliverable.

    **This is the gate.** A non-empty return is the only thing that can produce
    :data:`REGENERATE_MATERIAL`, and every entry names the check that produced it -- a material
    rejected with no stated reason is a material nobody can argue with, and a rejection costs a full
    regeneration.

    What blocks, and why each one is unfairness rather than an improvable note:

    * **a CRITICAL or MAJOR finding** (:data:`BLOCKING_SEVERITIES`). MINOR is not here; see
      :func:`advisory_notes`.
    * **a validator error.** Warnings do not block; see :func:`advisory_notes` and
      :data:`AT_CEILING_WARNINGS`.
    * **the audit did not cover exactly Q1-Q10.** Read from the cross-check's own recompute, not from
      the review's claim about itself.
    * **the cross-check does not agree on all ten items.** Not merely "no hard defect": ``agreed``
      counts only ``agree``, so an item parked in ``anchor_adjacent`` -- answers matching, anchors one
      turn apart with the auditor's quote not pinning either -- leaves the total at nine and blocks.
      Adjacency is explicitly *not* agreement (the neighbouring turn has to confirm the same fact,
      which no integer comparison establishes) and this gate is the last place anyone looks.

      Stated **only when it is not the arithmetic of the entries already listed.** Every non-agreeing
      item normally produces its own line above, so restating the total charges the same fact twice --
      three adjacencies became four blockers on a real run, and the fourth was the first three added
      up. Suppressed in that case; still stated whenever the shortfall exceeds what was named, because
      an item that failed to agree without appearing above is a hole in this report.
    * **a hard defect, leakage, or an equally-supported rival.**
    * **a review that disagrees with itself.**

    Leakage and rivals are checked separately from the findings rather than assumed to imply one. They
    are produced by Python from the auditor's own reconstruction, so they hold even when the auditor
    reported nothing -- which is exactly the case where reading only the findings would call a broken
    set clean. That is why relaxing the *findings* threshold to MINOR does not weaken this gate: the
    deterministic signals are independent of the auditor's severity grading, and every one of them
    still blocks unconditionally.
    """
    blockers: List[str] = []
    counts = candidate.counts
    for name in BLOCKING_SEVERITIES:
        count = int(counts.get(name, 0) or 0)
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
    # `sole_adjacency_release` is non-empty ONLY when these rows are the single remaining objection --
    # it returns [] the moment anything else is open, including a hard defect this same loop is about to
    # append. So the released rows move to `advisory_notes` and everything else keeps hard-blocking, and
    # an adjacency sitting beside any other defect is unaffected by the release.
    released = {row.get("number") for row in sole_adjacency_release(candidate)}
    for row in cross.needs_review:
        if row.get("number") in released:
            continue
        blockers.append("Q%s's evidence anchor is one turn from the writer's and unconfirmed"
                        % row.get("number"))

    reviewed = ((cross.consistency or {}).get("computed") or {}).get("reviewed_question_ids") or []
    if sorted(reviewed) != list(QUESTION_NUMBERS):
        blockers.append("the blind audit covered %s, not all ten items" % sorted(reviewed))
    # The shortfall blocks, but only where it says something the entries above have not. When the
    # non-agreeing items are exactly the ones already named one-by-one, "agrees on 7 of 10" is those
    # three lines added up: it charges one fact twice, inflates `blocker_count`, and makes a set look
    # further from deliverable than it is. It is kept for every other shortfall, because an item that
    # failed to agree without producing a named entry is a gap in the reporting above and must not pass
    # in silence.
    shortfall = (cross.compared - cross.agreed) if cross.compared else 0
    # Released rows count as named: they ARE accounted for -- as advisories rather than blockers -- and
    # leaving them out would re-block the released set through this line, which is the same fact charged
    # twice that the suppression below exists to prevent. Without this the release would be dead code:
    # the per-item blockers would go quiet and "agrees on 7 of 10" would withhold the set anyway.
    named = len(cross.hard_defects) + len(cross.needs_review)
    if shortfall and shortfall != named:
        blockers.append("the cross-check agrees on %d of %d items%s"
                        % (cross.agreed, cross.compared,
                           " beyond the %d already listed" % named if named else ""))

    for message in (cross.consistency or {}).get("errors") or []:
        blockers.append("the review disagrees with itself: %s" % message)

    for error in getattr(candidate.validation, "errors", None) or []:
        blockers.append("validator error: %s" % error)

    return blockers


def advisory_notes(candidate: QuestionCandidate) -> List[str]:
    """Improvable-but-shippable observations. Non-empty means deliver as ``WARNING``, not withhold.

    Two sources, and the boundary of each is narrow on purpose:

    * **MINOR findings.** The rules file grades a MINOR-only set ``WARNING``, which is the audit
      calling it usable. Nothing at MAJOR or above is ever an advisory -- see
      :data:`BLOCKING_SEVERITIES`.
    * **validator warnings**, except :data:`AT_CEILING_WARNINGS`, which report a legal limit was
      *reached* and describe nothing to fix. Those are not advisories either; they are silence.

    **These still drive revision.** An advisory buys a revision round while the budget lasts, because
    a round that clears it is a better set. What changed is the consequence of the budget running out:
    the set ships with the note attached instead of being destroyed along with the material. Measured
    on the production run, spending rounds on advisories was actively harmful -- both rounds made the
    set worse -- which is why the ranking keeps the best version rather than the last.
    """
    notes: List[str] = []
    count = int(candidate.counts.get("MINOR", 0) or 0)
    if count:
        notes.append("%d open MINOR finding(s) in the blind audit" % count)
    for warning in getattr(candidate.validation, "warnings", None) or []:
        if _is_at_ceiling_warning(warning):
            continue
        notes.append("validator warning: %s" % warning)
    # The third source, and the narrowest: one-turn anchor gaps on items whose answers agree, where
    # adjacency is the only thing open in the entire set. See :func:`sole_adjacency_release` for why this
    # is a note rather than a rewrite -- and note it ships as WARNING, so the gap is on the record the
    # reviewer reads rather than absorbed silently.
    for row in sole_adjacency_release(candidate):
        notes.append(
            "Q%s's evidence anchor is one turn from the writer's within narrator window %s; answers "
            "agree and nothing else is open, so this ships as a note -- confirm the neighbouring turn "
            "states the same fact" % (row.get("number"), row.get("writer_window")))
    return notes


def delivered_status(candidate: QuestionCandidate) -> str:
    """The status the DELIVERED record carries: ``WARNING`` whenever a note is still open.

    Until the adjacency release existed this function would have been redundant, and that is exactly
    why it is needed now. Both of the original advisory sources are MINOR findings or validator
    warnings, and a MINOR moves the rules file's own algorithm to ``WARNING`` -- so "ships with notes"
    and "auditor says WARNING" were the same set of cases, and the module header's promise (``PASS``
    when nothing at all is open, ``WARNING`` when only advisories are) held without anyone asserting
    it. An adjacency-released set breaks that coincidence: the audit found nothing, so its computed
    status is ``PASS``, while a note about an unconfirmed anchor is open. Shipping that as ``PASS``
    would put a set on the record as unqualified-clean with a caveat attached that only the
    ``advisories`` list mentions -- the "delivering them silently" failure one level up.

    Only ``PASS`` is floored, and only upward. A ``FAIL`` is never softened here: FAIL means CRITICAL
    or MAJOR, which :func:`hard_blockers` already refused to deliver, so this cannot be reached with
    one -- and if it ever could, rewriting it would be the more serious of the two bugs.

    The auditor's own value is not overwritten anywhere: it stays under ``review.question_qc_status``
    in the same payload, and :attr:`QuestionCandidate.status` still reports it unchanged. This is the
    delivery record's status, which is a different claim from the audit's verdict.
    """
    if advisory_notes(candidate) and candidate.status == "PASS":
        return WARNING_STATUS
    return candidate.status


def delivery_blockers(candidate: QuestionCandidate) -> List[str]:
    """Everything open against this set: hard blockers first, then advisories.

    **Reading this as the gate is the bug this signature now prevents someone from writing.** It was
    the gate, and every entry withheld the set; that is what produced zero deliveries in production.
    It survives as the *reporting* view -- the revision instruction and the failure detail both want
    the complete list -- and the gate is :func:`hard_blockers`.

    Order is hard-first so a truncated log line (``blockers[:3]``, used on the failure path) shows the
    reasons that actually withheld the set rather than three cosmetic notes.
    """
    return hard_blockers(candidate) + advisory_notes(candidate)


def is_clean_questions(candidate: QuestionCandidate) -> bool:
    """Is this set completely clean -- nothing open at all, not even an advisory?

    **Not the delivery gate.** ``is_clean_questions`` is False for a set that ships as ``WARNING``,
    which is the whole point of the distinction: "clean" and "deliverable" were the same question
    under the old contract and are not under this one. The gate is ``not hard_blockers(candidate)``,
    and :func:`is_deliverable` spells it so no caller has to remember which of the two to negate.
    """
    return not delivery_blockers(candidate)


def is_deliverable(candidate: QuestionCandidate) -> bool:
    """May this set ship? True exactly when :func:`hard_blockers` is empty.

    One line over ``hard_blockers`` so the predicate and the explanation can never disagree about what
    blocks: the version that decides whether to ship and the version that lists why it did not must be
    the same code.
    """
    return not hard_blockers(candidate)


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
                 "script_unchanged", "outcome", "rejected_candidate", "blockers", "rounds",
                 "advisories")

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
        advisories: Optional[List[str]] = None,
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
        # Open advisories on a DELIVERED set. Empty on the clean path and on every failure, so a reader
        # never has to correlate this with `ok` to know what it means: non-empty says "shipped, and
        # these are still open". A delivered set that carried notes nobody recorded would be the same
        # defect as withholding it -- one level quieter.
        self.advisories = advisories or []

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
            # Overridden, and only ever upward: see :func:`delivered_status`. The candidate's own
            # `as_dict` reports the auditor's computed value, which is the right answer to "what did
            # the audit conclude" and the wrong answer to "what is the state of the set we shipped"
            # once a note can be open without any finding behind it. The auditor's value is still in
            # this same payload under `review.question_qc_status`, unmodified.
            "status": delivered_status(self.candidate),
            "selected_version": self.selected_version,
            # Stated on every successful result, not only when it was violated. A reader must be able
            # to see that the script was checked, because "no warning" and "not checked" look identical.
            "script_unchanged": self.script_unchanged,
            "rounds": self.rounds,
            "timings": self.timings,
            # Always written, `[]` on the clean path, for the same reason as `script_unchanged`: an
            # absent key cannot be distinguished from a set that was never checked for advisories.
            "advisories": list(self.advisories),
        })
        return payload


async def run_questions(
    material: Dict[str, Any],
    blueprint: Dict[str, Any],
    emit: Optional[Callable] = None,
) -> QuestionResult:
    """Produce one deliverable question set for a finalised material, or refuse to deliver one.

    Returns ``ok=True`` for any set with no entry in :func:`hard_blockers` -- immediately when nothing
    at all is open, and after the revision budget when only advisories remain (``advisories`` on the
    result says which, and the candidate's own ``status`` is the auditor's ``WARNING``). Returns
    ``ok=False`` with ``outcome=REGENERATE_MATERIAL`` only when a **hard** blocker survived every
    round, with the best-diagnosed version under ``rejected_candidate``.

    **The invariant, and the reason this function was rewritten:** a candidate with no hard blocker is
    never regenerated. Under the previous contract any MINOR finding withheld the set, which in
    production meant five invocations, two scenarios and zero delivered question sets.

    This function still never returns a set it knows to be *unfair*. What it no longer does is treat
    "improvable" and "unfair" as one category.

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
        hard = hard_blockers(current)
        advisories = advisory_notes(current)
        if not hard and not advisories:
            # Nothing open at all. Deliver without a further revision: a revision from here could only
            # make it worse, and `pick_better_questions` discarding the result would not refund the call.
            await emit("question_set_clean", {"version": current.label, "status": current.status,
                                              "rounds": rounds})
            return QuestionResult(True, current, current.label, timings=timings, rounds=rounds)

        best = pick_better_questions(best, current)
        await emit("question_set_blocked", {"version": current.label, "rounds": rounds,
                                            "blockers": (hard + advisories)[:8],
                                            "blocker_count": len(hard) + len(advisories),
                                            "hard_blockers": len(hard),
                                            "advisory_notes": len(advisories)})

        if rounds >= MAX_QUESTION_REVISIONS:
            break

        instruction = build_question_revise_instruction(
            current.review,
            current.cross_check,
            current.validation.warnings,
            current.validation.errors,
        )
        if instruction.empty:
            # Open findings with nothing to instruct. Possible when the only entry is one the
            # instruction builder does not translate into prose -- a validator error, or a coverage
            # shortfall the generator cannot act on. Revising against an empty defect list would spend
            # a call asking for an unspecified change, so the loop stops here and the exit below
            # decides on the evidence.
            #
            # This must not deliver unconditionally: "we could not describe the defect" is not evidence
            # the defect is absent, and it is the one state where a deliver-anyway would look most
            # reasonable in a log. It also must not regenerate unconditionally -- that is what the
            # `hard`/advisory split at the exit is for, and an undescribable *advisory* is precisely a
            # note to attach rather than a reason to destroy a fair set.
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

    # The revision budget is spent (or there was nothing to instruct). Judge the BEST version, not the
    # last one: measured on the production run, both rounds made the set strictly worse, so deciding on
    # `current` here would grade a material by the worst thing the reviser did to it.
    best_hard = hard_blockers(best)
    best_advisories = advisory_notes(best)

    if not best_hard:
        # Deliverable with notes attached. This is the branch whose absence produced five invocations
        # and zero delivered sets: the best candidate was one MINOR from the old gate with 10/10
        # agreement, no hard defect, no leakage and no rival, and it was destroyed along with a
        # perfectly fair material.
        #
        # `status` is :func:`delivered_status`, which is the auditor's own computed value for every
        # shape that has a finding behind the note -- a MINOR-only set already computes WARNING from
        # the rules file (:data:`WARNING_STATUS`) -- and floors a finding-free set with an open
        # advisory to WARNING rather than shipping it as unqualified PASS. The advisories travel on
        # the result too, so the delivered record says what is open; delivering them silently would
        # be the same defect as withholding them, one level quieter.
        await emit("question_set_clean", {
            "version": best.label,
            "status": delivered_status(best),
            "rounds": rounds,
            "advisory_notes": best_advisories[:8],
            "advisory_count": len(best_advisories),
        })
        return QuestionResult(True, best, best.label, timings=timings, rounds=rounds,
                              advisories=best_advisories)

    # A hard blocker survived every round. THIS is what REGENERATE_MATERIAL is for: the best version of
    # this blueprint's questions can still mark a candidate wrong for reading the paper correctly, and
    # no further round against the same fixed ten points changes that. The replacement slot answers it
    # with a different material.
    blockers = best_hard + best_advisories
    await emit("questions_rejected", {
        "outcome": REGENERATE_MATERIAL,
        "rounds": rounds,
        "best_version": best.label,
        "best_key": list(best.key()),
        "blockers": blockers[:8],
        "blocker_count": len(blockers),
        "hard_blockers": len(best_hard),
    })
    return QuestionResult(
        False,
        reason=QUESTIONS_NOT_DELIVERABLE,
        outcome=REGENERATE_MATERIAL,
        detail=("%d revision round(s) did not produce a deliverable question set; best version %r "
                "still carries %d hard blocker(s): %s"
                % (rounds, best.label, len(best_hard), "; ".join(best_hard[:3]))),
        rejected_candidate=best,
        blockers=blockers,
        timings=timings,
        rounds=rounds,
    )
