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
  onSubmit,
}: {
  anchor: CommentAnchor | null
  saving: boolean
  onSubmit: (comment: CreateMaterialComment) => Promise<boolean>
}) {
  const [text, setText] = useState('')
  const [severity, setSeverity] = useState<CommentSeverity | null>(null)
  const canSubmit = Boolean(anchor && severity && text.trim() && !saving)

  const submit = async () => {
    if (!anchor || !severity || !text.trim()) return
    const saved = await onSubmit({ anchor, severity, text: text.trim() })
    if (!saved) return
    setText('')
    setSeverity(null)
  }

  return (
    <div className="comment-composer">
      <div className="comment-anchor">
        {anchor ? `位置：${anchorLabel(anchor)}` : '先点击左侧题目或对话 Turn'}
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
  onNavigate,
  onDelete,
}: {
  comment: MaterialComment
  disabled: boolean
  onNavigate: (anchor: CommentAnchor) => void
  onDelete: (id: string) => void
}) {
  return (
    <article className={`comment-card severity-${comment.severity}`}>
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
      </button>
      <button
        type="button"
        className="comment-delete"
        title="删除批注"
        aria-label={`删除 ${anchorLabel(comment.anchor)} 的批注`}
        disabled={disabled}
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
  onNavigate,
  onDelete,
}: {
  comments: MaterialComment[]
  saving: boolean
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
          disabled={saving}
          onNavigate={onNavigate}
          onDelete={onDelete}
        />
      ))}
    </div>
  )
}
