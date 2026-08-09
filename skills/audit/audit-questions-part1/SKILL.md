---
name: audit-questions-part1
description: Audit the ten IELTS Listening Part 1 completion items written for one material, working from the complete script, the candidate-visible page and pre-calculated counts only. Rebuilds every answer, its decisive evidence and its same-level rivals independently, then judges answer uniqueness, paraphrase fidelity, proposition alignment, semantic leakage, grammatical fit after insertion, gap meaningfulness, naturalness and register, spelling burden, whether the ten really form one form/note/table, and whether the script gives its own answers away. Returns one structured JSON review covering items 1-10 with per-item and group findings, a QC status and a declared coverage set. Use when a set of Part 1 items needs quality review before human sign-off. Never accepts a supplied answer, an accepted-alternatives list, a quotation table, a turn index or the writer's item plan.
---

# Audit IELTS Listening Part 1 Questions

Review the ten completion items written for one already-finalised material, and return the result as
one JSON object.

## Blind Reading Requirement

You receive exactly three things: the **complete script including its narration**, the
**candidate-visible page**, and **pre-calculated counts**. Nothing else exists in the request.

You are not given, and must not ask for, accept, infer from, or go looking for: the writer's answer for
any item, its accepted-alternatives list, the quotation the writer anchored it to, the turn index the
writer recorded, or the item plan the material was written against.

**If any of those appears anywhere in the request, stop.** Do not review the set. Reply with a JSON
object whose only content is the leak: what appeared, and where. Then stop.

This is stricter than it looks, and the reason is the failure mode. A review that has seen the answers
does not break — it agrees. Every reconstruction matches, every uniqueness check passes, the status
comes out clean, and the document is byte-for-byte the shape of a genuine review. Nothing in the output
would ever tell you. So the boundary cannot be a preference exercised carefully; it has to be a refusal.

**What is withheld is the answer, not the standard.** The rules state openly what a sound item looks
like — one recoverable answer, one factual proposition shared with its evidence, nothing on the page
that gives the answer away. Judging against that is your whole job. What you are not given is what
*this* writer decided: that item 4's answer is `Tuesday`, anchored at turn 19. Being told would end
your ability to judge whether a candidate could recover it — and, just as importantly, whether they
could get it without listening at all.

Do not go looking on the filesystem either. Everything you need is in this skill's own directory. You
have no way to run a command, and that is deliberate rather than incidental.

## Required Reference

**Paths here are relative to this skill's own directory, and `file_read` does not resolve them for
you.** The `Location:` line at the end of these instructions gives this file's absolute path; strip
`SKILL.md` from it and prefix every path below with what remains. A bare `references/...` resolves
against the process working directory and returns "No files found" — measured.

Read both before reviewing anything:

- `references/question-audit-rules.md` — the fifteen judgements, the reconstruction procedure, the
  per-dimension tolerances, and the grading table.
- `schemas/audit_questions.schema.json` — the output contract.

Read nothing else.

## Workflow

1. **Read the narration and mark the two question-number windows.**
   The narration is in the script for exactly this reason. Every item's evidence must lie inside its
   own window (SC-019 / AL-017), and a group may not straddle windows. Note where each window opens
   and closes before you look at a single item.

2. **Take the counts as given. You do not run this step, and you cannot.**
   The request already carries the item count, printed numbering, gap-position classes, gap-final
   total, numeric and spelling-answer tallies, category counts, window membership and group structure.
   Use those numbers as they stand. You have no shell: an auditor able to run commands could read the
   writer's answers off the filesystem, and a review inflated that way looks entirely normal. Do not
   recount by eye either — a number asserted without calculating it is the one most likely to be wrong,
   and here the calculation is already done. When a count is absent, say so rather than estimating it.

3. **Read the page as a page, before any item.**
   Group by group: its heading, its sub-headings, its navigation lines, its row and column labels, its
   carriers in printed order. Ask whether these ten lines are really one form, one note and one table —
   or ten unrelated sentences that happen to validate (SC-015 / QR-026). You will need this reading
   again in step 6, and it is much harder to do honestly once you know the answers.

   Four things are decidable here, from the page alone, before you know a single answer — take them
   now while the reading is still innocent (rules §2, dimensions 12–15):
   - **a label and its carrier naming the same field twice** — read the row exactly as printed:
     `Arrival date | Arrival date: ....`, or the near-synonym `Family name | Surname: ....`
     (SC-015 / QR-026 natural record structure);
   - **parenthetical filler rather than record content** — keep a necessary, source-consistent unit
     or scope limit such as `(day and month)`, but flag commentary on speaking, spelling or answering
     such as `(as spelt)` / `(as mentioned)` under naturalness. A labelled form field needs no
     carrier merely to stop the two carrier strings being empty;
   - **a `table` with no real comparison axis** — judge both axes and whether each column compares
     like with like. A two-column table can be valid; column count and filler headings such as
     `Detail` / `Notes` / `Information` are warning signs, not verdicts (SC-015 / QR-026);
   - **a navigation line that names nothing** — apply the transfer test: could it be copied unchanged
     onto a completely different Part 1 material? "Details are confirmed" can (QR-034);
   - **layouts alternating where the dialogue does not change mode** (SC-015).

   Also read what is *on* the page that should not be: a layout or question-type badge, a narrator
   window number, a group id, an internal range other than the instruction's own `Questions n–m`.
   Those belong to the internal audit region. Keeping them off the candidate face is this project's
   paper convention; report them as presentation findings rather than inventing a customer rule id.

4. **Rebuild each answer independently.** The core product.
   For each item: find the turn that *settles* it — which may be a later confirmation rather than the
   first mention — write the answer you would write, and quote the shortest span that occurs in that
   turn and **only** that turn. Then read the index back off the turns array and confirm your quote is
   in the entry at that number: the quote and `turn_index` must name one sentence, this is checked
   mechanically, and a mismatch costs the whole review a re-run (rules §3.4).
   Set confidence for a candidate hearing the recording once, not for yourself with the
   text in front of you. Where no decisive evidence exists, record an empty answer and raise it
   (SR-005 / AL-002): an invented answer destroys the only thing this review produces.

5. **Enumerate the same-level rivals and substitute each into the carrier** (AR-012 / QR-010).
   Another price in the same price list, another day in the same list of days — values the script
   supplies at the same level of the same structure, not any noun in it. Substitute, read the finished
   line, and decide whether the script supports the rival *as decisively* as your answer. Record the
   ones the carrier excludes together with what excludes them: those entries are the evidence that the
   step happened. Checking only that your own answer fits proves nothing.

6. **Scan the group's whole visible surface for the answers you rebuilt** (QR-040 / SC-012).
   Exact wording, case variants, ordinary inflections — and near-restatements of the same fact in
   other words, which is the half no word-level scan reaches. Note the structural advantage here: you
   have no supplied answer, so you can only scan for what you reconstructed, and anything you can
   produce from the page alone was free to the candidate too. That makes this simultaneously the
   leakage check and the check on whether the answer needed the recording at all.

7. **Judge the remaining dimensions, per item.**
   Paraphrase fidelity (AL-004 / QR-024); proposition-level alignment (AL-018); grammatical and
   semantic fit once your answer is written into the gap (QR-009 / AL-015); whether the gap takes a
   meaningful unit of information (QR-010); naturalness, register and unnecessary lexical difficulty
   (LG-001/002/003/005/015); spelling burden (QR-043 — word rarity is a triage signal for a human,
   never an automatic verdict); single interpretation, controllable locating burden, no transcription
   order (QR-003 / QR-034 / QR-037); and whether the script signposts its own answers or supports two
   mutually exclusive ones (SR-006 / SR-007).

   Gap position belongs here only as naturalness, never as arithmetic: the distribution and the
   7-of-10 cap arrived with the counts. "6 of 10 gaps are line-final" is not a finding; "this row was
   inverted into a sentence purely to move its gap, and now reads oddly" is.

8. **Write the findings.**
   One rule id per finding, from SC / QR / AR / AL / LG / SR. Each carries the shortest evidence that
   shows the defect with the turn index or printed line it sits on, and the smallest concrete fix
   naming the line and the edit: "improve the pacing" is not a fix, "add *before Friday* to the carrier
   on row 3 so the later date cannot fill the gap" is. Put a defect whose scope is the group in
   `group_findings` rather than repeating it per item — reporting one defect ten times makes it look
   like ten. **The script is never a fix:** audible text is frozen once the material is final (SR-021),
   so where the script is the cause, say so and put the repair on the page.

9. **Compute the status and declare coverage.**
   `question_qc_status` from unresolved findings by the reference file's algorithm. Write out
   `coverage.reviewed_question_ids` explicitly, and give a reason for anything unreviewed — a review
   that quietly stopped at nine items reads exactly like a clean one. `visual_qc_status` is `NOT_RUN`
   and `visual_findings` is empty: you were reading a script and a text page, and typography was never
   in front of you.

## Output Rules

- Return one valid JSON object conforming to `schemas/audit_questions.schema.json`, and nothing else:
  no Markdown fences, no commentary before or after it.
- `reconstructed_answers` is the load-bearing product, not a by-product of the findings. It is compared
  item by item against the writer's key by a separate deterministic step, which is what makes a
  non-recoverable answer visible — so it must be complete and it must be yours.
- Report a clean item as clean. A set with no defects is a real result; an invented finding costs a
  rewrite of something already correct and teaches the writer to discount the next report.
- Never mark your own finding `waived`. That state is available only when the request itself carries an
  explicit authorisation.
- Do not claim a count you were not given, and do not recompute one you were.
- Do not expose hidden reasoning; give concise evidence and conclusions.
- Do not claim official IELTS or Cambridge approval.
- This review covers the printed items against the script. It says nothing about audio production,
  document rendering, or whether a human has approved the content.
