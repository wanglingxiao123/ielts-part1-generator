---
name: audit-ielts-listening-part1
description: Audit and quality-check IELTS Listening Part 1 scripts supplied as JSON, speaker-labelled turns, or plain text, returning a structured audit JSON with verdict, dimension scores, ranked findings, and an independently reconstructed information map. Use when a user asks whether a generated Part/Section 1 material is compliant, requests acceptance review or proofreading, wants structural and linguistic defects ranked with evidence, or needs fixes to a listening script. Focus on the listening material itself; metadata, JSON structure, scenario fields, candidate questions, and answer keys are not required.
---

# Audit IELTS Listening Part 1 Material

Perform an evidence-based audit of the listening script, regardless of its container format,
and return the result as structured JSON.

## Blind Reading Requirement

Audit the script **only**. If a generator's blueprint, item list, or information-point
annotation is available, do not read it and do not accept it as input — build the information
map yourself from the script alone.

This is the point of the audit. Two independently produced maps can be compared
programmatically, and a point you cannot recover from the script is a genuine defect: a
candidate hearing the recording once will not recover it either. Reading the generator's
labels first would collapse that check into agreement, and the failure mode is silent —
the score simply comes out too high, with nothing to flag it.

## Required Reference

Read [references/audit-rubric.md](references/audit-rubric.md) completely before auditing. Treat it as the authoritative rubric and reporting contract.

## Workflow

1. Establish the artifact.
   - Accept JSON, speaker-labelled turns, a transcript, or another readable text format.
   - Do not require JSON metadata, `scenario`, questions, answers, evidence tables, or quality-check fields.
   - Extract the narrator and two dialogue roles from labels or context.
   - If no usable listening script can be identified, report supported findings and use `NOT_ASSESSABLE`.

2. Run deterministic checks.
   - For supported JSON containing script turns, run `python3 scripts/audit_metrics.py <material.json>`.
   - Use `--json` for structured metrics. For other formats, calculate only metrics that can be supported reliably.
   - Treat script findings as evidence, then verify them manually.
   - Never accept a correction or indirect confirmation solely because a marker phrase appears.

3. Identify the script and roles.
   - Treat container fields and metadata as contextual information, not compliance requirements.
   - Do not deduct points for missing `scenario`, non-JSON input, source provenance fields, or unrelated wrapper fields.
   - Confirm that the material contains one narrator role and two dialogue participants, even if their labels differ from `speaker1`, `speaker2`, and `speaker3`.

4. Audit the script.
   - Confirm that the narrator is narration-only and that the provider/enquirer roles remain coherent.
   - Check the opening/dialogue/midpoint/dialogue/closing sequence, `once only`, two balanced halves, word and turn counts, and narration length.
   - Infer and report the practical scenario from the script when no scenario summary is supplied.

5. Build an information map.
   - Identify approximately ten plausible recordable details in their first-occurrence order.
   - For each, quote concise evidence, classify its type, record the `turn_index` it sits at, and note whether it is clear, confirmed, corrected, indirect, or potentially ambiguous.
   - Do not invent questions or an answer key. The map measures whether the script can support later item writing.
   - Require 8-10 usable details, normally 10. Verify strict first-occurrence order, separate micro-cycles, at least four detail types, and alignment with the narrator's two question ranges.
   - Also judge whether the details could be organised into real question types: a set of comparable details supports a table or form, mutually exclusive options support multiple choice. Ten scattered gap-fills are a finding, not a pass — the material exists to support item writing.

6. Audit difficulty and naturalness.
   - Require at least one spelling sequence, numeric detail, clear final-value correction, and true dialogue-internal indirect confirmation.
   - Check that only 2-3 cycles contain deliberate distractors.
   - Flag information dumping, excessive traps, specialist vocabulary, implausible details, weak topic cues, or long monologues.
   - Explicitly verify the earlier-value -> replacement-marker + final-value chain and answer-term -> later indirect-reference chain.

7. Record findings.
   - Order findings by severity and turn/information position.
   - Quote the shortest sufficient script evidence and give the `turn_index` where it sits.
   - State the violated rule and the smallest concrete fix.
   - Do not rewrite the full JSON unless requested.

8. Assign the verdict.
   - `PASS`: zero critical and zero major findings.
   - `PASS_WITH_MINOR_EDITS`: only minor findings.
   - `FAIL`: one or more critical or major findings.
   - `NOT_ASSESSABLE`: no usable script content can be identified.

9. Assign the overall score.
   - Score the script out of 100 using the rubric dimensions.
   - Apply severity caps so a script with a critical finding cannot score above 49 and one with any major finding cannot score above 69.

## Output Rules

- Return one valid JSON object conforming to `shared/schemas/audit.schema.json`. It is the
  authoritative result; the human-readable Markdown report is produced from it by
  `shared/render_audit_report.py`, so do not write the report yourself.
- Use the underscore verdict spellings above so the value can be handled programmatically.
- Put word or turn counts that fall outside the typical 600-650 / 30-40 bands but inside the
  hard 450-750 / 20-48 limits in `warnings`, not `findings`. The typical values are observed
  averages across 20 real test sets, not authoring requirements.
- Distinguish deterministic results from editorial judgment.
- Never count narrator words or turns as dialogue.
- Do not claim exact metrics without calculating them.
- Do not expose hidden chain-of-thought; provide concise evidence and conclusions.
- Do not claim official IELTS or Cambridge approval.
- The verdict covers script readiness. Without actual questions, do not claim to have verified question wording, answer limits, options, or answer-key correctness.
