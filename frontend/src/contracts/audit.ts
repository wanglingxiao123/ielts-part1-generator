/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/audit/audit-listening-part1/schemas/audit.schema.json
 * Regenerate: npm run contracts:gen
 */
/**
 * Structured quality assessment. Native output of the audit skill; the human-readable Markdown report is a rendering of this document, not the source. blind_information_map MUST be built by reading the script only, without the generator's blueprint.
 */
export interface IELTSListeningPart1AuditResult {
  /**
   * Underscore form for programmatic handling; rendered with spaces in the Markdown report. FAIL is a usable-but-flawed material and is delivered with its findings; NOT_ASSESSABLE means no usable script was found, and the orchestrator re-runs that slot rather than delivering it.
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
  /**
   * Specification-compliance pass: the semantic items no script can check (C1-C6 in audit-rubric.md). Separate from `findings` because these are the items the generator revises against, while `findings` also carries script-derived results. Optional so an audit of an unassessable artifact stays valid.
   */
  compliance_review?: {
    /**
     * One entry per checklist item reviewed, compliant or not.
     */
    items: {
      /**
       * Checklist item in audit-rubric.md.
       */
      code: 'C1' | 'C2' | 'C3' | 'C4' | 'C5' | 'C6'
      compliant: boolean
      severity?: 'critical' | 'major' | 'minor'
      /**
       * null for a finding about the script as a whole.
       */
      turn_index?: number | null
      /**
       * Shortest sufficient quote from the script.
       */
      evidence?: string
      /**
       * Concrete, minimal, actionable. 'Consider improving the pacing' is not a fix; 'split turn 12 into two exchanges' is.
       */
      fix?: string
    }[]
    /**
     * One or two sentences on the overall compliance picture.
     */
    summary?: string
  }
}
