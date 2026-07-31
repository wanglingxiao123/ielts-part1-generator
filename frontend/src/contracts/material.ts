/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/generate/generate-listening-part1/schemas/material.schema.json
 * Regenerate: npm run contracts:gen
 */
/**
 * Delivered listening material. Contains the recording script only: no questions, answers, or analysis.
 */
export interface IELTSListeningPart1Material {
  /**
   * Generating model identifier, or 'unspecified'.
   */
  model: string
  /**
   * ISO 8601 UTC timestamp.
   */
  extracted_at: string
  test_package: string
  content_kind: 'listening_material'
  /**
   * Always [] for original material.
   *
   * @maxItems 0
   */
  source_htmls: []
  /**
   * @minItems 1
   * @maxItems 1
   */
  listening_material_parts: [Part]
}
export interface Part {
  reference: 'Part 1'
  test_package: string
  /**
   * One concise sentence: participants, practical need, setting.
   */
  scenario: string
  script: Script
  /**
   * @maxItems 0
   */
  source_htmls: []
}
export interface Script {
  reference: 'Part 1'
  test_package: string
  /**
   * @minItems 1
   */
  turns: [Turn, ...Turn[]]
  /**
   * Narrator plus two dialogue participants.
   */
  speaker_count: 3
}
export interface Turn {
  /**
   * speaker1 = exam narrator (narration only, never carries answers); speaker2 = information provider; speaker3 = enquirer.
   */
  speaker: 'speaker1' | 'speaker2' | 'speaker3'
  text: string
}
