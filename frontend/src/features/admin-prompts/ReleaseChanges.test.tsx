import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { adminPrompts } from '@/api/adminPrompts'
import { ReleaseChanges } from './ReleaseChanges'
import { changedLines } from './releaseDiff'

vi.mock('@/api/adminPrompts', () => ({ adminPrompts: { release: vi.fn() } }))
afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })

it('shows changed modules, lazily loads both versions, and offers unchanged full text', async () => {
  vi.mocked(adminPrompts.release).mockResolvedValue({
    release_id: 'b2', base_release_id: 'b1',
    values: { audit: '检查价格\n要求复述', generate: '不变的生成规则' },
    changes: [{ id: 'audit', before: '检查价格', after: '检查价格\n要求复述' }],
  })
  render(<ReleaseChanges release={{ release_id: 'b2', actor: 'admin', created_at: 0, changed_ids: ['audit'] }} catalogue={[
    { id: 'audit', name: '材料盲审', purpose: '', default: '', source: '', protected: '' },
    { id: 'generate', name: '材料生成', purpose: '', default: '', source: '', protected: '' },
  ]} />)
  expect(screen.getByText('修改了 1 项提示词')).toBeInTheDocument()
  expect(adminPrompts.release).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: '查看修改' }))
  await screen.findByRole('region', { name: '材料盲审修改后' })
  expect(screen.getByText('要求复述')).toHaveClass('pm-line-added')
  expect(screen.getByRole('button', { name: '复制材料盲审修改前' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '查看完整版本' }))
  expect(screen.getByText('不变的生成规则')).toBeInTheDocument()
})

it('marks changed paragraph ranges without changing the actual text', () => {
  expect(changedLines('same\nold\nend', 'same\nnew\nend').before).toEqual([
    { text: 'same', changed: false }, { text: 'old', changed: true }, { text: 'end', changed: false },
  ])
  expect(changedLines('same', 'same').after.every((row) => !row.changed)).toBe(true)
  expect(changedLines('same\nremove', 'same').before[1]?.changed).toBe(true)
  expect(changedLines('', 'new').after[0]?.text).toBe('new')
})

it('folds shared context, expands it on demand, and copies the complete original', async () => {
  const prefix = Array.from({ length: 12 }, (_, i) => `共同前文 ${i}`).join('\n')
  const suffix = Array.from({ length: 12 }, (_, i) => `共同后文 ${i}`).join('\n')
  const before = `${prefix}\n原来的要求\n${suffix}`, after = `${prefix}\n新的要求\n${suffix}`
  vi.mocked(adminPrompts.release).mockResolvedValue({
    release_id: 'b2', base_release_id: 'b1', values: { audit: after },
    changes: [{ id: 'audit', before, after }],
  })
  const user = userEvent.setup()
  vi.stubGlobal('isSecureContext', true)
  render(<ReleaseChanges release={{ release_id: 'b2', actor: 'admin', created_at: 0, changed_ids: ['audit'] }} catalogue={[
    { id: 'audit', name: '材料盲审', purpose: '', default: '', source: '', protected: '' },
  ]} />)
  await user.click(screen.getByRole('button', { name: '查看修改' }))
  await screen.findByText('新的要求')
  expect(screen.queryByText('共同前文 0')).not.toBeInTheDocument()
  expect(screen.getAllByText('共同前文 11')).toHaveLength(2)
  await user.click(screen.getByRole('button', { name: '复制材料盲审修改前' }))
  expect(await navigator.clipboard.readText()).toBe(before)
  await user.click(screen.getByRole('button', { name: '显示全部上下文' }))
  expect(screen.getAllByText('共同前文 0')).toHaveLength(2)
  await user.click(screen.getByRole('button', { name: '折叠未修改内容' }))
  expect(screen.queryByText('共同前文 0')).not.toBeInTheDocument()
  vi.unstubAllGlobals()
})
