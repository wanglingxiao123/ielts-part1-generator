/**
 * manifest → playback timeline (design.md §6.3). Pure.
 *
 * duration_ms + gap_after_ms let the progress bar and total duration render
 * without downloading a single byte of audio; without them the UI would need
 * 40 metadata probes before it could show anything.
 */
import type { AudioManifest, AudioSegment } from '@/contracts/manifest'

export interface PlaylistEntry {
  /** Position in the playback order (== manifest.segments index). */
  segmentIndex: number
  turnIndex: number
  url: string | null
  playable: boolean
  durationMs: number
  gapAfterMs: number
  /** Global timeline start, gaps included. */
  startMs: number
  endMs: number
}

export interface Playlist {
  entries: PlaylistEntry[]
  totalMs: number
  /** manifest.total_duration_ms; compared against totalMs to detect drift. */
  declaredTotalMs: number
  turnToEntry: Map<number, number>
  unplayableTurnIndexes: number[]
  /** segments out of ascending turn_index order or with gaps in numbering. */
  orderingProblems: string[]
  /**
   * manifest.engine, carried through so the player can say when the audio is a
   * local stand-in rather than a Polly product. Passing it as data avoids the
   * player reaching for global config to answer a question about this manifest.
   */
  engine: string
}

export function buildPlaylist(manifest: AudioManifest): Playlist {
  const entries: PlaylistEntry[] = []
  const turnToEntry = new Map<number, number>()
  const unplayableTurnIndexes: number[] = []
  const orderingProblems: string[] = []

  let cursor = 0
  let previousTurn = -1
  manifest.segments.forEach((seg: AudioSegment, segmentIndex) => {
    if (seg.turn_index <= previousTurn) {
      orderingProblems.push(`segment ${segmentIndex} turn_index ${seg.turn_index} 非升序`)
    } else if (previousTurn >= 0 && seg.turn_index !== previousTurn + 1) {
      orderingProblems.push(`turn ${previousTurn + 1}–${seg.turn_index - 1} 缺少音频片段`)
    }
    previousTurn = seg.turn_index

    const playable = typeof seg.url === 'string' && seg.url.length > 0
    if (!playable) unplayableTurnIndexes.push(seg.turn_index)

    const entry: PlaylistEntry = {
      segmentIndex,
      turnIndex: seg.turn_index,
      url: seg.url,
      playable,
      durationMs: seg.duration_ms,
      gapAfterMs: seg.gap_after_ms,
      startMs: cursor,
      endMs: cursor + seg.duration_ms,
    }
    cursor = entry.endMs + seg.gap_after_ms
    turnToEntry.set(seg.turn_index, segmentIndex)
    entries.push(entry)
  })

  // Trailing gap after the final segment is not part of the audible timeline.
  const last = entries[entries.length - 1]
  const totalMs = last ? last.endMs : 0

  return {
    entries,
    totalMs,
    declaredTotalMs: manifest.total_duration_ms,
    turnToEntry,
    unplayableTurnIndexes,
    orderingProblems,
    engine: manifest.engine,
  }
}

/** Next playable entry at or after `from`; null when nothing remains. */
export function nextPlayable(playlist: Playlist, from: number): PlaylistEntry | null {
  for (let i = Math.max(0, from); i < playlist.entries.length; i += 1) {
    const e = playlist.entries[i]!
    if (e.playable) return e
  }
  return null
}

export function entryForTurn(playlist: Playlist, turnIndex: number): PlaylistEntry | null {
  const idx = playlist.turnToEntry.get(turnIndex)
  if (idx === undefined) return null
  return playlist.entries[idx] ?? null
}

export function formatMs(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const mm = Math.floor(total / 60)
  const ss = total % 60
  return `${mm}:${String(ss).padStart(2, '0')}`
}
