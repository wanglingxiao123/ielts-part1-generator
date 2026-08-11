import type { MaterialComment } from '@/contracts/comments'
import type { QuestionRevisionStage } from '@/contracts/questionVersions'
import type { QuestionVersionsState } from './useQuestionVersions'

const STAGE_LABEL: Record<QuestionRevisionStage, string> = {
  queued: '正在准备修改',
  analysing: '正在分析批注',
  revising: '正在修改题目',
  validating: '正在检查完整十题',
  auditing: '正在独立复评',
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
    (comment) => comment.anchor.type === 'question',
  )
  const canRevise =
    pendingComments.length > 0 &&
    viewingActive &&
    !state.loading &&
    !state.adopting &&
    !state.revisionStage

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
      {state.revisionStage && (
        <p className="question-revision-progress" role="status">
          {STAGE_LABEL[state.revisionStage]}，请勿重复提交。
        </p>
      )}
      {state.revisionResult?.kind === 'needs_material' && (
        <div className="question-revision-result needs-material" role="alert">
          <strong>需要修改材料</strong>
          <p>以下意见无法只通过修改题目解决，本次已终止，现有版本未改变。</p>
          <ul>
            {state.revisionResult.reasons.map((reason) => (
              <li key={`${reason.comment_id}-${reason.question_number}`}>
                Q{reason.question_number}：{reason.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {state.revisionResult?.kind === 'failed' && (
        <div className="question-revision-result failed" role="alert">
          <strong>修改未完成</strong>
          <p>{state.revisionResult.message}</p>
        </div>
      )}
    </div>
  )
}
