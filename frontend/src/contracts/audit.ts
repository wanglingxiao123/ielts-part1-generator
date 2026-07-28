/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/ielts-listening-skills/shared/schemas/audit.schema.json
 * Regenerate: npm run contracts:gen
 */
/**
 * Structured quality assessment. Native output of the audit skill; the human-readable Markdown report is a rendering of this document, not the source. blind_information_map MUST be built by reading the script only, without the generator's blueprint.
 */
export interface IELTSListeningPart1AuditResult {
  /**
   * Underscore form for programmatic handling; rendered with spaces in the Markdown report. FAIL and NOT_ASSESSABLE both route to quarantine.
   */
  verdict: 'PASS' | 'PASS_WITH_MINOR_EDITS' | 'FAIL' | 'NOT_ASSESSABLE'
  assessable: boolean
  score: {
    /**
     * Severity caps apply: any critical finding caps at 49, any major finding caps at 69.
     */
    total: number
    dimensions: {
      scenario_purpose_frame: number
      information_map_quality: number
      role_consistency: number
      naturalness_level: number
      difficulty_distractor_control: number
      transcript_readiness: number
    }
  }
  findings: {
    severity: 'critical' | 'major' | 'minor'
    /**
     * The violated requirement.
     */
    rule: string
    /**
     * Shortest sufficient quote.
     */
    evidence: string
    /**
     * Where the defect sits; null for whole-script findings.
     */
    turn_index?: number | null
    /**
     * Smallest concrete correction.
     */
    fix: string
  }[]
  /**
   * Recordable details found by reading the script blind, in first-occurrence order. Compared against the blueprint by cross_check.py: points the auditor could not recover are genuine defects.
   */
  blind_information_map: {
    seq: number
    type: 'name' | 'number' | 'address' | 'price' | 'datetime' | 'quantity' | 'condition' | 'option'
    evidence: string
    turn_index: number
    speaker: 'speaker2' | 'speaker3'
    clarity: 'clear' | 'confirmed' | 'corrected' | 'indirect' | 'ambiguous'
    /**
     * Spelling, correction, distractor, or confirmation mechanism if present.
     */
    mechanism?: string | null
  }[]
  metrics: {
    dialogue_words: number
    dialogue_turns: number
    first_half_turns: number
    second_half_turns: number
    narrator_words: number
  }
  /**
   * Non-blocking observations, e.g. word count outside the typical 600-650 band while inside the hard 450-750 limit. Advisory input to the revise step; never a failure signal.
   */
  warnings?: string[]
}
