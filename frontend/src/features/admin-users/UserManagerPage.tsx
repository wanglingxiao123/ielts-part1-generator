import { ResetPasswordDialog } from "./ResetPasswordDialog"
import { useEffect, useRef, useState } from 'react'
import { adminUsers, type UserDirectory } from '@/api/adminUsers'
import { userMessage } from '@/api/http'
import type { AuthUser } from '@/auth/authApi'
import { useSession } from '@/auth/useSession'
import './user-manager.css'

export function UserManagerPage() {
  const session = useSession()
  return session.isAdmin ? <UserManager email={session.email} /> : <div className="page" role="alert">仅管理员可管理用户。</div>
}

function UserManager({ email }: { email: string }) {
  const [data, setData] = useState<UserDirectory | null>(null)
  const [resetEmail, setResetEmail] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [reload, setReload] = useState(0)
  const [pending, setPending] = useState<AuthUser | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    void adminUsers.list(controller.signal).then((next) => {
      if (!controller.signal.aborted) setData(next)
    }).catch((err) => {
      if (!controller.signal.aborted) setError(userMessage(err, '用户列表读取失败，请刷新重试。'))
    })
    return () => controller.abort()
  }, [reload])
  useEffect(() => {
    if (pending) dialog.current?.showModal()
    else dialog.current?.close()
  }, [pending])
  async function confirm() {
    if (!pending) return
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await adminUsers.role(pending)
      if (!alive.current) return
      setNotice(result.changed ? `${result.user.email} 已${result.user.is_admin ? '设为管理员' : '恢复为普通用户'}。` : '该用户已是目标身份，无需重复修改。')
      setPending(null); setReload((n) => n + 1)
    } catch (err) {
      if (alive.current) {
        setError(userMessage(err, '操作结果未确认，请刷新列表核对；重复确认不会重复添加记录。'))
        setPending(null); setReload((n) => n + 1)
      }
    } finally { if (alive.current) setBusy(false) }
  }
  const users = data?.users.filter((user) => user.email.toLowerCase().includes(query.trim().toLowerCase())) ?? []
  return <main className="page um-page">
    {resetEmail && <ResetPasswordDialog email={resetEmail} close={() => setResetEmail(null)} changed={() => setReload((n) => n + 1)} />}
    <div className="um-heading"><div><span className="um-eyebrow">管理员工作区</span><h1>用户管理</h1><p>管理已注册用户的权限，或协助重置密码。</p></div><button className="btn" disabled={busy} onClick={() => { setError(''); setReload((n) => n + 1) }}>刷新用户列表</button></div>
    {error && <p role="alert" className="um-error">{error}</p>}
    {notice && <p role="status" className="um-notice">{notice}</p>}
    <section className="panel um-panel" aria-label="已注册用户">
      <div className="um-toolbar"><label>搜索邮箱<input type="search" placeholder="输入邮箱或关键词" value={query} onChange={(event) => setQuery(event.target.value)} /></label><span>{data ? `${data.users.length} 位用户 · ${data.users.filter((user) => user.is_admin).length} 位管理员` : '正在读取…'}</span></div>
      <div className="um-user-list">{users.map((user) => <div className="um-user-row" key={user.email}>
        <div className="um-avatar" aria-hidden="true">{user.email[0]?.toUpperCase()}</div>
        <div className="um-user-info"><strong>{user.email}</strong><span>{user.email === email ? '当前登录账号' : user.created_at ? `注册于 ${new Date(user.created_at * 1000).toLocaleDateString()}` : '已注册用户'}</span></div>
        <span className={`um-role ${user.is_admin ? 'is-admin' : ''}`}>{user.is_admin ? '管理员' : '普通用户'}</span>
        <button className="btn" disabled={busy || user.email === email} aria-label={`${user.is_admin ? '取消管理员' : '设为管理员'}：${user.email}`} onClick={() => setPending(user)}>{user.email === email ? '本人不可修改' : user.is_admin ? '取消管理员' : '设为管理员'}</button>
        {user.email !== email && <button className="btn" disabled={busy} aria-label={`重置密码：${user.email}`} onClick={() => setResetEmail(user.email)}>重置密码</button>}
        {user.must_change_password && <span className="um-role">待修改临时密码</span>}
      </div>)}</div>
      {data && users.length === 0 && <p className="um-empty">没有匹配的已注册用户。请确认邮箱，或让对方先注册。</p>}
      <p className="um-help">管理员可管理提示词、发布版本和调整其他用户权限。不能修改自己的身份，系统始终保留至少一位管理员。</p>
    </section>
    <section className="panel um-panel" aria-labelledby="um-history-title"><div className="um-history-heading"><h2 id="um-history-title">账号操作记录</h2><span>最近 50 条 · 共 {data?.history_total ?? 0} 条</span></div>
      {data?.history.length === 0 && <p className="um-empty">暂无账号操作记录。</p>}
      <ol className="um-history">{data?.history.map((event) => <li key={event.id}><span className={`um-role ${event.after ? 'is-admin' : ''}`}>{event.kind === 'password_reset' ? '重置密码' : event.kind === 'password_change' ? '本人修改密码' : event.after ? '授予管理员' : '取消管理员'}</span><div><strong>{event.email}</strong><p>{event.actor} 操作 · {new Date(event.at * 1000).toLocaleString()}</p></div></li>)}</ol>
    </section>
    <dialog ref={dialog} className="um-dialog" aria-labelledby="um-confirm-title" onCancel={(event) => { event.preventDefault(); if (!busy) setPending(null) }}>
      <h2 id="um-confirm-title">{pending?.is_admin ? '取消管理员权限？' : '设为管理员？'}</h2><p className="um-confirm-email">{pending?.email}</p>
      <p>{pending?.is_admin ? '对方会恢复为普通用户，后续管理请求将立即被拒绝；已有材料保持不变。' : '对方将能够修改和发布提示词，也能管理其他用户的管理员权限。'}</p>
      <div className="um-dialog-actions"><button className="btn" disabled={busy} onClick={() => setPending(null)}>取消</button><button className="btn btn-primary" disabled={busy} onClick={() => void confirm()}>{busy ? '正在处理…' : '确认变更'}</button></div>
    </dialog>
  </main>
}
