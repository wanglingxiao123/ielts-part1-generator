import { displayDate } from '@/domain/displayDate'
import { WorkspaceDrawer } from "./WorkspaceDrawer"
import { useEffect, useMemo, useRef, useState } from 'react'
import type { PromptState } from '@/api/adminPrompts'
import { promptScenarios, type ScenarioRun, type ScenarioRunRequest, type ScenarioHistory, type ScenarioSide } from '@/api/promptScenarios'
import { ApiError, userMessage } from '@/api/http'
import { SCENARIO_CATALOG } from '@/config/scenarios.generated'
import { useModelPreference } from '@/stores/modelPreference'
import { QuestionPreviewPanel } from '@/features/material-reader/QuestionPreviewPanel'
import { CopyButton } from './TrialResults'
import type { Material } from '@/contracts'
import { getThresholds } from '@/config/runtimeConfig'
import { joinArtifacts } from '@/domain/joinArtifacts'
import { summariseExamPoints } from '@/domain/examPoints'
import { analyseFormGroups } from '@/domain/formGroups'
import { advancePhase, phaseOfStage, PHASE_LABEL, type ProgressPhase } from '@/domain/progressStages'
import { PhaseTrack } from '@/features/batch-progress/PhaseTrack'
import { MaterialReader } from '@/features/material-reader/MaterialReader'
import { ExamPointPanel } from '@/features/material-reader/ExamPointPanel'
import { QuestionTypePanel } from '@/features/material-reader/QuestionTypePanel'

const statusLabels: Record<string, string> = {
  queued: '等待开始', running: '执行中', succeeded: '已生成完整材料与题目',
  completed: '两侧均完成', finished_with_issues: '已结束，部分未完成',
  incomplete: '未完成', interrupted: '执行中断', failed: '执行失败', system_failure: '系统故障',
}
const stageLabels: Record<string, string> = {
  queued: '等待开始', starting: '连接生成服务', generating: '生成材料', validating: '校验材料',
  regenerating: '重新生成材料', auditing: '盲审材料', audited: '材料审核完成',
  revising: '修改材料', re_auditing: '重新审核材料', feasibility_checked: '出题可行性检查',
  question_generation_started: '生成题目', question_validated: '校验题目',
  questions_started: '生成题目', question_cross_check: '题目交叉检查',
  questions_rejected: '准备重新出题', questions_restarting: '重新出题',
  question_revision_skipped: '题目修订检查完成', question_set_blocked: '题目暂未通过审核',
  feasibility_checking: '出题可行性检查', material_started: '开始生成材料',
  question_revision_started: '修改题目', question_set_clean: '题目审核通过',
  set_complete: '整套材料完成', material_done: '材料阶段完成', infra_retry: '服务调用重试',
}
function duration(seconds = 0) {
  return `${Math.floor(seconds / 60)}分${Math.floor(seconds % 60)}秒`
}
function RunSide({ side, title, retrying, onRetry }: {
  side: ScenarioSide; title: string; retrying: boolean; onRetry: () => void
}) {
  const [tab, setTab] = useState('material')
  const [annotations, setAnnotations] = useState(false)
  const [jump, setJump] = useState<{ turnIndex: number; nonce: number } | null>(null)
  const result = side.material_result
  const view = useMemo(() => {
    if (!result?.blueprint || !result.audit || !result.cross_check) return null
    return joinArtifacts({
      materialId: result.material_id ?? title, scenarioKey: result.scenario_key ?? '',
      index: 0, material: result.material as Material, blueprint: result.blueprint,
      audit: result.audit, crossCheck: result.cross_check,
    })
  }, [result, title])
  const phase = (side.events ?? []).reduce<ProgressPhase | null>(
    (previous, event) => advancePhase(previous, phaseOfStage(event.stage ?? '')), null,
  ) ?? phaseOfStage(side.stage ?? '')
  const running = side.status === 'running' || side.status === 'queued'
  const turns = result?.material?.listening_material_parts?.flatMap((part) => part.script?.turns ?? []) ?? []
  const questions = side.summary?.sets?.[0]?.questions
  const script = turns.map((turn) => `${turn.speaker}: ${turn.text}`).join('\n')
  return <section className="pm-comparison-side" aria-label={title}>
    <h3>{title}</h3><span className={`pm-status ${side.status === 'succeeded' ? '' : 'is-dirty'}`}>{statusLabels[side.status] ?? side.status}</span>
    <p>{stageLabels[side.stage ?? ''] ?? side.stage} · 耗时 {duration(side.status === 'running' && side.started_at ? Date.now() / 1000 - side.started_at : side.elapsed_seconds)}</p>
    {running && <div className="pm-generation-progress" role="status">
      <strong>当前环节：{phase ? PHASE_LABEL[phase] : '连接生成服务'}</strong>
      <PhaseTrack phase={phase} finished={false} />
      <p>后台正在执行，结果会自动更新；离开页面后可从历史记录继续查看。</p>
    </div>}
    <div className="pm-metrics"><span>材料生成尝试 <strong>{side.counts?.generating ?? 0}</strong></span><span>材料修改 <strong>{side.counts?.revising ?? 0}</strong></span><span>题目修改 <strong>{side.counts?.question_revision_started ?? 0}</strong></span></div>
    {side.error && <p className="pm-alert pm-error">{side.error}</p>}
    {side.retry_action && <div className="pm-retry-action">
      <p>{side.retry_action === 'resume' ? '已保存检查点，可继续完成这一侧。' : '这一侧未交付完整结果，可以重新生成。'}沿用本次提示词与模型，另一侧结果保持不变。</p>
      <button className="btn btn-primary" disabled={retrying} onClick={onRetry}>{retrying ? '正在提交…' : `${side.retry_action === 'resume' ? '继续生成' : '重新生成'}${title}`}</button>
    </div>}
    <nav className="pm-tabs" aria-label={`${title}结果导航`}>{[
      ['material', '材料原文'], ['questions', '题目与答案'], ['audit', '审核与过程'],
    ].map(([id, label]) => <button key={id} className={tab === id ? 'is-active' : ''} onClick={() => setTab(id!)}>{label}</button>)}</nav>
    {tab === 'material' && <div>{turns.length ? <>
      <div className="pm-output-heading"><span>{turns.length} 轮对话</span><CopyButton text={script} label={`复制${title}材料`} /></div>
      {view ? <>
        <ExamPointPanel compact summary={summariseExamPoints(view)} onJump={(turnIndex) => setJump({ turnIndex, nonce: Date.now() })} />
        <button className="btn" aria-expanded={annotations} onClick={() => setAnnotations(!annotations)}>{annotations ? '折叠旁注' : '展开旁注'}</button>
        <MaterialReader view={view} height={520} narrow showAnnotations={annotations} showVerdict={false} jumpToTurn={jump} />
        <QuestionTypePanel analysis={analyseFormGroups(view, getThresholds())} />
      </> : <div className="pm-script">{turns.map((turn, i) => <div key={i}><span>{i + 1}</span><p><strong>{turn.speaker}</strong><br />{turn.text}</p></div>)}</div>}
    </> : <p className="pm-empty">尚无通过完整流程的材料。</p>}</div>}
    {tab === 'questions' && (questions?.package ? <QuestionPreviewPanel pkg={questions.package} blueprint={result?.blueprint ?? null} view={view} /> : <p className="pm-empty">尚未交付完整题目。</p>)}
    {tab === 'audit' && <div>
      {!!side.previous_attempts?.length && <details className="pm-disclosure"><summary>此前尝试（{side.previous_attempts.length} 次）</summary>{side.previous_attempts.map((attempt) => <p key={attempt.attempt}>第 {attempt.attempt + 1} 次 · {statusLabels[attempt.status] ?? attempt.status} · {duration(attempt.elapsed_seconds)}<br />{attempt.error}</p>)}</details>}
      {result?.audit != null && <details className="pm-disclosure"><summary>材料审核报告</summary><pre>{JSON.stringify(result.audit, null, 2)}</pre></details>}
      {questions?.review != null && <details className="pm-disclosure"><summary>题目审核报告</summary><pre>{JSON.stringify(questions.review, null, 2)}</pre></details>}
      {side.summary && <details className="pm-disclosure"><summary>重试计数与最终检查点</summary><pre>{JSON.stringify({ slots: side.summary.slots, system_faults: side.summary.system_faults }, null, 2)}</pre></details>}
      <ol className="pm-stage-list">{side.events?.map((event, i) => <li key={i}><strong>{stageLabels[event.stage ?? ''] ?? event.stage ?? event.reason ?? event.type}</strong>{event.at && <time>{new Date(event.at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</time>}{event.detail != null && <pre>{typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail, null, 2)}</pre>}</li>)}</ol>
    </div>}
  </section>
}

export function ScenarioComparison({ state, dirty }: { state: PromptState; dirty: boolean }) {
  const models = useModelPreference()
  const scenarios = SCENARIO_CATALOG.categories.flatMap((category) => category.scenarios)
  const [scenario, setScenario] = useState(scenarios[0]?.key ?? '')
  const [baseline, setBaseline] = useState('active')
  const [candidate, setCandidate] = useState('draft')
  const [historyError, setHistoryError] = useState('')
  const [historyLoading, setHistoryLoading] = useState(false)
  const [history, setHistory] = useState<ScenarioHistory | null>(null)
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const [runId, setRunId] = useState('')
  const [currentRunId, setCurrentRunId] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [run, setRun] = useState<ScenarioRun | null>(null)
  const [busy, setBusy] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef<ScenarioRunRequest | null>(null)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    setHistoryError(''); setHistoryLoading(true)
    void promptScenarios.history(offset, controller.signal).then((value) => {
      if (!controller.signal.aborted) setHistory(value)
    }).catch((err: unknown) => { if (!controller.signal.aborted) setHistoryError(userMessage(err, '场景对比历史读取失败')) }).finally(() => { if (!controller.signal.aborted) setHistoryLoading(false) })
    return () => controller.abort()
  }, [offset, reload])
  useEffect(() => {
    if (!runId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const next = await promptScenarios.detail(runId, controller.signal)
        if (controller.signal.aborted) return
        setRun(next)
        if (next.status === 'running') timer = setTimeout(() => void poll(), 5000)
      } catch (err) {
        if (!controller.signal.aborted) setError(userMessage(err, '进度暂时读取失败，点击刷新记录重试；后台任务不会因此取消。'))
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [runId, reload])
  const versions = [
    ...(state.bundled ? [{ id: state.bundled.release_id, label: '内置默认版本' }] : []),
    ...state.history.filter((r) => !r.release_id.startsWith('bundled-')).map((r) => ({
      id: r.release_id, label: `${r.release_id.slice(0, 8)} · ${r.created_at ? displayDate(r.created_at) : '默认'}`,
    })),
  ]
  async function retrySide(side: 'baseline' | 'candidate') {
    if (!run) return
    setError(''); setRetrying(true)
    try {
      await promptScenarios.retry(run.run_id, side, run.sides[side].attempt ?? 0)
      if (alive.current) setReload((n) => n + 1)
    } catch (err) {
      if (alive.current) setError(userMessage(err, '提交结果暂未确认，可点击同一按钮重试；不会重复启动。'))
    } finally { if (alive.current) setRetrying(false) }
  }
  async function start(retry = false) {
    setError(''); setBusy(true)
    try {
      if (!retry || !pending.current) {
        // crypto.randomUUID is not available on the HTTP China ALB.
        const bytes = crypto.getRandomValues(new Uint8Array(16))
        pending.current = {
          request_id: Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join(''),
          scenario_id: scenario, baseline, candidate, model_id: models.selectedModelId,
          active_etag: state.active_etag, draft_etag: state.draft_etag,
        }
      }
      const result = await promptScenarios.start(pending.current)
      if (!alive.current) return
      pending.current = null
      setRun(null); setRunId(result.run_id); setCurrentRunId(result.run_id); setOffset(0); setReload((n) => n + 1)
    } catch (err) {
      if (!alive.current) return
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) pending.current = null
      setError(userMessage(err, '启动结果尚未确认，请重试确认同一请求，避免重复生成。'))
    } finally { if (alive.current) setBusy(false) }
  }
  return <section className="pm-library" aria-label="完整场景对比">
    <div className="pm-trial-navigation"><button className="btn" onClick={() => setHistoryOpen(true)}>场景对比历史</button>{runId && runId !== currentRunId && <button className="btn" onClick={() => { setRun(null); setRunId(currentRunId); setReload((n) => n + 1) }}>返回当前对比</button>}</div>
    {runId && runId !== currentRunId && <p className="pm-history-banner">正在查看历史对比；下方材料采用当时的版本快照，当前对比设置已保留。</p>}
    {error && <p role="alert" className="pm-alert pm-error">{error}</p>}
    <div hidden={!!runId && runId !== currentRunId}>
    <div className="pm-card-heading"><div><span className="pm-eyebrow">发布验收</span><h3>完整场景对比</h3><p className="pm-purpose">同一场景、同一模型，两侧各生成一套材料和题目，执行完整审核与校验。</p></div><span className="pm-model">{models.models.find((m) => m.id === models.selectedModelId)?.label ?? models.selectedModelId}</span></div>
    <fieldset disabled={busy || !!pending.current} className="pm-scenario-form">
      <label>测试场景<select aria-label="测试场景" value={scenario} onChange={(e) => setScenario(e.target.value)}>{scenarios.map((s) => <option key={s.key} value={s.key}>{s.titleZh}</option>)}</select></label>
      <label>基线版本<select aria-label="基线版本" value={baseline} onChange={(e) => setBaseline(e.target.value)}><option value="active">当前生效版本 · {state.active.release_id.slice(0, 8)}</option>{versions.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}</select></label>
      <label>候选版本<select aria-label="候选版本" value={candidate} onChange={(e) => setCandidate(e.target.value)}><option value="draft" disabled={!state.draft}>已保存草稿</option>{versions.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}</select></label>
    </fieldset>
    <div className="pm-trial-actions"><span>{dirty ? '请先保存本地编辑，再启动场景对比。' : '共生成 2 套，仅测试文字链路；关闭页面后后台继续执行。'}</span>
      <button className="btn btn-primary" disabled={busy || dirty || !models.selectedModelId || (!state.draft && candidate === 'draft')} onClick={() => void start(!!pending.current)}>{busy ? '正在提交…' : pending.current ? '重试确认同一请求' : '开始完整对比'}</button>
    </div>
    <p className="pm-purpose">两侧内容可能不同，请结合材料、题目和运行过程判断；单次结果不代表稳定的质量提升。测试结果不会自动进入正式审核队列。</p>
    </div>
    <WorkspaceDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} title="场景对比历史">
      <button className="btn" disabled={historyLoading} onClick={() => { setHistoryError(''); setReload((n) => n + 1) }}>刷新记录</button>
      {historyLoading && <p role="status">正在读取历史记录…</p>}
      {historyError && <p role="alert" className="pm-alert pm-error">{historyError}，请点击刷新记录重试。</p>}
      <div className="pm-history-list">{history?.items.map((item) => <button className={`pm-trial-history-row ${runId === item.run_id ? 'is-selected' : ''}`} key={item.run_id} onClick={() => { setRun(null); setRunId(item.run_id); setHistoryOpen(false); setReload((n) => n + 1) }}><strong>{item.scenario.title}</strong><span>{displayDate(item.created_at)}</span><span>{item.model_id}</span><span>{statusLabels[item.status] ?? item.status}</span><code>{item.run_id.slice(0, 8)}</code></button>)}</div>
      {!historyLoading && !historyError && history?.items.length === 0 && <p>暂无完整场景对比。</p>}
      <div className="pm-history-pagination"><button className="btn" disabled={historyLoading || !offset} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button><button className="btn" disabled={historyLoading || !!historyError || history?.next_offset == null} onClick={() => setOffset(history!.next_offset!)}>下一页</button></div>
    </WorkspaceDrawer>
    {runId && !run && <p>正在读取对比详情…</p>}
    {run && <div className="pm-scenario-results">
      <div className="pm-result-heading"><strong>{run.scenario.title} · {statusLabels[run.status] ?? run.status}</strong><span>{run.model_id} · {run.run_id.slice(0, 8)}</span></div>
      <details className="pm-disclosure"><summary>查看本次提示词差异与版本（{run.prompt_changes.length} 项）</summary>
        <p>基线来源：{run.versions.baseline.source_release}<br />基线快照：{run.versions.baseline.release_id}<br />候选来源：{run.versions.candidate.source_release}<br />候选快照：{run.versions.candidate.release_id}</p>
        {run.prompt_changes.map((change) => <details key={change.id}><summary>{change.name}</summary><div className="pm-columns"><div><h4>基线文本</h4><pre>{change.baseline || '（空）'}</pre></div><div><h4>候选文本</h4><pre>{change.candidate || '（空）'}</pre></div></div></details>)}
      </details>
      <div className="pm-scenario-columns"><RunSide key={`${run.run_id}-base`} title="基线版本" side={run.sides.baseline} retrying={retrying} onRetry={() => void retrySide('baseline')} /><RunSide key={`${run.run_id}-candidate`} title="候选版本" side={run.sides.candidate} retrying={retrying} onRetry={() => void retrySide('candidate')} /></div>
    </div>}
  </section>
}
