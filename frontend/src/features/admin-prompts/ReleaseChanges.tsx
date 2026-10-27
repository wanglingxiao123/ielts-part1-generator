import { useEffect, useState } from 'react'
import { adminPrompts, type PromptReleaseDetail, type PromptState } from '@/api/adminPrompts'
import { userMessage } from '@/api/http'
import { CopyButton } from './TrialResults'

/** Highlight the changed paragraph range while retaining identical leading/trailing lines. */
export function changedLines(before: string, after: string) {
  const left = before.split('\n'), right = after.split('\n')
  let start = 0, end = 0
  while (start < left.length && start < right.length && left[start] === right[start]) start++
  while (end < left.length - start && end < right.length - start &&
    left[left.length - end - 1] === right[right.length - end - 1]) end++
  return {
    before: left.map((text, i) => ({ text, changed: i >= start && i < left.length - end })),
    after: right.map((text, i) => ({ text, changed: i >= start && i < right.length - end })),
  }
}

export function ReleaseChanges({ release, catalogue }: {
  release: PromptState['history'][number]; catalogue: PromptState['catalogue']
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
    <div className="pm-release-summary">
      <div><strong>{ids ? `修改了 ${ids.length} 项提示词` : '版本修改详情'}</strong>
        {!!ids?.length && <p>{ids.map(name).join('、')}</p>}
      </div>
      <button className="btn" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起修改' : '查看修改'}</button>
    </div>
    {open && <div className="pm-release-detail">
      {error ? <p role="alert">{error} <button className="btn" onClick={() => setRetry((n) => n + 1)}>重试读取</button></p>
        : !detail ? <p role="status">正在读取版本内容…</p> : <>
          <p className="pm-purpose">{detail.base_label ?? '发布前版本'} <code>{detail.base_release_id.slice(0, 12)}</code> → 此版本 <code>{detail.release_id.slice(0, 12)}</code></p>
          {detail.comparison_note && <p className="pm-alert">{detail.comparison_note}</p>}
          <p className="pm-diff-legend"><span className="pm-line-removed">− 修改前段落</span><span className="pm-line-added">+ 修改后段落</span></p>
          {detail.changes.length === 0 && <p>提示词内容没有变化。</p>}
          {detail.changes.map((change, index) => {
            const lines = changedLines(change.before, change.after)
            return <details className="pm-disclosure" key={change.id} open={index === 0}>
              <summary>{name(change.id)}</summary>
              <div className="pm-columns pm-diff-columns">
                {(['before', 'after'] as const).map((side) => <section key={side} aria-label={`${name(change.id)}${side === 'before' ? '修改前' : '修改后'}`}>
                  <div className="pm-output-heading"><strong>{side === 'before' ? '修改前' : '修改后'}</strong><CopyButton text={change[side]} label={`复制${name(change.id)}${side === 'before' ? '修改前' : '修改后'}`} /></div>
                  <pre className="pm-text-diff">{lines[side].map((line, i) => <span key={i} className={line.changed ? side === 'before' ? 'pm-line-removed' : 'pm-line-added' : ''}><b aria-hidden="true">{line.changed ? side === 'before' ? '−' : '+' : ' '}</b>{line.text || '\u00a0'}</span>)}</pre>
                </section>)}
              </div>
            </details>
          })}
          <button className="btn" aria-expanded={full} onClick={() => setFull(!full)}>{full ? '收起完整版本' : '查看完整版本'}</button>
          {full && <div className="pm-full-release">{Object.entries(detail.values).map(([id, text]) => <details className="pm-disclosure" key={id}><summary>{name(id)}</summary><CopyButton text={text} label={`复制${name(id)}完整文本`} /><pre>{text || '（空）'}</pre></details>)}</div>}
        </>}
    </div>}
  </div>
}
