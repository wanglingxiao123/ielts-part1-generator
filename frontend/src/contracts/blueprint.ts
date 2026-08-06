/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/generate/generate-listening-part1/schemas/blueprint.schema.json
 * Regenerate: npm run contracts:gen
 */
export type ItemNumberList = number[]

/**
 * Ten information points with turn anchors and completion-layout grouping. Delivered alongside material.json for reviewer annotation display. MUST NOT be given to the audit step, which builds its own map blind.
 *
 * VERSIONED CONTRACT. New generation MUST write blueprint_schema_version: 2. Records with no version field are v1 and are still readable by this schema on purpose -- roughly 400 archived blueprints predate v2 and are neither rewritten nor migrated. Version is decided by the presence and value of blueprint_schema_version ALONE, never inferred from whether the v2 fields happen to be present: a v2 record that forgot response_form must fail, not silently downgrade to v1.
 */
export interface IELTSListeningPart1InformationPointBlueprint {
  /**
   * Present and equal to 2 for v2 records; absent for v1. Deliberately NOT in the top-level required list -- the conditional requirements below key off its presence so that v1 records remain valid against this same schema. Any other value is rejected here rather than being treated as v1.
   */
  blueprint_schema_version?: 2
  narration_mode: 'full' | 'short'
  /**
   * Last item number of the first question group; must match the narrator's stated ranges.
   */
  split_after: 5 | 6
  /**
   * v2 name. Item numbers grouped by completion layout. Flattened, must equal exactly 1..10 with no repeats. Redundant with items[].item_form by design: this view supports auditing overall layout distribution, the per-item field supports rendering each annotation. Renamed from question_type_coverage because it never held IELTS question types -- Part 1 has exactly one question type (completion) and these three values are its layouts.
   */
  completion_layout_coverage?: {
    form?: ItemNumberList
    table?: ItemNumberList
    note?: ItemNumberList
  }
  /**
   * v1 ONLY -- read compatibility, MUST NOT be written by new generation. Kept as a declared property because the top level is additionalProperties: false, so deleting it outright would make every archived v1 record fail validation. Note additionalProperties is deliberately NOT false here: v1 records may carry a multiple_choice key, which v2 forbids as a layout but which historical data legitimately contains. The v2 branch below rejects this name outright, so a record cannot hedge by writing both.
   */
  question_type_coverage?: {
    form?: ItemNumberList
    table?: ItemNumberList
    note?: ItemNumberList
    multiple_choice?: ItemNumberList
  }
  /**
   * @minItems 10
   * @maxItems 10
   */
  items: [Item, Item, Item, Item, Item, Item, Item, Item, Item, Item]
  /**
   * One self-correction where a final value cancels an earlier value. Order in script must be earlier -> marker -> final.
   */
  correction: {
    earlier: string
    final: string
    marker: string
  }
  /**
   * Optional. The paraphrase/indirect-reference cycle, when the material uses one. Measured over the 27 usable archived papers: only 4 contain an indirect reference, while 24 use 先说后改 and 21 use a qualifier. The spec asks for 2-3 distraction cycles chosen from five mechanisms (§4B-4), not for this specific one, so requiring it made the generator chase a convention the real papers rarely follow.
   */
  indirect_confirmation?: {
    answer_term: string
    reference_phrase: string
  }
}
export interface Item {
  number: number
  /**
   * Question group; derived from split_after.
   */
  group: 1 | 2
  /**
   * Detail type per spec 4B-3. At least four distinct types required across the ten items.
   */
  type: 'name' | 'number' | 'address' | 'price' | 'datetime' | 'quantity' | 'condition' | 'option'
  /**
   * The recordable value. Must occur inside evidence.
   */
  target: string
  /**
   * Shortest sufficient quote from the dialogue turn carrying this point.
   */
  evidence: string
  /**
   * Index into material.json turns array. Anchors the annotation for marginal display. Must point at a non-speaker1 turn whose text contains evidence. Revision MUST keep this in sync -- a stale anchor shows reviewers an annotation beside the wrong sentence, which is very hard to detect downstream.
   */
  turn_index: number
  /**
   * Completion layout this point can support. Part 1 delivers Form / Note / Table completion only; multiple choice is out of scope. Not to be confused with type: "option", which names the kind of detail and is still a valid completion answer.
   *
   * v1 records may carry item_form: "multiple_choice" and therefore do NOT validate against this schema. That is accepted, not an oversight: nothing in any read path validates archived records against this schema (the only two validation sites are the fixture tests and the audit-reply probe), so this enum is the WRITE-side contract. Widening it to four values to admit old data would put the deleted value back into the generated TypeScript union. Readers must tolerate the extra value themselves -- see frontend/src/domain/blueprintVersion.ts and the coverage flattening in formGroups.ts, which key off the data's own declared values rather than this enum.
   */
  item_form: 'form' | 'table' | 'note'
  /**
   * Points sharing a form_group combine into one table/form question. v2 requires a non-empty string on every item (narrowed in the v2 branch below); null is v1 only and meant a standalone gap-fill. At least one group of >=3 points is required so the material can actually support a table or form question. v2 adds four more relational constraints, checked in validate_part1.py because JSON Schema cannot express them: contiguous item numbers, contiguity in the ordered evidence sequence, and no group spanning a narrator window.
   */
  form_group: string | null
  /**
   * v2 required. Shape of the recordable answer, by TOKEN COUNT: numeric = every token is a pure number/time/date form; word = a single non-numeric token; phrase = multiple tokens. Serves the word_limit decision, so hyphenated compounds count as ONE word (two-bedroom is word, not phrase). Declared here but independently recomputed from target by validate_part1.py and compared -- a mismatch is an error. Derive from the actual target text, never from type: address lives in NUMERIC_TYPES yet 118 Fordyce is a phrase.
   */
  response_form?: 'numeric' | 'word' | 'phrase'
  /**
   * v2 required. Micro-category of the answer, used to check QR-027 (no micro-category tested by 3+ of the ten items). This is an INTERNAL closed taxonomy of this system, NOT a client-supplied enum: the client rule only asks for an answer category per item and offers location / price / service as examples. If the client ever supplies its own category list, this enum gives way to it. There is deliberately no 'other' fallback -- a catch-all bucket would make unrelated points collide and misfire the same-category count, and would give the model a 'when unsure, pick other' escape hatch that destroys the field's only value. A point that fits none of the 13 is an error, not a degradation: it belongs back in the material stage. Boundaries: contact = how to reach a person (extension number), location = where a thing is (postcode); price = currency only; date/time/duration never merge; service = a purchasable offering, facility = a physical place or equipment being described; requirement = a condition asked for, preference = the one chosen after weighing two alternatives. Judge the nature of the answer, not the wording of the sentence. Python checks the enum, the ten-item completeness and the counts; semantic accuracy is the audit agent's job.
   */
  answer_category?:
    | 'person_name'
    | 'contact'
    | 'location'
    | 'date'
    | 'time'
    | 'duration'
    | 'price'
    | 'quantity'
    | 'service'
    | 'facility'
    | 'requirement'
    | 'preference'
    | 'document'
  /**
   * v2 required. Which narrator window this item falls in (SC-019). Its ONLY purpose is cross-checking: validate_part1.py reparses the narrator's stated ranges and recomputes the window from the item number, then compares. Implementing this as 'read the field, check it is 1 or 2' would hand window attribution back to the model's own say-so and make the field worthless. Redundant with group by construction; both are kept so the declaration can be contradicted and caught.
   */
  narrator_window_id?: 1 | 2
  /**
   * Part of a deliberate distractor cycle. Must be a complete census, not selected examples: 2-3 true across the ten items.
   */
  distractor: boolean
  /**
   * Repeated or confirmed in dialogue. >=3 required; spelling and numeric points must be confirmed since they are the easiest to mishear under once-only listening.
   */
  confirmed: boolean
}
