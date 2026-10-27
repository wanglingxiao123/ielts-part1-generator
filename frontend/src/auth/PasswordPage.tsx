import { useState, type FormEvent } from 'react'
import { changePassword } from './authApi'
import { useSession } from './useSession'
import './password.css'

export function PasswordPage() {
  const session = useSession()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !current || next.length < 8 || next !== confirmation) return
    setBusy(true); setError('')
    try {
      await changePassword(current, next)
      setCurrent(''); setNext(''); setConfirmation(''); setDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '修改结果未确认，请尝试用新密码重新登录。')
    } finally { setBusy(false) }
  }
  return <main className="page password-page"><section className="panel password-card">
    <span className="password-eyebrow">账号安全</span><h1>{done ? '密码已修改' : '修改密码'}</h1>
    <p>{session.email}</p>
    {done ? <><p role="status">旧登录会话已失效，请使用新密码重新登录。</p><button className="btn btn-primary" onClick={() => void session.signOut()}>重新登录</button></> : <>
      <p>{session.user?.must_change_password ? '请先将管理员提供的临时密码改为自己的密码，再继续使用。' : '修改后，所有设备上的旧登录会话都会失效。'}</p>
      <form onSubmit={(event) => void submit(event)}>
        <label>{session.user?.must_change_password ? '临时密码' : '当前密码'}<input type="password" autoComplete="current-password" required maxLength={1024} value={current} onChange={(e) => setCurrent(e.target.value)} disabled={busy} /></label>
        <label>新密码<input aria-label="新密码" type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={next} onChange={(e) => setNext(e.target.value)} disabled={busy} /><small>至少 8 位，不能与当前密码相同。</small></label>
        <label>确认新密码<input type="password" autoComplete="new-password" required maxLength={1024} value={confirmation} onChange={(e) => setConfirmation(e.target.value)} disabled={busy} /></label>
        {confirmation && next !== confirmation && <small role="status">两次输入的新密码不一致。</small>}
        {error && <p role="alert">{error}</p>}
        <button className="btn btn-primary" disabled={busy || !current || next.length < 8 || next === current || next !== confirmation}>{busy ? '正在修改…' : '保存新密码'}</button>
      </form>
    </>}
  </section></main>
}
