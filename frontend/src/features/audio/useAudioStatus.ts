import { useEffect, useState } from 'react'
import { api } from '@/api/endpoints'
import type { AudioStatusResponse } from '@/contracts/api'

/**
 * Polls GET /audio while synthesising (design.md §6.4): short-lived, low
 * frequency, single resource — polling is simpler than SSE here.
 *
 * `restartKey` restarts the loop. `not_requested` is a settled state and stops polling, which is
 * right (nobody has asked for audio, so nothing will ever change on its own) — but 生成音频 is
 * exactly the event that makes it change. Bumping the key after that POST is what resumes polling;
 * without it the panel would sit on the stale `not_requested` until the page was reloaded.
 */
export function useAudioStatus(materialId: string, enabled: boolean, restartKey = 0) {
  const [status, setStatus] = useState<AudioStatusResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let attempts = 0
    let timer: number | null = null

    const tick = async () => {
      try {
        const res = await api.getAudio(materialId)
        if (stopped) return
        setStatus(res)
        setError(null)
        if (res.status === 'ready' || res.status === 'failed') return
        // `not_requested` is a settled state, not a step on the way to ready:
        // synthesis starts only on selection. Polling it forever burned a
        // request every 2s for the whole time a reviewer read a material.
        if (res.status === 'not_requested') return
      } catch (err) {
        if (stopped) return
        setError(err instanceof Error ? err.message : String(err))
      }
      attempts += 1
      // 2s for the first 30 attempts, then back off to 10s.
      timer = window.setTimeout(() => void tick(), attempts < 30 ? 2000 : 10_000)
    }
    void tick()
    return () => {
      stopped = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [materialId, enabled, restartKey])

  return { status, error }
}
