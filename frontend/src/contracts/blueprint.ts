/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/ielts-listening-skills/shared/schemas/blueprint.schema.json
 * Regenerate: npm run contracts:gen
 */
export type ItemNumberList = number[]

/**
 * Ten information points with turn anchors and question-type grouping. Delivered alongside material.json for reviewer annotation display. MUST NOT be given to the audit step, which builds its own map blind.
 */
export interface IELTSListeningPart1InformationPointBlueprint {
  narration_mode: 'full' | 'short'
  /**
   * Last item number of the first question group; must match the narrator's stated ranges.
   */
  split_after: 5 | 6
  /**
   * Item numbers grouped by question type. Flattened, must equal exactly 1..10 with no repeats. Redundant with items[].item_form by design: this view supports auditing overall type distribution, the per-item field supports rendering each annotation.
   */
  question_type_coverage: {
    form?: ItemNumberList
    table?: ItemNumberList
    multiple_choice?: ItemNumberList
    note?: ItemNumberList
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
   * Question type this point can support.
   */
  item_form: 'form' | 'table' | 'multiple_choice' | 'note'
  /**
   * Points sharing a form_group can combine into one table/form question. null means a standalone gap-fill. At least one group of >=3 points is required so the material can actually support a table or form question.
   */
  form_group: string | null
  /**
   * Part of a deliberate distractor cycle. Must be a complete census, not selected examples: 2-3 true across the ten items.
   */
  distractor: boolean
  /**
   * Repeated or confirmed in dialogue. >=3 required; spelling and numeric points must be confirmed since they are the easiest to mishear under once-only listening.
   */
  confirmed: boolean
}
