/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/generate/generate-listening-part1/schemas/blueprint.read.schema.json
 * Regenerate: npm run contracts:gen
 */
export type ItemNumberList = number[]

/**
 * READ-side contract: every blueprint a reader may be handed, v1 or v2. The title is deliberately the bare artefact name and carries no 'read-side' qualifier, because json-schema-to-typescript derives the exported interface name from it: this is the shape the frontend's `Blueprint` type means. Ten information points with turn anchors and completion-layout grouping. Delivered alongside material.json for reviewer annotation display. MUST NOT be given to the audit step, which builds its own map blind.
 *
 * THE WRITE-SIDE CONTRACT IS blueprint.schema.json, which layers v2-only strictness on top of this document. The two answer different questions and the answers genuinely differ:
 *   - 'may we emit this?' -> blueprint.schema.json. No to a missing version field, no to question_type_coverage, no to item_form: multiple_choice, no to a null form_group.
 *   - 'must we be able to display this?' -> this file. Yes to all four, because archived records carry them and are neither rewritten nor migrated -- measured in this repo: 3 captured blueprints plus backend/docs/sample/blueprint.json, 9 multiple_choice points between them, every one with form_group null.
 * A v1 record validating HERE and failing blueprint.schema.json is the designed outcome, not a defect in either document. This split replaced a single schema whose description called itself the write-side contract while an `else` branch quietly required v1's coverage name -- doc and behaviour in direct conflict.
 *
 * Only this file feeds frontend codegen (frontend/scripts/gen-contracts.mjs), because the frontend RECEIVES records rather than emitting them: `Blueprint` must admit a null form_group and the v1 coverage name or the reader cannot type its own inputs. Version-conditional rules live in `if/then/else` on purpose -- json-schema-to-typescript IGNORES if/then (measured), so the generated type keeps the tolerant top-level shape while Python's jsonschema, which does honour it, enforces the per-version rules. `item_form` carries all FOUR values at the item level and is narrowed to three inside the v2 branch -- that direction, and not the reverse. Measured: a four-value enum placed in the v1 `else` branch does NOT widen the three-value enum it inherits, because JSON Schema branches only ever intersect, and the real v1 fixture failed on exactly its three multiple_choice items. The cost is that the generated TypeScript union has four values, since json-schema-to-typescript ignores if/then; `CurrentLayout` in frontend/src/domain/blueprintVersion.ts is the three-value type the UI renders, and the write schema is where 'the generator may not emit MC' is enforced.
 *
 * Version is decided by the presence and value of blueprint_schema_version ALONE, never inferred from whether the v2 fields happen to be present: a v2 record that forgot response_form must fail, not silently downgrade to v1. An unrecognised version (3, say) fails this schema too -- 'readable' means v1 or v2, and a record this build cannot interpret is a thing to surface, not to render through whichever field name it happens to carry.
 */
export interface IELTSListeningPart1InformationPointBlueprint {
  /**
   * Present and equal to 2 for v2 records; absent for v1. Deliberately NOT in this file's required list -- its absence is what marks a v1 record, and the branches below key off exactly that. blueprint.schema.json DOES require it, which is the single line that turns this read contract into a v2 write contract. Any other value is rejected rather than treated as v1.
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
   * v1 ONLY. Readable, never writable: blueprint.schema.json forbids this name outright, and the v2 branch below does too, so a record cannot hedge by writing both. Declared here because the top level is additionalProperties: false, so omitting it would make every archived v1 record fail the read contract. additionalProperties is deliberately NOT false: v1 records legitimately carry a multiple_choice key. An empty layout array is real data -- the captured batch in frontend/src/api/__fixtures__/real-batch.sse.txt has note: [] -- and readers must preserve it, since a declared-but-empty layout is different information from an absent one.
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
   * Completion layout this point declares. Part 1 delivers Form / Note / Table completion only; multiple_choice was removed when the client narrowed the brief. Not to be confused with type: "option", which names the kind of detail and is still a valid completion answer.
   *
   * FOUR values here, three in the v2 branch above and in blueprint.schema.json. All four are listed at this level because JSON Schema branches INTERSECT rather than override: a three-value enum here could not be widened by a four-value enum inside the v1 branch, so an archived multiple_choice record would fail the very contract that exists to admit it. Measured -- that was this file's first shape, and the real v1 fixture failed it on exactly these three items. Narrowing works, widening does not, so the wide set lives here and the narrow one in the branches.
   *
   * This is also what the generated TypeScript union contains, and deliberately so: the frontend receives archived records, so a three-value union would be a type that lies about its own inputs and force a cast at every read. The three layouts the UI actually RENDERS are named by CURRENT_LAYOUTS in frontend/src/domain/blueprintVersion.ts, whose `CurrentLayout` type is what the glyph and label maps are keyed on -- see also itemLayout() there and the coverage flattening in formGroups.ts, which key off the data's own declared values.
   *
   * Measured over every archived blueprint and captured batch reachable in this repo: 9 multiple_choice points exist and every one of them has form_group: null, so no real record needs a mixed-layout group tolerated as well -- validate_part1.py's homogeneity check stays strict for that reason.
   */
  item_form: 'form' | 'table' | 'note' | 'multiple_choice'
  /**
   * Points sharing a form_group combine into one printed form/note/table layout. Nullable HERE because null is real v1 data meaning a standalone gap-fill; the v2 branch above and blueprint.schema.json both narrow it to a non-empty string. At least one group of >=3 points is required so the material can actually support a table or form question. v2 also requires contiguous item numbers and contiguity in the ordered evidence sequence. Narrator windows constrain each item's evidence, not the printed layout boundary.
   */
  form_group: string | null
  /**
   * v2 required (optional here only so v1 records validate; the v2 branch above requires it). Shape of the recordable answer, by TOKEN COUNT: numeric = every token is a pure number/time/date form; word = a single non-numeric token; phrase = multiple tokens. Serves the word_limit decision, so hyphenated compounds count as ONE word (two-bedroom is word, not phrase). Declared here but independently recomputed from target by validate_part1.py and compared -- a mismatch is an error. Derive from the actual target text, never from type: address lives in NUMERIC_TYPES yet 118 Fordyce is a phrase.
   */
  response_form?: 'numeric' | 'word' | 'phrase'
  /**
   * v2 required (optional here only so v1 records validate; the v2 branch above requires it). Micro-category of the answer, used to check QR-027 (no micro-category tested by 3+ of the ten items). This is an INTERNAL closed taxonomy of this system, NOT a client-supplied enum: the client rule only asks for an answer category per item and offers location / price / service as examples. If the client ever supplies its own category list, this enum gives way to it. There is deliberately no 'other' fallback -- a catch-all bucket would make unrelated points collide and misfire the same-category count, and would give the model a 'when unsure, pick other' escape hatch that destroys the field's only value. A point that fits none of the 13 is an error, not a degradation: it belongs back in the material stage. Judge the nature of the answer, not the wording of the sentence. Where two values both look defensible the decision is an ORDERED procedure, not a weighing: (1) form first -- a name/date/time/span/currency/count settles it, and price is currency only; (2) an artefact issued or shown is document, above the facility or service it governs; (3) contact is a route to a PERSON, so a reference code is not contact; (4) location is a position, above facility; (5) service vs facility is performed-vs-merely-present, NOT charged-vs-included; (6) preference needs alternatives named in the script, else requirement. First rule that fires ends it. Stated in generate-listening-part1/references/specification.md (authoritative) and machine-readable in feasibility-listening-part1/references/answer-category-decisions.json; prose boundaries alone let one run call an included breakfast a service and a named restaurant a facility while reading them as contradictory. Python checks the enum, the ten-item completeness and the counts; semantic accuracy is the audit agent's job.
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
   * v2 required (optional here only so v1 records validate; the v2 branch above requires it). Which narrator window this item's decisive evidence falls in. Its ONLY purpose is cross-checking: validate_part1.py reparses the narrator's stated ranges and recomputes the window from the item number, then compares. Implementing this as 'read the field, check it is 1 or 2' would hand window attribution back to the model's own say-so and make the field worthless.
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
