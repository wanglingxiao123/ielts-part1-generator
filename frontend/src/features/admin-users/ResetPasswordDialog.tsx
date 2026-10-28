import { useEffect, useRef, useState } from 'react'
import { adminUsers } from '@/api/adminUsers'
import { userMessage } from '@/api/http'

export function ResetPasswordDialog({ email, close, changed }: { email: string; close: () => void; changed: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [password, setPassword] = useState('')
  const [temporary, setTemporary] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])
  async function reset() {
    if (!password || busy) return
    setBusy(true); setError('')
    try {
      const result = await adminUsers.resetPassword(email, password)
      setTemporary(result.temporary_password); changed()
    } catch (err) { setError(userMessage(err, '结果未确认。若未收到临时密码，请重新重置；之前的临时密码将失效。')) }
    finally { setBusy(false); setPassword('') }
  }
  return <dialog ref={dialog} className="um-dialog" aria-labelledby="um-reset-title" onCancel={(event) => { event.preventDefault(); if (!busy) close() }}>
    <h2 id="um-reset-title">{temporary ? '临时密码已生成' : '重置用户密码？'}</h2><p className="um-confirm-email">{email}</p>
    {temporary ? <><p>请将临时密码交给本人，24 小时内登录并设置新密码。关闭后无法再次查看。</p><label className="um-password-field">临时密码<input readOnly value={temporary} onFocus={(e) => e.target.select()} autoComplete="off" /></label><p className="um-help">可以选中复制。系统已使对方旧密码和旧登录会话失效。</p></> : <>
      <p>系统将生成临时密码，使对方的旧登录会话失效。对方下次登录必须先修改密码。</p>
      <label className="um-password-field">你的管理员密码<input type="password" autoComplete="current-password" value={password} disabled={busy} onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p role="alert" className="um-error">{error}</p>}
    </>}
    <div className="um-dialog-actions"><button className="btn" disabled={busy} onClick={close}>{temporary ? '已记下，关闭' : '取消'}</button>{!temporary && <button className="btn btn-primary" disabled={busy || !password} onClick={() => void reset()}>{busy ? '正在重置…' : '确认重置密码'}</button>}</div>
  </dialog>
}
