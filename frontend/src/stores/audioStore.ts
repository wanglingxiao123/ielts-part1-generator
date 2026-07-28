/**
 * Playback cursor only (design.md §7.2).
 *
 * HTMLAudioElement instances deliberately live in a non-reactive useRef pool in
 * useAudioPool: DOM objects inside a reactive store trigger pointless equality
 * checks and subscription storms.
 */
import { create } from 'zustand'

export interface AudioState {
  /** Index into playlist.entries, not turn_index. */
  cursor: number
  playing: boolean
  /** Elapsed ms within the current segment. */
  positionMs: number
  rate: 0.75 | 1 | 1.25
  follow: boolean
  /** Set once the iOS gesture unlock has run. */
  unlocked: boolean
}

interface Actions {
  setCursor: (cursor: number) => void
  setPlaying: (playing: boolean) => void
  setPositionMs: (ms: number) => void
  setRate: (rate: AudioState['rate']) => void
  toggleFollow: () => void
  markUnlocked: () => void
  reset: () => void
}

const INITIAL: AudioState = {
  cursor: 0,
  playing: false,
  positionMs: 0,
  rate: 1,
  follow: true,
  unlocked: false,
}

export const useAudioStore = create<AudioState & Actions>((set) => ({
  ...INITIAL,
  setCursor: (cursor) => set({ cursor, positionMs: 0 }),
  setPlaying: (playing) => set({ playing }),
  setPositionMs: (positionMs) => set({ positionMs }),
  setRate: (rate) => set({ rate }),
  toggleFollow: () => set((s) => ({ follow: !s.follow })),
  markUnlocked: () => set({ unlocked: true }),
  reset: () => set({ ...INITIAL }),
}))
