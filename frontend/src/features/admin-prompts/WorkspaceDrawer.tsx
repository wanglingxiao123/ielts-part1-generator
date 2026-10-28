import { useEffect, useId, useRef, type ReactNode } from 'react'

/** Native modal drawer: Escape, focus containment and return-to-trigger are browser-owned. */
export function WorkspaceDrawer({ open, onClose, title, children, centered = false }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode; centered?: boolean
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    if (open) dialog.current?.showModal()
    else dialog.current?.close()
  }, [open])
  return <dialog ref={dialog} className={`pm-drawer${centered ? ' pm-confirm-dialog' : ''}`} aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); onClose() }}>
    <div className="pm-drawer-heading"><h3 id={titleId}>{title}</h3><button className="btn" aria-label={`关闭${title}`} onClick={onClose}>关闭</button></div>
    <div className="pm-drawer-body">{children}</div>
  </dialog>
}
