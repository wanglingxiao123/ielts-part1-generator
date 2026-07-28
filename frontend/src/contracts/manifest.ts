/**
 * audio/manifest.json — hard requirement on 07-28-audio-storage (design.md §8.3).
 *
 * Hand-written because there is no JSON Schema for the manifest yet; this file
 * IS the frontend's contract proposal. Fields not listed here are not assumed
 * to exist.
 */
import type { SpeakerId } from './index'

export interface AudioSegment {
  /**
   * Index into material...script.turns — the SAME index space as
   * blueprint.items[].turn_index. Non-negotiable: this is the only join key
   * between material / blueprint / audit / audio. A separate "k-th dialogue
   * turn" index would misalign highlight and annotation without erroring.
   */
  turn_index: number
  speaker: SpeakerId
  /** null when this single segment failed to synthesise; siblings stay usable. */
  url: string | null
  duration_ms: number
  /** Natural pause after this segment. Required (design.md §6.2). */
  gap_after_ms: number
  bytes?: number
  error?: string | null
}

export interface AudioManifest {
  material_id: string
  generated_at: string
  engine: string
  format: string
  sample_rate_hz: number
  voice_map: Record<SpeakerId, string>
  total_duration_ms: number
  /** Pre-signed URLs expire; the player refetches the manifest on 401/403. */
  url_expires_at: string
  segments: AudioSegment[]
}
