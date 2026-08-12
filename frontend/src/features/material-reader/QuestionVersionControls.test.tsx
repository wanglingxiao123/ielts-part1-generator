import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { MaterialComment } from '@/contracts/comments'
import type { QuestionPackageVersion } from '@/contracts/questionVersions'
import { QUESTION_PACKAGE } from '@/mocks/fixtures'
import { QuestionRevisionAction, QuestionVersionBar } from './QuestionVersionControls'
import type { QuestionVersionsState } from './useQuestionVersions'

const COMMENTS: MaterialComment[] = [
  {
    id: 'comment-1',
    created_at: '2026-08-11T08:00:00Z',
    anchor: { type: 'question', index: 3 },
    severity: 'major',
    text: '题面表达需要更清楚',
  },
]

function version(ordinal: number, active = false): QuestionPackageVersion {
  return {
    id: `version-${ordinal}`,
    created_at: `2026-08-11T0${ordinal}:00:00Z`,
    based_on_version_id: ordinal === 1 ? null : `version-${ordinal - 1}`,
    source_comment_ids: ordinal === 1 ? [] : ['comment-1'],
    status: ordinal === 1 ? 'original' : 'ready',
    package: QUESTION_PACKAGE,
    is_active: active,
    ordinal,
  }
}

function state(overrides: Partial<QuestionVersionsState> = {}): QuestionVersionsState {
  const versions = [version(1), version(2), version(3, true), version(4), version(5)]
  return {
    versions,
    activeVersionId: 'version-3',
    selectedVersion: versions[2]!,
    selectedVersionId: 'version-3',
    setSelectedVersionId: vi.fn(),
    loading: false,
    error: null,
    adopting: false,
    adopt: vi.fn(),
    revisionStage: null,
    revisionRequest: null,
    revisionResult: null,
    dismissRevisionResult: vi.fn(),
    revise: vi.fn(),
    reload: vi.fn(),
    ...overrides,
  }
}

describe('题目版本控件', () => {
  it('版本首次读取失败时仍显示错误和重试操作', async () => {
    const reload = vi.fn()
    render(
      <QuestionVersionBar
        state={state({
          versions: [],
          selectedVersion: null,
          selectedVersionId: '',
          activeVersionId: '',
          error: '题目版本暂时读取不到',
          reload,
        })}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('题目版本暂时读取不到')
    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(reload).toHaveBeenCalledOnce()
  })

  it('可展示五个完整版本，并标识原始版本和当前采用版本', () => {
    render(<QuestionVersionBar state={state()} />)
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(5)
    expect(options[0]).toHaveTextContent('V1 · 原始版本')
    expect(options[2]).toHaveTextContent('V3 · 当前采用')
    expect(screen.getByText('当前采用')).toBeInTheDocument()
  })

  it('切换只选择查看版本，采用需要单独点击', async () => {
    const selected = version(2)
    const setSelectedVersionId = vi.fn()
    const adopt = vi.fn()
    const current = state({
      selectedVersion: selected,
      selectedVersionId: selected.id,
      setSelectedVersionId,
      adopt,
    })
    render(<QuestionVersionBar state={current} />)

    await userEvent.selectOptions(screen.getByRole('combobox', { name: '题目版本' }), 'version-4')
    expect(setSelectedVersionId).toHaveBeenCalledWith('version-4')
    expect(adopt).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: '采用此版本' }))
    expect(adopt).toHaveBeenCalledOnce()
  })
})

describe('根据批注修改题目', () => {
  it('只有当前采用版本且存在题目批注时可以提交', async () => {
    const revise = vi.fn()
    render(<QuestionRevisionAction state={state({ revise })} comments={COMMENTS} />)
    const button = screen.getByRole('button', { name: '提交修改' })
    expect(button).toBeEnabled()
    await userEvent.click(button)
    expect(revise).toHaveBeenCalledWith(COMMENTS)
  })

  it('已处理和需改材料的批注不会再次提交', async () => {
    const revise = vi.fn()
    const comments: MaterialComment[] = [
      ...COMMENTS,
      { ...COMMENTS[0]!, id: 'resolved', status: 'resolved' },
      { ...COMMENTS[0]!, id: 'material', status: 'needs_material' },
    ]
    render(<QuestionRevisionAction state={state({ revise })} comments={comments} />)

    await userEvent.click(screen.getByRole('button', { name: '提交修改' }))

    expect(revise).toHaveBeenCalledWith([COMMENTS[0]])
  })

  it('查看历史版本时禁止提交并解释原因', () => {
    const historical = version(2)
    render(
      <QuestionRevisionAction
        state={state({ selectedVersion: historical, selectedVersionId: historical.id })}
        comments={COMMENTS}
      />,
    )
    expect(screen.getByRole('button', { name: '提交修改' })).toBeDisabled()
    expect(screen.getByText('只能基于当前采用版本提交修改。')).toBeInTheDocument()
  })

  it('显示修改进度，并阻止重复提交', () => {
    render(
      <QuestionRevisionAction
        state={state({
          revisionStage: 'auditing',
          revisionRequest: {
            request_id: 'request-1',
            status: 'running',
            stage: 'auditing',
            base_version_id: 'version-3',
            comment_count: 1,
          },
        })}
        comments={COMMENTS}
      />,
    )
    expect(screen.getByRole('button', { name: '正在独立复评' })).toBeDisabled()
    const progress = screen.getByRole('status')
    expect(progress).toHaveTextContent('已提交 1 条批注 · 基于 V3')
    expect(progress).toHaveTextContent('Agent 正在修改')
    expect(progress).toHaveTextContent('完整校验')
    expect(progress).toHaveTextContent('独立盲审')
    expect(progress).toHaveTextContent('请勿重复提交')
  })

  it('需要修改材料时列出逐题原因且不提供题目版本操作', () => {
    render(
      <QuestionRevisionAction
        state={state({
          revisionResult: {
            kind: 'needs_material',
            reasons: [
              {
                comment_id: 'comment-1',
                question_number: 3,
                reason: '录音中存在两个同样合理的答案',
              },
            ],
          },
        })}
        comments={COMMENTS}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('需要修改材料')
    expect(screen.getByRole('alert')).toHaveTextContent('Q3：录音中存在两个同样合理的答案')
    expect(screen.getByRole('alert')).toHaveTextContent('本次已终止')
  })

  it('成功结果持续说明新版本尚未自动采用，并可关闭', async () => {
    const dismissRevisionResult = vi.fn()
    render(
      <QuestionRevisionAction
        state={state({
          revisionResult: {
            kind: 'revised',
            versionId: 'version-4',
            baselineAdvisories: ['Q9 复审结论发生波动'],
          },
          dismissRevisionResult,
        })}
        comments={COMMENTS}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('新版本已生成')
    expect(screen.getByRole('alert')).toHaveTextContent('当前采用版本不会自动改变')
    expect(screen.getByText('查看 1 条基线提醒')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '关闭修改结果' }))
    expect(dismissRevisionResult).toHaveBeenCalledOnce()
  })
})
