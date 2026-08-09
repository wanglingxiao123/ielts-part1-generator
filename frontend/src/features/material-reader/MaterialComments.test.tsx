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
})
