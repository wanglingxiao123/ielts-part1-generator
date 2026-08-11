import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { MaterialComment } from '@/contracts/comments'
import type {
  MaterialQuestionVersionsResponse,
  QuestionRevisionEvent,
} from '@/contracts/questionVersions'
import { QUESTION_PACKAGE } from '@/mocks/fixtures'
import { useQuestionVersions } from './useQuestionVersions'

const listVersions = vi.fn<() => Promise<MaterialQuestionVersionsResponse>>()
const adoptVersion = vi.fn()
const streamRevision = vi.fn()

vi.mock('@/api/endpoints', () => ({
  api: {
    materialQuestionVersions: () => listVersions(),
    adoptQuestionVersion: (...args: unknown[]) => adoptVersion(...args),
  },
}))

vi.mock('@/api/questionRevisions', () => ({
  streamQuestionRevision: (...args: unknown[]) => streamRevision(...args),
}))

const response = (active = 'original'): MaterialQuestionVersionsResponse => ({
  material_id: 'material-1',
  active_version_id: active,
  versions: [
    {
      id: 'original',
      created_at: '2026-08-11T08:00:00Z',
      based_on_version_id: null,
      source_comment_ids: [],
      status: 'original',
      package: QUESTION_PACKAGE,
      is_active: active === 'original',
      ordinal: 1,
    },
  ],
})

const comment: MaterialComment = {
  id: 'comment-q3',
  created_at: '2026-08-11T09:00:00Z',
  anchor: { type: 'question', index: 3 },
  severity: 'major',
  text: '修改 Q3',
}

beforeEach(() => {
  vi.clearAllMocks()
  listVersions.mockResolvedValue(response())
})

describe('useQuestionVersions', () => {
  it('成功修改后刷新版本并选择新版本，但不自动采用', async () => {
    const revised = {
      ...response(),
      versions: [
        ...response().versions,
        {
          ...response().versions[0]!,
          id: 'version-2',
          based_on_version_id: 'original',
          status: 'ready' as const,
          is_active: false,
          ordinal: 2,
        },
      ],
    }
    listVersions.mockResolvedValueOnce(response()).mockResolvedValueOnce(revised)
    streamRevision.mockImplementation(
      async (
        _materialId: string,
        _body: unknown,
        onEvent: (event: QuestionRevisionEvent) => void,
      ) => {
        onEvent({ event: 'progress', request_id: 'request-1', stage: 'validating' })
        return { event: 'revised', request_id: 'request-1', version_id: 'version-2' }
      },
    )

    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await waitFor(() => expect(result.current.selectedVersionId).toBe('original'))
    await act(() => result.current.revise([comment]))

    expect(streamRevision).toHaveBeenCalledWith(
      'material-1',
      { base_version_id: 'original', comment_ids: ['comment-q3'] },
      expect.any(Function),
      expect.any(AbortSignal),
    )
    await waitFor(() => expect(result.current.selectedVersionId).toBe('version-2'))
    expect(result.current.activeVersionId).toBe('original')
  })

  it('需要修改材料时保留当前版本并公开逐条原因', async () => {
    streamRevision.mockResolvedValue({
      event: 'needs_material_revision',
      request_id: 'request-2',
      reasons: [
        {
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '录音信息不足',
        },
      ],
    })
    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await waitFor(() => expect(result.current.selectedVersionId).toBe('original'))
    await act(() => result.current.revise([comment]))

    expect(listVersions).toHaveBeenCalledOnce()
    expect(result.current.selectedVersionId).toBe('original')
    expect(result.current.revisionResult).toEqual({
      kind: 'needs_material',
      reasons: [
        {
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '录音信息不足',
        },
      ],
    })
  })
})
