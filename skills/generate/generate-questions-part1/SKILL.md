---
name: generate-questions-part1
description: Write the ten IELTS Listening Part 1 completion items for an already-finalised material, using the ten information points its blueprint already fixed. Returns one JSON question package in three physically separated blocks - the candidate-visible question face, the answer key with accepted alternatives and word limits, and per-item verbatim script evidence. Supports form, note and table completion, one layout or a mix. Use when a material has been generated, validated, audited and passed the question-feasibility preflight. Do not use to write or revise the recording script, to choose different information points, or for Listening Parts 2-4.
---

# Write the Part 1 Questions for a Finalised Material

You are given a finished `material.json` and its `blueprint.json`, and you produce **one** question
package: ten completion items, their answer key, and the script evidence for each.

Seeing the blueprint is normal here — you are on the generation side and cross no boundary. What you
must not do is treat it as a menu.

## Required Reference

**Paths here are relative to this skill's own directory, and `file_read` does not resolve them for
you.** The `Location:` line at the end of these instructions gives this file's absolute path; strip
`SKILL.md` from it and prefix every path below with what remains. A bare `references/...` resolves
against the process working directory and returns "No files found" — measured.

Read both of these before writing anything:

- `references/question-rules.md` — the authoring rules, the two-layer taxonomy, and the AR-003
  tiering. It is authoritative: writing from memory of what an IELTS form looks like produces a
  package the validator rejects.
- `schemas/question_package.schema.json` — the output contract, including which field belongs on
  which layer.

Read nothing else. Everything you need is in this skill's own directory and in the request.

## Five Boundaries — these are prohibitions, not preferences

1. **Never change the audible script.** Not one word, not the turn order, not a number. The
   recording already exists, or is about to be produced from this exact text; a question that would
   work better against a different script is a question that does not work (SR-021). Write the items
   to fit the recording, never the reverse.

2. **Use all ten blueprint points, in the blueprint's numbering and evidence order.** They are given
   input, not a candidate pool: do not drop one, do not swap one for a detail you like better, do not
   reorder them, and do not change a point's `answer_category`. The answer-variety balance of exactly
   these ten was already checked and approved by the feasibility preflight, so a substitution here
   silently invalidates that approval. If you believe a point genuinely cannot carry a reliable item,
   say so in your reply and stop — the only compliant remedy is a new material, and it is not yours
   to apply.

3. **Part 1 has one question type: completion.** `form`, `note` and `table` are its layouts, not
   separate types. No multiple choice, no matching, no short answer, no map labelling.

4. **Never write an answer, a quote or a turn index into the question face.** The three blocks are
   separated physically because a single object holding all three eventually gets passed whole to a
   blind auditor, and an audit that has seen the answer key returns a score that is merely too high:
   nothing errors and nothing in the delivered package shows it happened.

5. **Never print the answer in the candidate's own reading matter.** Titles, signposts, row and
   column labels, note headings, every carrier and every neighbouring item are all visible before the
   recording starts. An answer that appears there — including as a plural or an ordinary inflection —
   makes the item answerable without listening (QR-040).

## Workflow

1. Read the rules reference and the schema. Read the material's narration and note where it tells
   candidates which questions to look at: those are the question-number windows.

2. **Choose the candidate-visible groups**, keeping the existing number order. First test whether
   all ten points form one natural Form, Note, or Table. If they do, use one group for Q1-Q10 even
   though its evidence crosses the midpoint cue. Split only where the visible record structure
   genuinely changes. The constraints are
   relational, and all of them are checked:
   - each group is homogeneous — one `layout`, declared once on the group;
   - narrator windows constrain each item's decisive evidence, not the printed layout boundary:
     a natural continuous group may span a window cue, but Q1-5 evidence must remain in the first
     window and Q6-10 evidence in the second (SC-019 / QR-022 as clarified);
   - group question numbers are contiguous, and a group's points must not be interleaved with
     another group's in the evidence order;
   - all ten items belong to a group; no floating point.

   Group **count is not pre-set or preferred**. It may be one, two, or three, derived from the
   candidate-visible task and record structure rather than the narrator split or a desire for
   variety. A package may use one continuous layout or mix form, note and table as long as every
   group is itself homogeneous.

   **The script's own structure picks each layout**, and it is the only thing that does:
   - **form** — the dialogue fills in a record field by field (name, date, number, selection);
   - **note** — hierarchical or narrative information: a topic with points under it, preferences
     talked through. This is the fallback when neither of the other two fits;
   - **table** — only where both axes carry real meaning and each column compares like with like down
     its length. Under the current schema, use exactly one content column because there is no
     question-to-cell mapping. A filler heading
     such as `Detail` / `Notes` / `Information`, repeated row/cell wording, or unrelated facts placed
     behind borders are warning signs that the content is really a form or note. The auditor judges
     the printed structure as a whole.

     Name the left row-label column with `structure.row_header_label`; put only the content-column
     headings to its right in `structure.column_labels`. Use one row-header label and exactly one
     content-column label. Declaring two content columns is invalid until every question can identify
     its cell.

   Do not alternate layouts to look varied. Mix them only where the script really changes mode.

3. **Write the carriers and budget the blank positions.** Keep end-of-line blanks at seven or fewer
   and let all three positions appear (QR-026) — but from lines that are genuinely written
   differently, not by bending a form row out of shape to fill a quota. Position is judged by content
   words either side of the blank, not by character offset: a bare labelled `Name | ____` counts as
   final. A form row label, or a table's real row and column labels, is context; do not add filler
   carrier text merely to make one of those fields non-empty. A blank with neither carrier text nor
   structural labels is still invalid.

   In a form group the **row label and the carrier print side by side, so they may not say the same
   thing**. The label names the field; the carrier adds only what the line needs beyond its name — a
   unit, a short qualifier, or nothing. Do not turn a form value into an instruction sentence:
   `Surname | Please record ____ for correspondence` is not a natural form row. `row_label: Arrival date` with
   `carrier_before: "Arrival date:"` prints the words twice; leave `carrier_before` empty instead. A
   near-synonym restatement (`Family name` / `Surname:`) is the same defect. Parentheses are allowed
   only when removing them would create a real ambiguity or lose a source-supported record-format
   limit. They must be natural, non-redundant, non-leaking and not commentary on speaking, spelling
   or answering: `(day and month)` may be useful; `(as spelt)` and `(as mentioned)` are not.

4. **Write the titles and signposts.** As this project's paper convention, every group gets a short,
   specific, non-leaking scenario title in **capitals** — `HOTEL BOOKING`, `ARRIVAL AND FACILITIES`.
   QR-031 makes this a customer requirement for note groups; applying it to form and table groups is
   the project's consistency rule. The title carries no layout name, question range or narrator
   window number. Also by project convention, a note group's `structure.hierarchy` goes at most one
   level under the title (main item + sub-items), with concrete names taken from the conversation.
   Write `structure.note_sections` with one object per visible heading. Each object carries
   `heading` and the exact `question_numbers` rendered immediately below it. Together the sections
   cover every question in the group exactly once; never emit a detached heading list.

   `signposts` is audit/navigation metadata, not candidate-facing prose. Every window needs at least
   one blank-free, specific, script-grounded navigation line (QR-026),
   naming **what is talked about** at that point: "The receptionist goes through what the rate
   includes" locates the candidate; "Details are confirmed" or "Information is given" does not. If the
   line could be copied unchanged onto a different material, it is not a signpost.

5. **Write the answer key.** The canonical is the blueprint item's target, in the script's own
   wording. AR-003 applies in tiers, and **which tier applies is decided by tokenising the canonical
   you actually wrote, not by the word limit you printed**: a one-token answer must match one
   complete token of the decisive evidence, with no substring credit in either direction; a
   multi-token answer needs every one of its words present in that evidence. A hyphenated compound
   counts as one word but must stay whole — do not pre-fill `eco-` in the carrier and leave the
   candidate the second half (AR-014). Add `alternatives` only where a variant is genuinely
   equivalent in this context and satisfies the same limit; empty is a normal answer.

6. **Choose each group's `word_limit`.** There is no default. Per group, take the **strictest**
   standard rubric that every one of that group's canonicals satisfies, print it verbatim inside
   `instruction_text`, and restate it on each of that group's answer-key entries. A looser rubric
   than the answers need accepts responses your own key marks wrong.

   `instruction_text` uses the **standard IELTS wording only** — the layout sentence plus the rubric
   sentence, and nothing describing the content:

   ```
   Complete the form below. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.
   Complete the notes below. Write ONE WORD ONLY for each answer.
   Complete the table below. Write NO MORE THAN THREE WORDS for each answer.
   ```

   `Complete the booking record.` and `Complete the hotel information table below.` are both wrong.
   The layout word must be the group's own `layout`, with `note` printed as `notes`. Exactly one
   instruction per group, at the head of the group; never repeated above an item, in a signpost, or in
   a title.

7. **Write the evidence**: the turn index, the shortest sufficient verbatim quote from that turn, the
   window, how the carrier relates to it, and the AL-018 alignment fields. The decisive evidence may
   be a confirmation turn rather than the first mention — that is ordinary Part 1 writing — but it
   may not leave the item's window, and the ten indices must advance with the question numbers.

8. **Run the validator yourself. This step is yours, not the caller's.**
   - Write the package to a `.json` file **inside the working directory the request gives you**. Do
     not write to `/tmp` directly: other requests run at the same time, and a shared filename means
     reading back someone else's file.
   - Write it in ONE `shell` call, with a quoted heredoc, and check it parses before going on.
     **Spell the directory out in full every time.** Do not define a shell variable for it: measured,
     an agent that wrote `WORK=/path | cat > $WORK/material.json` used a pipe instead of `;`, so
     `$WORK` was empty in the second command and three calls failed before it gave up on the variable.

     ```
     cat > /the/absolute/working/directory/questions.json <<'EOF'
     ...the complete question package JSON...
     EOF
     python3 -m json.tool /the/absolute/working/directory/questions.json > /dev/null && echo PARSE_OK
     ```

     Emit the JSON exactly as it will appear in your reply. If `PARSE_OK` does not appear, rewrite
     the whole file rather than patching it with `sed`: a patch that half-applies leaves the file
     broken in a new way.
   - Run the validator with `shell`, using the absolute skill path from `Location:` and the material
     and blueprint paths from the request:

     ```
     python3 <skill dir>/scripts/validate_questions_part1.py <workdir>/material.json \
         --blueprint <workdir>/blueprint.json --questions <workdir>/questions.json
     ```
   - Read its output and fix every error, then run it again. Repeat until it reports no errors, or
     until further attempts stop making progress — a package the validator still complains about is
     delivered with those complaints attached, so a stuck loop is worse than an honest report.
   - Warnings are advice, not blockers. Do not restructure a compliant package to clear one.
   - Recomputed item/evidence fields are the usual first failures: `blank_position`,
     `response_form`, `narrator_window_id` and `numeral_allowance` are all re-derived and compared,
     so when one is reported, change the declaration to the computed value rather than arguing with
     it — or change the carrier, if the computed value is the one you did not want. A group spanning
     both windows omits the legacy group-level `narrator_window_id`.

9. After the validator is clean, reread the whole question face **without** the answer key beside it
   and ask the question a candidate would: can each blank be filled from the recording, and can any
   of them be filled without it?

## Output Rules

**Reply with ONE JSON object conforming to `schemas/question_package.schema.json`:**

```
{"reference": "Part 1", "test_package": "...", "material_id": "...",
 "question_face": {"instructions": [...], "groups": [...], "questions": [...]},
 "answer_key": [...],
 "evidence": [...]}
```

All three blocks are required, and the caller reads only your reply — files you wrote while working
are scratch space on a container that is discarded, so an artifact left in the working directory is
an artifact that was never delivered.

- Return no Markdown fences, introduction, explanation, rendered form/table, or quality report
  around the JSON. Only the object is read; prose around it is discarded, and a fenced or annotated
  reply is a failed call.
- Write **only** the keys the schema names. An extra key means you are answering against a different
  contract than the one the caller reads, and the reply is rejected.
- Keep the three blocks separate. No answer, alternative, quote or turn index anywhere inside
  `question_face`; no carrier text inside `answer_key`.
- Declare item/evidence `blank_position`, `response_form`, `narrator_window_id` and
  `numeral_allowance` honestly. They are recomputed, so a convenient value is reported rather than
  believed. Omit the optional group-level `narrator_window_id` when that printed group spans both
  windows.
- Do not expose hidden chain-of-thought. The evidence fields are the reasoning that gets delivered.
- Do not claim official IELTS or Cambridge approval, and do not reproduce proprietary question
  papers.
