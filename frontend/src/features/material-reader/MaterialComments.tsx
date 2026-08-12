import { useState } from 'react'
import type {
  CommentAnchor,
  CommentSeverity,
  CreateMaterialComment,
  MaterialComment,
} from '@/contracts/comments'

const SEVERITY: Record<
  CommentSeverity,
  { label: string; short: string }
> = {
  critical: { label: '重要', short: '重要' },
  major: { label: '一般', short: '一般' },
  minor: { label: '轻微', short: '轻微' },
}

function anchorLabel(anchor: CommentAnchor): string {
  return anchor.type === 'question' ? `Q${anchor.index}` : `Turn ${anchor.index}`
}

export function CommentComposer({
  anchor,
  saving,
  disabled = false,
  onSubmit,
}: {
  anchor: CommentAnchor | null
  saving: boolean
  disabled?: boolean
  onSubmit: (comment: CreateMaterialComment) => Promise<boolean>
}) {
  const [text, setText] = useState('')
  const [severity, setSeverity] = useState<CommentSeverity | null>(null)
  const canSubmit = Boolean(anchor && severity && text.trim() && !saving && !disabled)

  const submit = async () => {
    if (!anchor || !severity || !text.trim()) return
    const saved = await onSubmit({ anchor, severity, text: text.trim() })
    if (!saved) return
    setText('')
    setSeverity(null)
  }

  return (
    <div className={`comment-composer${disabled ? ' disabled' : ''}`}>
      <div className="comment-anchor">
        {disabled
          ? '历史版本仅供查看'
          : anchor
            ? `位置：${anchorLabel(anchor)}`
            : '先点击左侧题目或对话 Turn'}
      </div>
      <div className="comment-severity" aria-label="严重程度">
        {(Object.keys(SEVERITY) as CommentSeverity[]).map((value) => (
          <button
            type="button"
            key={value}
            className={`comment-severity-choice severity-${value}${
              severity === value ? ' selected' : ''
            }`}
            aria-pressed={severity === value}
            disabled={disabled}
            onClick={() => setSeverity(value)}
          >
            {SEVERITY[value].label}
          </button>
        ))}
      </div>
      <textarea
        value={text}
        maxLength={4000}
        rows={3}
        placeholder="写下批注意见"
        aria-label="批注内容"
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
      />
      <button type="button" className="btn comment-submit" disabled={!canSubmit} onClick={submit}>
        {saving ? '保存中…' : '添加批注'}
      </button>
    </div>
  )
}

export function CommentCard({
  comment,
  disabled,
  resolvedVersionLabel,
  onNavigate,
  onDelete,
}: {
  comment: MaterialComment
  disabled: boolean
  resolvedVersionLabel?: (versionId: string) => string | null
  onNavigate: (anchor: CommentAnchor) => void
  onDelete: (id: string) => void
}) {
  const status = comment.anchor.type === 'question' ? (comment.status ?? 'open') : 'open'
  const readOnly = status !== 'open'
  const statusLabel =
    status === 'needs_material'
        ? '需修改材料'
      : status === 'needs_replan'
        ? '需重新命题'
      : status === 'no_change'
        ? '无需修改'
      : status === 'resolved'
        ? comment.resolved_by_version_id
          ? `已在 ${resolvedVersionLabel?.(comment.resolved_by_version_id) ?? '新版本'} 处理`
          : '已处理'
        : null
  return (
    <article className={`comment-card severity-${comment.severity} ${status}`}>
      <button
        type="button"
        className="comment-card-body"
        onClick={() => onNavigate(comment.anchor)}
      >
        <span className="comment-card-meta">
          <strong>{anchorLabel(comment.anchor)}</strong>
          <span className={`comment-severity-tag severity-${comment.severity}`}>
            {SEVERITY[comment.severity].short}
          </span>
          {statusLabel && <span className="comment-status-tag">{statusLabel}</span>}
          <time dateTime={comment.created_at}>
            {new Date(comment.created_at).toLocaleString('zh-CN', {
              month: 'numeric',
              day: 'numeric',
              hour: '2-digit',
              minute: '2-digit',
            })}
          </time>
        </span>
        <span className="comment-card-text">{comment.text}</span>
        {readOnly && comment.decision_reason && (
          <span className="comment-decision">
            <strong>处理理由：</strong>{comment.decision_reason}
            {(comment.decision_references?.length ?? 0) > 0 && (
              <span>
                <strong>核对依据：</strong>{comment.decision_references?.join('；')}
              </span>
            )}
          </span>
        )}
      </button>
      <button
        type="button"
        className="comment-delete"
        title="删除批注"
        aria-label={`删除 ${anchorLabel(comment.anchor)} 的批注`}
        disabled={disabled || readOnly}
        onClick={() => onDelete(comment.id)}
      >
        ×
      </button>
    </article>
  )
}

export function CommentList({
  comments,
  saving,
  resolvedVersionLabel,
  onNavigate,
  onDelete,
}: {
  comments: MaterialComment[]
  saving: boolean
  resolvedVersionLabel?: (versionId: string) => string | null
  onNavigate: (anchor: CommentAnchor) => void
  onDelete: (id: string) => void
}) {
  if (comments.length === 0) {
    return <div className="comment-empty">还没有批注</div>
  }
  return (
    <div className="comment-list">
      {comments.map((comment) => (
        <CommentCard
          key={comment.id}
          comment={comment}
          disabled={saving || (
            comment.anchor.type === 'question' && (comment.status ?? 'open') !== 'open'
          )}
          resolvedVersionLabel={resolvedVersionLabel}
          onNavigate={onNavigate}
          onDelete={onDelete}
        />
      ))}
    </div>
  )
}
