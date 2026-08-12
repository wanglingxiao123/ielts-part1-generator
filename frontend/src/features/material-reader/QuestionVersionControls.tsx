import type { ReactNode } from 'react'
import type { MaterialComment } from '@/contracts/comments'
import type { QuestionRevisionStage } from '@/contracts/questionVersions'
import type { QuestionVersionsState } from './useQuestionVersions'

const STAGE_LABEL: Record<QuestionRevisionStage, string> = {
  queued: '正在准备修改',
  analysing: '正在分析批注',
  validating: '正在检查完整十题',
  auditing: '正在独立复评',
  storing: '正在生成新版本',
}

const REVISION_STEPS = [
  '已接收请求',
  'Agent 正在修改',
  '完整校验',
  '独立盲审',
  '生成新版本',
] as const

const STAGE_INDEX: Record<QuestionRevisionStage, number> = {
  queued: 0,
  analysing: 1,
  validating: 2,
  auditing: 3,
  storing: 4,
}

export function QuestionVersionBar({
  state,
}: {
  state: QuestionVersionsState
}) {
  if (state.loading && state.versions.length === 0) {
    return <div className="question-version-bar muted">正在读取题目版本…</div>
  }
  if (state.versions.length === 0) {
    return state.error ? (
      <div className="question-version-bar">
        <span className="comment-error" role="alert">
          {state.error}
        </span>
        <button type="button" className="btn btn-sm" onClick={state.reload}>
          重试
        </button>
      </div>
    ) : null
  }

  return (
    <div className="question-version-bar">
      <label>
        <span>题目版本</span>
        <select
          aria-label="题目版本"
          value={state.selectedVersionId}
          onChange={(event) => state.setSelectedVersionId(event.target.value)}
        >
          {state.versions.map((version) => (
            <option key={version.id} value={version.id}>
              V{version.ordinal}
              {version.status === 'original' ? ' · 原始版本' : ''}
              {version.is_active ? ' · 当前采用' : ''}
            </option>
          ))}
        </select>
      </label>
      {state.selectedVersion && state.selectedVersion.id !== state.activeVersionId && (
        <button
          type="button"
          className="btn btn-sm"
          disabled={state.adopting || Boolean(state.revisionStage)}
          onClick={() => void state.adopt()}
        >
          {state.adopting ? '采用中…' : '采用此版本'}
        </button>
      )}
      {state.selectedVersion?.id === state.activeVersionId && (
        <span className="question-active-label">当前采用</span>
      )}
      {state.error && (
        <span className="comment-error" role="alert">
          {state.error}
        </span>
      )}
    </div>
  )
}

export function QuestionRevisionAction({
  state,
  comments,
}: {
  state: QuestionVersionsState
  comments: MaterialComment[]
}) {
  const viewingActive = state.selectedVersion?.id === state.activeVersionId
  const pendingComments = comments.filter(
    (comment) =>
      comment.anchor.type === 'question' && (comment.status ?? 'open') === 'open',
  )
  const canRevise =
    pendingComments.length > 0 &&
    viewingActive &&
    !state.loading &&
    !state.adopting &&
    !state.revisionStage
  const request = state.revisionRequest
  const currentStage = state.revisionStage
  const baseVersion = state.versions.find(
    (version) => version.id === request?.base_version_id,
  )

  return (
    <div className="question-revision-action">
      <button
        type="button"
        className="btn btn-primary"
        disabled={!canRevise}
        onClick={() => void state.revise(pendingComments)}
      >
        {state.revisionStage ? STAGE_LABEL[state.revisionStage] : '提交修改'}
      </button>
      {!viewingActive && (
        <p className="muted">只能基于当前采用版本提交修改。</p>
      )}
      {currentStage && (
        <div className="question-revision-progress" role="status">
          <div className="question-revision-result-head">
            <strong>正在修改题目</strong>
          </div>
          <p>
            已提交 {request?.comment_count ?? pendingComments.length} 条批注
            {baseVersion ? ` · 基于 V${baseVersion.ordinal}` : ''}
          </p>
          <ol className="question-revision-steps">
            {REVISION_STEPS.map((label, index) => {
              const current = STAGE_INDEX[currentStage]
              const status = index < current ? 'done' : index === current ? 'current' : 'pending'
              return (
                <li key={label} className={status}>
                  <span aria-hidden="true">
                    {status === 'done' ? '✓' : status === 'current' ? '●' : '○'}
                  </span>
                  {label}
                </li>
              )
            })}
          </ol>
          <p className="muted">{STAGE_LABEL[currentStage]}，请勿重复提交。</p>
        </div>
      )}
      {state.revisionResult?.kind === 'revised' && (
        <RevisionResultShell state={state} className="succeeded" title="新版本已生成">
          <p>
            修改已通过完整检查。新版本正在显示，但当前采用版本不会自动改变。
          </p>
          {state.revisionResult.baselineAdvisories.length > 0 && (
            <details>
              <summary>查看 {state.revisionResult.baselineAdvisories.length} 条基线提醒</summary>
              <ul>
                {state.revisionResult.baselineAdvisories.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            </details>
          )}
        </RevisionResultShell>
      )}
      {state.revisionResult?.kind === 'needs_material' && (
        <RevisionResultShell state={state} className="needs-material" title="需要修改材料">
          <p>以下意见无法只通过修改题目解决，本次已终止，现有版本未改变。</p>
          <ul>
            {state.revisionResult.reasons.map((reason) => (
              <li key={`${reason.comment_id}-${reason.question_number}`}>
                Q{reason.question_number}：{reason.reason}
              </li>
            ))}
          </ul>
        </RevisionResultShell>
      )}
      {state.revisionResult?.kind === 'failed' && (
        <RevisionResultShell state={state} className="failed" title="修改未完成">
          <p>{state.revisionResult.message}</p>
          {state.revisionResult.blockers.length > 0 && (
            <ul>
              {state.revisionResult.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
        </RevisionResultShell>
      )}
    </div>
  )
}

function RevisionResultShell({
  state,
  className,
  title,
  children,
}: {
  state: QuestionVersionsState
  className: string
  title: string
  children: ReactNode
}) {
  return (
    <div className={`question-revision-result ${className}`} role="alert">
      <div className="question-revision-result-head">
        <strong>{title}</strong>
        <button
          type="button"
          className="icon-btn"
          aria-label="关闭修改结果"
          title="关闭"
          onClick={state.dismissRevisionResult}
        >
          ×
        </button>
      </div>
      {children}
    </div>
  )
}
