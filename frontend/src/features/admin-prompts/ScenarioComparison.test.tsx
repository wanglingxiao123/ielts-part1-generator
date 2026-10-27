import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { promptScenarios, type ScenarioRun } from '@/api/promptScenarios'
import type { PromptState } from '@/api/adminPrompts'
import { ScenarioComparison } from './ScenarioComparison'
import { BASE_MATERIAL, BASE_BLUEPRINT, BASE_AUDIT_ALIGNED, CROSS_CHECK_ALIGNED } from '@/mocks/fixtures'

vi.mock('@/api/promptScenarios', () => ({ promptScenarios: { start: vi.fn(), retry: vi.fn(), detail: vi.fn(), history: vi.fn() } }))
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
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', '') } },
    close: { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute('open') } },
  })
  vi.mocked(promptScenarios.history).mockResolvedValue({ items: [], next_offset: null })
  vi.mocked(promptScenarios.start).mockResolvedValue({ run_id: result.run_id, reused: false })
  vi.mocked(promptScenarios.detail).mockResolvedValue(result)
})
afterEach(() => {
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close
  vi.resetAllMocks()
})

it('uses the existing information-point reader with expandable annotations', async () => {
  vi.mocked(promptScenarios.detail).mockResolvedValue({
    ...result, sides: { ...result.sides, candidate: {
      status: 'succeeded', material_result: {
        material: BASE_MATERIAL, blueprint: BASE_BLUEPRINT,
        audit: BASE_AUDIT_ALIGNED, cross_check: CROSS_CHECK_ALIGNED,
      },
    } },
  })
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  const toggle = await screen.findByRole('button', { name: '展开旁注' })
  await userEvent.click(toggle)
  expect(screen.getByRole('button', { name: '折叠旁注' })).toHaveAttribute('aria-expanded', 'true')
  expect(document.querySelectorAll('.ann-card').length).toBeGreaterThan(0)
})

it('retries only the failed side with the server-projected action and attempt', async () => {
  vi.mocked(promptScenarios.detail).mockResolvedValue({
    ...result, sides: { ...result.sides, baseline: {
      ...result.sides.baseline, retry_action: 'resume', attempt: 2,
    } },
  })
  vi.mocked(promptScenarios.retry).mockResolvedValue({ run_id: result.run_id, reused: false })
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  await userEvent.click(await screen.findByRole('button', { name: '继续生成基线版本' }))
  expect(promptScenarios.retry).toHaveBeenCalledWith(result.run_id, 'baseline', 2)
  expect(screen.queryByRole('button', { name: /生成候选版本/ })).not.toBeInTheDocument()
})

it('shows the current pipeline module while waiting for artifacts', async () => {
  vi.mocked(promptScenarios.detail).mockResolvedValue({
    ...result, status: 'running', sides: { ...result.sides, baseline: {
      status: 'running', stage: 'question_cross_check', events: [
        { stage: 'generating' }, { stage: 'question_generation_started' },
        { stage: 'question_cross_check' },
      ],
    } },
  })
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  expect(await screen.findByText('当前环节：题目审核')).toBeInTheDocument()
  expect(screen.getByLabelText('生成进度')).toBeInTheDocument()
})

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
  await userEvent.click(screen.getByRole('button', { name: '场景对比历史' }))
  const button = await screen.findByRole('button', { name: /酒店预订/ })
  await userEvent.click(button)
  await screen.findByText('The room costs 105 pounds.')
  await userEvent.click(screen.getByRole('button', { name: '场景对比历史' }))
  await userEvent.click(button)
  await screen.findByText('The room costs 105 pounds.')
  await waitFor(() => expect(promptScenarios.detail).toHaveBeenCalledTimes(2))
  expect(promptScenarios.start).not.toHaveBeenCalled()
})

it('returns from historical inspection to the current comparison without starting again', async () => {
  const older = { ...result, run_id: 'b'.repeat(32), scenario: { ...result.scenario, title: '历史酒店' } }
  vi.mocked(promptScenarios.history).mockResolvedValue({ items: [older], next_offset: null })
  vi.mocked(promptScenarios.detail).mockImplementation(async (id) => id === older.run_id ? older : result)
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '开始完整对比' }))
  await screen.findByText('The room costs 105 pounds.')
  await userEvent.click(screen.getByRole('button', { name: '场景对比历史' }))
  await userEvent.click(await screen.findByRole('button', { name: /历史酒店/ }))
  await screen.findByText(/正在查看历史对比/)
  await userEvent.click(screen.getByRole('button', { name: '返回当前对比' }))
  await waitFor(() => expect(promptScenarios.detail).toHaveBeenLastCalledWith(result.run_id, expect.any(AbortSignal)))
  expect(promptScenarios.start).toHaveBeenCalledTimes(1)
})

it('keeps historical read errors visible after hiding the new-comparison form', async () => {
  vi.mocked(promptScenarios.history).mockResolvedValue({ items: [result], next_offset: null })
  vi.mocked(promptScenarios.detail).mockRejectedValue(new Error('offline'))
  render(<ScenarioComparison state={state} dirty={false} />)
  await userEvent.click(screen.getByRole('button', { name: '场景对比历史' }))
  await userEvent.click(await screen.findByRole('button', { name: /酒店预订/ }))
  expect(await screen.findByRole('alert')).toBeVisible()
  expect(screen.getByRole('button', { name: '返回当前对比' })).toBeEnabled()
})
