# IELTS Listening Part 1 Script Specification

## Contents

1. Content profile
2. Script architecture
3. Information design
4. Language and speaker roles
5. JSON output contract
6. Final private checklist

## 1. Content Profile

- Use an everyday/social-life setting, not academic or specialist content.
- Center one clear practical need, such as booking, service enquiry, registration, accommodation, job enquiry, complaint/refund, community participation, health service, driving lessons, or shipping.
- Use exactly two dialogue participants plus the exam narrator.
- Make the exchange genuinely two-way. Usually the enquirer asks and chooses while the provider supplies organized factual details.
- State that the recording is heard `once only`.
- Internally design approximately ten testable details in two groups, although no questions or answers appear in the output.
- Use original content only.

## 2. Script Architecture

### Exam frame

Use this sequence:

1. `speaker1`: whole-test introduction in `full` mode, then scenario introduction and first reading prompt; omit only the whole-test boilerplate in explicit `short` mode.
2. `speaker2` and `speaker3`: first dialogue segment.
3. `speaker1`: midpoint prompt for the second question group.
4. `speaker2` and `speaker3`: second dialogue segment.
5. `speaker1`: end-of-Part-1 checking prompt.

In default `full` mode, the opening should naturally cover four recordings, answer instructions, preparation/checking time, `once only`, four parts, the Part 1 scenario, and the first question range. Only `once only` is required verbatim; the rest are checked by meaning, so natural phrasings such as "a number of different recordings" are accepted. In explicit `short` mode, retain `once only`, the scenario, and first range. The midpoint gives the second range. The closing identifies the end of Part 1, checking time, and transition to Part 2.

### Dialogue frame

Follow this progression:

1. Greeting and provider identity.
2. Enquirer states the practical purpose.
3. Sequential information micro-cycles.
4. Comparison or decision.
5. Thanks and next step.

Use the micro-cycle:

`question/topic cue -> target detail -> optional correction, spelling, repetition, or confirmation`

Keep the ten private target details in strict order. Prefer one target per cycle and use clear topic changes.

### Length and pacing

- Dialogue words, excluding `speaker1`: 450-750, enforced as a limit; 600-650 is the observed
  typical value across 20 real test sets, not a requirement. A compliant 530-word script is
  acceptable and must not be rewritten merely to reach 600.
- Dialogue turns, excluding `speaker1`: 20-48, enforced as a limit; 30-40 typical.
- Halves should be roughly even. Fewer than 8 turns in either half is an error; a lopsided
  split within that floor is reported as advice.
- Each dialogue half: at least 8 turns.
- Narration: 160-230 words in `full` mode; 70-110 words in explicitly requested standalone `short` mode.
- Favor short turns and frequent exchange over long information-dense monologues.

## 3. Information Design

Privately plan ten recordable details selected from:

- spelled name or proper noun;
- telephone/reference number;
- address or house number;
- price or total;
- date, time, or shift;
- quantity, size, weight, or maximum;
- condition, eligibility, or requirement;
- option or preference.

Include:

- at least one natural letter-by-letter spelling;
- at least one numeric target;
- at least three confirmed points, including at least one spelled name and one numeric detail;
  these are the easiest to mishear under once-only listening, so a single confirmation across
  ten points is too thin. Keep confirmations natural rather than mechanical;
- one clear self-correction in which a final value replaces an earlier value;
- one dialogue-internal indirect confirmation.

### Question-type support

The material's only purpose is to support later item writing, so the ten points must be
organisable into real question types. Ten scattered gap-fills satisfy every other rule yet
leave an item writer unable to build a table question.

Part 1 delivers **Form completion, Note completion, and Table completion only**. Multiple choice,
matching, plan/map/diagram labelling and short-answer questions are out of scope, so every point
must be answerable by writing a word, number or short phrase into a gap.

Assign each point an `item_form` (`form`, `table`, or `note`). `item_form` names the **completion
layout**, not the IELTS question type — Part 1 has exactly one question type (completion) and these
three are its layouts. **Every point must also carry a non-empty `form_group`**: a point that
belongs to no group is a scattered gap-fill, which is the shape this whole section exists to
prevent. A single-point group is legal; an absent group is not. Requirements:

- at least one `form_group` containing 3 or more points, **all sharing the same `item_form`**,
  and that form must be `form` or `table` — a group of `note` points cannot become a table,
  and a group mixing forms cannot become one question;
- a group's **item numbers must be contiguous** (`5, 6, 7`, never `5, 7, 9`);
- a group's points must be **contiguous in the ordered evidence sequence** — no other group's point
  may fall between them. Since evidence positions strictly increase, this means one group's points
  are heard together rather than interleaved with another's;
- a group **must not cross the narrator window boundary**. Candidates read questions 1-N during the
  first pause and the rest during the second, so a group split across the boundary is a question
  whose halves are read at different times. One window may hold several groups;
- points in one `form_group` should sit reasonably close together; a group spanning most of the
  script forces candidates to hold answers across half the recording. This one is reported as
  advice rather than enforced, since the spec sets no span limit.

For indirect confirmation, explicitly say the answer term first, then refer to it. The answer
term must also be the `target` of one of the ten points: an item writer has to be able to use
the audio's own wording as the key, so indirect reference may only add listening difficulty on
top of a spoken answer, never replace it.

```text
speaker2: Would you like it by email or by post?
speaker3: The latter, please. I would rather read it on paper.
```

Use only 2-3 deliberate distractor-bearing cycles. Valid mechanisms are self-correction, option comparison, negation, and conditional limitation. Signal the final information clearly. Keep Part 1 accessible.

Option comparison remains valid even though multiple choice does not. The mechanism is about the
*dialogue* weighing two alternatives before one is settled on; the settled alternative is then
written into a gap ("Property type: ______"). The rejected alternative is the distractor. What was
dropped is the multiple-choice *question layout*, not this way of building difficulty.

Before finalizing, scan every dialogue turn independently of the blueprint. Count all earlier values later replaced, rejected alternatives, selection-driving comparisons, exclusions, and applicability-changing conditions. The full-script census must equal the 2-3 items marked as distractors. If the script contains an additional unmarked trap, simplify or remove it.

## 4. Language and Speaker Roles

- `speaker1`: narrator only; never provides testable factual answers.
- `speaker2`: service provider, receptionist, adviser, or other information holder.
- `speaker3`: customer, student, applicant, or other enquirer.
- Use exactly these IDs; do not use names in the `speaker` field.
- Keep spoken English polite, natural, and everyday.
- Use moderate sentence length and light conversational markers such as `Of course`, `Let me check`, and `That's right`.
- Avoid specialist terminology, rare idioms, implausible details, and artificial lists.

## 5. JSON Output Contract

Return only valid JSON with this shape:

```json
{
  "model": "known-model-id-or-unspecified",
  "extracted_at": "2026-07-22T07:28:21.905274+00:00",
  "test_package": "Test 1",
  "content_kind": "listening_material",
  "source_htmls": [],
  "listening_material_parts": [
    {
      "reference": "Part 1",
      "test_package": "Test 1",
      "scenario": "A student calls an international shipping company to ask about sending luggage abroad.",
      "script": {
        "reference": "Part 1",
        "test_package": "Test 1",
        "turns": [
          {
            "speaker": "speaker1",
            "text": "This is the IELTS listening test..."
          },
          {
            "speaker": "speaker2",
            "text": "Hello, Move It Shipping."
          },
          {
            "speaker": "speaker3",
            "text": "Hi. I'd like some information about your services, please."
          }
        ],
        "speaker_count": 3
      },
      "source_htmls": []
    }
  ]
}
```

Contract rules:

- Set `content_kind` exactly to `listening_material`.
- Use a non-empty `listening_material_parts` array.
- Include a non-empty `scenario` in every part.
- Keep top-level, part-level, and script-level package/reference values consistent.
- Use one turn object per spoken turn with only `speaker` and `text`.
- Use exactly `speaker1`, `speaker2`, and `speaker3`.
- Count the narrator in `speaker_count`; therefore its value is `3`.
- Use JSON strings with escaped quotation marks and no trailing commas.
- For original generation, use `[]` for both `source_htmls` arrays.
- Do not include `candidate_questions`, `questions`, `answer_key`, `answers`, `item_evidence`, `analysis`, or `quality_check`.

### Blueprint

The blueprint is a **second delivered artifact**, saved as its own JSON file beside the
material. Reviewers read it as marginal annotations beside the script to judge whether the ten
points are evenly distributed. It still contains no questions and no answer key.

It must never be handed to the audit step: the auditor rebuilds an information map by reading
the script blind, and comparing the two independently-produced maps is a stronger check than
trusting the generator's own labels. A point the auditor cannot recover is a real defect.

```json
{
  "blueprint_schema_version": 2,
  "narration_mode": "full",
  "split_after": 5,
  "completion_layout_coverage": {
    "form": [1, 2, 3],
    "table": [4, 5, 6, 7],
    "note": [8, 9, 10]
  },
  "items": [
    {
      "number": 1,
      "group": 1,
      "type": "name",
      "target": "Patel",
      "evidence": "The surname is P-A-T-E-L.",
      "turn_index": 7,
      "item_form": "form",
      "form_group": "A",
      "response_form": "word",
      "answer_category": "person_name",
      "narrator_window_id": 1,
      "distractor": false,
      "confirmed": true
    }
  ],
  "correction": {
    "earlier": "half past nine",
    "final": "nine o'clock",
    "marker": "Actually"
  },
  "indirect_confirmation": {
    "answer_term": "a pannier",
    "reference_phrase": "the latter"
  }
}
```

Include exactly 10 items numbered 1-10. Use a 1-5 / 6-10 or 1-6 / 7-10 split. Every target must occur inside its evidence, every evidence phrase must occur in a distinct dialogue turn, and evidence positions must strictly increase. Use at least four detail types (`name`, `number`, `address`, `price`, `datetime`, `quantity`, `condition`, `option`), exactly 2-3 distractor items, and at least three confirmed items. The correction's earlier value must precede its final value and marker. The indirect answer term must precede its reference phrase and must equal one item's `target`.

`turn_index` is the index into the material's `turns` array for the turn carrying that
evidence. It anchors the reviewer's annotation. If the script is later revised, the anchors
must be revised with it — a stale anchor puts an annotation beside the wrong sentence, and that
error is almost impossible to notice downstream.

`completion_layout_coverage` restates each item's `item_form` grouped by layout. Flattened it must
equal 1-10 exactly once, and each listed number's `item_form` must match. The redundancy is
deliberate: the per-item field drives annotation rendering, the grouped view makes the overall
layout balance reviewable at a glance.

### Blueprint version 2

**Always write `blueprint_schema_version: 2`.** Records with no version field are v1 — roughly 400
archived blueprints predate this contract, and they are read as v1 rather than rewritten. The version
is decided by that field alone, never by whether the fields below happen to be present: a v2 record
missing one of them is an error, not a v1.

Two names changed with v2. Write `completion_layout_coverage`; `question_type_coverage` is the v1
name and must not be written. Never write both — a record carrying both leaves a reader no way to
know which to trust, and it is rejected outright.

Three fields are required on every item in v2:

- **`response_form`** — `numeric | word | phrase`, the shape of the recordable answer **by token
  count**. `numeric` when every token is a pure number/time/date form, `word` for a single
  non-numeric token, `phrase` for several. It serves the word-limit decision, so a hyphenated
  compound counts as **one** word: `two-bedroom` is `word`, not `phrase`. Derive it from the actual
  `target` text and nothing else — `118 Fordyce` is a `phrase` even though its `type` is `address`,
  and `BT14 9BJ` is a `phrase` because "contains a digit" is not the same as "is a number".
- **`answer_category`** — which micro-category of information the answer is. Used to check that the
  ten items do not test the same micro-category three or more times. The 13 values are
  `person_name`, `contact`, `location`, `date`, `time`, `duration`, `price`, `quantity`, `service`,
  `facility`, `requirement`, `preference`, `document`. **There is no catch-all.** A point that fits
  none of them is not a categorisation problem, it is a material problem — take it back to the
  script rather than forcing a label. Boundaries worth stating: `contact` is how to reach a person
  (an extension number) while `location` is where a thing is (a postcode); `price` is currency only,
  so `10 lessons` is `quantity`; `date`, `time` and `duration` never merge; `service` is a
  purchasable offering while `facility` is a physical place or piece of equipment **being
  described**; `requirement` is a condition **asked for** (a `guest room` the caller wants) while
  `facility` covers what already exists in the setting (a `park` nearby); `preference` is the one
  chosen after the dialogue weighs two alternatives. Judge the nature of the **answer**, not the
  wording of the sentence — an answer reached through "we definitely need a two-bedroom property" is
  still a `quantity`, because `two-bedroom` is a specification.
- **`narrator_window_id`** — `1` or `2`, which narrator window the item falls in. Redundant with
  `group` by construction, and deliberately so: the window is recomputed from the narration and
  compared against this declaration, so a wrong value is caught rather than believed.

Answer variety follows from these two fields: keep purely numeric answers to **4 or fewer** of the
ten, ensure **at least 4** require spelling out a word or phrase, and never test one micro-category
in three or more items.

The `distractor` booleans must be a complete census, not merely selected examples. Recheck the entire script for unmarked alternatives, corrections, negations, and qualifiers.

## 6. Final Private Checklist

- [ ] Everyday scenario and one clear practical need.
- [ ] `scenario` accurately summarizes the generated script.
- [ ] Exactly `speaker1`, `speaker2`, and `speaker3`; `speaker_count` is 3.
- [ ] Full narration/dialogue/midpoint/dialogue/closing order.
- [ ] Opening explicitly contains `once only`.
- [ ] Ten target details occur in order across two balanced halves.
- [ ] At least one spelling point and one numeric point.
- [ ] At least three confirmed points, including a spelled name and a numeric detail.
- [ ] One clear self-correction and one true dialogue-internal indirect confirmation whose
      answer term is also an item target.
- [ ] Only 2-3 deliberate distractor cycles.
- [ ] Dialogue is 450-750 words and 20-48 turns; each half has at least 8 turns.
- [ ] Every `turn_index` points at the turn that actually carries its evidence.
- [ ] Every point carries a non-empty `form_group`; one group holds 3+ points, all sharing
      `item_form` `form` or `table`.
- [ ] Each group's item numbers are contiguous, its points are not interleaved with another
      group's, and no group crosses the narrator window boundary.
- [ ] Every `item_form` is `form`, `table` or `note` — no non-completion layouts.
- [ ] `completion_layout_coverage` flattens to 1-10 exactly once and agrees with each `item_form`;
      the v1 name `question_type_coverage` is absent.
- [ ] `blueprint_schema_version` is `2`.
- [ ] Every item has `response_form`, `answer_category` and `narrator_window_id`, each derived from
      the item itself rather than guessed.
- [ ] At most 4 purely numeric answers, at least 4 that must be spelled out, and no micro-category
      used by 3 or more items.
- [ ] Spoken English is natural and appropriate for Part 1.
- [ ] JSON parses and matches the contract.
- [ ] Blueprint passes deterministic order, split, evidence, anchor, grouping, correction,
      indirect-reference, and distractor checks.
- [ ] Delivered material contains only listening material, with no questions, answers, or analysis.
