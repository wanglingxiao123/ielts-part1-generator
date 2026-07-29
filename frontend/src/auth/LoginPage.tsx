/**
 * In-SPA login / register, against `/api/auth/*` (web/app.py).
 *
 * Replaces a redirect to a Cognito Hosted UI that was never deployed. The web
 * tier also serves a standalone `/login` HTML page for anonymous document
 * requests; this component is the same flow inside the SPA, so a session that
 * expires mid-session does not bounce the user out of the app.
 *
 * Registration UX is the client's stated requirement: email + password entered
 * twice, a show/hide toggle, and an explicit 注册成功 confirmation rather than a
 * silent jump. The mismatch check is client-side because the server never sees
 * the second field — sending one password and hoping is how a typo becomes an
 * account nobody can log into.
 */
import { useEffect, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { AuthError } from './authApi'
import { useSession } from './useSession'

type Mode = 'login' | 'register'

/** Mirrors web/auth.py MIN_PASSWORD_LENGTH — checked there too, this is only kindness. */
const MIN_PASSWORD = 8

interface FromState {
  from?: string
}

export function LoginPage() {
  const session = useSession()
  const navigate = useNavigate()
  const location = useLocation()
  const returnTo = (location.state as FromState | null)?.from ?? '/'

  const [mode, setMode] = useState<Mode>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [reveal, setReveal] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [registered, setRegistered] = useState(false)

  /**
   * Anyone who already has a session has no business on this page — including
   * the user who just logged in, which is how the redirect back to `returnTo`
   * happens. Not navigating from the submit handler keeps one code path for
   * "signed in, so leave" whether the session came from this form or from the
   * mount-time /api/auth/me probe.
   *
   * Registration is the exception: it holds the success panel until the user
   * acknowledges it, per the client's 然后显示注册成功 -> 然后才能登录.
   */
  useEffect(() => {
    if (session.isAuthenticated && !registered) navigate(returnTo, { replace: true })
  }, [session.isAuthenticated, registered, navigate, returnTo])

  function switchTo(next: Mode) {
    setMode(next)
    setError(null)
    setConfirm('')
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)

    if (mode === 'register') {
      if (password !== confirm) {
        setError('两次输入的密码不一致，请重新输入')
        return
      }
      if (password.length < MIN_PASSWORD) {
        setError(`密码至少 ${MIN_PASSWORD} 位`)
        return
      }
    }

    setBusy(true)
    try {
      if (mode === 'register') {
        await session.signUp(email, password)
        setRegistered(true)
      } else {
        await session.signIn(email, password)
      }
    } catch (err) {
      setError(
        err instanceof AuthError
          ? err.message
          : err instanceof Error
            ? `无法连接服务器：${err.message}`
            : '登录失败，请稍后再试',
      )
    } finally {
      setBusy(false)
    }
  }

  if (registered && session.isAuthenticated) {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <h2>注册成功</h2>
          {/* banner-good for the colour, auth-note to stack instead of using the
              banner's default row layout — the label wraps to three lines in a
              340px card otherwise. */}
          <div className="banner banner-good auth-note">
            <strong>已创建账号</strong>
            <div>
              <span className="mono">{session.email}</span> 已注册并登录
              {session.isAdmin && '（首个账号，具管理员标记）'}。
            </div>
          </div>
          <button
            type="button"
            className="btn btn-primary auth-submit"
            onClick={() => {
              setRegistered(false)
              navigate(returnTo, { replace: true })
            }}
          >
            进入系统
          </button>
        </div>
      </div>
    )
  }

  const registering = mode === 'register'

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={(e) => void submit(e)}>
        <h2>{registering ? '注册账号' : '登录'}</h2>
        <p className="muted auth-lead">IELTS Part 1 材料生成与审阅</p>

        <label className="auth-field">
          <span>邮箱</span>
          <input
            type="email"
            value={email}
            autoComplete="username"
            required
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        <label className="auth-field">
          <span>密码</span>
          <input
            type={reveal ? 'text' : 'password'}
            value={password}
            autoComplete={registering ? 'new-password' : 'current-password'}
            required
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {registering && (
          <label className="auth-field">
            <span>确认密码</span>
            <input
              type={reveal ? 'text' : 'password'}
              value={confirm}
              autoComplete="new-password"
              required
              onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
        )}

        <label className="auth-reveal">
          <input type="checkbox" checked={reveal} onChange={(e) => setReveal(e.target.checked)} />
          <span>显示密码</span>
        </label>

        {error && (
          <div className="banner banner-bad auth-error" role="alert">
            <span>{error}</span>
          </div>
        )}

        <button type="submit" className="btn btn-primary auth-submit" disabled={busy}>
          {busy ? '提交中…' : registering ? '注册' : '登录'}
        </button>

        <button
          type="button"
          className="btn auth-switch"
          onClick={() => switchTo(registering ? 'login' : 'register')}
        >
          {registering ? '已有账号？返回登录' : '没有账号？注册'}
        </button>

        {registering && <p className="muted auth-hint">密码至少 {MIN_PASSWORD} 位。</p>}
      </form>
    </div>
  )
}
