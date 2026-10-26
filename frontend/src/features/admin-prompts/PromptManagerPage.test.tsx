import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { adminPrompts, type PromptState } from '@/api/adminPrompts'
import { ApiError } from '@/api/http'
import { SessionContext, type Session } from '@/auth/sessionContext'
import { App } from '@/App'
import { PromptManagerPage } from './PromptManagerPage'

vi.mock('@/api/adminPrompts', () => ({
  adminPrompts: { get: vi.fn(), save: vi.fn(), publish: vi.fn(), rollback: vi.fn(), test: vi.fn(), result: vi.fn(), history: vi.fn(), restoreDefaults: vi.fn() },
}))
vi.mock('@/stores/modelPreference', () => ({
  useModelPreference: () => ({
    selectedModelId: 'model-a', models: [{ id: 'model-a', label: 'Model A' }], loading: false,
  }),
  loadModels: vi.fn(),
  selectModel: vi.fn(),
}))

const initial: PromptState = {
  catalogue: [
    { id: 'generate', name: '材料生成', purpose: '编写对话', default: '默认生成规则', source: 'agents.py', protected: ['输出 JSON', '必需变量 {input}'] },
    { id: 'audit', name: '独立审核', purpose: '审核对话', default: '默认审核规则', source: 'audit.py', protected: '只读审核权限' },
  ],
  active: { release_id: 'release-1', values: { generate: '当前生成规则', audit: '当前审核规则' } },
  bundled: { release_id: 'bundled-default', values: { generate: '默认生成规则', audit: '默认审核规则' } },
  active_etag: 'active-1',
  draft: { values: { generate: '草稿生成规则', audit: '草稿审核规则' } },
  draft_etag: 'draft-1',
  history: [
    { release_id: 'release-1', actor: 'admin@example.com', created_at: '2026-10-24T10:00:00Z' },
    { release_id: 'release-0', actor: 'other@example.com', created_at: '2026-10-23T10:00:00Z' },
  ],
  skills: [{ path: 'generation/SKILL.md', text: '固定的 Skill 规则' }],
}

function session(isAdmin = true): Session {
  return {
    isAuthenticated: true, isLoading: false, isAdmin,
    email: 'admin@example.com', user: null, error: null,
    signIn: vi.fn(), signUp: vi.fn(), signOut: vi.fn(),
  }
}

function renderPage(isAdmin = true, app = false) {
  return render(
    <SessionContext.Provider value={session(isAdmin)}>
      <MemoryRouter initialEntries={['/admin/prompts']}>
        {app ? <App /> : <PromptManagerPage />}
      </MemoryRouter>
    </SessionContext.Provider>,
  )
}

async function ready() {
  renderPage()
  await screen.findByLabelText('草稿业务文本')
}

beforeEach(() => {
  vi.mocked(adminPrompts.get).mockResolvedValue(structuredClone(initial))
})
afterEach(() => vi.resetAllMocks())

describe('administrator prompt manager', () => {
  it('blocks direct non-admin navigation without requesting prompt data', async () => {
    renderPage(false, true)
    expect(screen.getByRole('alert')).toHaveTextContent('仅管理员')
    expect(screen.queryByRole('link', { name: '提示词管理' })).not.toBeInTheDocument()
    expect(adminPrompts.get).not.toHaveBeenCalled()
  })

  it('mounts the admin route and shows current/draft text and read-only rules', async () => {
    renderPage(true, true)
    await screen.findByLabelText('草稿业务文本')
    expect(screen.getByRole('link', { name: '提示词管理' })).toHaveAttribute('href', '/admin/prompts')
    expect(screen.getByLabelText('当前生效文本')).toHaveValue('当前生成规则')
    expect(screen.getByLabelText('当前生效文本')).toHaveAttribute('readonly')
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('草稿生成规则')
    expect(screen.getByText('固定的 Skill 规则').tagName).toBe('PRE')
    expect(screen.getByText(/输出 JSON/)).toBeInTheDocument()
    expect(screen.getByText(/这不是完整材料生成流程/)).toBeInTheDocument()
  })

  it('retains edits while switching prompts and saves the full draft without publishing', async () => {
    await ready()
    fireEvent.change(screen.getByLabelText('草稿业务文本'), { target: { value: '新的业务规则' } })
    await userEvent.click(screen.getByRole('button', { name: '选择提示词：独立审核' }))
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('草稿审核规则')
    await userEvent.click(screen.getByRole('button', { name: '选择提示词：材料生成' }))
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('新的业务规则')
    expect(screen.getByRole('button', { name: '发布整套草稿' })).toBeDisabled()
    const values = { generate: '新的业务规则', audit: '草稿审核规则' }
    vi.mocked(adminPrompts.save).mockResolvedValue({ ...initial, draft: { values }, draft_etag: 'draft-2' })
    await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    expect(adminPrompts.save).toHaveBeenCalledWith(values, 'draft-1')
    await screen.findByText('草稿已保存，尚未发布。')
    expect(adminPrompts.publish).not.toHaveBeenCalled()
  })

  it('restores defaults locally, then saves without publishing', async () => {
    await ready()
    await userEvent.click(screen.getByRole('button', { name: '恢复全部默认文本' }))
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('默认生成规则')
    expect(adminPrompts.save).not.toHaveBeenCalled()
    const values = { generate: '默认生成规则', audit: '默认审核规则' }
    vi.mocked(adminPrompts.save).mockResolvedValue({ ...initial, draft: { values } })
    await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    expect(adminPrompts.save).toHaveBeenCalledWith(values, 'draft-1')
    expect(adminPrompts.publish).not.toHaveBeenCalled()
  })

  it('retains unsaved edits on 409, refreshes tokens explicitly, then saves', async () => {
    await ready()
    fireEvent.change(screen.getByLabelText('草稿业务文本'), { target: { value: '保留我的编辑' } })
    vi.mocked(adminPrompts.save).mockRejectedValueOnce(new ApiError(409, 'CONFLICT', 'stale'))
    await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('版本冲突')
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('保留我的编辑')
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled()
    const next = { ...initial, draft_etag: 'draft-2', active_etag: 'active-2' }
    vi.mocked(adminPrompts.get).mockResolvedValue(next)
    await userEvent.click(screen.getByRole('button', { name: /刷新服务器版本/ }))
    await screen.findByText(/已刷新服务器版本，本地编辑已保留/)
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('保留我的编辑')
    vi.mocked(adminPrompts.save).mockResolvedValue(next)
    await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    expect(adminPrompts.save).toHaveBeenLastCalledWith({ generate: '保留我的编辑', audit: '草稿审核规则' }, 'draft-2')
  })

  it('keeps local edits on a validation error and lets the user retry', async () => {
    await ready()
    fireEvent.change(screen.getByLabelText('草稿业务文本'), { target: { value: '无效但需保留' } })
    vi.mocked(adminPrompts.save).mockRejectedValue(new ApiError(422, 'INVALID', '缺少必需变量'))
    await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('缺少必需变量')
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('无效但需保留')
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeEnabled()
  })

  it.each(['publish', 'rollback'] as const)('handles a %s conflict without replacing the editor', async (operation) => {
    await ready()
    await userEvent.click(screen.getByRole('button', { name: '发布记录' }))
    vi.mocked(adminPrompts[operation]).mockRejectedValue(new ApiError(409, 'CONFLICT', 'stale'))
    await userEvent.click(screen.getByRole('button', {
      name: operation === 'publish' ? '发布整套草稿' : '回滚至 release-0',
    }))
    expect(await screen.findByRole('alert')).toHaveTextContent('版本冲突')
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('草稿生成规则')
    expect(screen.getByRole('button', { name: '发布整套草稿' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: '发布记录' }))
    expect(screen.getByRole('button', { name: '回滚至 release-0' })).toBeDisabled()
  })

  it('requires saved edits before trial or rollback', async () => {
    await ready()
    await userEvent.click(screen.getByRole('button', { name: '试运行' }))
    fireEvent.change(screen.getByLabelText('输入样例'), { target: { value: '测试场景' } })
    fireEvent.change(screen.getByLabelText('草稿业务文本'), { target: { value: '未保存的草稿' } })
    expect(screen.getByRole('button', { name: '运行对比' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: '发布记录' }))
    expect(screen.getByRole('button', { name: '回滚至 release-0' })).toBeDisabled()
    expect(adminPrompts.test).not.toHaveBeenCalled()
  })

  it('publishes using both tokens and rolls back with the updated active token', async () => {
    await ready()
    const published = { ...initial, active: { release_id: 'release-2', values: initial.draft!.values }, active_etag: 'active-2', draft: null, draft_etag: null }
    vi.mocked(adminPrompts.publish).mockResolvedValue(published)
    await userEvent.click(screen.getByRole('button', { name: '发布整套草稿' }))
    expect(adminPrompts.publish).toHaveBeenCalledWith('active-1', 'draft-1')
    await screen.findByText('整套配置已发布，仅对新任务生效。')
    vi.mocked(adminPrompts.rollback).mockResolvedValue({ ...published, active: { ...published.active, release_id: 'rollback-3' } })
    await userEvent.click(screen.getByRole('button', { name: '发布记录' }))
    await userEvent.click(screen.getByRole('button', { name: '回滚至 release-0' }))
    expect(adminPrompts.rollback).toHaveBeenCalledWith('release-0', 'active-2')
    await screen.findByText('rollback-3')
  })

  it('can create a draft from bundled defaults when no draft exists', async () => {
    vi.mocked(adminPrompts.get).mockResolvedValue({ ...initial, active: { release_id: 'bundled', values: {} }, active_etag: null, draft: null, draft_etag: null })
    await ready()
    expect(screen.getByLabelText('草稿业务文本')).toHaveValue('默认生成规则')
    expect(screen.getByRole('button', { name: '发布整套草稿' })).toBeDisabled()
    vi.mocked(adminPrompts.save).mockResolvedValue(initial)
    await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    expect(adminPrompts.save).toHaveBeenCalledWith({ generate: '默认生成规则', audit: '默认审核规则' }, null)
  })

  it('runs the selected saved task with model and etag, and compares terminal outputs', async () => {
    await ready()
    vi.mocked(adminPrompts.test).mockResolvedValue({ test_id: 'test-1' })
    vi.mocked(adminPrompts.result).mockResolvedValueOnce({ status: 'running' })
      .mockResolvedValue({ status: 'completed', baseline: '当前输出样例', draft: '草稿输出样例', model_id: 'model-a' })
    await userEvent.click(screen.getByRole('button', { name: '试运行' }))
    fireEvent.change(screen.getByLabelText('输入样例'), { target: { value: '测试场景' } })
    await userEvent.click(screen.getByRole('button', { name: '运行对比' }))
    expect(adminPrompts.test).toHaveBeenCalledWith({ prompt_id: 'generate', input: '测试场景', model_id: 'model-a', draft_etag: 'draft-1' })
    expect(await screen.findByText('正在运行独立对比…')).toBeInTheDocument()
    expect(await screen.findByText('草稿输出样例', {}, { timeout: 3000 })).toBeInTheDocument()
    expect(screen.getByText('当前输出样例')).toBeInTheDocument()
    expect(adminPrompts.publish).not.toHaveBeenCalled()
  })

  it('retries failed result reads without creating another trial and shows trial failure', async () => {
    await ready()
    vi.mocked(adminPrompts.test).mockResolvedValue({ test_id: 'test-2' })
    vi.mocked(adminPrompts.result).mockRejectedValueOnce(new ApiError(503, 'UNAVAILABLE', '稍后重试'))
      .mockResolvedValue({ status: 'failed', error: '试运行模型失败' })
    await userEvent.click(screen.getByRole('button', { name: '试运行' }))
    fireEvent.change(screen.getByLabelText('输入样例'), { target: { value: '测试场景' } })
    await userEvent.click(screen.getByRole('button', { name: '运行对比' }))
    await userEvent.click(await screen.findByRole('button', { name: '重试查询结果' }))
    expect(await screen.findByText('试运行模型失败')).toBeInTheDocument()
    expect(adminPrompts.test).toHaveBeenCalledTimes(1)
  })

  it('aborts prompt loading when administrator access is removed', async () => {
    let resolve: (value: PromptState) => void = () => {}
    vi.mocked(adminPrompts.get).mockReturnValue(new Promise((done) => { resolve = done }))
    const view = renderPage()
    const signal = vi.mocked(adminPrompts.get).mock.calls[0]![0]
    view.rerender(<SessionContext.Provider value={session(false)}><PromptManagerPage /></SessionContext.Provider>)
    expect(signal?.aborted).toBe(true)
    await act(async () => resolve(initial))
    expect(screen.queryByLabelText('草稿业务文本')).not.toBeInTheDocument()
  })

  it('aborts the trial observer on unmount', async () => {
    const view = renderPage()
    await screen.findByLabelText('草稿业务文本')
    vi.mocked(adminPrompts.test).mockResolvedValue({ test_id: 'test-observer' })
    vi.mocked(adminPrompts.result).mockImplementation(() => new Promise(() => {}))
    await userEvent.click(screen.getByRole('button', { name: '试运行' }))
    fireEvent.change(screen.getByLabelText('输入样例'), { target: { value: '测试场景' } })
    await userEvent.click(screen.getByRole('button', { name: '运行对比' }))
    const signal = vi.mocked(adminPrompts.result).mock.calls[0]![1]
    view.unmount()
    expect(signal?.aborted).toBe(true)
  })

  it('recovers an initial load error through explicit refresh', async () => {
    vi.mocked(adminPrompts.get).mockRejectedValueOnce(new ApiError(503, 'UNAVAILABLE', '配置暂不可用'))
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('配置暂不可用')
    await userEvent.click(screen.getByRole('button', { name: /刷新服务器版本/ }))
    await waitFor(() => expect(screen.getByLabelText('草稿业务文本')).toHaveValue('草稿生成规则'))
  })
})

it('clears a completed trial and input after publishing a new active version', async () => {
  await ready()
  vi.mocked(adminPrompts.test).mockResolvedValue({ test_id: 'old-test' })
  vi.mocked(adminPrompts.result).mockResolvedValue({ status: 'completed', baseline: '旧版本结果', draft: '旧草稿结果' })
  await userEvent.click(screen.getByRole('button', { name: '试运行' }))
  fireEvent.change(screen.getByLabelText('输入样例'), { target: { value: '旧输入' } })
  await userEvent.click(screen.getByRole('button', { name: '运行对比' }))
  await screen.findByText('旧草稿结果')
  vi.mocked(adminPrompts.publish).mockResolvedValue({ ...initial, active: { ...initial.active, release_id: 'new-release' } })
  await userEvent.click(screen.getByRole('button', { name: '发布整套草稿' }))
  await screen.findByText('整套配置已发布，仅对新任务生效。')
  expect(screen.getByLabelText('输入样例')).toHaveValue('')
  expect(screen.queryByText('旧草稿结果')).not.toBeInTheDocument()
  expect(screen.queryByText(/old-test/)).not.toBeInTheDocument()
})

it('shows immutable historical prompt text and reuses input without restoring old versions', async () => {
  await ready()
  vi.mocked(adminPrompts.history).mockResolvedValue({ items: [{ test_id: 'history-1', status: 'completed', prompt_id: 'audit', model_id: 'model-a', has_input: true }], next_offset: null })
  vi.mocked(adminPrompts.result).mockResolvedValue({ status: 'completed', prompt_id: 'audit', input: '停车对话', baseline_prompt: '历史基线规则', draft_prompt: '历史草稿规则', baseline: '历史生效输出', draft: '历史草稿输出' })
  await userEvent.click(screen.getByRole('button', { name: '试运行历史' }))
  await userEvent.click(await screen.findByRole('button', { name: /独立审核/ }))
  await screen.findByText('历史草稿输出')
  expect(screen.getByText('历史基线规则')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '复用输入' }))
  expect(screen.getByLabelText('输入样例')).toHaveValue('停车对话')
  expect(screen.getByLabelText('草稿业务文本')).toHaveValue('草稿审核规则')
  expect(screen.queryByText('历史草稿输出')).not.toBeInTheDocument()
  expect(adminPrompts.test).not.toHaveBeenCalled()
  expect(adminPrompts.rollback).not.toHaveBeenCalled()
})

it('labels missing legacy input and snapshots without substituting current text', async () => {
  await ready()
  vi.mocked(adminPrompts.history).mockResolvedValue({ items: [{ test_id: 'legacy', status: 'completed', prompt_id: 'audit', has_input: false }], next_offset: null })
  vi.mocked(adminPrompts.result).mockResolvedValue({ status: 'completed', prompt_id: 'audit', baseline: '旧输出' })
  await userEvent.click(screen.getByRole('button', { name: '试运行历史' }))
  await userEvent.click(await screen.findByRole('button', { name: /独立审核/ }))
  await screen.findByText('该记录未保存输入。')
  expect(screen.getByRole('button', { name: '复用输入' })).toBeDisabled()
  expect(screen.getAllByText('该记录未保存可恢复的提示词快照。')).toHaveLength(2)
})

it('requires preview confirmation before publishing bundled defaults', async () => {
  await ready()
  await userEvent.click(screen.getByRole('button', { name: '发布记录' }))
  await userEvent.click(screen.getByRole('button', { name: '恢复内置默认提示词' }))
  expect(adminPrompts.restoreDefaults).not.toHaveBeenCalled()
  expect(screen.getByText('默认生成规则')).toBeInTheDocument()
  vi.mocked(adminPrompts.restoreDefaults).mockResolvedValue({ ...initial, active: { release_id: 'restored', values: initial.bundled!.values } })
  await userEvent.click(screen.getByRole('button', { name: '确认恢复并发布' }))
  expect(adminPrompts.restoreDefaults).toHaveBeenCalledWith('bundled-default', 'active-1')
  await screen.findByText('已恢复内置默认提示词，并保留发布历史。')
})
