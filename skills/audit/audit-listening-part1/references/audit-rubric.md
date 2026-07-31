# IELTS Listening Part 1 Material Audit Rubric

## Contents

1. Scope and accepted input
2. Severity model
3. Input handling
4. Script compliance
5. Information-map review
6. Verdict and report format

## 1. Scope and Accepted Input

Audit a text-only IELTS Listening Part 1 script supplied as JSON, speaker-labelled turns, or plain transcript text. The script is the assessed artifact. JSON, metadata, a `scenario` field, candidate questions, answer keys, evidence tables, and analysis are not required.

A meaningful audit requires a readable, non-empty listening script whose narrator and two dialogue participants can be identified from labels or context. If no usable script can be identified, use `NOT_ASSESSABLE`.

## 2. Severity Model

### Critical

- Empty or unusable script.
- Fewer or more than the required three roles, or dialogue uses more/fewer than two participants.
- The narrator participates in the conversation or answer-bearing information appears only in narration.
- Scenario is academic/specialist rather than Part 1, or no practical purpose exists.

### Major

- Dialogue is outside 450-750 words or 20-48 turns.
- Either half has fewer than 8 dialogue turns.
- Opening/midpoint/closing order is broken or `once only` is absent.
- Narrator, provider, and enquirer roles cannot be followed consistently.
- Fewer than roughly 8 usable factual details, preventing a ten-item Part 1 design.
- No natural spelling point, numeric detail, self-correction, or dialogue-internal indirect confirmation.
- Information order is incoherent, details are ambiguous, or multiple targets are crowded into long monologues.
- Deliberate distractors substantially exceed 2-3 cycles and raise difficulty beyond Part 1.
- Language is unnatural, specialist, or implausible for everyday interaction.

### Minor

- Narration is outside 160-230 words in `full` mode or 70-110 words in explicit standalone `short` mode, while remaining complete.
- Slight half imbalance, awkward phrasing, weak confirmation, or a topic cue that could be clearer.
- Nonessential metadata style inconsistency that does not break the contract.
- Fewer than three confirmed details, or no confirmation on a spelled name or numeric value.
- Details cannot be organised into varied question types: no set of comparable details able to
  support a table or form, or no mutually exclusive options able to support multiple choice.

### Not a finding

Dialogue outside the typical 600-650 words or 30-40 turns while inside the hard 450-750 /
20-48 limits belongs in `warnings`, not `findings`. Those figures are observed averages across
20 real test sets, not authoring requirements — a compliant 530-word script is acceptable and
must not be scored down for it.

## 3. Input Handling

Do not score the container format. Specifically:

- Accept valid JSON wrappers, extracted webpage data, speaker-labelled text, and plain transcripts.
- Do not require `scenario`, metadata, provenance, package/reference fields, or exact speaker IDs.
- Ignore unrelated wrapper fields unless they make the script ambiguous.
- Use named speakers or arbitrary IDs when their functions can be inferred consistently.
- Mention malformed structure only as an informational note when it risks losing, merging, or misattributing script content.
- If questions or answers are supplied, exclude them from the script audit unless the user explicitly asks to review them.

Infer the scenario from the script. If a supplied scenario summary contradicts the script, report the contradiction as a content-coherence finding, not a missing-field issue.

## 4. Script Compliance

### Frame

Confirm this order:

1. Narrator: full test explanation, scene introduction, and first question-range prompt.
2. First `speaker2`/`speaker3` dialogue half.
3. Narrator: second question-range prompt.
4. Second dialogue half.
5. Narrator: end and checking prompt.

In `full` mode, the opening should cover four recordings, instructions, preparation/checking time, `once only`, four parts, and the scene; narration should total 160-230 words. In explicitly abbreviated standalone `short` mode, retain the scene, both question ranges, `once only`, transition, and closing in 70-110 words. Narration must not supply factual targets.

### Quantitative profile

- Dialogue words, excluding the narrator: 450-750 hard limit; 600-650 typical (advisory only).
- Dialogue turns, excluding the narrator: 20-48 hard limit; 30-40 typical (advisory only).
- Each half: at least 8 dialogue turns.
- Full narration: target 160-230 words.
- Frequent short exchanges; avoid long answer-dense turns.

### Roles and language

- Provider role: holds service or practical information.
- Enquirer role: asks, states needs, chooses, and confirms.
- Natural everyday English, moderate sentence length, polite tone.
- One practical need and coherent opening-body-decision-closing progression.

### Difficulty mechanisms

Require:

- one natural letter-by-letter spelling;
- one or more numeric details;
- one explicit correction that cancels an earlier value;
- one true dialogue-internal indirect confirmation where the answer term is spoken first and then identified by reference or explanation;
- only 2-3 deliberate distractor-bearing cycles;
- natural repetition/confirmation of important details.

Do not treat marker detection as proof. Verify an earlier-value -> explicit replacement + final-value chain. Verify an answer-term -> later reference/interpretation chain. `Sorry`, `rather`, or `That's right` alone never proves compliance.

## 5. Information-Map Review

Identify approximately ten plausible testable details without writing questions or prescribing final answers.

Build the map from the script alone. Never read a generator blueprint or supplied
information-point annotation, even when one is offered: the map's value lies in being an
independent reconstruction that can be compared against the generator's own, and a detail you
cannot recover from the script is a real defect rather than a disagreement.

For every detail record:

- sequence number (`seq`);
- detail type (`name`, `number`, `address`, `price`, `datetime`, `quantity`, `condition`, `option`);
- concise script evidence;
- `turn_index` of the turn carrying it;
- speaker;
- clarity status (`clear`, `confirmed`, `corrected`, `indirect`, `ambiguous`);
- correction, distractor, or confirmation mechanism if present.

The details should:

- occur in a stable linear order;
- be concrete and recordable;
- be introduced by recognizable topic cues;
- be sufficiently separated for once-only listening;
- include a useful mixture of detail types;
- avoid unresolved ambiguity;
- be organisable into varied question types, since supporting later item writing is the whole
  purpose of the material.

The information map is an editorial diagnostic, not a question set or answer key.

Because the artifact intentionally omits questions, report script readiness only. Do not claim to have verified question wording, word limits, option quality, or answer-key correctness.

## 6. Verdict and Output Format

Use:

- `PASS`: no critical or major findings.
- `PASS_WITH_MINOR_EDITS`: no critical or major findings and at least one minor finding.
- `FAIL`: at least one critical or major finding.
- `NOT_ASSESSABLE`: no usable script can be identified.

Underscore spellings keep the value machine-readable; the rendered report converts them to
spaced form.

### Overall score

Score each dimension, then total to 100:

| Dimension | Points |
|---|---:|
| Part 1 scenario, purpose, and frame | 20 |
| Information-map quality and item-writing support | 25 |
| Role consistency, progression, and coherence | 20 |
| Naturalness, grammar, and level | 15 |
| Difficulty mechanisms and distractor control | 15 |
| Transcript completeness and production readiness | 5 |

Use evidence-based judgment within each dimension. Then apply these caps:

- Any critical finding: maximum overall score 49.
- Any major finding: maximum overall score 69.
- Minor findings alone do not impose a cap.

The score supplements rather than replaces the severity verdict.

### Output contract

Return one JSON object conforming to `shared/schemas/audit.schema.json`:

```json
{
  "verdict": "PASS_WITH_MINOR_EDITS",
  "assessable": true,
  "score": {
    "total": 78,
    "dimensions": {
      "scenario_purpose_frame": 18,
      "information_map_quality": 19,
      "role_consistency": 17,
      "naturalness_level": 13,
      "difficulty_distractor_control": 6,
      "transcript_readiness": 5
    }
  },
  "findings": [
    {
      "severity": "minor",
      "rule": "the rule that was violated",
      "evidence": "shortest sufficient quote",
      "turn_index": 12,
      "fix": "smallest concrete correction"
    }
  ],
  "blind_information_map": [
    {
      "seq": 1,
      "type": "name",
      "evidence": "The surname is P-A-T-E-L.",
      "turn_index": 7,
      "speaker": "speaker2",
      "clarity": "confirmed",
      "mechanism": "spelling"
    }
  ],
  "metrics": {
    "dialogue_words": 618,
    "dialogue_turns": 34,
    "first_half_turns": 17,
    "second_half_turns": 17,
    "narrator_words": 195
  },
  "warnings": ["dialogue words outside preferred 600-650: 535"]
}
```

This JSON is the authoritative result. The human-readable Markdown report — findings by
severity, information map, metrics, priority fixes, scope notes, and a final score table with a
readiness label (`Ready`, `Minor editing needed`, `Substantial revision needed`,
`Not assessable`) — is produced from it by `shared/render_audit_report.py`. Do not write that
report by hand; keeping the data primary is what lets an automated loop read the verdict and
build revision instructions without parsing prose.

Use `turn_index: null` for findings about the script as a whole. Keep evidence concise and
fixes actionable. Leave `findings` empty rather than inventing an issue.

---

## Specification Compliance Review

A second, separate pass over the same script, and the only one that produces revision advice.

The blind information map answers *"can a listener recover this detail?"*. This pass answers a
different question: *"does this script obey the authoring specification in ways a script cannot
check?"* — and it is the output the generator reads in order to revise.

**The checklist below is the standard, and it is deliberately public.** Knowing that a compliant
script "should contain a self-correction" is not the same as knowing that *this* script's
self-correction sits at turn 8 and replaces $45 with $39. The first is a rule; the second is the
answer, and the answer is what you must not be given. So review against these items freely, and do
not ask for or accept the generator's information-point annotation in order to do it.

**Do not re-check what `audit_metrics.py` and the generator's validator already compute.** Word
counts, turn counts, half balance, speaker IDs, narration length, the presence of a spelling
sequence or a numeric detail, `turn_index` accuracy — all of those are decided by script, and a
second opinion on an arithmetic result is noise. Every item here is a judgement no script can make.

### C1. Scenario is everyday, not academic or specialist

The setting must be ordinary social or transactional life — booking, enquiry, registration,
accommodation, a job application, a complaint, a community activity, health services, lessons,
shipping. A script about laboratory procedure or a university seminar belongs to Parts 2-4.

Also judge whether the scenario is *coherent*: one clear practical need that a real person would
phone or visit about, not two unrelated errands stitched together.

### C2. Language is natural spoken English

Polite, everyday, moderate sentence length, with light conversational markers (`Of course`,
`Let me check`, `That's right`). Flag: specialist terminology, rare idioms, written-register
sentences nobody says aloud, implausible details, and artificial lists — the "and we also offer A,
B, C, D, and E" cadence that reads as a brochure rather than a conversation.

### C3. Distraction is natural, not mechanical

A compliant script carries a small number of deliberate distraction cycles. Judge each one for
*plausibility in the scene*, not for its presence:

- a self-correction should sound like a person misremembering, not like a device inserted to be
  corrected two lines later;
- a rejected alternative should be something the enquirer would plausibly have wanted;
- a qualifier or condition should matter to the enquirer's actual need.

Flag the opposite failure too: distraction so heavy that the script becomes a trap course, or so
absent that every detail is stated once, plainly, with nothing to mishear.

### C4. Information is ordered and separable

Details should arrive one at a time, in an order a listener can follow, each attached to its own
turn or short exchange. Flag information dumping — a single provider turn that delivers three or
four recordable details in one breath — because a candidate writing on an answer sheet cannot keep
up, and an item writer cannot cleanly separate them.

This is about *pacing within the dialogue*, which no script can judge. The clustering metric counts
adjacency; this item asks whether the script *reads* as followable.

### C5. Dialogue rhythm is genuinely two-way

Both participants should hold the floor. Flag:

- any provider turn long enough to read as a monologue — as a rough guide, a turn over roughly 60
  words, or one carrying three or more distinct facts, is worth flagging;
- long stretches where the enquirer only says `Right` / `OK` / `I see`;
- an enquirer who never asks, chooses, or pushes back, which makes the exchange a briefing rather
  than a conversation.

### C6. Roles and relationship are clear

The provider must read as the one who holds the information and the enquirer as the one who needs
it, consistently, from the first dialogue turn. Flag: a provider who starts asking the enquirer for
factual details, an enquirer who supplies the service information, an unexplained relationship, or
a narrator turn that carries answer content instead of framing the exam.

### Reporting compliance findings

Each finding gets a concrete, minimal fix — something the generator can act on without guessing:

> C5 · turn 12 · The provider delivers the deposit, the notice period and the inspection date in
> one 78-word turn. Split it into two or three short exchanges, letting the enquirer acknowledge or
> ask between them.

Weak, unactionable phrasing to avoid: "the dialogue could be more natural", "consider improving the
pacing". If the fix cannot be stated concretely, the finding is not ready to report.

Severity for compliance findings follows the same scale as the rest of this rubric: `critical` for
an academic/specialist scenario or an incoherent role relationship, `major` for information dumping
or monologue-length turns, `minor` for register slips and mechanical-sounding distraction.

Where the script is compliant on an item, say so briefly rather than inventing a finding. A clean
compliance pass is a useful result.
