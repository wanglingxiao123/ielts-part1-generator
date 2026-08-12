import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { MaterialComment } from '@/contracts/comments'
import type {
  MaterialQuestionVersionsResponse,
  QuestionRevisionEvent,
} from '@/contracts/questionVersions'
import { BASE_BLUEPRINT, QUESTION_PACKAGE } from '@/mocks/fixtures'
import { useQuestionVersions } from './useQuestionVersions'

const listVersions = vi.fn<() => Promise<MaterialQuestionVersionsResponse>>()
const adoptVersion = vi.fn()
const streamRevision = vi.fn()
const streamReplan = vi.fn()

vi.mock('@/api/endpoints', () => ({
  api: {
    materialQuestionVersions: () => listVersions(),
    adoptQuestionVersion: (...args: unknown[]) => adoptVersion(...args),
  },
}))

vi.mock('@/api/questionRevisions', () => ({
  streamQuestionRevision: (...args: unknown[]) => streamRevision(...args),
  streamQuestionReplan: (...args: unknown[]) => streamReplan(...args),
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

afterEach(() => {
  vi.useRealTimers()
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

    expect(listVersions).toHaveBeenCalledTimes(2)
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

  it('无需修改时不生成版本并保留理由与核对依据', async () => {
    const reasons = [{
      comment_id: 'comment-q3',
      question_number: 3,
      reason: '现有题目已经正确',
      references: ['题面', '标准答案', '材料证据'],
    }]
    streamRevision.mockResolvedValue({
      event: 'no_change',
      request_id: 'request-no-change',
      reasons,
    })
    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await waitFor(() => expect(result.current.selectedVersionId).toBe('original'))
    await act(() => result.current.revise([comment]))

    expect(result.current.revisionResult).toEqual({ kind: 'no_change', reasons })
    expect(result.current.versions).toHaveLength(1)
    expect(result.current.selectedVersionId).toBe('original')
  })

  it('刷新后从持久请求记录恢复审核阶段', async () => {
    listVersions.mockResolvedValue({
      ...response(),
      running_request: {
        request_id: 'request-running',
        status: 'running',
        base_version_id: 'original',
        created_at: '2026-08-11T09:00:00Z',
      },
      revision_request: {
        request_id: 'request-running',
        status: 'running',
        stage: 'auditing',
        base_version_id: 'original',
        comment_count: 2,
      },
    })

    const { result } = renderHook(() => useQuestionVersions('material-1', true))

    await waitFor(() => expect(result.current.revisionStage).toBe('auditing'))
    expect(result.current.revisionRequest?.comment_count).toBe(2)
  })

  it('刷新后恢复持久失败原因而不是静默清空', async () => {
    listVersions.mockResolvedValue({
      ...response(),
      revision_request: {
        request_id: 'request-failed',
        status: 'failed',
        base_version_id: 'original',
        message: '修改后的题目未通过完整质量检查。',
        blockers: ['Q5 has an equally-supported rival'],
      },
    })

    const { result } = renderHook(() => useQuestionVersions('material-1', true))

    await waitFor(() =>
      expect(result.current.revisionResult).toEqual({
        kind: 'failed',
        message: '修改后的题目未通过完整质量检查。',
        blockers: ['Q5 has an equally-supported rival'],
      }),
    )
  })

  it('刷新后恢复需重新命题终态', async () => {
    listVersions.mockResolvedValue({
      ...response(),
      revision_request: {
        request_id: 'request-replan',
        status: 'replan_questions',
        base_version_id: 'original',
        reasons: [{
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '需要更换信息点',
        }],
      },
    })

    const { result } = renderHook(() => useQuestionVersions('material-1', true))

    await waitFor(() =>
      expect(result.current.revisionResult).toEqual({
        kind: 'needs_replan',
        reasons: [{
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '需要更换信息点',
        }],
      }),
    )
  })

  it('确认重新命题后生成带蓝图快照的新版本且不自动采用', async () => {
    const replanDecision: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-needs-replan',
        status: 'replan_questions',
        base_version_id: 'original',
        reasons: [{
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '需要重新规划题组',
        }],
      },
    }
    const replanned = {
      ...response(),
      revision_request: {
        request_id: 'request-replan-execution',
        status: 'completed' as const,
        operation: 'replan_questions' as const,
        source_request_id: 'request-needs-replan',
        base_version_id: 'original',
        version_id: 'version-replanned',
      },
      versions: [
        ...response().versions,
        {
          ...response().versions[0]!,
          id: 'version-replanned',
          based_on_version_id: 'original',
          status: 'ready' as const,
          is_active: false,
          ordinal: 2,
          blueprint: BASE_BLUEPRINT,
        },
      ],
    }
    listVersions
      .mockResolvedValueOnce(replanDecision)
      .mockResolvedValueOnce(replanned)
    streamReplan.mockResolvedValue({
      event: 'revised',
      request_id: 'request-replan-execution',
      version_id: 'version-replanned',
    })

    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await waitFor(() => expect(result.current.revisionResult?.kind).toBe('needs_replan'))
    await act(() => result.current.replan())

    expect(streamReplan).toHaveBeenCalledWith(
      'material-1',
      { source_request_id: 'request-needs-replan' },
      expect.any(Function),
      expect.any(AbortSignal),
    )
    expect(result.current.selectedVersionId).toBe('version-replanned')
    expect(result.current.activeVersionId).toBe('original')
    expect(result.current.selectedVersion?.blueprint).toBeDefined()
  })

  it('连续确认重新命题只启动一个执行请求', async () => {
    const replanDecision: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-needs-replan',
        status: 'replan_questions',
        base_version_id: 'original',
        reasons: [],
      },
    }
    listVersions.mockResolvedValue(replanDecision)
    let finish!: (value: {
      event: 'revised'
      request_id: string
      version_id: string
    }) => void
    streamReplan.mockImplementation(() => new Promise((resolve) => {
      finish = resolve
    }))

    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await waitFor(() => expect(result.current.revisionResult?.kind).toBe('needs_replan'))
    let first!: Promise<void>
    await act(async () => {
      first = result.current.replan()
      await result.current.replan()
    })

    expect(streamReplan).toHaveBeenCalledOnce()
    finish({
      event: 'revised',
      request_id: 'request-replan-execution',
      version_id: 'version-replanned',
    })
    await act(() => first)
  })

  it('重新命题失败后使用持久源请求重试', async () => {
    const failed: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-failed',
        status: 'failed',
        operation: 'replan_questions',
        source_request_id: 'request-needs-replan',
        base_version_id: 'original',
        message: 'model unavailable',
      },
    }
    listVersions.mockResolvedValue(failed)
    streamReplan.mockResolvedValue({
      event: 'failed',
      request_id: 'request-retry',
      message: 'still unavailable',
    })

    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await waitFor(() => expect(result.current.revisionResult?.kind).toBe('failed'))
    await act(() => result.current.replan())

    expect(streamReplan).toHaveBeenCalledWith(
      'material-1',
      { source_request_id: 'request-needs-replan' },
      expect.any(Function),
      expect.any(AbortSignal),
    )
  })

  it('连接提前失败时等待持久状态从运行中变为需重新命题', async () => {
    vi.useFakeTimers()
    const running: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-replan',
        status: 'running',
        stage: 'auditing',
        base_version_id: 'original',
      },
    }
    const needsReplan: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-replan',
        status: 'replan_questions',
        base_version_id: 'original',
        reasons: [{
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '需要重新规划题组',
        }],
      },
    }
    listVersions
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(needsReplan)
      .mockResolvedValueOnce(needsReplan)
    streamRevision.mockImplementation(
      async (
        _materialId: string,
        _body: unknown,
        onEvent: (event: QuestionRevisionEvent) => void,
      ) => {
        onEvent({ event: 'progress', request_id: 'request-replan', stage: 'auditing' })
        throw new Error('题目修改连接提前结束，请稍后重试')
      },
    )

    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await act(async () => {
      await vi.runAllTimersAsync()
    })
    expect(result.current.selectedVersionId).toBe('original')

    let revision: Promise<void>
    act(() => {
      revision = result.current.revise([comment])
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
      await revision
    })

    expect(result.current.revisionResult).toEqual({
      kind: 'needs_replan',
      reasons: [{
        comment_id: 'comment-q3',
        question_number: 3,
        reason: '需要重新规划题组',
      }],
    })
    expect(result.current.revisionStage).toBeNull()
  })

  it('失败终态到达时优先恢复随后落盘的需重新命题结果', async () => {
    vi.useFakeTimers()
    const running: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-replan',
        status: 'running',
        stage: 'auditing',
        base_version_id: 'original',
      },
    }
    const needsReplan: MaterialQuestionVersionsResponse = {
      ...response(),
      revision_request: {
        request_id: 'request-replan',
        status: 'replan_questions',
        base_version_id: 'original',
        reasons: [{
          comment_id: 'comment-q3',
          question_number: 3,
          reason: '需要重新规划题组',
        }],
      },
    }
    listVersions
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce(needsReplan)
      .mockResolvedValueOnce(needsReplan)
    streamRevision.mockResolvedValue({
      event: 'failed',
      request_id: 'request-replan',
      message: '题目修改连接提前结束，请稍后重试',
    })

    const { result } = renderHook(() => useQuestionVersions('material-1', true))
    await act(async () => {
      await vi.runAllTimersAsync()
    })

    let revision: Promise<void>
    act(() => {
      revision = result.current.revise([comment])
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
      await revision
    })

    expect(result.current.revisionResult).toEqual({
      kind: 'needs_replan',
      reasons: [{
        comment_id: 'comment-q3',
        question_number: 3,
        reason: '需要重新规划题组',
      }],
    })
    expect(result.current.revisionStage).toBeNull()
  })
})
