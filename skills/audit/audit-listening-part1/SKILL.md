---
name: audit-listening-part1
description: Audit and quality-check IELTS Listening Part 1 scripts supplied as JSON, speaker-labelled turns, or plain text, returning a structured audit JSON with verdict, dimension scores, ranked findings, an independently reconstructed information map, and a specification-compliance review carrying concrete revision advice. Use when a user asks whether a generated Part/Section 1 material is compliant, requests acceptance review or proofreading, wants structural and linguistic defects ranked with evidence, or needs fixes to a listening script. Focus on the listening material itself; metadata, JSON structure, scenario fields, candidate questions, and answer keys are not required.
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

**What is withheld is the answer, not the standard.** The compliance checklist in the rubric states
openly what a compliant script contains — a self-correction, a spelling sequence, confirmed points.
Reviewing against that is your job. What you are not given is where *this* script put them: that a
self-correction sits at turn 8 replacing $45 with $39 is the answer, and being told it would end
your ability to judge whether a listener could recover it.

## Required Reference

**Paths here are relative to this skill's own directory, and `file_read` does not resolve them for
you.** The `Location:` line at the end of these instructions gives this file's absolute path; strip
`SKILL.md` from it and prefix every path below with what remains. A bare `references/...` resolves
against the process working directory and returns "No files found" — measured.

Read both of these before auditing:

- `references/audit-rubric.md` — the authoritative rubric, the compliance checklist (C1-C6), and the
  reporting contract.
- `schemas/audit.schema.json` — the output schema.

Read nothing else. Everything you need is in this skill's own directory; the generator's
specification and its information-point annotation are not here, and that is deliberate — see below.
Do not go looking for them elsewhere on the filesystem either. An audit built on the generator's
plan is worth nothing, and the failure is silent: the score simply comes out too high.

## Workflow

1. Establish the artifact.
   - Accept JSON, speaker-labelled turns, a transcript, or another readable text format.
   - Do not require JSON metadata, `scenario`, questions, answers, evidence tables, or quality-check fields.
   - Extract the narrator and two dialogue roles from labels or context.
   - If no usable listening script can be identified, report supported findings and use `NOT_ASSESSABLE`.

2. Take the deterministic metrics as given. **You do not run this step, and you cannot.**
   - The request already carries the output of `scripts/audit_metrics.py` — word counts, turn counts,
     half balance, narration length, and candidate spelling/numeric/correction markers. Use those
     numbers as they stand.
   - You have no way to run the script yourself: it executes in an isolated environment that holds
     only the material, because an auditor able to run commands could read the generator's plan off
     the filesystem, and a score inflated that way looks entirely normal. So do not attempt to
     execute it, and do not recount by eye — a metric you assert without calculating is the one most
     likely to be wrong, and here the calculation has already been done.
   - When a metric is absent from the request, say so rather than estimating it. An artifact that
     arrived as loose text may have no countable structure, and an invented count is worse than a
     missing one.
   - Treat the script's marker findings as evidence, then verify them by reading. Never accept a
     correction or an indirect confirmation solely because a marker phrase appears: `actually` in a
     sentence that corrects nothing is not a self-correction.

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

7. Review specification compliance (C1-C6 in the rubric).

   The second of your two outputs, and the one the generator revises against. Work through the
   checklist: everyday scenario, natural spoken register, plausible distraction, ordered and
   separable information, two-way rhythm, clear roles.

   - Judge only what a script cannot. Word counts, turn counts, half balance, speaker IDs and
     `turn_index` accuracy were settled in step 2; re-litigating an arithmetic result adds noise.
   - Every non-compliant item needs a concrete minimal fix naming the turn and the change. "Consider
     improving the pacing" is not a fix; "split turn 12 into two or three short exchanges" is.
   - Where the script is compliant, record it as compliant. A clean pass is a result, and inventing
     a finding to look thorough costs the generator a pointless rewrite.
   - Report this in `compliance_review`, not mixed into `findings`.

8. Record findings.
   - Order findings by severity and turn/information position.
   - Quote the shortest sufficient script evidence and give the `turn_index` where it sits.
   - State the violated rule and the smallest concrete fix.
   - Do not rewrite the full JSON unless requested.

9. Assign the verdict.
   - `PASS`: zero critical and zero major findings.
   - `PASS_WITH_MINOR_EDITS`: only minor findings.
   - `FAIL`: one or more critical or major findings.
   - `NOT_ASSESSABLE`: no usable script content can be identified.

10. Assign the overall score.
   - Score the script out of 100 using the rubric dimensions.
   - Apply severity caps so a script with a critical finding cannot score above 49 and one with any major finding cannot score above 69.

## Output Rules

Two products, in one JSON object:

1. **`blind_information_map`** — what a listener working from the script alone can recover. Consumed
   programmatically and compared against the generator's plan; the comparison is what makes a
   point nobody could recover visible.
2. **`compliance_review`** — the C1-C6 semantic review with actionable fixes. This is what the
   generator reads when it revises.

- Return one valid JSON object conforming to `schemas/audit.schema.json`. It is the
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
