import { useEffect, useId, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useSession } from './useSession'

export function AccountMenu() {
  const session = useSession()
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panelId = useId()

  useEffect(() => { setOpen(false) }, [location])
  useEffect(() => {
    if (!open) return
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false)
    }
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false)
        trigger.current?.focus()
      }
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  const name = session.email.split('@')[0] || '账号'
  return (
    <div className="account-menu" ref={root} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
    }}>
      <button ref={trigger} type="button" className="account-trigger"
        aria-label="账号菜单" aria-expanded={open} aria-controls={panelId}
        title={session.email} onClick={() => setOpen(!open)}>
        <span className="account-avatar" aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>
        <span className="account-name">{name}</span>
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4" stroke="currentColor" strokeWidth="1.5" /></svg>
      </button>
      {open && <div id={panelId} className="account-panel" aria-label="账号选项">
        <div className="account-identity"><strong>{session.email}</strong><span>{session.isAdmin ? '管理员' : '普通用户'}</span></div>
        <Link to="/account/password" onClick={() => setOpen(false)}>修改密码<span aria-hidden="true">›</span></Link>
        <button type="button" onClick={() => { setOpen(false); void session.signOut() }}>退出登录</button>
      </div>}
    </div>
  )
}
