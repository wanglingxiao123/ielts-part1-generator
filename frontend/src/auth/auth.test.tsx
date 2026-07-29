/**
 * The cookie-session auth flow, driven through the real components.
 *
 * The seam is `setAuthFetch`, i.e. the same one the VITE_MOCK layer uses, so
 * these tests exercise AuthProvider / RequireAuth / LoginPage exactly as the app
 * wires them and only the network is fake. The fake speaks web/app.py's shapes:
 * `{user:{email,is_admin,created_at}}` on 200 and `{error:{code,message}}` on
 * 4xx, with the messages and codes taken from web/auth.py.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './AuthProvider'
import { LoginPage } from './LoginPage'
import { RequireAuth } from './RequireAuth'
import { setAuthFetch, type AuthFetch } from './authApi'
import { useSession } from './useSession'

const USER = { email: 'a@amazon.com', is_admin: false, created_at: 1_700_000_000 }

function ok(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fail(status: number, code: string, message: string): Response {
  return ok({ error: { code, message } }, status)
}

/** A 401 from /api/auth/me is the normal "nobody is signed in" answer. */
const anonymous = () => fail(401, 'UNAUTHENTICATED', 'no session cookie')

interface Server {
  me: () => Response
  login?: (email: string, password: string) => Response
  register?: (email: string, password: string) => Response
}

/** Records every call so a test can assert what was NOT sent, too. */
let calls: Array<{ path: string; body: Record<string, string> }> = []

function install(server: Server) {
  const impl: AuthFetch = (path, init) => {
    const body = init.body
      ? (JSON.parse(String(init.body)) as Record<string, string>)
      : ({} as Record<string, string>)
    calls.push({ path, body })
    if (path === '/auth/me') return Promise.resolve(server.me())
    if (path === '/auth/logout') return Promise.resolve(ok({ ok: true }))
    if (path === '/auth/login') {
      return Promise.resolve(
        server.login?.(body.email ?? '', body.password ?? '') ??
          fail(401, 'INVALID_CREDENTIALS', '邮箱或密码不正确'),
      )
    }
    if (path === '/auth/register') {
      return Promise.resolve(
        server.register?.(body.email ?? '', body.password ?? '') ?? ok({ user: USER }),
      )
    }
    throw new Error(`unexpected auth call ${path}`)
  }
  setAuthFetch(impl)
}

/** The whole shell: a guarded page, the login route, and a topbar sign-out. */
function TopBar() {
  const session = useSession()
  return (
    <div>
      <span>{session.email || '未登录'}</span>
      {session.isAuthenticated && (
        <button type="button" onClick={() => void session.signOut()}>
          退出
        </button>
      )}
    </div>
  )
}

function Shell({ start = '/' }: { start?: string }) {
  return (
    <MemoryRouter initialEntries={[start]}>
      <AuthProvider>
        <TopBar />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <div>场景选择页</div>
              </RequireAuth>
            }
          />
          <Route
            path="/review-queue"
            element={
              <RequireAuth>
                <div>审核队列页</div>
              </RequireAuth>
            }
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  // Restore the default (real fetch) so a leaked stub cannot affect another file.
  setAuthFetch((path, init) => fetch(`/api${path}`, init))
  vi.restoreAllMocks()
})

describe('session discovery', () => {
  it('establishes the session from /api/auth/me on mount, with no token in sight', async () => {
    install({ me: () => ok({ user: USER }) })
    render(<Shell />)

    expect(await screen.findByText('场景选择页')).toBeInTheDocument()
    expect(screen.getByText(USER.email)).toBeInTheDocument()
    expect(calls.map((c) => c.path)).toEqual(['/auth/me'])
  })

  /**
   * The bug this replaces: with `devBypass:false` the app redirected to a Cognito
   * Hosted UI that was never deployed, so an anonymous visitor got a broken
   * navigation instead of a way in. A 401 from /api/auth/me must land on the
   * login form — not a crash, not a blank screen, not an error banner.
   */
  it('sends an unauthenticated visitor to the login form, not a crash or blank screen', async () => {
    install({ me: anonymous })
    render(<Shell />)

    expect(await screen.findByRole('button', { name: '登录' })).toBeInTheDocument()
    expect(screen.getByLabelText('邮箱')).toBeInTheDocument()
    expect(screen.queryByText('场景选择页')).not.toBeInTheDocument()
    // A 401 is an answer, not a failure: no error banner in front of a visitor
    // who has simply not logged in yet.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('surfaces a failed probe as an outage, not as bad credentials', async () => {
    install({ me: () => ok({ error: { code: 'X', message: 'boom' } }, 503) })
    render(<Shell />)

    expect(await screen.findByText('无法确认登录状态')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '登录' })).not.toBeInTheDocument()
  })
})

describe('login', () => {
  it('signs in and returns the user to the page they were headed for', async () => {
    let signedIn = false
    install({
      me: () => (signedIn ? ok({ user: USER }) : anonymous()),
      login: () => {
        signedIn = true
        return ok({ user: USER })
      },
    })
    render(<Shell start="/review-queue" />)

    await screen.findByRole('button', { name: '登录' })
    await userEvent.type(screen.getByLabelText('邮箱'), USER.email)
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    // Back to /review-queue, not to '/': RequireAuth passed the attempted path
    // in router state and LoginPage honoured it.
    expect(await screen.findByText('审核队列页')).toBeInTheDocument()
  })

  it('renders bad credentials as a Chinese sentence, never a raw code', async () => {
    install({ me: anonymous })
    render(<Shell />)

    await screen.findByRole('button', { name: '登录' })
    await userEvent.type(screen.getByLabelText('邮箱'), USER.email)
    await userEvent.type(screen.getByLabelText('密码'), 'wrong-password')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('邮箱或密码不正确')
    expect(alert).not.toHaveTextContent('INVALID_CREDENTIALS')
  })

  it('toggles the password between hidden and visible', async () => {
    install({ me: anonymous })
    render(<Shell />)

    await screen.findByRole('button', { name: '登录' })
    expect(screen.getByLabelText('密码')).toHaveAttribute('type', 'password')
    await userEvent.click(screen.getByLabelText('显示密码'))
    expect(screen.getByLabelText('密码')).toHaveAttribute('type', 'text')
  })
})

describe('register', () => {
  async function openRegister() {
    await screen.findByRole('button', { name: '登录' })
    await userEvent.click(screen.getByRole('button', { name: '没有账号？注册' }))
    await screen.findByLabelText('确认密码')
  }

  /**
   * The confirm field only means anything if a mismatch is caught here: the
   * server never sees the second value, so submitting on a typo would create an
   * account whose password is not what the user thinks they typed.
   */
  it('blocks mismatched passwords client-side and sends no request', async () => {
    install({ me: anonymous })
    render(<Shell />)
    await openRegister()

    await userEvent.type(screen.getByLabelText('邮箱'), USER.email)
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.type(screen.getByLabelText('确认密码'), 'hunter2hunter3')
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('两次输入的密码不一致')
    expect(calls.filter((c) => c.path === '/auth/register')).toHaveLength(0)
    expect(screen.queryByText('注册成功')).not.toBeInTheDocument()
  })

  it('shows the server message when the email domain is not allowed', async () => {
    install({
      me: anonymous,
      register: () =>
        fail(
          403,
          'EMAIL_DOMAIN_NOT_ALLOWED',
          '邮箱域名不在允许列表内（当前允许：amazon.com）',
        ),
    })
    render(<Shell />)
    await openRegister()

    await userEvent.type(screen.getByLabelText('邮箱'), 'outsider@gmail.com')
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.type(screen.getByLabelText('确认密码'), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    // The server's own text, because only it knows the allowlist — but never the
    // code, and the user stays on the form to try another address.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('邮箱域名不在允许列表内（当前允许：amazon.com）')
    expect(alert).not.toHaveTextContent('EMAIL_DOMAIN_NOT_ALLOWED')
    expect(screen.getByLabelText('确认密码')).toBeInTheDocument()
  })

  it('translates an English-only server message rather than showing it raw', async () => {
    install({
      me: anonymous,
      register: () =>
        fail(403, 'EMAIL_DOMAIN_NOT_ALLOWED', "'nope' is not an email address"),
    })
    render(<Shell />)
    await openRegister()

    await userEvent.type(screen.getByLabelText('邮箱'), 'nope@example.com')
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.type(screen.getByLabelText('确认密码'), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('邮箱地址不符合注册要求')
    expect(alert).not.toHaveTextContent('is not an email address')
  })

  it('reports a duplicate account as a readable message', async () => {
    install({ me: anonymous, register: () => fail(409, 'USER_EXISTS', '该邮箱已注册') })
    render(<Shell />)
    await openRegister()

    await userEvent.type(screen.getByLabelText('邮箱'), USER.email)
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.type(screen.getByLabelText('确认密码'), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('该邮箱已注册')
  })

  /**
   * The client's requirement verbatim: 显示邮箱+重复输入两次密码 -> 然后显示注册成功
   * -> 然后才能登录. The backend signs you in on register, so there is no second
   * login to force — but the success state must be SEEN, not skipped past.
   */
  it('confirms 注册成功 explicitly, then enters the app without a second login', async () => {
    install({ me: anonymous, register: () => ok({ user: USER }) })
    render(<Shell />)
    await openRegister()

    await userEvent.type(screen.getByLabelText('邮箱'), USER.email)
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.type(screen.getByLabelText('确认密码'), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    expect(await screen.findByText('注册成功')).toBeInTheDocument()
    // Held on the confirmation rather than jumped straight into the app.
    expect(screen.queryByText('场景选择页')).not.toBeInTheDocument()
    // No /auth/login call: registering already set the cookie.
    expect(calls.filter((c) => c.path === '/auth/login')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: '进入系统' }))
    expect(await screen.findByText('场景选择页')).toBeInTheDocument()
  })

  it('marks the very first account as admin in the success panel', async () => {
    install({ me: anonymous, register: () => ok({ user: { ...USER, is_admin: true } }) })
    render(<Shell />)
    await openRegister()

    await userEvent.type(screen.getByLabelText('邮箱'), USER.email)
    await userEvent.type(screen.getByLabelText('密码'), 'hunter2hunter2')
    await userEvent.type(screen.getByLabelText('确认密码'), 'hunter2hunter2')
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    expect(await screen.findByText(/首个账号，具管理员标记/)).toBeInTheDocument()
  })
})

describe('sign out', () => {
  /**
   * The old top bar gated 退出 on `mode === 'cognito'`, so under the dev bypass —
   * the only mode that actually worked — there was no way to sign out at all.
   * There is one login path now, so the button follows the session, nothing else.
   */
  it('is available whenever a session exists and returns to the login form', async () => {
    install({ me: () => ok({ user: USER }) })
    render(<Shell />)

    await screen.findByText('场景选择页')
    await userEvent.click(screen.getByRole('button', { name: '退出' }))

    expect(await screen.findByRole('button', { name: '登录' })).toBeInTheDocument()
    expect(screen.getByText('未登录')).toBeInTheDocument()
    expect(calls.map((c) => c.path)).toContain('/auth/logout')
  })

  /**
   * The top bar calls this as `void signOut()`, so a rejection here would be an
   * unhandled rejection rather than anything a user could see or act on.
   */
  it('drops the local session even if the logout request fails, without rejecting', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    setAuthFetch((path, init) => {
      calls.push({ path, body: {} })
      if (path === '/auth/me') return Promise.resolve(ok({ user: USER }))
      if (path === '/auth/logout') return Promise.reject(new Error('network down'))
      throw new Error(`unexpected ${path} ${String(init.method)}`)
    })
    render(<Shell />)

    await screen.findByText('场景选择页')
    await userEvent.click(screen.getByRole('button', { name: '退出' }))

    await waitFor(() => expect(screen.getByText('未登录')).toBeInTheDocument())
  })
})
