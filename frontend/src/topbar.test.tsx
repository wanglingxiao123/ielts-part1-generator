/**
 * 顶栏的三个页签。
 *
 * 客户的原话：「三个 Tab 应该始终都能切换，不存在『灰置不可点』的情况。」
 *
 * 「生成结果」原来在 `store.batchId` 为空时渲染成一个不可点的 `<span>`。而 `store.batchId` 是**本页
 * 会话**的批次、刷新即空，所以 S3 里躺着十几个历史批次的用户打开页面，看到的是一个灰的页签——他得先
 * 勾一个场景、提交一次生成，才能看见自己以前生成的东西。
 *
 * 这个文件守的就是那件事不再发生：三个页签都是链接，任何 store 状态下都是。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from '@/auth/AuthProvider'
import { setAuthFetch } from '@/auth/authApi'
import { useBatchStore } from '@/stores/batchStore'
import { App } from './App'

const USER = { email: 'a@amazon.com', is_admin: false, created_at: 1_700_000_000 }

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  useBatchStore.getState().reset()
  // 已登录：页签只在有会话时渲染（匿名访客的每个目标都在 RequireAuth 后面）。
  setAuthFetch((path) =>
    Promise.resolve(
      path.endsWith('/me')
        ? jsonResponse({ user: USER })
        : jsonResponse({ error: { code: 'X', message: 'x' } }, 400),
    ),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  useBatchStore.getState().reset()
})

async function renderTopBar() {
  // 渲染真实的 `App`（Router 在 main.tsx 里，所以这里自己包一个），而不是把 TopBar 单独导出来测。
  // 页签是 App 的一部分，测它就该按用户看到的样子测。
  render(
    <MemoryRouter initialEntries={['/']}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
  // 等会话确认完成，否则页签还没渲染。
  await waitFor(() => expect(screen.getByText(USER.email)).toBeInTheDocument())
}

describe('三个页签始终可点', () => {
  it('没有活批次时「生成结果」仍是链接', async () => {
    // 这就是客户报的场景：刚打开页面，store 是空的，但 S3 里有历史。
    await renderTopBar()

    for (const name of ['场景选择', '生成结果', '审核队列']) {
      const link = screen.getByRole('link', { name })
      expect(link, `「${name}」不是链接`).toBeInTheDocument()
    }
    // 「生成结果」指向无 id 的落地页，由它去找最近一批。
    expect(screen.getByRole('link', { name: '生成结果' }).getAttribute('href')).toBe('/batches')
  })

  it('页面上没有任何灰置的页签', async () => {
    await renderTopBar()
    // 那个 `<span class="nav-disabled">` 曾是唯一的灰置项，样式也已一并删除。
    expect(document.querySelector('.nav-disabled')).toBeNull()
    expect(document.querySelectorAll('.topbar nav a').length).toBe(3)
  })

  it('有活批次时「生成结果」直接指向那一批', async () => {
    // 生成过程中点这个页签必须立刻回到那一批，不该先绕一次历史查询——那一批可能还没写完索引。
    useBatchStore.setState({ batchId: 'web-live-9' })
    await renderTopBar()

    expect(screen.getByRole('link', { name: '生成结果' }).getAttribute('href')).toBe(
      '/batches/web-live-9',
    )
  })
})
