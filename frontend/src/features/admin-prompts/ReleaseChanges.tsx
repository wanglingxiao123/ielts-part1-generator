import { useEffect, useState, type ReactNode } from 'react'
import { adminPrompts, type PromptReleaseDetail, type PromptState } from '@/api/adminPrompts'
import { userMessage } from '@/api/http'
import { CopyButton } from './TrialResults'
import { changedLines, collapseContext } from './releaseDiff'

function ChangeDetail({ change, name, initialOpen }: {
  change: PromptReleaseDetail['changes'][number]; name: string; initialOpen: boolean
}) {
  const [context, setContext] = useState(false)
  const lines = changedLines(change.before, change.after)
  const folded = {
    before: collapseContext(lines.before, lines.start, lines.end),
    after: collapseContext(lines.after, lines.start, lines.end),
  }
  const hasFold = [...folded.before, ...folded.after].some((row) => 'omitted' in row)
  return <details className="pm-disclosure pm-change-module" open={initialOpen}>
    <summary>{name}</summary>
    {hasFold && <div className="pm-context-toolbar"><span>已保留修改处附近的上下文</span><button className="btn" aria-expanded={context} onClick={() => setContext(!context)}>{context ? '折叠未修改内容' : '显示全部上下文'}</button></div>}
    <div className="pm-columns pm-diff-columns">
      {(['before', 'after'] as const).map((side) => {
        const rows = context ? lines[side].map((line) => ({ line })) : folded[side]
        return <section key={side} aria-label={`${name}${side === 'before' ? '修改前' : '修改后'}`}>
          <div className="pm-output-heading"><strong>{side === 'before' ? '修改前' : '修改后'}</strong><CopyButton text={change[side]} label={`复制${name}${side === 'before' ? '修改前' : '修改后'}`} /></div>
          <pre className="pm-text-diff">{rows.map((row, i) => 'omitted' in row
            ? <span key={i} className="pm-diff-omitted">⋯ 已折叠 {row.omitted} 行未修改内容</span>
            : <span key={i} className={row.line.changed ? side === 'before' ? 'pm-line-removed' : 'pm-line-added' : ''}><b aria-hidden="true">{row.line.changed ? side === 'before' ? '−' : '+' : ' '}</b>{row.line.text || '\u00a0'}</span>)}</pre>
        </section>
      })}
    </div>
  </details>
}

export function ReleaseChanges({ release, catalogue, heading, actions }: {
  release: PromptState['history'][number]; catalogue: PromptState['catalogue']; heading?: ReactNode; actions?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [full, setFull] = useState(false)
  const [detail, setDetail] = useState<PromptReleaseDetail | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const name = (id: string) => catalogue.find((item) => item.id === id)?.name ?? id
  useEffect(() => {
    if (!open || detail) return
    const controller = new AbortController()
    setError('')
    void adminPrompts.release(release.release_id, controller.signal).then((result) => {
      if (!controller.signal.aborted) setDetail(result)
    }).catch((err) => {
      if (!controller.signal.aborted) setError(userMessage(err, '版本详情读取失败'))
    })
    return () => controller.abort()
  }, [open, detail, release.release_id, retry])
  const ids = release.changed_ids
  return <div className="pm-release-changes">
    <div className="pm-release-row">
      <div className="pm-release-overview">
        {heading}
        <div className="pm-module-tags"><span className="pm-change-count">{ids ? `修改了 ${ids.length} 项提示词` : '版本修改详情'}</span>
          {ids?.slice(0, open ? undefined : 3).map((id) => <span className="pm-module-tag" key={id}>{name(id)}</span>)}
          {!open && !!ids && ids.length > 3 && <span className="pm-tag-more">另 {ids.length - 3} 项</span>}
        </div>
      </div>
      <div className="pm-release-actions"><button className="btn pm-details-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起修改' : '查看修改'}<span aria-hidden="true">{open ? '−' : '+'}</span></button>{actions}</div>
    </div>
    {open && <div className="pm-release-detail">
      {error ? <p role="alert">{error} <button className="btn" onClick={() => setRetry((n) => n + 1)}>重试读取</button></p>
        : !detail ? <p role="status">正在读取版本内容…</p> : <>
          <p className="pm-purpose">{detail.base_label ?? '发布前版本'} <code>{detail.base_release_id.slice(0, 12)}</code> → 此版本 <code>{detail.release_id.slice(0, 12)}</code></p>
          {detail.comparison_note && <p className="pm-alert">{detail.comparison_note}</p>}
          <p className="pm-diff-legend"><span className="pm-line-removed">− 修改前段落</span><span className="pm-line-added">+ 修改后段落</span></p>
          {detail.changes.length === 0 && <p>提示词内容没有变化。</p>}
          {detail.changes.map((change, index) => <ChangeDetail key={change.id} change={change} name={name(change.id)} initialOpen={index === 0} />)}
          <button className="btn" aria-expanded={full} onClick={() => setFull(!full)}>{full ? '收起完整版本' : '查看完整版本'}</button>
          {full && <div className="pm-full-release">{Object.entries(detail.values).map(([id, text]) => <details className="pm-disclosure" key={id}><summary>{name(id)}</summary><CopyButton text={text} label={`复制${name(id)}完整文本`} /><pre>{text || '（空）'}</pre></details>)}</div>}
        </>}
    </div>}
  </div>
}
