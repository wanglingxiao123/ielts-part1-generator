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

2. **Cut the ten points into groups**, keeping the existing number order. The constraints are
   relational, and all of them are checked:
   - each group is homogeneous — one `layout`, declared once on the group;
   - each group sits **entirely inside one window**; it may not straddle or merge windows
     (SC-019 / QR-022);
   - group question numbers are contiguous, and a group's points must not be interleaved with
     another group's in the evidence order;
   - all ten items belong to a group; no floating point.

   Group **count is not pre-set**: one window may hold several consecutive groups, and one package
   may mix form, note and table as long as each group is itself homogeneous. Let the script's own
   structure decide — personal details are a form, preferences discussed in prose are notes,
   requirements compared along two axes are a table.

3. **Write the carriers and budget the blank positions.** Aim for all three positions across the ten
   items and keep end-of-line blanks at seven or fewer (QR-026). Position is judged by content words
   either side of the blank, not by character offset: a bare `Name: ____` counts as final. No blank
   may sit with no context on either side.

4. **Write the titles and signposts.** A note group must have a short, specific, non-leaking
   scenario title (QR-031). Every window needs at least one blank-free, specific, script-grounded
   navigation line (QR-026) — "Requirements for the new home are discussed next" locates the
   candidate; "Information" does not.

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
   - Recomputed fields are the usual first failures: `blank_position`, `response_form`,
     `narrator_window_id` and `numeral_allowance` are all re-derived and compared, so when one is
     reported, change the declaration to the computed value rather than arguing with it — or change
     the carrier, if the computed value is the one you did not want.

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
- Declare `blank_position`, `response_form`, `narrator_window_id` and `numeral_allowance` honestly.
  They are recomputed, so a convenient value is reported rather than believed.
- Do not expose hidden chain-of-thought. The evidence fields are the reasoning that gets delivered.
- Do not claim official IELTS or Cambridge approval, and do not reproduce proprietary question
  papers.
