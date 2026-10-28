import { displayDate } from '@/domain/displayDate'
import { useEffect, useState } from 'react'
import { adminPrompts, type PromptState, type PromptTrial, type TrialHistory } from '@/api/adminPrompts'
import { userMessage } from '@/api/http'

export function CopyButton({ text, label }: { text: string | undefined; label: string }) {
  const [message, setMessage] = useState('')
  async function copy() {
    if (text === undefined) return
    try {
      if (navigator.clipboard?.writeText && window.isSecureContext) {
        await navigator.clipboard.writeText(text)
      } else {
        // The China POC uses an HTTP ALB; Clipboard API requires a secure context.
        const focus = document.activeElement
        const field = document.createElement('textarea')
        field.value = text
        field.style.cssText = 'position:fixed;left:-9999px;top:0'
        document.body.appendChild(field)
        try {
          field.focus()
          field.select()
          if (!document.execCommand('copy')) throw new Error('Copy unavailable')
        } finally {
          field.remove()
          if (focus instanceof HTMLElement) focus.focus()
        }
      }
      setMessage('已复制')
    } catch {
      setMessage('复制失败，请选中文本手动复制')
    }
  }
  return <span className="pm-copy"><button className="btn" aria-label={label} disabled={text === undefined} onClick={() => void copy()}>复制</button><span role="status">{message}</span></span>
}

export function TrialResults({ trial, id }: { trial: PromptTrial; id: string }) {
  return <>
    {trial.error && <p role="alert" className="pm-error">{trial.error}</p>}
    <div className="pm-columns">{(['baseline', 'draft'] as const).map((side) => {
      const title = side === 'baseline' ? '生效版本' : '草稿版本'
      const text = trial[`${side}_prompt`]
      return <div key={side}>
        <details className="pm-disclosure"><summary>{title}提示词（本次测试快照）</summary>
          {text === undefined ? <p>该记录未保存可恢复的提示词快照。</p> : <>
            <CopyButton text={text} label={`复制${title}提示词`} />
            <pre>{text || '（未设置业务补充）'}</pre>
          </>}
        </details>
        <div className="pm-output-heading"><h4>{title}输出</h4><CopyButton text={trial[side]} label={`复制${title}输出`} /></div>
        <pre>{trial[side] ?? (trial.status === 'running' ? '等待输出…' : '未产生输出')}</pre>
      </div>
    })}</div>
    <details className="pm-disclosure"><summary>查看测试版本与编号</summary><p>对比版本：{trial.baseline_release ?? '正在确认'}<br />草稿版本：{trial.draft_release ?? '正在确认'}<br />测试编号：{id}</p></details>
  </>
}

export function TrialHistoryPanel({ catalogue, selected, onSelect }: {
  catalogue: PromptState['catalogue']; selected: string; onSelect: (id: string) => void
}) {
  const [page, setPage] = useState<TrialHistory | null>(null)
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError('')
    void adminPrompts.history(offset, controller.signal).then((next) => {
      if (!controller.signal.aborted) setPage(next)
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(userMessage(err, '历史记录读取失败'))
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [offset, reload])
  const name = (id?: string) => catalogue.find((item) => item.id === id)?.name ?? id ?? '未知模块'
  return <section className="pm-library" aria-label="试运行历史">
    <div className="pm-card-heading"><div><h3>最近的测试</h3><p className="pm-purpose">查看当时的样例与结果，或复用输入进行新的对比。</p></div><button className="btn" disabled={loading} onClick={() => { setOffset(0); setReload((n) => n + 1) }}>刷新记录</button></div>
    {error && <p role="alert">{error}</p>}
    {loading ? <p>正在读取历史记录…</p> : page?.items.length ? <div className="pm-history-list">
      {page.items.map((item) => <button className={`pm-trial-history-row ${selected === item.test_id ? 'is-selected' : ''}`} key={item.test_id} onClick={() => onSelect(item.test_id)}>
        <strong>{name(item.prompt_id)}</strong><span>{item.created_at ? displayDate(item.created_at) : '时间未知'}</span><span>{item.model_id}</span><span>{item.status === 'completed' ? '已完成' : item.status === 'failed' ? '失败' : '运行中'}</span><span>{item.baseline_release ? item.baseline_release.slice(0, 8) : '版本未记录'} → {item.draft_release?.slice(0, 8) ?? '版本未记录'}</span><code>{item.test_id.slice(0, 8)}</code>
      </button>)}
    </div> : <p>暂无试运行记录。</p>}
    <div className="pm-history-pagination"><button className="btn" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button><button className="btn" disabled={loading || page?.next_offset == null} onClick={() => setOffset(page!.next_offset!)}>下一页</button></div>
  </section>
}
