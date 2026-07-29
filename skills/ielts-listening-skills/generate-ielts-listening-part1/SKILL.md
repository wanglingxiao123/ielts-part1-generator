---
name: generate-ielts-listening-part1
description: Generate and validate original text-only IELTS Listening Part 1 scripts as structured JSON containing metadata, a scenario field, speaker-labelled turns, and speaker count, plus a companion blueprint JSON marking the ten information points with turn anchors and question-type grouping. Use when a user asks for an IELTS Listening Section/Part 1 listening material, dialogue, recording script, or scenario-based practice transcript. Return only the listening material and its blueprint, without candidate questions, answer keys, or quality-check commentary. Do not use for audio production or academic Listening Parts 2-4.
---

# Generate IELTS Listening Part 1

Create an original, internally coherent Part 1 recording script and return two JSON artifacts:
the listening material and its information-point blueprint.

## Required Reference

Read [references/specification.md](references/specification.md) completely before generating a script. It defines the authoring constraints and exact JSON contract.

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
   - Include at least one spelled name/proper noun, one numeric target, one genuine self-correction, and one dialogue-internal indirect confirmation whose answer term is also an item target.
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

5. Validate the JSON.
   - Save the material and the blueprint as separate `.json` files.
   - Run `python3 scripts/validate_part1.py <material.json> --blueprint <blueprint.json>`.
   - Fix every error. Warnings report a word or turn count outside the typical 600-650 / 30-40 band while still inside the hard limits; treat them as advice, not blockers, and do not rewrite a compliant script merely to hit the typical band.
   - Keep the blueprint a separate file; the delivered material JSON must not embed it.
   - After deterministic validation, reread the complete script once without relying on blueprint labels and repeat the semantic checklist.

## Output Rules

- Return the material JSON and the blueprint JSON, each conforming exactly to its reference schema (`shared/schemas/material.schema.json` and `shared/schemas/blueprint.schema.json`).
- Return no Markdown fences, introduction, explanation, questions, answer key, or quality report around the JSON.
- Keep the two artifacts separate. The material must not contain blueprint fields, and the blueprint must not contain questions or an answer key.
- Preserve turn order; store one spoken turn per `{speaker, text}` object.
- Do not add named-speaker fields. Speaker identities may be introduced naturally in turn text.
- Default to `full` narration. Use `short` only when the user explicitly requests an abbreviated standalone frame.
- Do not claim official IELTS/Cambridge authorship or reproduce proprietary scripts.
