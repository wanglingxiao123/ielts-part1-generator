---
name: generate-listening-part1
description: Generate and validate original text-only IELTS Listening Part 1 scripts as structured JSON containing metadata, a scenario field, speaker-labelled turns, and speaker count, plus a companion blueprint JSON marking the ten information points with turn anchors and question-type grouping. Use when a user asks for an IELTS Listening Section/Part 1 listening material, dialogue, recording script, or scenario-based practice transcript. Return only the listening material and its blueprint, without candidate questions, answer keys, or quality-check commentary. Do not use for audio production or academic Listening Parts 2-4.
---

# Generate IELTS Listening Part 1

Create an original, internally coherent Part 1 recording script and return two JSON artifacts:
the listening material and its information-point blueprint.

## Required Reference

**Paths here are relative to this skill's own directory, and `file_read` does not resolve them for
you.** The `Location:` line at the end of these instructions gives this file's absolute path; strip
`SKILL.md` from it and prefix every path below with what remains. A bare `references/...` resolves
against the process working directory and returns "No files found" — measured.

Read all three of these before writing anything:

- `references/specification.md` — the authoring constraints and the exact JSON contract. It is
  authoritative: drafting from memory of what an IELTS script looks like produces material the
  validator rejects.
- `schemas/material.schema.json`
- `schemas/blueprint.schema.json`

## Workflow

1. Resolve metadata and scenario.
   - Use the user's scenario and package/reference values when supplied.
   - Otherwise choose one realistic everyday scenario, set `test_package` to `Test 1`, and set `reference` to `Part 1`.
   - Write `scenario` as one concise Chinese or English sentence that identifies the participants, practical need, and setting.
   - Use the actual model identifier when known; otherwise use `unspecified`.
   - Set `extracted_at` to the current UTC time in ISO 8601 format.
   - For original material, set every `source_htmls` field to `[]`.

2. Build the ten-item blueprint.
   - Plan ten factual targets in strict order, divided into two groups such as 1-5 / 6-10.
   - Include at least one spelled name/proper noun, one numeric target, and one genuine self-correction.
   - A dialogue-internal indirect confirmation is optional. Add one only when it fits the scene naturally; if you do, its answer term must also be an item target and must be spoken in full before the phrase that refers back to it. The spec asks for 2-3 distraction cycles drawn from five mechanisms (self-correction, paraphrase, option trap, negation, qualifier) -- forcing a paraphrase into every material is not one of them, and only 4 of the 27 real papers contain one.
   - Mark at least three points as confirmed, including a spelled name and a numeric detail.
   - Plan question-type support: give each point an `item_form`, group comparable points with a shared `form_group` so one group holds 3+ points, mark at least 2 points `multiple_choice`, and record the same layout in `question_type_coverage`.
   - Use only 2-3 deliberate distractor-bearing cycles.
   - Save the blueprint as its own JSON file using the schema in the reference. It is a second delivered artifact, not a temporary sidecar.
   - Never hand the blueprint to the audit step; the auditor must rebuild an information map blind. Do not include questions, answers, or analysis anywhere.

3. Draft the recording script.
   - Use `speaker1` only for narration, `speaker2` for the service/information provider, and `speaker3` for the enquirer.
   - Use exactly these three speaker IDs and set `speaker_count` to `3`.
   - Follow this order: full test/scene introduction and first reading prompt; first dialogue half; midpoint reading prompt; second dialogue half; closing check prompt.
   - Make answer-bearing information follow the private target order without returning to earlier targets.
   - Keep narration free of answer content.
   - Aim for 600-650 dialogue words and 30-40 dialogue turns. These are the targets; 450 words and 20 turns are failure thresholds, not goals. Measured over the real past papers: the shortest dialogue is 477 words and the median is 605, so a draft near 450 is shorter than every real paper and will be rejected. Write past the target rather than up to it. At least 8 dialogue turns in each half.
   - Narration: 160-230 words when `narration_mode` is `full`, 70-110 when `short`. Every real full narration runs 160-231 words, so the whole-test preamble has to be quoted in full rather than paraphrased.
   - Use short, natural, polite turns and everyday English.

   - Set each item's `turn_index` to the **zero-based position in the material's `turns` array**, counting the narration turns. The opening narration is turn 0, so the first line of dialogue is turn 1. Copy the index from the array rather than counting by eye: an anchor one position off puts a reviewer's annotation beside the wrong sentence, and it is the single most common reason a draft is rejected. `evidence` must be a verbatim substring of that exact turn's text.

4. Validate semantics privately.
   - Check that all ten private targets are unique and recoverable.
   - Check that answer terms occur explicitly before any indirect reference to them.
   - Check spelling, numeric details, correction, dialogue-internal paraphrase, confirmation, and distractor density.
   - Perform an adversarial distractor census over every dialogue turn. Count every self-correction, rejected alternative, option comparison used to select an answer, negation/exclusion, and condition that changes applicability.
   - Ensure the census contains exactly 2-3 cycles and matches the items marked `distractor: true` in the blueprint. Treat an unmarked cycle as a validation failure and revise the script.
   - Check that no dialogue turn carries more than one private target and that no long provider turn crowds several potential answers.
   - Do not expose this analysis in the result.

5. Validate the JSON, and fix what it reports. **This step is yours to run, not the caller's.**
   - Write the material and the blueprint to two separate `.json` files under `/tmp`.
   - Write each file in ONE `shell` call, with a quoted heredoc, and check it parses before going on:

     ```
     cat > /tmp/material.json <<'EOF'
     ...the complete material JSON...
     EOF
     python3 -m json.tool /tmp/material.json > /dev/null && echo PARSE_OK
     ```

     Emit the JSON exactly as it will appear in your reply. Measured: writing it by hand cost six
     failed `shell` calls in a row, all spent repairing one malformed object — `{"speaker1","text":
     ...}` instead of `{"speaker": "speaker1", "text": ...}`. Every turn object needs both keys
     spelled out. If `PARSE_OK` does not appear, rewrite the whole file rather than patching it with
     `sed`: a patch that half-applies leaves the file broken in a new way.
   - Run the validator with `shell`, using the absolute skill path from `Location:`:
     `python3 <skill dir>/scripts/validate_part1.py /tmp/material.json --blueprint /tmp/blueprint.json`
   - Read its output and fix every error, then run it again. Repeat until it reports no errors, or
     until further attempts stop making progress — a script the validator still complains about is
     delivered with those complaints attached, so a stuck loop is worse than an honest report.
   - Warnings are different from errors: they report a word or turn count outside the typical
     600-650 / 30-40 band while still inside the hard limits. Treat them as advice, not blockers,
     and do not rewrite a compliant script merely to hit the typical band.
   - The most common error is an off-by-one `turn_index`. When you see it, copy the index out of the
     `turns` array rather than recounting by eye.
   - Keep the blueprint a separate file; the delivered material JSON must not embed it.
   - After the validator is clean, reread the complete script once *without* relying on blueprint
     labels and repeat the semantic checklist in step 4.

## Output Rules

**Reply with ONE JSON object carrying both artifacts under these exact keys:**

```
{"material": { ...conforms to schemas/material.schema.json... },
 "blueprint": { ...conforms to schemas/blueprint.schema.json... }}
```

Both keys are required, and the caller reads only your reply — files you wrote while working are
scratch space on a container that is discarded, so an artifact left in `/tmp` is an artifact that
was never delivered. Measured: an earlier version of this section said "return the material JSON and
the blueprint JSON" without naming a container, and the agent replied with the material alone,
having written both to files. The two-key envelope is what makes that impossible to get wrong.

- Return no Markdown fences, introduction, explanation, questions, answer key, or quality report around the JSON.
- Keep the two artifacts separate. The material must not contain blueprint fields, and the blueprint must not contain questions or an answer key.
- Preserve turn order; store one spoken turn per `{speaker, text}` object.
- Do not add named-speaker fields. Speaker identities may be introduced naturally in turn text.
- Default to `full` narration. Use `short` only when the user explicitly requests an abbreviated standalone frame.
- Do not claim official IELTS/Cambridge authorship or reproduce proprietary scripts.
