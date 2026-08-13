import type { ReactNode } from 'react'
import type { MaterialComment } from '@/contracts/comments'
import type { QuestionRevisionStage } from '@/contracts/questionVersions'
import type { QuestionVersionsState } from './useQuestionVersions'

const STAGE_LABEL: Record<QuestionRevisionStage, string> = {
  queued: '正在准备修改',
  analysing: '正在分析批注',
  planning: '正在重新规划信息点',
  material_revising: '正在修改听力材料',
  material_auditing: '正在复核新材料',
  revising_material: '正在修改听力材料',
  validating_material: '正在校验新材料',
  auditing_material: '正在复核新材料',
  feasibility: '正在检查新方案',
  generating: '正在生成完整十题',
  revising: '正在修改题目',
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

const REPLAN_STEPS = [
  '已接收请求',
  '重新规划信息点',
  '材料校验与可行性',
  '生成完整十题',
  '独立盲审',
  '生成新版本',
] as const

const MATERIAL_REVISION_STEPS = [
  '已接收请求',
  '修改听力材料',
  '复核新材料',
  '重建信息点',
  '生成完整十题',
  '独立盲审',
  '生成新版本',
] as const

const STAGE_INDEX: Record<QuestionRevisionStage, number> = {
  queued: 0,
  analysing: 1,
  planning: 1,
  material_revising: 1,
  material_auditing: 2,
  revising_material: 1,
  validating_material: 2,
  auditing_material: 2,
  feasibility: 2,
  generating: 2,
  revising: 1,
  validating: 2,
  auditing: 3,
  storing: 4,
}

const REPLAN_STAGE_INDEX: Record<QuestionRevisionStage, number> = {
  queued: 0,
  analysing: 1,
  planning: 1,
  material_revising: 1,
  material_auditing: 2,
  revising_material: 1,
  validating_material: 2,
  auditing_material: 2,
  feasibility: 2,
  generating: 3,
  revising: 3,
  validating: 2,
  auditing: 4,
  storing: 5,
}

const MATERIAL_STAGE_INDEX: Record<QuestionRevisionStage, number> = {
  queued: 0,
  analysing: 0,
  material_revising: 1,
  material_auditing: 2,
  revising_material: 1,
  validating_material: 2,
  auditing_material: 2,
  planning: 3,
  feasibility: 3,
  generating: 4,
  revising: 4,
  validating: 4,
  auditing: 5,
  storing: 6,
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
        <span>材料与题目版本</span>
        <select
          aria-label="材料与题目版本"
          value={state.selectedVersionId}
          onChange={(event) => state.setSelectedVersionId(event.target.value)}
        >
          {state.versions.map((version) => (
            <option key={version.id} value={version.id}>
              V{version.ordinal}
              {version.status === 'original' ? ' · 原始版本' : ''}
              {version.is_active ? ' · 当前采用' : ''}
              {!version.is_active ? ' · 历史版本' : ''}
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
  const revisedVersionId =
    state.revisionResult?.kind === 'revised' ? state.revisionResult.versionId : null
  const revisedVersion = revisedVersionId
    ? state.versions.find((version) => version.id === revisedVersionId)
    : undefined
  const replanAvailable =
    state.availableAction === 'confirm_replan' ||
    state.availableAction === 'retry_replan'
  const canReplan =
    replanAvailable &&
    Boolean(state.actionSourceRequestId) &&
    viewingActive &&
    !state.loading &&
    !state.adopting &&
    !state.revisionStage
  const materialAvailable =
    state.availableAction === 'confirm_material' ||
    state.availableAction === 'retry_material'
  const canReviseMaterial =
    materialAvailable &&
    Boolean(state.actionSourceRequestId) &&
    viewingActive &&
    !state.loading &&
    !state.adopting &&
    !state.revisionStage
  const isMaterialRevision =
    state.inFlightOperation === 'revise_material' ||
    request?.operation === 'revise_material' ||
    currentStage === 'material_revising' ||
    currentStage === 'material_auditing' ||
    currentStage === 'revising_material' ||
    currentStage === 'validating_material' ||
    currentStage === 'auditing_material'
  const isReplanning =
    state.inFlightOperation === 'replan_questions' ||
    request?.operation === 'replan_questions' ||
    (request?.status === 'replan_questions' && Boolean(currentStage)) ||
    currentStage === 'planning' ||
    currentStage === 'feasibility' ||
    currentStage === 'generating'
  const progressSteps = isMaterialRevision
    ? MATERIAL_REVISION_STEPS
    : isReplanning
      ? REPLAN_STEPS
      : REVISION_STEPS
  const progressIndex = isMaterialRevision
    ? MATERIAL_STAGE_INDEX
    : isReplanning
      ? REPLAN_STAGE_INDEX
      : STAGE_INDEX

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
            <strong>
              {isMaterialRevision
                ? '正在修改材料并重新命题'
                : isReplanning
                  ? '正在重新命题'
                  : '正在修改题目'}
            </strong>
          </div>
          <p>
            已提交 {request?.comment_count ?? pendingComments.length} 条批注
            {baseVersion ? ` · 基于 V${baseVersion.ordinal}` : ''}
          </p>
          <ol className="question-revision-steps">
            {progressSteps.map((label, index) => {
              const current = progressIndex[currentStage]
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
          <VersionDiffSummary
            changes={revisedVersion?.field_changes ?? []}
          />
        </RevisionResultShell>
      )}
      {state.revisionResult?.kind === 'needs_material' && (
        <RevisionResultShell
          state={state}
          className="needs-material"
          title="需要修改材料"
          dismissable={false}
        >
          <p>这些意见需要修改听力材料。确认后将生成新材料、重建信息点和完整十题；现有版本不会被覆盖或自动采用。</p>
          <ReasonList reasons={state.revisionResult.reasons} />
          <div className="question-replan-confirm">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!canReviseMaterial}
              onClick={() => void state.reviseMaterial()}
            >
              确认修改材料
            </button>
            {!canReviseMaterial && (
              <span className="muted">
                {state.actionUnavailableReason ??
                  (!viewingActive ? '请先切回当前采用版本。' : '此修改请求当前不可执行。')}
              </span>
            )}
          </div>
        </RevisionResultShell>
      )}
      {state.revisionResult?.kind === 'needs_replan' && (
        <RevisionResultShell
          state={state}
          className="needs-replan"
          title="需要重新命题"
          dismissable={false}
        >
          <p>这些意见需要更换信息点或重新规划题组。确认后将保持听力材料不变，重建完整蓝图和十道题。</p>
          <ReasonList reasons={state.revisionResult.reasons} />
          <div className="question-replan-confirm">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!canReplan}
              onClick={() => void state.replan()}
            >
              确认重新命题
            </button>
            {!canReplan && (
              <span className="muted">
                {state.actionUnavailableReason ??
                  (!viewingActive ? '请先切回当前采用版本。' : '此修改请求当前不可执行。')}
              </span>
            )}
          </div>
        </RevisionResultShell>
      )}
      {state.revisionResult?.kind === 'no_change' && (
        <RevisionResultShell state={state} className="no-change" title="无需修改">
          <p>核对后确认现有题目正确，本次未生成新版本。</p>
          <ReasonList reasons={state.revisionResult.reasons} showReferences />
        </RevisionResultShell>
      )}
      {state.revisionResult?.kind === 'failed' && (
        <RevisionResultShell
          state={state}
          className="failed"
          title="修改未完成"
          dismissable={!replanAvailable && !materialAvailable}
        >
          <p>{state.revisionResult.message}</p>
          {state.revisionResult.blockers.length > 0 && (
            <ul>
              {state.revisionResult.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
          {state.availableAction === 'retry_replan' && (
            <div className="question-replan-confirm">
              <button
                type="button"
                className="btn btn-primary"
                disabled={!canReplan}
                onClick={() => void state.replan()}
              >
                重新尝试命题
              </button>
            </div>
          )}
          {state.availableAction === 'retry_material' && (
            <div className="question-replan-confirm">
              <button
                type="button"
                className="btn btn-primary"
                disabled={!canReviseMaterial}
                onClick={() => void state.reviseMaterial()}
              >
                重新尝试修改材料
              </button>
            </div>
          )}
          {!state.availableAction && state.actionUnavailableReason && (
            <p className="muted">{state.actionUnavailableReason}</p>
          )}
        </RevisionResultShell>
      )}
    </div>
  )
}

const SECTION_LABEL = {
  question: '题面',
  answer_key: '答案',
  evidence: '证据',
  group: '题组版式',
  instruction: '作答说明',
} as const

const FIELD_LABEL: Record<string, string> = {
  carrier_before: '空格前文字',
  carrier_after: '空格后文字',
  canonical: '标准答案',
  alternatives: '可接受答案',
  turn_index: '材料位置',
  quote: '证据原句',
  word_limit: '词数限制',
  instruction_text: '作答要求',
  title: '题组标题',
}

function VersionDiffSummary({
  changes,
}: {
  changes: NonNullable<QuestionVersionsState['selectedVersion']>['field_changes']
}) {
  if (!changes?.length) return null
  return (
    <details className="question-version-diff">
      <summary>查看 {changes.length} 项修改</summary>
      <ul>
        {changes.map((change, index) => (
          <li key={`${change.question_number}-${change.section}-${change.field}-${index}`}>
            <strong>
              Q{change.question_number} · {SECTION_LABEL[change.section]} ·{' '}
              {FIELD_LABEL[change.field] ?? change.field}
            </strong>
            <span>{displayDiffValue(change.before)} → {displayDiffValue(change.after)}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

function displayDiffValue(value: unknown): string {
  if (value === '' || value == null) return '（空）'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function ReasonList({
  reasons,
  showReferences = false,
}: {
  reasons: { comment_id: string; question_number: number; reason: string; references?: string[] }[]
  showReferences?: boolean
}) {
  return (
    <ul>
      {reasons.map((reason) => (
        <li key={`${reason.comment_id}-${reason.question_number}`}>
          Q{reason.question_number}：{reason.reason}
          {showReferences && (reason.references?.length ?? 0) > 0 && (
            <span className="revision-references">
              核对依据：{reason.references?.join('；')}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

function RevisionResultShell({
  state,
  className,
  title,
  children,
  dismissable = true,
}: {
  state: QuestionVersionsState
  className: string
  title: string
  children: ReactNode
  dismissable?: boolean
}) {
  return (
    <div className={`question-revision-result ${className}`} role="alert">
      <div className="question-revision-result-head">
        <strong>{title}</strong>
        {dismissable && (
          <button
            type="button"
            className="icon-btn"
            aria-label="关闭修改结果"
            title="关闭"
            onClick={state.dismissRevisionResult}
          >
            ×
          </button>
        )}
      </div>
      {children}
    </div>
  )
}
