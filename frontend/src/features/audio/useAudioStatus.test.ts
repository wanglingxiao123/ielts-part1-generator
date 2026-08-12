import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/endpoints'
import type { AudioStatusResponse } from '@/contracts/api'
import { useAudioStatus } from './useAudioStatus'

vi.mock('@/api/endpoints', () => ({
  api: { getAudio: vi.fn() },
}))

describe('useAudioStatus version isolation', () => {
  beforeEach(() => {
    vi.mocked(api.getAudio).mockReset()
  })

  it('does not expose the previous version status while the next request is pending', async () => {
    let resolveSecond!: (value: AudioStatusResponse) => void
    vi.mocked(api.getAudio)
      .mockResolvedValueOnce({
        status: 'ready',
        progress: { done: 1, total: 1 },
        manifest: {
          material_id: 'material-1',
          generated_at: '2026-08-12T12:00:00Z',
          engine: 'polly',
          format: 'mp3',
          sample_rate_hz: 24_000,
          voice_map: { speaker1: 'Amy', speaker2: 'Arthur', speaker3: 'Brian' },
          total_duration_ms: 0,
          url_expires_at: '2026-08-12T13:00:00Z',
          segments: [],
        },
      })
      .mockImplementationOnce(
        () =>
          new Promise<AudioStatusResponse>((resolve) => {
            resolveSecond = resolve
          }),
      )

    const { result, rerender } = renderHook(
      ({ versionId }) => useAudioStatus('material-1', versionId, true),
      { initialProps: { versionId: 'original' } },
    )
    await waitFor(() => expect(result.current.status?.status).toBe('ready'))

    rerender({ versionId: 'version-2' })
    expect(result.current.status).toBeNull()

    await act(async () => {
      resolveSecond({ status: 'not_requested', progress: { done: 0, total: 0 } })
    })
    await waitFor(() =>
      expect(result.current.status?.status).toBe('not_requested'),
    )
  })
})
