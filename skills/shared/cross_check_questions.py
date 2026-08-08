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

**Adjacency is never agreement on the strength of the integers.** A turn index one away from the
writer's is reported as ``anchor_adjacent`` and not as a match: the audit rules allow +-1 only when the
neighbouring turn *confirms the same fact*, which is a reading of two sentences and not something an
integer comparison can establish. Three things are checked deterministically before that release is
even considered -- the answers match, both anchors sit in the same narration-derived window, and the
writer's own evidence row is marked ``proposition_alignment_result == "aligned"`` -- and any of them
unmet makes the one-turn gap a hard defect rather than a note. Every unknown falls to the hard side,
because ``anchor_adjacent`` is a release and a release granted by silence is not a check.

**Where the line actually falls, and it is narrower than "within one turn".** Two different situations
both present as a one-turn gap, and only one of them is a judgment:

* the auditor quoted the **writer's own sentence** and mistyped the index. Resolving the quote puts both
  anchors on one turn, the gap closes to zero, and it is agreement -- there is no second sentence and
  nothing for a reader to adjudicate. Rows reaching agreement this way carry ``adjacency_normalised``
  and are listed in that key of the result, because five of them in one set is an auditor mis-counting
  the narration systematically and that has to stay legible in an otherwise clean result.
* the auditor quoted the **neighbouring sentence**. Then the two anchors really are two sentences, and
  whether the neighbour confirms the same fact is the reading no integer settles. This stays
  ``anchor_adjacent`` and stays hard, and a *uniquely* located quote does not change that: locating the
  quote establishes which sentence was read, never that two sentences state one fact.

``quote_pins_one_turn`` records which of those it was on every row. A span occurring in more than one
turn of the neighbourhood pins nothing at all, which is why both the writer's validator and the
auditor's instructions now require a quote long enough to occur once.

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


def _quote_turns_nearby(turns: list, index, quote: object) -> list:
    """EVERY turn within +-1 of ``index`` containing ``quote``, ascending. Not just the first.

    The counterpart of :func:`_resolve_quote_nearby`, which stops at the first hit with the declared
    turn searched first. That preference is right for *locating* a quote and wrong for asking whether
    the location is knowable: a span occurring in both the declared turn and its neighbour resolves to
    the declared one and looks pinned, when in fact two readings fit it equally. Only the full list
    distinguishes "the auditor mistyped an index" from "the auditor's evidence does not say which
    sentence it read", and those two get opposite treatment below.
    """
    if index is None or not turns or not normalise(quote):
        return []
    return [candidate for candidate in (index - ADJACENT, index, index + ADJACENT)
            if 0 <= candidate < len(turns) and _quote_is_in_turn(turns, candidate, quote)]


def _unique_quote_turn(turns: list, index, quote: object):
    """The single turn in the +-1 neighbourhood carrying ``quote`` verbatim, or None if not unique.

    What a unique match establishes is narrow, and the narrowness is the point: it identifies *which
    sentence the auditor read*, by the auditor's own evidence rather than by an assumption about how it
    counted turns. It does not establish that this sentence and the writer's state the same fact -- that
    is a reading of two sentences, and no amount of uniqueness performs it.

    So the caller uses this only to describe the row (``quote_pins_one_turn``) and never as grounds to
    promote a genuine two-sentence gap to agreement. None when the neighbourhood carries the span twice
    or not at all: twice means two sentences share a substring, which pins nothing, and a short quote
    makes that likelier rather than rarer.
    """
    found = _quote_turns_nearby(turns, index, quote)
    return found[0] if len(found) == 1 else None


def quote_anchor_errors(review: object, material: object) -> list:
    """Every rebuilt answer whose ``quote`` is not verbatim in the ``turn_index`` it declares.

    The auditor's side of AL-007, which until now nothing enforced. The writer's side has been strict
    from the start -- ``validate_questions_part1.validate_evidence`` rejects a package whose quote is
    not in the turn it names -- so a drifted anchor could only ever come from the auditor, and it
    arrived downstream as an ``anchor_adjacent`` row that read like a semantic question about two
    sentences. It is not one. It is a mistyped integer, and this is where it should be caught and
    retried rather than deliberated over three stages later.

    Reported per item rather than raised, and stated as prose the auditor can act on: a retry needs to
    know which item and which turn, and a caller may want to log every drifted row rather than the
    first. An empty list means every quote sits in the turn it claims -- which makes the +-1 machinery
    below dead code on a well-formed review, and that is the intended end state.

    Rows with no locatable quote at all are NOT reported here. That is ``quote_unverifiable``, a
    different and more serious defect that :func:`compare` already classifies, and duplicating it would
    charge one fault twice.
    """
    turns = _turns(material)
    if not turns:
        return []
    errors = []
    for number, row in sorted(_by_number((review or {}).get("reconstructed_answers")).items()):
        quote = row.get("quote")
        if not normalise(quote):
            continue
        declared = _anchor_of(row)
        if declared is not None and _quote_is_in_turn(turns, declared, quote):
            continue
        located = _resolve_quote_nearby(turns, declared, quote)
        if located is None:
            # Not in the neighbourhood at all: `quote_unverifiable`, not a drifted index.
            continue
        errors.append(
            "Q%s quotes %r but that span is in turn %s, not the turn %s it declares; the quote and the "
            "turn_index must describe the same sentence (AL-007)"
            % (number, quote, located, declared)
        )
    return errors


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

        # Is the sentence the auditor read *known*, or merely narrowed to a neighbourhood? Only a single
        # verbatim occurrence in the +-1 window settles it; two occurrences resolve to the declared turn
        # by preference alone, which reads as pinned while two readings fit equally. Recorded on every
        # row because it is what tells a reader whether the anchor comparison below rests on located
        # evidence or on the auditor's say-so -- it does not by itself decide any outcome.
        pinned = _unique_quote_turn(turns, auditor_turn, mine.get("quote")) is not None if turns \
            else False
        row["quote_pins_one_turn"] = pinned

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
        # Recorded on the row, not merely used here. The three conditions are the *reason* an
        # `anchor_adjacent` row is the narrow kind rather than the hard kind, and a later reader -- a
        # gate deciding whether the gap may ship as a note, a human reading the failure -- otherwise has
        # to re-derive them from the script it no longer has. Reporting only; no outcome depends on
        # these keys, and every consumer re-checks rather than trusts them.
        row["writer_window"] = writer_window
        row["auditor_window"] = auditor_window
        row["same_narrator_window"] = same_window
        row["proposition_aligned"] = aligned

        if gap == 0:
            row["outcome"] = "agree"
            # Agreement reached only because the quote relocated a stated index that was one out. The
            # sentence is the writer's, so this is agreement and not a note -- but it is recorded, because
            # five of these in one set is an auditor mis-counting the narration systematically and that
            # must stay visible in an otherwise clean result rather than being absorbed without trace.
            if row.get("stated_turn_shift"):
                row["adjacency_normalised"] = True
                row["normalised_turn"] = effective_turn
                row["reason"] = ("answers agree and the auditor's quote occurs verbatim in exactly one "
                                 "turn of the +-1 neighbourhood, which is the writer's own anchor (turn "
                                 "%s); the stated index %s was one out, so this is a mis-stated index "
                                 "rather than a second reading, normalised and counted as agreement"
                                 % (effective_turn, auditor_turn))
        elif gap == ADJACENT and same_window and aligned:
            # Deliberately NOT `agree`, and this is the branch the narrow quote exception does NOT reach.
            # Reaching here means the quote was located -- possibly uniquely -- in a turn that is *not*
            # the writer's. So the auditor read a different sentence from the one the writer anchored,
            # and whether that neighbouring sentence confirms the same fact is a reading of two
            # sentences. A unique match cannot settle that: it establishes *which* sentence was read, not
            # that the two sentences state one fact. Promoting it here would be exactly the "adjacency is
            # agreement" move the module docstring rules out.
            row["outcome"] = "anchor_adjacent"
            row["reason"] = ("answers agree, both anchors sit in narrator window %s and the writer's "
                             "evidence is proposition-aligned, but the auditor's quote is in turn %s "
                             "while the writer's evidence is in turn %s -- two different sentences one "
                             "turn apart%s; adjacency alone is not agreement -- confirm the neighbouring "
                             "turn really confirms the same fact"
                             % (writer_window, effective_turn, writer_turn,
                                "" if pinned else ", and the quote does not even pin one of them"))
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
        # Rows that agree only because a uniquely-located quote normalised a one-turn index gap. Counted
        # in `agreed`, and listed here as well: five of these in one set is an auditor mis-counting the
        # narration systematically, and that is worth seeing even though no item is defective.
        "adjacency_normalised": [row for row in items if row.get("adjacency_normalised")],
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
    # Printed even though these count as agreement: the run is clean, and a reader still has to be able
    # to see that N anchors needed normalising before calling the auditor's turn numbering sound.
    for row in result["adjacency_normalised"]:
        print("NORMALISED: Q%s auditor turn %s -> %s (quote pins one turn)"
              % (row["number"], row.get("effective_auditor_turn"), row.get("normalised_turn")))
    for row in result["leakage"]:
        print("LEAKAGE: Q%s %r is on the page" % (row["number"], row["auditor_answer"]))
    for row in result["equally_supported_rivals"]:
        print("RIVAL: Q%s %r also fits -- %s" % (row["number"], row["text"], row["reason"]))
    print("PASS: the blind reconstruction agrees with the key"
          if result["ok"] else "FAIL: the blind reconstruction diverges from the key")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
