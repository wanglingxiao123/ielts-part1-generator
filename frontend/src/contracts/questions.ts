/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/generate/generate-questions-part1/schemas/question_package.schema.json
 * Regenerate: npm run contracts:gen
 */
export type TableCell =
  | {
      text: string
    }
  | {
      question_number: number
    }

/**
 * The ten Part 1 completion items written for one already-finalised material, in THREE PHYSICALLY SEPARATED BLOCKS: question_face (candidate-visible, and the only block a question auditor may ever receive), answer_key (never to an auditor), evidence (never to an auditor). The separation is structural, not tidiness: a single object holding all three is eventually passed whole, and a blind audit that has seen the answer key produces a score that is merely too high -- there is no error and nothing in the delivered artifact to notice.
 *
 * TWO LAYERS, and which field sits on which is load-bearing. Part 1 has exactly one question_type (completion); form / note / table are its LAYOUTS. `instruction_text`, `word_limit` and `numeral_allowance` belong to the completion layer and are therefore properties of an `instructions` entry, identical in kind across all three layouts. `title`, table row/column labels and note hierarchy belong to the layout layer and are therefore properties of a `groups` entry. Putting them on the wrong layer is how a table group acquires a note-style title structure.
 *
 * `layout` is declared ONCE, on the group. An `instructions` entry deliberately does NOT restate it: two authorities for one fact disagree eventually, and the group is the layer that owns it. Homogeneity within a group is then true by construction rather than by check. The validator checks membership, contiguous question numbers and contiguity in the ordered evidence sequence. Narrator windows remain strict for each item's evidence, but they are listening/read-ahead boundaries rather than mandatory printed-layout boundaries.
 *
 * RECORDED CONSEQUENCE, so the next reader does not discover it at runtime: question_face.questions[] carries `answer_category` and `response_form`, and both strings are in backend/deterministic/guards.py's BLUEPRINT_ONLY_KEYS. A question face therefore CANNOT be passed through `assert_blind`, which exists to keep the generator's plan away from the MATERIAL auditor. The question audit needs its own guard over its own ANSWER_ONLY_KEYS (canonical / alternatives / quote / turn_index / blueprint); loosening BLUEPRINT_ONLY_KEYS to make the existing guard accept a question face would silently reopen the material-audit leak it was built for.
 *
 * The ten items are given input, not a candidate pool: they are the blueprint's ten points, in the blueprint's numbering and evidence order, with nothing deleted, replaced or reordered, and the audible script untouched (SR-021). A material that cannot carry ten reliable items is rejected upstream by the feasibility preflight, whose only remedy is a new material.
 */
export interface IELTSListeningPart1QuestionPackage {
  reference: 'Part 1'
  /**
   * Same value as the material's test_package. Matched by the validator, so a package cannot be delivered against a different material than the one it was written for.
   */
  test_package: string
  /**
   * The finalised material this package belongs to. A question set is written to fit one specific recording; without this the pairing survives only in whoever ran the batch.
   */
  material_id: string
  /**
   * Block A: everything a candidate sees. The ONLY block a question auditor may receive, because the auditor's core product is the answer it rebuilds from the script by itself.
   */
  question_face: {
    /**
     * Exactly one entry per group. The completion layer: what the candidate is told to write and how much of it.
     *
     * @minItems 1
     * @maxItems 3
     */
    instructions: [Instruction] | [Instruction, Instruction] | [Instruction, Instruction, Instruction]
    /**
     * The layout layer. Group count is never pre-set: it follows the candidate-visible task and natural record structure. A continuous layout may cross a narrator read-ahead window; a real change of layout or information structure may create another group. A set may mix form / note / table as long as each group is itself homogeneous.
     *
     * @minItems 1
     * @maxItems 3
     */
    groups: [Group] | [Group, Group] | [Group, Group, Group]
    /**
     * @minItems 10
     * @maxItems 10
     */
    questions: [Question, Question, Question, Question, Question, Question, Question, Question, Question, Question]
  }
  /**
   * Block B. Never reaches an auditor in any form, including summarised or counted form.
   *
   * @minItems 10
   * @maxItems 10
   */
  answer_key: [
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry,
    AnswerKeyEntry
  ]
  /**
   * Block C. Never reaches an auditor: a quote plus a turn index is the answer with directions to it.
   *
   * @minItems 10
   * @maxItems 10
   */
  evidence: [
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry,
    EvidenceEntry
  ]
}
export interface Instruction {
  group_id: string
  /**
   * "1-5", or a bare "7" for a single-item group. Checked against the group's actual question numbers rather than trusted: a range that disagrees with the numbering is a rubric the candidate cannot follow.
   */
  question_range: string
  /**
   * The rubric as printed. Must contain word_limit verbatim (LG-006): a rubric that says one thing while the machine-readable field says another is unanswerable, and the printed text is the one the candidate obeys.
   */
  instruction_text: string
  /**
   * A closed set of standard rubrics, per group. There is deliberately NO global default: the limit is chosen per group as the strictest standard rubric every canonical in THAT group satisfies, and the validator re-derives it. Free text was the alternative and it fails QR-017 quietly -- an invented phrasing is neither countable nor comparable against the answers.
   */
  word_limit:
    | 'ONE WORD ONLY'
    | 'ONE WORD AND/OR A NUMBER'
    | 'NO MORE THAN TWO WORDS'
    | 'NO MORE THAN TWO WORDS AND/OR A NUMBER'
    | 'NO MORE THAN THREE WORDS'
    | 'NO MORE THAN THREE WORDS AND/OR A NUMBER'
  /**
   * How many purely numeric tokens the rubric permits: 1 for the AND/OR A NUMBER forms, 0 otherwise. Redundant with word_limit by construction and kept anyway, because the validator recomputes it and compares -- the pair exists so a mis-set rubric can be contradicted and caught.
   */
  numeral_allowance: number
}
export interface Group {
  group_id: string
  /**
   * Legacy compatibility field for a group wholly inside one narrator window. New groups spanning both windows omit it because no single value is truthful. Item and evidence window fields remain mandatory and are independently recomputed.
   */
  narrator_window_id?: 1 | 2
  /**
   * Part 1 delivers completion only; these are its three layouts. Declared here and nowhere else.
   */
  layout: 'form' | 'note' | 'table'
  /**
   * Short, specific, non-leaking scenario/topic heading. REQUIRED for a note group (QR-031) and optional for form and table, which carry their identity in their labels. It is scanned for the group's own answers along with every other candidate-visible string.
   */
  title?: string
  /**
   * Blank-free, specific, script-grounded navigation lines. QR-026 asks for at least one per narrator window. A group spanning multiple windows lists at least one line for each covered window in window order; one line is not counted for both.
   */
  signposts: string[]
  /**
   * The layout layer's own labels. Which key is required depends on `layout`, checked in the validator so errors can name the layout: form needs row labels, new tables need column labels plus an explicit rectangular table_rows matrix, and notes need note_sections. Legacy table labels remain readable for archived packages but are not valid new output.
   */
  structure: {
    /**
     * Legacy Table only: the heading above the archived row-label column. New generated Tables put every printed heading in column_labels and use table_rows.
     */
    row_header_label?: string
    row_labels?: string[]
    column_labels?: string[]
    /**
     * Table only: candidate-visible rows in printed order. Every ordinary cell is either meaningful fixed information or one question reference; placeholder padding such as dashes or N/A is invalid. The validator requires rectangular rows and an exact, ordered cover of the table group's questions.
     *
     * @minItems 1
     */
    table_rows?: [TableRow, ...TableRow[]]
    /**
     * Legacy note headings in printed order. Kept readable for archived packages; new note groups must use note_sections so headings cannot be detached from their questions.
     */
    hierarchy?: string[]
    /**
     * Candidate-visible note sections in printed order. Each heading is rendered immediately above the questions listed in question_numbers; the validator requires an exact, non-overlapping cover of the note group's questions.
     *
     * @minItems 1
     */
    note_sections?: [
      {
        heading: string
        /**
         * @minItems 1
         */
        question_numbers: [number, ...number[]]
      },
      ...{
        heading: string
        /**
         * @minItems 1
         */
        question_numbers: [number, ...number[]]
      }[]
    ]
  }
}
export interface TableRow {
  /**
   * @minItems 1
   */
  cells: [TableCell, ...TableCell[]]
}
export interface Question {
  number: number
  group_id: string
  /**
   * Printed text before the blank. It may be empty together with carrier_after when a form row label, or a table's real row and column labels, already supplies candidate-visible structural context. With no such labels, an isolated blank is invalid (QR-026).
   */
  carrier_before: string
  /**
   * The printed blank as the candidate sees it, e.g. "3 ................". Must contain its own question number: the number is how an answer sheet is matched to an item, and a blank that omits it is unanswerable however well written the carrier is.
   */
  blank: string
  carrier_after: string
  /**
   * QR-026 position class, using QR-025's content-word criterion: initial = at most one content word before the blank and content after it; final = no content word after and content before; otherwise medial. Recomputed from the carriers and compared, for the same reason as narrator_window_id -- a self-declared distribution is exactly the one that satisfies the rule on paper.
   */
  blank_position: 'initial' | 'medial' | 'final'
  /**
   * The blueprint item's category, carried through unchanged and checked for equality with it. There is no catch-all: this taxonomy is closed, its semantic accuracy was reviewed at the feasibility preflight, and relabelling a point here would move the answer-variety counts away from the set that was actually approved.
   */
  answer_category:
    | 'person_name'
    | 'contact'
    | 'location'
    | 'date'
    | 'time'
    | 'duration'
    | 'price'
    | 'quantity'
    | 'job_title'
    | 'service'
    | 'facility'
    | 'requirement'
    | 'preference'
    | 'document'
  /**
   * Shape of the expected answer by TOKEN COUNT: numeric = every token is a pure number/time/amount form, word = one non-numeric token, phrase = several. A hyphenated compound is ONE token (two-bedroom is word, not phrase), because this field serves the word-limit decision. Recomputed from the canonical and compared. It lives on the question face because it is what the candidate is being asked to produce; it is not the internal QR-027 class, which splits on character composition instead and is never persisted (Room 4B is phrase here and mixed there).
   */
  response_form: 'numeric' | 'word' | 'phrase'
}
export interface AnswerKeyEntry {
  number: number
  /**
   * The marking answer, in the script's own wording. AR-003 applies in tiers decided by tokenising THIS string, never by the declared word_limit: a one-token canonical must match one complete orthographic token of the decisive evidence with no substring credit in either direction (Educational cannot key education), while a multi-token canonical needs every one of its words in that evidence and must satisfy the limit -- it is never required to equal a single token. Must equal its blueprint item's target: the ten recordable values were settled upstream.
   */
  canonical: string
  /**
   * Accepted variants, each independently satisfying the same limit and equivalent in this context (AR-004). Empty is a normal answer, not an omission -- inventing a variant that is not actually equivalent marks wrong answers as right.
   */
  alternatives: string[]
  /**
   * The limit this answer was marked against. Restated per answer rather than only per group because marking reads this block alone, and it is checked equal to the group's rubric -- two limits that disagree mean the printed paper and the marking key are different tests.
   */
  word_limit:
    | 'ONE WORD ONLY'
    | 'ONE WORD AND/OR A NUMBER'
    | 'NO MORE THAN TWO WORDS'
    | 'NO MORE THAN TWO WORDS AND/OR A NUMBER'
    | 'NO MORE THAN THREE WORDS'
    | 'NO MORE THAN THREE WORDS AND/OR A NUMBER'
  numeral_allowance: number
  /**
   * The counting basis actually used, stated because QR-017 requires the report to name it: hyphenated compound counts as one word, whitespace splits tokens, a pure number consumes the numeral allowance rather than a word, a slash does not create a second answer.
   */
  counting_rule: string
}
export interface EvidenceEntry {
  number: number
  /**
   * Zero-based index into the material's turns array, counting narration turns. Must point at a non-narrator turn whose text contains `quote` verbatim, and the ten indices must strictly increase with question number (QR-004 / AL-003). It may differ from the blueprint item's anchor when the decisive evidence is the confirmation turn rather than the first mention -- confirmation is standard Part 1 writing -- but it may not leave the item's narrator window.
   */
  turn_index: number
  /**
   * Shortest sufficient verbatim span from that turn. Verbatim is checkable and a paraphrase is not, which is the whole reason this field is a quote.
   */
  quote: string
  /**
   * Checked three ways for agreement: against the window computed from the question number, against the window the declared turn actually falls in, and against the blueprint item. Crossing a window is AL-017 / SC-019 failure, and it has no tolerance -- the narration is where candidates are told to stop answering one announced range and move to the next.
   */
  narrator_window_id: 1 | 2
  /**
   * How the carrier relates to the evidence (QR-024). `signpost` means a retained locating label rather than a rewrite; retaining one is allowed and often required (QR-034).
   */
  paraphrase_relation: 'exact' | 'signpost' | 'paraphrase'
  /**
   * What the carrier asserts the answer is about. Paired with evidence_entity for AL-018: the two must be one factual proposition, and a question label sitting on the same printed line as the answer word is not alignment.
   */
  carrier_entity: string
  evidence_entity: string
  /**
   * How the two entities relate in the script -- same subject, same time, same place, stated in words rather than as a boolean, because this is what a reviewer checks when the alignment claim is disputed.
   */
  proposition_relation: string
  /**
   * A closed pair, not free text. `not_aligned` is an error rather than a note: it is the generator stating that this item fails AL-018, and a package that reports its own failure must not be delivered as if it had passed.
   */
  proposition_alignment_result: 'aligned' | 'not_aligned'
}
