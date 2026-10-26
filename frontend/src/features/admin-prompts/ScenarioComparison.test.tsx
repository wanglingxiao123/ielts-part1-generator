import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { promptScenarios, type ScenarioRun } from '@/api/promptScenarios'
import type { PromptState } from '@/api/adminPrompts'
import { ScenarioComparison } from './ScenarioComparison'

vi.mock('@/api/promptScenarios', () => ({ promptScenarios: { start: vi.fn(), detail: vi.fn(), history: vi.fn() } }))
vi.mock('@/stores/modelPreference', () => ({ useModelPreference: () => ({ selectedModelId: 'kimi', models: [{ id: 'kimi', label: 'Kimi' }] }) }))
const state: PromptState = {
  catalogue: [], active: { release_id: 'b2', values: {} }, active_etag: 'active-token',
  draft: { values: {} }, draft_etag: 'draft-token', bundled: { release_id: 'bundled', values: {} },
  history: [{ release_id: 'b1', actor: 'admin', created_at: 100 }, { release_id: 'b2', actor: 'admin', created_at: 200 }], skills: [],
}
const result: ScenarioRun = {
  run_id: 'a'.repeat(32), scenario: { id: 'booking-hotel', title: '酒店预订' }, model_id: 'kimi', created_at: 123, actor: 'admin', status: 'finished_with_issues',
  versions: { baseline: { source_release: 'b1', release_id: 'snapshot-1', digest: 'd1' }, candidate: { source_release: 'b2', release_id: 'snapshot-2', digest: 'd2' } },
  prompt_changes: [{ id: 'role.audit', name: '材料盲审专家', baseline: 'B1规则', candidate: 'B2规则' }],
  sides: { baseline: { status: 'failed', error: '模型超时', counts: { generating: 2 } }, candidate: { status: 'succeeded', material_result: { material: { listening_material_parts: [{ script: { turns: [{ speaker: 'Receptionist', text: 'The room costs 105 pounds.' }] } }] } }, counts: { generating: 1 } } },
}
beforeEach(() => {
  vi.mocked(promptScenarios.history).mockResolvedValue({ items: [], next_offset: null })
  vi.mocked(promptScenarios.start).mockResolvedValue({ run_id: result.run_id, reused: false })
  vi.mocked(promptScenarios.detail).mockResolvedValue(result)
})
afterEach(() => vi.resetAllMocks())

it('starts active versus draft with one scenario and preserves authoritative result versions', async () => {
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  await screen.findByText('The room costs 105 pounds.')
  expect(promptScenarios.start).toHaveBeenCalledWith(expect.objectContaining({ baseline: 'active', candidate: 'draft', scenario_id: 'booking-hotel', model_id: 'kimi', active_etag: 'active-token', draft_etag: 'draft-token' }))
  const request = vi.mocked(promptScenarios.start).mock.calls[0]![0]
  expect(request.request_id).toMatch(/^[a-f0-9]{32}$/)
  expect(screen.getByText('模型超时')).toBeInTheDocument()
  expect(screen.getByText(/snapshot-1/)).toBeInTheDocument()
})

it('allows historical B1 versus B2 without publishing or modifying drafts', async () => {
  render(<ScenarioComparison state={state} dirty={false} />)
  fireEvent.change(screen.getByLabelText('基线版本'), { target: { value: 'b1' } })
  fireEvent.change(screen.getByLabelText('候选版本'), { target: { value: 'b2' } })
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  expect(promptScenarios.start).toHaveBeenCalledWith(expect.objectContaining({ baseline: 'b1', candidate: 'b2' }))
})

it('blocks unsaved editor changes', () => {
  render(<ScenarioComparison state={state} dirty />)
  expect(screen.getByRole('button', { name: '开始完整对比' })).toBeDisabled()
})

it('retries an uncertain POST with exactly the original id and settings', async () => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.mocked(promptScenarios.start).mockRejectedValueOnce(new TypeError('offline'))
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  await userEvent.click(await screen.findByRole('button', { name: '重试确认同一请求' }))
  expect(vi.mocked(promptScenarios.start).mock.calls[0]![0]).toEqual(vi.mocked(promptScenarios.start).mock.calls[1]![0])
  vi.restoreAllMocks()
})

it('opens persisted history and reopens the same run without an empty loading panel', async () => {
  vi.mocked(promptScenarios.history).mockResolvedValue({ items: [result], next_offset: null })
  render(<ScenarioComparison state={state} dirty={false} />)
  const button = await screen.findByRole('button', { name: /酒店预订/ })
  await userEvent.click(button)
  await screen.findByText('The room costs 105 pounds.')
  await userEvent.click(button)
  await screen.findByText('The room costs 105 pounds.')
  await waitFor(() => expect(promptScenarios.detail).toHaveBeenCalledTimes(2))
  expect(promptScenarios.start).not.toHaveBeenCalled()
})
