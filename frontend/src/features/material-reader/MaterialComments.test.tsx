import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CommentCard, CommentComposer } from './MaterialComments'

describe('单人材料批注', () => {
  it('requires an anchor, severity and text before submitting', async () => {
    const onSubmit = vi.fn().mockResolvedValue(true)
    const { rerender } = render(
      <CommentComposer anchor={null} saving={false} onSubmit={onSubmit} />,
    )
    const submit = screen.getByRole('button', { name: '添加批注' })
    expect(submit).toBeDisabled()

    rerender(
      <CommentComposer
        anchor={{ type: 'question', index: 3 }}
        saving={false}
        onSubmit={onSubmit}
      />,
    )
    await userEvent.type(screen.getByRole('textbox', { name: '批注内容' }), '题面不自然')
    expect(submit).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: '一般' }))
    expect(submit).toBeEnabled()
    await userEvent.click(submit)

    expect(onSubmit).toHaveBeenCalledWith({
      anchor: { type: 'question', index: 3 },
      severity: 'major',
      text: '题面不自然',
    })
  })

  it('navigates from the card and exposes delete as a separate action', async () => {
    const onNavigate = vi.fn()
    const onDelete = vi.fn()
    render(
      <CommentCard
        comment={{
          id: 'c1',
          created_at: '2026-08-09T14:30:00Z',
          anchor: { type: 'turn', index: 4 },
          severity: 'critical',
          text: '拼读确认过于直接',
        }}
        disabled={false}
        onNavigate={onNavigate}
        onDelete={onDelete}
      />,
    )

    await userEvent.click(screen.getByText('拼读确认过于直接'))
    expect(onNavigate).toHaveBeenCalledWith({ type: 'turn', index: 4 })
    await userEvent.click(screen.getByRole('button', { name: '删除 Turn 4 的批注' }))
    expect(onDelete).toHaveBeenCalledWith('c1')
  })

  it('renders handled comments as read-only with their outcome', () => {
    const onDelete = vi.fn()
    render(
      <CommentCard
        comment={{
          id: 'c2',
          created_at: '2026-08-11T14:30:00Z',
          anchor: { type: 'question', index: 5 },
          severity: 'major',
          text: '修改金额题',
          version_id: 'original',
          status: 'resolved',
          resolved_by_version_id: 'version-2',
        }}
        disabled={false}
        resolvedVersionLabel={() => 'V2'}
        onNavigate={vi.fn()}
        onDelete={onDelete}
      />,
    )

    expect(screen.getByText('已在 V2 处理')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '删除 Q5 的批注' })).toBeDisabled()
    expect(screen.getByText('修改金额题').closest('article')).toHaveClass('resolved')
  })

  it('marks material-dependent comments with the matching read-only class', () => {
    render(
      <CommentCard
        comment={{
          id: 'c3',
          created_at: '2026-08-11T14:30:00Z',
          anchor: { type: 'question', index: 6 },
          severity: 'major',
          text: '需要改录音原文',
          status: 'needs_material',
        }}
        disabled={false}
        onNavigate={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    expect(screen.getByText('需修改材料')).toBeInTheDocument()
    expect(screen.getByText('需要改录音原文').closest('article')).toHaveClass(
      'needs_material',
    )
    expect(screen.getByRole('button', { name: '删除 Q6 的批注' })).toBeDisabled()
  })

  it('shows a no-change decision with durable reason and evidence', () => {
    render(
      <CommentCard
        comment={{
          id: 'c4',
          created_at: '2026-08-11T14:30:00Z',
          anchor: { type: 'question', index: 4 },
          severity: 'major',
          text: '答案可能不对',
          status: 'no_change',
          decision_reason: '现有答案与材料一致。',
          decision_references: ['题面', '标准答案', '材料证据'],
        }}
        disabled={false}
        onNavigate={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    expect(screen.getByText('无需修改')).toBeInTheDocument()
    expect(screen.getByText(/现有答案与材料一致/)).toBeInTheDocument()
    expect(screen.getByText(/题面；标准答案；材料证据/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '删除 Q4 的批注' })).toBeDisabled()
  })

  it('disables the composer for a historical version', () => {
    render(
      <CommentComposer
        anchor={{ type: 'question', index: 3 }}
        saving={false}
        disabled
        onSubmit={vi.fn()}
      />,
    )

    expect(screen.getByText('历史版本仅供查看')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '批注内容' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '添加批注' })).toBeDisabled()
  })
})
