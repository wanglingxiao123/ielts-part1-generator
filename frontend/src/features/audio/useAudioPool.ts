/**
 * Double-buffered <audio> pool (design.md §6.2 option B).
 *
 * Three HTMLAudioElements in a ring. While segment n plays, n+1 and n+2 already
 * have their src set and preload="auto", so `ended` → `play()` is a handoff of
 * tens of ms rather than a fresh network round trip.
 *
 * Why not Web Audio: playbackRate with pitch preservation is one line on
 * <audio>, native HTTP caching and streaming keep working, and — the strongest
 * reason — segment.gap_after_ms means the exam audio WANTS a pause between
 * turns. Gapless is not actually a requirement here.
 *
 * MEASURED (Chromium 151, mock clips, 2026-07-28): the handoff overshoots the
 * intended gap_after_ms by ~26ms. design.md §6.2's upgrade trigger is "gap is
 * audible", so the Web Audio path behind flag.audio.webaudio stays unbuilt.
 * Not yet measured on Safari, or against real Polly MP3s over the network.
 */
import { useCallback, useEffect, useRef } from 'react'
import type { Playlist } from '@/domain/playlist'
import { nextPlayable } from '@/domain/playlist'
import { useAudioStore } from '@/stores/audioStore'

const POOL_SIZE = 3

export interface AudioPoolApi {
  playFrom: (entryIndex: number, opts?: { single?: boolean }) => void
  pause: () => void
  resume: () => void
  stop: () => void
  /** Observed handoff gap in ms, for the §6.2 "measure before upgrading" step. */
  lastGapMs: () => number | null
}

export function useAudioPool(playlist: Playlist | null): AudioPoolApi {
  const pool = useRef<HTMLAudioElement[]>([])
  const timer = useRef<number | null>(null)
  const singleMode = useRef(false)
  const endedAt = useRef<number | null>(null)
  /** Intended pause at the last handoff, subtracted from the measurement. */
  const intendedGap = useRef(0)
  /** Excess over the intended gap — the §6.2 "measure before upgrading" number. */
  const gap = useRef<number | null>(null)
  const store = useAudioStore

  if (pool.current.length === 0) {
    pool.current = Array.from({ length: POOL_SIZE }, () => {
      const el = new Audio()
      el.preload = 'auto'
      return el
    })
  }

  const clearTimer = () => {
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = null
  }

  const elementFor = (entryIndex: number) => pool.current[entryIndex % POOL_SIZE]!

  const prime = useCallback(
    (entryIndex: number) => {
      if (!playlist) return
      for (let k = 1; k <= 2; k += 1) {
        const entry = playlist.entries[entryIndex + k]
        if (!entry?.playable || !entry.url) continue
        const el = elementFor(entryIndex + k)
        if (el.src !== entry.url) {
          el.src = entry.url
          el.load()
        }
      }
    },
    [playlist],
  )

  const playAt = useCallback(
    (entryIndex: number) => {
      if (!playlist) return
      const entry = nextPlayable(playlist, entryIndex)
      if (!entry) {
        store.getState().setPlaying(false)
        return
      }
      const el = elementFor(entry.segmentIndex)
      if (entry.url && el.src !== entry.url) el.src = entry.url
      el.playbackRate = store.getState().rate
      el.currentTime = 0

      store.getState().setCursor(entry.segmentIndex)
      store.getState().setPlaying(true)
      prime(entry.segmentIndex)

      const onEnded = () => {
        el.removeEventListener('ended', onEnded)
        el.removeEventListener('timeupdate', onTime)
        endedAt.current = performance.now()
        if (singleMode.current) {
          store.getState().setPlaying(false)
          return
        }
        // Natural pause between turns; the exam audio has one, and this is why
        // the double-buffer's residual gap is a non-issue.
        clearTimer()
        intendedGap.current = entry.gapAfterMs
        timer.current = window.setTimeout(
          () => playAt(entry.segmentIndex + 1),
          entry.gapAfterMs,
        )
      }
      const onTime = () => {
        store.getState().setPositionMs(el.currentTime * 1000)
      }
      el.addEventListener('ended', onEnded)
      el.addEventListener('timeupdate', onTime)

      void el
        .play()
        .then(() => {
          if (endedAt.current !== null) {
            gap.current = Math.max(
              0,
              performance.now() - endedAt.current - intendedGap.current,
            )
            endedAt.current = null
            intendedGap.current = 0
          }
        })
        .catch((err) => {
          console.warn('[audio] play rejected', err)
          store.getState().setPlaying(false)
        })
    },
    [playlist, prime, store],
  )

  const playFrom = useCallback(
    (entryIndex: number, opts?: { single?: boolean }) => {
      singleMode.current = Boolean(opts?.single)
      // iOS Safari: unlock every element inside the user gesture, otherwise the
      // SECOND segment silently fails when played programmatically.
      if (!store.getState().unlocked) {
        for (const el of pool.current) {
          void el
            .play()
            .then(() => el.pause())
            .catch(() => {})
        }
        store.getState().markUnlocked()
      }
      for (const el of pool.current) {
        el.pause()
      }
      clearTimer()
      playAt(entryIndex)
    },
    [playAt, store],
  )

  const pause = useCallback(() => {
    clearTimer()
    for (const el of pool.current) el.pause()
    store.getState().setPlaying(false)
  }, [store])

  const resume = useCallback(() => {
    const cursor = store.getState().cursor
    const el = elementFor(cursor)
    el.playbackRate = store.getState().rate
    if (el.src) {
      void el.play().catch(() => playAt(cursor))
      store.getState().setPlaying(true)
    } else {
      playAt(cursor)
    }
  }, [playAt, store])

  const stop = useCallback(() => {
    clearTimer()
    for (const el of pool.current) {
      el.pause()
      el.currentTime = 0
    }
    store.getState().reset()
  }, [store])

  // Rate changes apply live.
  const rate = useAudioStore((s) => s.rate)
  useEffect(() => {
    for (const el of pool.current) el.playbackRate = rate
  }, [rate])

  useEffect(
    () => () => {
      clearTimer()
      for (const el of pool.current) {
        el.pause()
        el.src = ''
      }
    },
    [],
  )

  return { playFrom, pause, resume, stop, lastGapMs: () => gap.current }
}
