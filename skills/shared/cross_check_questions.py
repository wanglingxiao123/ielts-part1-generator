#!/usr/bin/env python3
"""Compare the blind auditor's rebuilt answers against the writer's answer key and evidence.

The question auditor reads the script and the printed page and never sees the key, so its
``reconstructed_answers`` are an independent second attempt at the same ten items. Where the two
disagree, one of them is wrong about a question a candidate will be graded on -- and which one it is
follows from *how* they disagree, which is what this script classifies.

The asymmetry is the whole value. On the material side a divergence means "the script hides a point";
here it means something sharper, because the auditor was answering the very question the candidate
will answer, from the very same information the candidate gets:

* **different answer** -- the item has two defensible answers, or the intended one is not actually
  the one the carrier asks for. Either way a candidate writing the auditor's answer has been marked
  wrong for reading the paper correctly. This is the defect this script exists to find.
* **same answer, different evidence turn** -- the answer survives but the writer's ``turn_index``
  points somewhere that does not carry it, so every later reviewer is reading the wrong sentence.
* **``derivable_without_recording``** -- the auditor produced the answer off the page. It had no key,
  so anything it recovered that way the candidate recovers too (QR-040).
* **an ``equally_supported`` rival** -- substitution found a second answer that also fits (AR-012).

This is deterministic Python. No model, so it costs nothing per run and its verdict cannot drift.

**Adjacency is never agreement.** A turn index one away from the writer's is reported as
``anchor_adjacent`` and never as a match: the audit rules allow +-1 only when the neighbouring turn
*confirms the same fact*, which is a reading of two sentences and not something an integer comparison
can establish. Three things are checked deterministically before that release is granted -- the answers
match, both anchors sit in the same narration-derived window, and the writer's own evidence row is
marked ``proposition_alignment_result == "aligned"`` -- and any of them unmet makes the one-turn gap a
hard defect rather than a note. Every unknown falls to the hard side, because ``anchor_adjacent`` is a
release and a release granted by silence is not a check.

The auditor's quote is also resolved within +-1 of the turn it named rather than only in that exact
turn, which is what makes the above reachable. Measured: a real re-audit counted a narration turn the
writer had not, shifting every later ``turn_index`` by one; five items whose answers matched the key
exactly were reported as ``quote_unverifiable`` and a sound set was rejected at 4/10 agreement.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Part 1 is exactly ten items. Named rather than inlined because three separate checks below depend on
# it and a mismatch between them would be invisible.
NUMBERS = tuple(range(1, 11))

SEVERITIES = ("CRITICAL", "MAJOR", "MINOR", "INFO", "ADVISORY_WARNING")

# Only these two move question_qc_status, per severity.md 3.2 and the audit skill's own rules file.
BLOCKING = ("CRITICAL", "MAJOR")

# A finding in any other state has been dealt with, so it is outside the count that decides status.
COUNTED_STATE = "open"

# One turn apart. Not a tolerance -- see the module docstring -- but the distance at which the
# divergence is worth reporting as "check whether this is the confirmation turn" rather than as a
# wrong anchor.
ADJACENT = 1

_PUNCTUATION = re.compile(r"[^0-9a-z]+")


def normalise(value: object) -> str:
    """Casefold and strip everything that is not a letter or a digit.

    Same treatment as ``cross_check.normalise_evidence`` and for the same measured reason: the writer
    quotes the script while the auditor retypes what it read, so the identical answer routinely differs
    by a hyphen, an apostrophe or a capital. Comparing raw strings would report those as two different
    answers, which is precisely the false alarm that makes a cross-check get ignored.

    It also means ``two-bedroom`` and ``two bedroom`` compare equal, which is correct here: the answer
    key's own ``counting_rule`` treats a hyphenated compound as one word, so the two are the same
    answer written twice. ``two bedrooms`` still differs -- the plural survives normalisation -- and
    that is the Q8 divergence this script must not swallow.
    """
    if not isinstance(value, str):
        return ""
    return _PUNCTUATION.sub("", value.casefold())


def read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("ERROR: invalid %s JSON: %s" % (label, exc))
    if not isinstance(data, dict):
        raise SystemExit("ERROR: %s must be a JSON object" % label)
    return data


def _by_number(rows: object) -> dict:
    """Index a list of numbered rows. Later duplicates lose, and the caller reports them separately.

    Silently keeping the last one would be the usual shortcut; here it would let a review that lists
    Q3 twice and Q4 never look complete, because the count of *entries* would still be ten.
    """
    indexed = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        number = row.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number not in indexed:
            indexed[number] = row
    return indexed


def _turns(material: object) -> list:
    """The script's turns, or an empty list. Never raises: the quote check degrades instead.

    A missing or oddly-shaped material must not stop the answer comparison, which needs no script at
    all. It only costs the ``quote_unverifiable`` check, and that absence is reported.
    """
    if not isinstance(material, dict):
        return []
    parts = material.get("listening_material_parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return []
    script = parts[0].get("script")
    if not isinstance(script, dict):
        return []
    turns = script.get("turns")
    return [t for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []


def _accepted(entry: object) -> list:
    """Canonical plus every accepted alternative, normalised.

    Alternatives are included because an auditor writing an accepted alternative has NOT diverged --
    the answer key says so itself. Treating that as a divergence would manufacture a defect out of the
    writer's own allowance, and the resulting revision would narrow a correct item.
    """
    if not isinstance(entry, dict):
        return []
    values = [entry.get("canonical")]
    alternatives = entry.get("alternatives")
    if isinstance(alternatives, list):
        values.extend(alternatives)
    return [n for n in (normalise(v) for v in values) if n]


def _anchor_of(entry: object):
    """The turn index, or None when absent or malformed.

    None rather than -1, for the reason ``cross_check.anchor_of`` records: -1 sits within one of both
    another -1 and a genuine opening turn, so two unanchored rows would read as adjacent.
    """
    if isinstance(entry, dict):
        value = entry.get("turn_index")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _quote_is_in_turn(turns: list, index, quote: object) -> bool:
    """Is the auditor's quote really in the turn it named?

    The one precondition of the +-1 rule that Python *can* settle, and the check that keeps a rebuilt
    answer falsifiable: an auditor that reports the right answer against a quote from nowhere has
    produced something nobody can verify, which is worse than a clean disagreement.
    """
    if index is None or not turns or not (0 <= index < len(turns)):
        return False
    needle = normalise(quote)
    if not needle:
        return False
    return needle in normalise(turns[index].get("text"))


def _resolve_quote_nearby(turns: list, index, quote: object):
    """The turn within +-1 of ``index`` that actually contains ``quote``, or None.

    **Why this exists, measured.** A real re-audit counted the narration's "Before you hear the rest of
    the conversation..." turn where the writer had not, so every ``turn_index`` after it was one too
    high. Five items whose answers matched the key exactly came back as ``quote_unverifiable``, the
    agreed count fell from ten to four, and a sound question set was rejected -- because
    ``quote_unverifiable`` returned before the adjacency logic, so a one-off index could never reach the
    ``anchor_adjacent`` branch that exists for precisely that situation.

    Searching the declared turn first matters: when the same span appears in both neighbours, the
    auditor's own claim wins and no shift is reported.
    """
    if index is None or not turns or not normalise(quote):
        return None
    for candidate in (index, index - ADJACENT, index + ADJACENT):
        if 0 <= candidate < len(turns) and _quote_is_in_turn(turns, candidate, quote):
            return candidate
    return None


def narrator_window_of(turns: list, index):
    """Which narration-delimited window a turn index falls in, or None.

    Derived from the script the same way the validator's ``narrator_windows`` derives it -- from the
    positions of the three ``speaker1`` turns, never from a declared field -- because a window id the
    auditor stated would be the auditor's own arithmetic about the numbering that is under suspicion
    here. Returns None when the narration is not the expected three turns, and every caller then treats
    the window as unknown rather than as matching.
    """
    if not turns:
        return None
    narrator = [i for i, turn in enumerate(turns)
                if isinstance(turn, dict) and turn.get("speaker") == "speaker1"]
    if len(narrator) != 3 or index is None or not (0 <= index < len(turns)):
        return None
    if index <= narrator[0]:
        return None
    return 1 if index <= narrator[1] else 2


def review_consistency(review: object) -> dict:
    """Recompute coverage, the severity counts and ``question_qc_status`` from the findings.

    **Three separate reasons this is not redundant with the schema.** The schema checks that the fields
    are present and well-typed; it cannot check that they agree with each other. A review can satisfy
    every constraint in ``audit_questions.schema.json`` and still report two MAJOR findings above a
    ``counts`` block of zeros and a status of ``PASS`` -- and that document is worse than a malformed
    one, because it reads as a clean review and the orchestrator would act on the status.

    What is recomputed:

    * **coverage must be exactly Q1-Q10.** The schema deliberately permits a partial review with a
      stated reason, because an auditor that could only reach nine items should say so rather than
      invent the tenth. The orchestrator's requirement is stricter: ten items were asked for, and nine
      is a failed call to retry, not a result to deliver. Both layers are needed -- the schema keeps the
      auditor honest, this keeps the pipeline complete.
    * **reconstructed_answers must cover the same set it claims to have reviewed.** The self-hiding
      case: nine rebuilt answers under a ``reviewed_question_ids`` of ten passes every schema rule,
      and the missing item then looks like an item with nothing wrong with it.
    * **counts and status must follow from the findings.** Over ``open`` findings only, item-level and
      group-level together, exactly as the rules file states the algorithm.

    Returns a report rather than raising, so a caller can log every disagreement at once instead of
    the first.
    """
    errors = []
    if not isinstance(review, dict):
        return {"ok": False, "errors": ["review is not a JSON object"], "computed": {}}

    rebuilt = _by_number(review.get("reconstructed_answers"))
    rebuilt_numbers = sorted(rebuilt)

    coverage = review.get("coverage") if isinstance(review.get("coverage"), dict) else {}
    reviewed_raw = coverage.get("reviewed_question_ids")
    reviewed = sorted({n for n in (reviewed_raw if isinstance(reviewed_raw, list) else [])
                       if isinstance(n, int) and not isinstance(n, bool)})
    unreviewed_raw = coverage.get("unreviewed")
    unreviewed = sorted({n for n in (unreviewed_raw if isinstance(unreviewed_raw, list) else [])
                         if isinstance(n, int) and not isinstance(n, bool)})

    expected = list(NUMBERS)
    if reviewed != expected:
        errors.append(
            "coverage.reviewed_question_ids is %s, not the ten items Q1-Q10 that were sent; "
            "missing %s, unexpected %s"
            % (reviewed, sorted(set(expected) - set(reviewed)), sorted(set(reviewed) - set(expected)))
        )
    if unreviewed:
        errors.append(
            "coverage.unreviewed is %s; a complete review leaves nothing unreviewed, and the stated "
            "reason %r does not make the set complete" % (unreviewed, coverage.get("reason"))
        )
    if rebuilt_numbers != reviewed:
        # Reported separately from the coverage check above because it is a different failure: the two
        # lists disagreeing means the review is untruthful about itself, whereas both being short
        # together means it simply stopped early.
        errors.append(
            "reconstructed_answers covers %s but coverage claims %s; an item counted as reviewed "
            "with no rebuilt answer is indistinguishable from an item with nothing wrong"
            % (rebuilt_numbers, reviewed)
        )
    # Duplicates are counted from the raw list, since `_by_number` has already collapsed them.
    raw_rebuilt = review.get("reconstructed_answers")
    raw_count = len(raw_rebuilt) if isinstance(raw_rebuilt, list) else 0
    if raw_count != len(rebuilt_numbers):
        errors.append(
            "reconstructed_answers has %d entries for %d distinct items; a duplicated number hides "
            "a missing one behind a correct total" % (raw_count, len(rebuilt_numbers))
        )

    findings = []
    for key in ("per_question_findings", "group_findings"):
        value = review.get(key)
        if isinstance(value, list):
            findings.extend([f for f in value if isinstance(f, dict)])

    counts = {name: 0 for name in SEVERITIES}
    for finding in findings:
        if finding.get("state") != COUNTED_STATE:
            continue
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1

    if any(counts[name] for name in BLOCKING):
        status = "FAIL"
    elif counts["MINOR"]:
        status = "WARNING"
    else:
        status = "PASS"

    stated_status = review.get("question_qc_status")
    if stated_status != status:
        errors.append(
            "question_qc_status is %r but the open findings compute to %r (%s); the status is what "
            "the pipeline routes on, so a disagreement here is a wrong routing decision"
            % (stated_status, status,
               ", ".join("%s=%d" % (name, counts[name]) for name in SEVERITIES if counts[name])
               or "no open findings")
        )

    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    stated_counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    for name in SEVERITIES:
        if stated_counts.get(name) != counts[name]:
            errors.append(
                "summary.counts.%s is %r but %d open findings carry that severity"
                % (name, stated_counts.get(name), counts[name])
            )

    return {"ok": not errors, "errors": errors,
            "computed": {"counts": counts, "question_qc_status": status,
                         "reviewed_question_ids": reviewed}}


def compare(package: dict, review: dict, material: object = None) -> dict:
    """Classify every item's agreement between the writer's key and the auditor's reconstruction.

    ``material`` is optional and only powers the quote check. Passing it is strongly preferred: without
    it an unverifiable quote cannot be distinguished from a verified one, and the report says so.
    """
    answers = _by_number(package.get("answer_key"))
    evidence = _by_number(package.get("evidence"))
    rebuilt = _by_number(review.get("reconstructed_answers"))
    turns = _turns(material)

    items = []
    for number in NUMBERS:
        row = {"number": number}
        mine = rebuilt.get(number)
        if mine is None:
            row["outcome"] = "not_reviewed"
            row["reason"] = "the blind review carries no rebuilt answer for this item"
            items.append(row)
            continue

        accepted = _accepted(answers.get(number))
        auditor_answer = normalise(mine.get("answer"))
        row["writer_answer"] = (answers.get(number) or {}).get("canonical")
        row["auditor_answer"] = mine.get("answer")
        row["confidence"] = mine.get("confidence")

        writer_turn = _anchor_of(evidence.get(number))
        auditor_turn = _anchor_of(mine)
        row["writer_turn"] = writer_turn
        row["auditor_turn"] = auditor_turn

        if not auditor_answer:
            # An empty answer is the auditor's declared "no decisive evidence", which the schema
            # explicitly permits. It is a defect about the item, not a missing review.
            row["outcome"] = "no_answer_found"
            row["reason"] = ("the auditor found no decisive evidence for this gap; an item a careful "
                             "reader cannot settle from the script is not answerable under test "
                             "conditions (SR-005 / AL-002)")
            items.append(row)
            continue

        if accepted and auditor_answer not in accepted:
            row["outcome"] = "answer_divergence"
            row["reason"] = ("the auditor read this gap as %r while the key accepts %r; a candidate "
                             "writing the auditor's answer would be marked wrong for reading the "
                             "paper correctly (AR-012 / AL-018)"
                             % (mine.get("answer"), (answers.get(number) or {}).get("canonical")))
            items.append(row)
            continue

        if not accepted:
            row["outcome"] = "no_key_entry"
            row["reason"] = "the answer key has no usable entry for this item, so nothing to compare"
            items.append(row)
            continue

        # From here the answers agree. What remains is whether the evidence agrees.
        #
        # The quote is resolved within +-1 of the turn the auditor named, not only in that exact turn.
        # An auditor that counts a narration turn the writer did not shifts every later index by one
        # while quoting the script perfectly, and that must reach the adjacency logic below rather than
        # being cut off as unverifiable -- see `_resolve_quote_nearby`.
        quote_turn = _resolve_quote_nearby(turns, auditor_turn, mine.get("quote")) if turns else None
        if turns and quote_turn is None:
            row["outcome"] = "quote_unverifiable"
            row["quote"] = mine.get("quote")
            row["reason"] = ("the quote is not in turn %s of the script nor in either neighbour, so the "
                             "rebuilt answer cannot be checked against anything -- a right answer with "
                             "an unverifiable anchor is not evidence that the item is sound"
                             % auditor_turn)
            items.append(row)
            continue

        # Where the quote was actually found is the auditor's effective anchor. Comparing the *stated*
        # index would re-import the off-by-one the resolution just identified.
        effective_turn = auditor_turn if quote_turn is None else quote_turn
        row["effective_auditor_turn"] = effective_turn
        if quote_turn is not None and quote_turn != auditor_turn:
            row["stated_turn_shift"] = quote_turn - auditor_turn

        gap = None if writer_turn is None or effective_turn is None else abs(
            writer_turn - effective_turn)

        # All three conditions for waving a one-turn gap through, and each is checked against the
        # script or the package rather than against anything the auditor asserted about itself:
        #
        # * the answers already agree -- established above, this branch is unreachable otherwise;
        # * the two turns sit in the same narration window, derived from the three speaker1 turns;
        # * the writer's evidence row says its quote and the carrier state the same fact
        #   (`proposition_alignment_result == "aligned"`), which is the "confirms the same fact"
        #   precondition the audit rules attach to +-1.
        #
        # A window that cannot be derived, or a writer row that never claimed alignment, is NOT treated
        # as satisfying its condition. That is the whole point: `anchor_adjacent` is a release, so every
        # unknown has to fall to the hard side or the release is granted by silence.
        writer_window = narrator_window_of(turns, writer_turn)
        auditor_window = narrator_window_of(turns, effective_turn)
        same_window = (writer_window is not None and writer_window == auditor_window)
        aligned = (evidence.get(number) or {}).get("proposition_alignment_result") == "aligned"

        if gap == 0:
            row["outcome"] = "agree"
        elif gap == ADJACENT and same_window and aligned:
            # Deliberately NOT `agree`. The audit rules permit +-1 only when the neighbouring turn
            # confirms the same fact; the three conditions above are the strongest evidence Python can
            # gather that it does, and a reader is still told to confirm it.
            row["outcome"] = "anchor_adjacent"
            row["reason"] = ("answers agree, both anchors sit in narrator window %s and the writer's "
                             "evidence is proposition-aligned, but the anchors are one turn apart "
                             "(writer %s, auditor %s); adjacency alone is not agreement -- confirm the "
                             "neighbouring turn really confirms the same fact"
                             % (writer_window, writer_turn, effective_turn))
        elif gap == ADJACENT:
            # One turn apart with a condition unmet. Hard, not advisory: without the same window and an
            # aligned proposition there is nothing supporting the claim that the neighbour confirms the
            # same fact, and the permissive reading would let a wrong anchor through as a note.
            missing = []
            if not same_window:
                missing.append("the anchors are in different narrator windows (writer %s, auditor %s)"
                               % (writer_window, auditor_window))
            if not aligned:
                missing.append("the writer's evidence is not marked proposition-aligned (%r)"
                               % (evidence.get(number) or {}).get("proposition_alignment_result"))
            row["outcome"] = "anchor_divergence"
            row["reason"] = ("answers agree and the anchors are one turn apart (writer %s, auditor %s), "
                             "but the +-1 allowance does not apply: %s"
                             % (writer_turn, effective_turn, "; ".join(missing)))
        else:
            row["outcome"] = "anchor_divergence"
            row["reason"] = ("answers agree but the anchors are %s apart (writer %s, auditor %s); the "
                             "recorded evidence turn does not carry the answer, so every later "
                             "reviewer reads the wrong sentence"
                             % ("unknown distance" if gap is None else "%d turns" % gap,
                                writer_turn, effective_turn))
        items.append(row)

    # Leakage and uniqueness come from the auditor's own reconstruction rather than from the
    # comparison, because they are things it discovered without a key -- which is exactly what makes
    # them credible. An auditor holding the answers could not have produced either signal honestly.
    leakage = [{
        "number": number,
        "auditor_answer": rebuilt[number].get("answer"),
        "reason": ("the auditor produced this answer from the printed page alone; it had no key, so "
                   "a candidate reading the same page recovers it too (QR-040 / SC-012)"),
    } for number in sorted(rebuilt) if rebuilt[number].get("derivable_without_recording") is True]

    rivals = []
    for number in sorted(rebuilt):
        for rival in rebuilt[number].get("competing_candidates") or []:
            if isinstance(rival, dict) and rival.get("equally_supported") is True:
                rivals.append({
                    "number": number,
                    "text": rival.get("text"),
                    "reason": rival.get("reason"),
                })

    by_outcome = {}
    for row in items:
        by_outcome.setdefault(row["outcome"], []).append(row["number"])

    # Hard: the answer itself is wrong, missing, unanchored beyond adjacency, or unverifiable. Each
    # one means a candidate could be graded against something the paper does not support.
    hard = [row for row in items if row["outcome"] in (
        "answer_divergence", "no_answer_found", "anchor_divergence", "quote_unverifiable",
        "not_reviewed", "no_key_entry")]

    return {
        "ok": not hard and not leakage and not rivals,
        "compared": len(NUMBERS),
        "agreed": len(by_outcome.get("agree", [])),
        "by_outcome": {key: sorted(value) for key, value in sorted(by_outcome.items())},
        "items": items,
        "hard_defects": hard,
        "needs_review": [row for row in items if row["outcome"] == "anchor_adjacent"],
        "leakage": leakage,
        "equally_supported_rivals": rivals,
        # Stated rather than implied: without the script the quote check did not run, and a reader of
        # this report must not mistake "not checked" for "checked and fine".
        "quotes_checked": bool(turns),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("questions", type=Path, help="the question package")
    parser.add_argument("review", type=Path, help="the blind question audit result")
    parser.add_argument("--material", type=Path, default=None,
                        help="the script, so auditor quotes can be verified against their turns")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()

    package = read_json(args.questions, "question package")
    review = read_json(args.review, "review")
    material = read_json(args.material, "material") if args.material else None

    result = compare(package, review, material)
    # A review that disagrees with itself cannot be trusted to have compared anything, so its own
    # inconsistency is part of the overall verdict rather than a separate advisory note.
    #
    # Assembled the same way the backend's wrapper assembles it, and that is a constraint rather than a
    # coincidence: when only this function attached the block, the CLI reported inconsistencies while
    # the in-process caller silently read zero counts for every severity. Two entry points to one
    # comparison must not disagree about what the comparison includes.
    consistency = review_consistency(review)
    result["consistency"] = consistency
    result["ok"] = bool(result["ok"] and consistency["ok"])

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print("Compared: %d  Agreed: %d  Quotes checked: %s"
          % (result["compared"], result["agreed"], result["quotes_checked"]))
    for message in consistency["errors"]:
        print("INCONSISTENT: %s" % message)
    for row in result["hard_defects"]:
        print("%s: Q%s writer=%r auditor=%r -- %s"
              % (row["outcome"].upper(), row["number"], row.get("writer_answer"),
                 row.get("auditor_answer"), row.get("reason")))
    for row in result["needs_review"]:
        print("ADJACENT: Q%s %s" % (row["number"], row.get("reason")))
    for row in result["leakage"]:
        print("LEAKAGE: Q%s %r is on the page" % (row["number"], row["auditor_answer"]))
    for row in result["equally_supported_rivals"]:
        print("RIVAL: Q%s %r also fits -- %s" % (row["number"], row["text"], row["reason"]))
    print("PASS: the blind reconstruction agrees with the key"
          if result["ok"] else "FAIL: the blind reconstruction diverges from the key")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
