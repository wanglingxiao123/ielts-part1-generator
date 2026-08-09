# Part 1 Question Audit Rules

What this file covers: the judgements that cannot be made mechanically, and how each one is graded.
Everything countable was settled before the request reached you — item count, printed numbering, gap
positions, gap-final tallies, window membership, group shape, category counts. Those arrive as
`question_metrics` and are not yours to recompute; a number asserted without calculating it is the one
most likely to be wrong, and here the calculation is already done.

What is left is everything a count cannot see, and it is the whole reason this review exists.

## Contents

1. Your position, and why it is the strong one
2. The fifteen dimensions
3. Reconstruction: how to arrive at an answer
4. Same-level rivals, and what counts as one
5. Leakage from the page
6. Tolerances, by dimension
7. Grading
8. What is not a finding

---

## 1. Your Position, and Why It Is the Strong One

You have the complete script including its narration, the candidate-visible page, and the counts.
You do **not** have an authored answer, a quotation table, or the item plan the writer worked from.

That is not a handicap to work around. Reconstructing each answer yourself tests two things at once,
and the second is not otherwise testable:

- whether the answer is recoverable **from the recording** — if you cannot find it, a candidate
  hearing the script once will not either;
- whether the answer is recoverable **without** the recording — because the only answers you can
  scan the printed page for are the ones you rebuilt, and anything you can produce from the page
  alone was free to the candidate too.

A review that started from a supplied answer collapses both checks into agreement with the writer.
It produces a report that is merely too agreeable, and nothing in the output looks wrong.

**If an answer, an accepted-alternatives list, a quotation, a turn index or the writer's item plan
appears anywhere in the request, stop.** Do not review the set. Report the leak, name where it
appeared, and return nothing else. Continuing would produce a document indistinguishable from a
genuine review.

---

## 2. The Fifteen Dimensions

Each row is a judgement, the rule id that owns it, and the observable that decides it. One id per
finding: a finding citing three rules cannot be fixed once.

| # | Judgement | Rule id | What decides it |
|---|---|---|---|
| 1 | **Answer uniqueness** | AR-012, QR-010 | Enumerate the same-level rivals and substitute each into the carrier. Checking only that your own answer fits proves nothing — one candidate fitting is compatible with three fitting. See §4. |
| 2 | **Paraphrase fidelity** | AL-004, QR-024 | Subject, scope, polarity, tense, causality, modality and answer granularity survive the rewording, and the result is still natural and still locatable. A carrier that quietly narrows "most weekends" to "every weekend" changes which answer is right. |
| 3 | **Proposition-level alignment** | AL-018 | The carrier's assertion and your decisive evidence are **one factual proposition**: same subject, same object, same place, same time, same relation. A question number printed on the same line as the answer word is proximity, not alignment. |
| 4 | **Semantic leakage** | QR-040 (second half), SC-012 | A near-restatement of the same fact in different words gives the answer away as effectively as the word itself. Word-level hits were already caught; the paraphrase is yours. See §5. |
| 5 | **Fit after insertion** | QR-009, AL-015 | Write your answer into the gap and read the whole line. It must be grammatical and mean what the evidence means — number agreement, article, preposition, and the sense of the finished line. |
| 6 | **Is the gap a real information unit** | QR-010 | The gap should take one meaningful piece of information. A gap over a function word, over half a compound, or over a stretch a listener never hears as a unit is a defect however cleanly it validates. |
| 7 | **Naturalness, register, lexical load** | LG-001, LG-002, LG-003, LG-005, LG-015 | The printed page reads as an ordinary form, note or table someone would really fill in. Flag stilted phrasing, a register that drifts academic, vocabulary made harder than the point requires, and instruction sentences used as form values (`Surname | Please record ...`). A parenthetical unit or answer-scope qualifier is allowed only when removing it creates real ambiguity, it agrees with the recording/record format, and it is non-redundant and non-leaking. Commentary on speaking, spelling or answering such as `(as spelt)` and `(as mentioned)` is a naturalness finding; parentheses alone are not. |
| 8 | **Spelling burden** | QR-043 | A one-word answer with an avoidable orthographic trap, where an equally valid low-risk alternative existed in the same evidence. **Word rarity is a triage signal for a human, never an automatic verdict** — no frequency threshold decides this, and low frequency alone is not a finding. |
| 9 | **Do the ten really form one form / note / table** | SC-015, QR-026 | Read the page as a page. A form's rows belong to one record; a note's headings hold a real hierarchy and each heading is immediately followed by its items; a table's axes mean something in both directions. A detached run of note headings followed by a separate run of questions is a finding. Ten grammatically sound sentences that share no structure is not a pass. |
| 10 | **Single reading, controllable locating burden, no transcription order** | QR-003, QR-034, QR-037 | Each carrier has exactly one plausible interpretation. The listener can find the place from the printed cue without holding the whole script in memory. Items are not answerable merely by writing down what is heard in order. |
| 11 | **Does the script itself give the answers away** | SR-006, SR-007 | Two mutually exclusive answers equally supported by the decisive evidence is a defect in the item (SR-006). A script that signposts its own answers — "and the *answer* is", an unnatural pause-and-repeat around every key word — is SR-007. Both are reported; neither is fixed by editing the recording. |
| 12 | **Label and carrier saying the same thing twice** | SC-015, QR-026 | In a form row the label and the carrier print side by side. Read the line as printed: `Arrival date | Arrival date: ....` names the field twice, and so does the near-synonym version (`Family name | Surname: ....`). The label names the field; the carrier adds only what the line needs beyond its name. Same test for a table's row/column heading against its cell text, and a note heading against the line under it. **The finding is the duplication, not the wording** — the fix is to empty or repurpose the carrier, not to re-synonymise it. This is a natural-record-structure judgement, not a QR-015 accessibility claim. |
| 13 | **Pseudo-table or unsupported columns** | SC-015, QR-026 | Judge the printed axes, not a mechanical column minimum. The current package can represent one row-header column plus exactly one content column because it has no question-to-cell mapping. More than one `column_labels` entry is therefore a structural finding, even if the headings sound meaningful. With one content column, a filler heading (`Detail`, `Details`, `Notes`, `Information`, `Answer`), repeated row/cell wording, or unrelated facts placed behind borders remain warning signs; call it a pseudo-table when the grid communicates no real relationship and a form or note expresses the material more naturally. |
| 14 | **Generalised signpost** | QR-034 | A navigation line must name what is being talked about at that point in the recording. Apply the transfer test: could this line be copied unchanged onto a completely different Part 1 material? "Details are confirmed", "Information is given", "The following details apply" all pass that test and therefore carry no navigation value. Vacuous metadiscourse about the page rather than the recording is the QR-034 defect. |
| 15 | **Forced split or layout mixing** | SC-015 | First test whether Q1-Q10 form one natural candidate-visible record. A boundary at the narrator midpoint is a finding when the same record and layout continue on both sides; narrator windows constrain evidence, not page structure. Layouts alternating group by group with no corresponding change in the conversation are likewise variety for its own sake. A real change in record structure remains a valid reason to split. Say which boundary or layout lacks support and what continuous or replacement structure fits. |

**Where the defect is the page's shape, the fix is the page.** Dimensions 12–15 are all repaired
without touching a single answer, a single item's evidence or the question numbering: a carrier
emptied, a `table` rewritten as notes, a signpost re-grounded in the dialogue. A finding here that
proposes changing which point an item tests has mis-diagnosed itself.

**The script is never a fix.** Audible text is frozen once the material is final (SR-021). Where the
script is the cause, say so and put the repair on the page — a carrier limit, a different gap
position, a different item on the same evidence.

---

## 3. Reconstruction: How to Arrive at an Answer

Per item, in this order:

1. **Read the narration first and mark the two question-number windows.** Every item's evidence must
   lie inside its own window (SC-019 / AL-017). Narration is in the script you were given precisely
   so this is decidable.
2. **Read the printed line** — its group's heading and labels, the carrier before the gap, the
   carrier after it. That is what the candidate is holding.
3. **Find the decisive turn.** The turn that settles the answer, not merely the first that mentions
   the topic. A later confirmation turn is ordinary Part 1 writing and is a legitimate anchor.
4. **Write the answer you would write**, and take the shortest verbatim span from that exact turn as
   the quote. If nothing decisive exists, record an empty answer and raise it — SR-005 / AL-002,
   MAJOR. An answer invented to fill the field destroys the only product this review has.

   **Then read the index back off the array, and check that your quote is in *that* entry.** Not the
   turn you believe you were reading — the entry at the number you are about to write. These two must
   describe one sentence. A review is rejected and re-run when they do not, which costs a whole call, so
   spend the moment now.

   This is the one error that has actually happened, measured. A review counted the narration turn
   "Before you hear the rest of the conversation…" where the writer had not, and every index after it
   was one too high. The quotes were perfect and the answers all matched the key; the set was still
   rejected, and it took three stages to work out that nothing was wrong with the questions. An index
   that is one out does not read as a typo further down the pipeline — it reads as a claim that the
   answer lives in a different sentence than the writer says, which is a serious finding about the
   item. Do not make that claim by accident.

   Make the quote long enough to occur **once**. A span so short that it also appears in the
   neighbouring turn identifies no sentence at all, and no later check can recover which one you meant.
5. **Set confidence for a candidate hearing it once**, not for yourself with the text in front of
   you. `low` is itself worth a finding.
6. **Then** do §4 and §5, in that order. Rivals before leakage: the leakage scan needs the
   reconstructed answers to scan for.

---

## 4. Same-Level Rivals, and What Counts as One

A same-level rival is a value the script supplies at the **same level of the same structure** as your
answer — another price in the same price list, another day in the same list of days, another room in
the same set of rooms. Not any noun in the script.

For each rival: substitute it into the carrier, read the finished line, and decide whether the script
supports it **as decisively** as your answer.

- Excluded by the carrier → record it with `equally_supported: false` and say what excludes it. These
  entries are the evidence that the check was performed.
- Not excluded → `equally_supported: true`, and raise AR-012. Two equally supported answers is MAJOR
  by default (severity.md 2.5).

An empty rival list asserts that you enumerated and found none. It must never mean the step was
skipped.

Where the answer differs from a rival only by a limit the carrier does not state — a time, a subject,
a place — the defect is usually the carrier's under-specification (QR-010) rather than the answer, and
the fix names the limit to add.

---

## 5. Leakage from the Page

Scope is the **group**, not the line. Before the recording starts a candidate reads the group's whole
visible surface: its heading, every sub-heading, every navigation line, every row and column label,
every carrier, and every neighbouring item.

Scan that surface for each answer you reconstructed:

- the exact wording, case variants, and ordinary inflections — a plural, an `-ing`, an `-ed`. A
  heading reading *Parks* leaks the answer *park*;
- **a near-restatement of the same fact in other words.** This is the half a word-level scan cannot
  reach, and it is yours. A note heading *Moving in the spring* leaks the answer *March* as
  effectively as printing it.

Anything you can produce from the page alone: set `derivable_without_recording: true` on that answer
and raise QR-040 or SC-012. One item is MAJOR; a pattern across the set is CRITICAL
(severity.md 2.7).

---

## 6. Tolerances, by Dimension

A single overall tolerance is wrong in both directions — too loose lets a wrong answer pair with a
neighbouring turn, too tight rejects the standard "stated, then confirmed" pattern that Part 1
dialogue is built on. So each dimension is judged on its own.

| Dimension | Tolerance |
|---|---|
| The answer text | **Strict.** An answer is either right or it is not. |
| The factual proposition (AL-018: subject, object, place, time, relation) | **Strict.** A displaced proposition is an AL-018 failure regardless of how near the turns are. |
| Narrator window membership | **Strict.** SC-019 calls it an unbreakable structural boundary; evidence outside the item's window is AL-017. |
| The quote existing in the turn you named | **Strict, and machine-checked.** A quote that is not in that turn is AL-007, and the review is rejected and re-run rather than interpreted. |
| The **position** of the decisive turn | **±1, conditionally.** |

The ±1 applies only when all three hold **at once**:

1. the turn you anchored on is the **adjacent confirmation** of that fact — confirming the same fact
   in content, not merely sitting next to it;
2. it still supports the **same answer**;
3. it is still the **same factual proposition** and the **same narrator window**.

**Adjacency alone is never enough.** Two neighbouring turns that support different answers are two
different facts; pairing them because they are neighbours turns a real defect into a pass, and that
failure is silent.

**The ±1 is for a genuine confirmation turn, not for a miscounted index.** If your quote turns out to
sit in the turn next to the one you wrote down, you have not used the tolerance — you have mis-stated
where your evidence is, and §3.4 is where that gets fixed. The tolerance exists for the case where you
deliberately anchored on the confirming turn and said so.

---

## 7. Grading

Severity is by real impact, never by how much work the fix is (severity.md 2).

| Situation | Default |
|---|---|
| Two equally supported answers | MAJOR |
| No script evidence for a gap at all | MAJOR; CRITICAL if it runs through the set |
| Candidate-visible text exposes an answer | MAJOR for one item; CRITICAL when systematic |
| Structure broken — group not one real form/note/table, or item evidence outside its own window | MAJOR |
| A `table` with no real comparison axis (pseudo-table) | MINOR; MAJOR when the false structure leaves a cell unanswerable or dominates the whole page |
| Label and carrier printing the same field twice | MINOR; MAJOR when the duplication is every row of the group |
| A signpost that names nothing (generalised metadiscourse) | MINOR; MAJOR when no window has a real one, since the set then has no navigation at all |
| Layout mixing that does not follow the dialogue | MINOR |
| Avoidable spelling burden with an equally valid low-risk alternative | MINOR; MAJOR if it alone makes fair marking unreliable, or if several items together shift the construct towards orthography |
| Spelling, punctuation or formatting that does not change how an answer is marked | MINOR |
| Wording that is merely less natural, the original being correct and clear | INFO at most |
| A rule you evaluated that does not apply | not a finding — state `not_applicable` |

One problem affecting several items may go up one step, never beyond its real impact. Name the items
it reaches in `affected_questions` so the claim can be read against the spread.

`question_qc_status`, from **unresolved** findings only (severity.md 3.2):

```text
if CRITICAL > 0 or MAJOR > 0:  FAIL
elif MINOR > 0:                WARNING
else:                          PASS
```

`INFO` and `ADVISORY_WARNING` are counted in `summary.counts` and change nothing. Findings marked
`resolved`, `waived` or `not_applicable` are outside the count. **You may never waive your own
finding** — `waived` is available only when the request carries an explicit authorisation.

`content_review_readiness` is a separate question: can a human reviewer read these items, their
numbering, their gaps and their instructions. A set can be readable and still `FAIL`. Merging the two
is how an unreadable draft gets called ready.

`visual_qc_status` is `NOT_RUN` and `visual_findings` is empty, always. You are reading a script and a
text page; typography and pagination were never in front of you. A layout problem that actually hides
required content is a content finding under QR-015 instead.

---

## 8. What Is Not a Finding

- **Anything already counted.** Item count, printed numbering, gap-position distribution, gap-final
  totals, numeric and spelling-answer tallies, category repetition, window membership, group
  contiguity, word limits, the one-token/multi-token answer tiers, quote presence, evidence
  monotonicity, word-level leakage. All decided before you were called, and re-litigating an
  arithmetic result adds noise the writer has to read past.

  This includes the **gap-final total**: `question_metrics` already carries the distribution, and the
  7-of-10 cap is enforced upstream. What is yours is dimension 3's neighbours — whether a line's gap
  position is *natural* for that line — never the tally. "6 of 10 gaps are line-final" is not a
  finding; "this row was inverted into a sentence purely to move its gap, and now reads oddly" is.
- **A layout you would personally have chosen differently.** Dimensions 13 and 15 are about the page
  contradicting itself — a grid with nothing to compare, a shape that changes where the dialogue does
  not. A form that could arguably have been notes, where both would read naturally, is INFO at most.
- **A preference between two correct phrasings.** INFO at most, and usually nothing.
- **A word being uncommon.** Only an avoidable burden with a demonstrated equally valid alternative
  in the same evidence is QR-043.
- **A clean item.** Record the reconstruction and move on. An invented finding costs a rewrite of
  something that was already right, and it teaches the writer to discount the next report.
- **A visual remark.** See §7.
