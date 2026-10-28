import { WorkspaceDrawer } from "./WorkspaceDrawer"
import { ReleaseChanges } from './ReleaseChanges'
import { useEffect, useRef, useState } from 'react'
import { adminPrompts, type PromptState, type PromptTrial, type PromptTrialRequest, type PromptValues } from '@/api/adminPrompts'
import { ApiError, userMessage } from '@/api/http'
import { useSession } from '@/auth/useSession'
import { useModelPreference } from '@/stores/modelPreference'
import './prompt-manager.css'
import { TrialResults, TrialHistoryPanel } from './TrialResults'
import { ScenarioComparison } from './ScenarioComparison'
import { displayDate } from '@/domain/displayDate'
import { mergeDraft } from './mergeDraft'
import { recoverDraft, recoverTrial, rememberWorkspace } from './workspaceRecovery'

function valuesFor(state: PromptState): PromptValues {
  return Object.fromEntries(state.catalogue.map((prompt) => [
    prompt.id, state.draft?.values[prompt.id] ?? state.active.values[prompt.id] ?? prompt.default,
  ]))
}

function protectedText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? ''
}

function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    edit: 'm15 4 5 5M4 20l4-1L20 7a2 2 0 0 0-5-5L3 14l-1 6ZM13 20h8',
    trial: 'm9 5 11 7-11 7ZM3 5v14',
    history: 'M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2',
    skills: 'M12 5v16M12 5C8 2 4 3 2 4v15c4-2 7-1 10 2 3-3 6-4 10-2V4c-2-1-6-2-10 1Z',
    search: 'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0Zm-1 5 5 5',
    refresh: 'M20 7a9 9 0 0 0-15-2L2 8m0-6v6h6M4 17a9 9 0 0 0 15 2l3-3m0 6v-6h-6',
    lock: 'M5 10h14v11H5ZM8 10V7a4 4 0 0 1 8 0v3M12 14v3',
  }
  return <svg className="pm-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.edit} /></svg>
}
function shortVersion(id: string) { return id.startsWith('bundled') ? '应用默认' : id.length > 12 ? id.slice(0, 8) : id }

export function PromptManagerPage() {
  const session = useSession()
  // Mount the fetching component only for an authenticated administrator.
  if (session.isLoading) return <div className="page">加载中…</div>
  if (!session.isAuthenticated || !session.isAdmin) {
    return <div className="page" role="alert">仅管理员可访问提示词管理。</div>
  }
  return <PromptEditor key={session.email} email={session.email} />
}

function PromptEditor({ email }: { email: string }) {
  const [pendingTrial, setPendingTrial] = useState<PromptTrialRequest | null>(() => recoverTrial(email))
  const [view, setView] = useState('edit')
  const [rulesOpen, setRulesOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null)
  const [historicalId, setHistoricalId] = useState('')
  const [historical, setHistorical] = useState<PromptTrial | null>(null)
  const [historicalError, setHistoricalError] = useState('')
  const [historicalReload, setHistoricalReload] = useState(0)
  const [scenarioOpened, setScenarioOpened] = useState(false)
  const [search, setSearch] = useState('')
  const [compare, setCompare] = useState(false)
  const [showDefaults, setShowDefaults] = useState(false)
  const [state, setState] = useState<PromptState | null>(null)
  const [values, setValues] = useState<PromptValues>({})
  const [baseline, setBaseline] = useState<PromptValues>({})
  const [conflictIds, setConflictIds] = useState<string[]>([])
  const [selected, setSelected] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [conflict, setConflict] = useState(false)
  const [input, setInput] = useState('')
  const [trialId, setTrialId] = useState('')
  const [trial, setTrial] = useState<PromptTrial | null>(null)
  const [trialError, setTrialError] = useState('')
  const [pollAttempt, setPollAttempt] = useState(0)
  const [trialContext, setTrialContext] = useState<{ prompt: string; model: string; release: string; etag: string | null } | null>(null)
  const models = useModelPreference()
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    setBusy(true)
    void adminPrompts.get(controller.signal).then((next) => {
      if (controller.signal.aborted) return
      setState(next)
      const recovered = recoverDraft(email)
      const saved = valuesFor(next)
      const compatible = recovered && next.catalogue.every((item) => typeof recovered.values[item.id] === 'string')
      if (compatible && next.catalogue.some((item) => recovered.values[item.id] !== saved[item.id])) {
        const merged = mergeDraft(recovered.baseline ?? (recovered.draft_etag === next.draft_etag && !recovered.conflicted ? saved : {}), recovered.values, saved)
        setValues(merged.values); setBaseline(merged.baseline); setConflictIds(merged.conflicts)
        setSelected(next.catalogue.some((item) => item.id === recovered.selected) ? recovered.selected : next.catalogue[0]?.id ?? '')
        setConflict(merged.conflicts.length > 0)
        setNotice(merged.conflicts.length ? '已恢复未保存编辑；同一模块有不同修改，请逐项选择保留的内容。' : '已恢复本标签页未保存的编辑，并合并其他管理员的修改。')
      } else {
        setValues(saved); setBaseline(saved)
        setSelected(next.catalogue[0]?.id ?? '')
      }
      const pending = recoverTrial(email)
      if (pending) {
        setInput(pending.input)
        if (next.catalogue.some((item) => item.id === pending.prompt_id)) setSelected(pending.prompt_id)
        setView('trial')
      }
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(userMessage(err, '提示词读取失败，请重试。'))
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false)
    })
    return () => { mounted.current = false; controller.abort() }
  }, [email])

  const saved = state ? valuesFor(state) : {}
  const dirty = state !== null && state.catalogue.some((prompt) => values[prompt.id] !== saved[prompt.id])
  const prompt = state?.catalogue.find((item) => item.id === selected)
  const testing = !!trialId && trial?.status === 'running'

  useEffect(() => {
    if (!state) return
    rememberWorkspace(email, 'draft', dirty ? {
      values, baseline, draft_etag: state.draft_etag, selected, conflicted: conflict,
    } : null)
  }, [email, state, values, baseline, selected, conflict, dirty])

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  useEffect(() => {
    if (!trialId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const result = await adminPrompts.result(trialId, controller.signal)
        if (controller.signal.aborted) return
        setTrial(result)
        setTrialError('')
        if (result.status === 'running') timer = setTimeout(() => void poll(), 1500)
      } catch (err) {
        if (!controller.signal.aborted) setTrialError(userMessage(err, '试运行结果读取失败，可重试查询。'))
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [trialId, pollAttempt])

  useEffect(() => {
    if (!historicalId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    setHistorical(null); setHistoricalError('')
    async function read() {
      try {
        const record = await adminPrompts.result(historicalId, controller.signal)
        if (controller.signal.aborted) return
        setHistorical(record)
        if (record.status === 'running') timer = setTimeout(() => void read(), 1500)
      } catch (err) {
        if (!controller.signal.aborted) setHistoricalError(userMessage(err, '历史测试读取失败，请重试。'))
      }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [historicalId, historicalReload])

  function fail(err: unknown) {
    if (err instanceof ApiError && err.status === 409 &&
      !['TRIAL_BUSY', 'TRIAL_REQUEST_CONFLICT'].includes(err.code)) {
      setConflict(true)
      setError('版本冲突：其他管理员已更新配置。本地编辑已保留，请刷新服务器版本，核对后重新保存。')
    } else {
      setError(userMessage(err, '操作失败，本地编辑已保留，请重试。'))
    }
  }

  function clearTrial() {
    setHistoricalId(''); setHistorical(null)
    setInput(''); setTrialId(''); setTrial(null); setTrialContext(null); setTrialError('')
  }

  async function mutate(operation: () => Promise<PromptState>, message: string) {
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await operation()
      if (!mounted.current) return
      if (next.active.release_id !== state?.active.release_id) clearTrial()
      setPublishOpen(false)
      setRollbackTarget(null)
      setShowDefaults(false)
      setState(next); setValues(valuesFor(next)); setBaseline(valuesFor(next)); setConflictIds([]); setConflict(false); setNotice(next.operation_notice ?? message)
    } catch (err) {
      if (mounted.current) fail(err)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function refresh() {
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await adminPrompts.get()
      if (!mounted.current) return
      if (next.active.release_id !== state?.active.release_id) clearTrial()
      setState(next)
      const merged = mergeDraft(baseline, values, valuesFor(next))
      setValues(merged.values); setBaseline(merged.baseline); setConflictIds(merged.conflicts)
      setSelected((id) => next.catalogue.some((item) => item.id === id) ? id : next.catalogue[0]?.id ?? '')
      setConflict(merged.conflicts.length > 0)
      setNotice(merged.conflicts.length ? '同一模块有不同修改，请逐项选择后再保存。' : '已刷新服务器版本，并合并其他管理员的修改。本地编辑已保留。')
    } catch (err) {
      if (mounted.current) fail(err)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function startTrial() {
    if (!state || (!prompt && !pendingTrial)) return
    setBusy(true); setError(''); setNotice(''); setTrialError('')
    const request = pendingTrial ?? {
      request_id: Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join(''),
      prompt_id: selected, input, model_id: models.selectedModelId, draft_etag: state.draft_etag,
    }
    rememberWorkspace(email, 'trial', request)
    setPendingTrial(request)
    try {
      const context = {
        prompt: state.catalogue.find((item) => item.id === request.prompt_id)?.name ?? request.prompt_id,
        model: request.model_id,
        release: state.active.release_id, etag: request.draft_etag,
      }
      const result = await adminPrompts.test(request)
      if (!mounted.current) return
      rememberWorkspace(email, 'trial', null)
      setPendingTrial(null)
      setTrialContext(context); setTrial({ status: 'running' }); setTrialId(result.test_id)
    } catch (err) {
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        rememberWorkspace(email, 'trial', null)
        if (mounted.current) setPendingTrial(null)
      }
      if (mounted.current) fail(err)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function saveDraft(toTrial = false) {
    if (!state || busy || conflict || conflictIds.length) return
    setBusy(true); setError(''); setNotice('')
    let current = state
    let local = values
    let base = baseline
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const next = await adminPrompts.save(local, current.draft_etag)
          if (!mounted.current) return
          if (next.active.release_id !== state.active.release_id) clearTrial()
          setState(next); setValues(valuesFor(next)); setBaseline(valuesFor(next))
          setConflictIds([]); setConflict(false)
          setNotice('草稿已保存，其他管理员的修改已保留，尚未发布。')
          if (toTrial) { setHistoricalId(''); setView('trial') }
          return
        } catch (err) {
          if (!(err instanceof ApiError) || err.status !== 409) throw err
          current = await adminPrompts.get()
          if (!mounted.current) return
          const merged = mergeDraft(base, local, valuesFor(current))
          local = merged.values; base = merged.baseline
          setState(current); setValues(local); setBaseline(base); setConflictIds(merged.conflicts)
          setConflict(merged.conflicts.length > 0)
          if (current.active.release_id !== state.active.release_id) clearTrial()
          if (merged.conflicts.length) {
            setView('edit'); setError('版本冲突：同一模块被同时修改。请对照双方内容并逐项选择，再保存草稿。')
            return
          }
        }
      }
      setError('其他管理员正在连续保存。已合并收到的修改，请稍后再保存。')
    } catch (err) { if (mounted.current) fail(err) }
    finally { if (mounted.current) setBusy(false) }
  }

  function resolveConflict(id: string, useServer: boolean) {
    if (!state) return
    const server = valuesFor(state)[id]!
    if (useServer) setValues((previous) => ({ ...previous, [id]: server }))
    setBaseline((previous) => ({ ...previous, [id]: server }))
    const remaining = conflictIds.filter((key) => key !== id)
    setConflictIds(remaining); setConflict(remaining.length > 0)
    setError('')
    setNotice(remaining.length ? '请继续处理其余冲突。' : '冲突已处理。请核对内容并保存草稿，尚未发布。')
  }

  async function saveAndTrial() {
    if (!state || busy || conflict) return
    if (dirty || !state.draft) { await saveDraft(true); return }
    setHistoricalId(''); setView('trial')
    setNotice('已带入当前模块。填写样例后点击运行对比，才会开始测试。')
  }

  const changedCount = state?.catalogue.filter((item) => (values[item.id] ?? item.default) !== (state.active.values[item.id] ?? item.default)).length ?? 0
  const visiblePrompts = state?.catalogue.filter((item) => `${item.name} ${item.purpose}`.toLowerCase().includes(search.toLowerCase())) ?? []
  const tabs = [{ id: 'edit', label: '提示词编辑' }, { id: 'trial', label: '样例试运行' }, { id: 'scenario', label: '完整场景对比' }, { id: 'history', label: '版本管理' }]
  return (
    <main className="page prompt-manager">
      <header className="pm-header">
        <div className="pm-heading"><span className="pm-brand"><Icon name="edit" /></span><div><h2>提示词管理</h2><p>调整 AI 的工作指令，让每一次生成更符合你的要求。</p></div></div>
        <div className="pm-header-actions"><span className="pm-admin"><Icon name="lock" />管理员工作区</span><button className="btn" disabled={!state} onClick={() => setRulesOpen(true)}><Icon name="skills" />规则参考</button></div>
      </header>
      {error && <div hidden={publishOpen} role="alert" className="pm-alert pm-error">{error}</div>}
      {notice && <div role="status" className="pm-alert pm-notice">{notice}</div>}
      <div className="pm-toolbar">
        <nav aria-label="提示词管理导航" className="pm-tabs">{tabs.map((tab) => <button key={tab.id} aria-current={view === tab.id ? 'page' : undefined} className={view === tab.id ? 'is-active' : ''} onClick={() => { setView(tab.id); if (tab.id === 'scenario') setScenarioOpened(true) }}><Icon name={tab.id} />{tab.label}{tab.id === 'trial' && testing && <span className="pm-dot" />}</button>)}</nav>
        <button className="pm-refresh" disabled={busy} aria-label="刷新服务器版本（保留本地编辑）" title="刷新服务器版本，保留本地编辑" onClick={() => void refresh()}><Icon name="refresh" />刷新</button>
      </div>
      {!state && <div className="pm-empty">{busy ? '正在加载工作区…' : '暂时无法加载配置，请点击刷新重试。'}</div>}
      {state && <>
        <p className="pm-draft-owner">{state.draft ? <>共享草稿 · 最近保存：{state.draft.actor ?? '未知管理员'} · {state.draft.updated_at ? displayDate(state.draft.updated_at) : '时间未记录'}</> : '共享草稿尚未保存'}<span>不同模块自动合并，同一模块的不同修改需确认。</span></p>
        {!!conflictIds.length && <section className="pm-conflicts" aria-label="草稿冲突处理">
          <h3>有 {conflictIds.length} 个模块需要你确认</h3>
          <p>以下内容尚未保存。逐项选择后再保存整套草稿；若期间有人继续修改，系统会再次检查。</p>
          {conflictIds.map((id) => <section key={id} className="pm-conflict-item" aria-label={`处理冲突：${state.catalogue.find((item) => item.id === id)?.name ?? id}`}>
            <h4>{state.catalogue.find((item) => item.id === id)?.name ?? id}</h4>
            <div className="pm-conflict-columns">
              <div><label className="pm-field">我的编辑<textarea readOnly value={values[id] ?? ''} /></label><button className="btn" disabled={busy} onClick={() => resolveConflict(id, false)}>保留我的编辑</button></div>
              <div><label className="pm-field">服务器最新草稿<textarea readOnly value={valuesFor(state)[id] ?? ''} /></label><button className="btn" disabled={busy} onClick={() => resolveConflict(id, true)}>采用服务器内容</button></div>
            </div>
          </section>)}
        </section>}
        <div className="pm-release-bar"><div className="pm-publish-state"><span className="pm-status"><i />已生效</span><code title={state.active.release_id}>{shortVersion(state.active.release_id)}</code><span className="pm-publish-note">发布仅影响新任务</span></div><div className="pm-publish-actions"><span className={dirty || changedCount ? 'pm-pending-state' : ''} title={dirty ? '本地编辑尚未保存，请先保存草稿。' : changedCount ? '已保存草稿与当前生效版本不同，发布后用于新任务。' : '草稿已保存，且与当前生效版本一致。'}>{busy ? '处理中…' : dirty ? '有未保存修改' : changedCount ? `草稿与生效版本有 ${changedCount} 项差异` : '已同步'}</span><button className="btn btn-primary" disabled={busy || conflict || dirty || !state.draft || changedCount === 0} title={changedCount === 0 ? '与当前生效版本一致' : undefined} onClick={() => { setError(''); setPublishOpen(true) }}>发布整套草稿 <span aria-hidden="true">↗</span></button></div></div>
        <div className="pm-workspace" hidden={view !== 'edit' && view !== 'trial'}>
          <aside className="pm-sidebar" aria-label="提示词目录">
            <div className="pm-sidebar-head"><strong>提示词目录</strong><span>{state.catalogue.length}</span></div>
            <label className="pm-search"><Icon name="search" /><input aria-label="搜索提示词" placeholder="搜索名称或用途…" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
            <div className="pm-prompt-list">
              {[{ label: '角色指令', role: true }, { label: '任务指令', role: false }].map((group) => {
                const items = visiblePrompts.filter((item) => item.id.startsWith('role.') === group.role)
                return items.length > 0 && <div key={group.label} className="pm-group"><h3>{group.label}<span>{items.length}</span></h3>{items.map((item) => <button key={item.id} aria-label={`选择提示词：${item.name}`} aria-pressed={selected === item.id} className={selected === item.id ? 'is-selected' : ''} disabled={busy || !!pendingTrial} onClick={() => setSelected(item.id)}><span className="pm-prompt-symbol">{group.role ? 'A' : 'T'}</span><span>{item.name}</span>{(values[item.id] ?? item.default) !== (state.active.values[item.id] ?? item.default) && <i className="pm-changed-dot" title="与生效版本不同" />}</button>)}</div>
              })}
              {!visiblePrompts.length && <p className="pm-no-match">未找到匹配的提示词</p>}
            </div>
            <div className="pm-sidebar-foot"><span><i className="pm-changed-dot" />已调整 {changedCount} 项</span><button disabled={busy || conflict || !prompt} onClick={() => { setValues(Object.fromEntries(state.catalogue.map((item) => [item.id, item.default]))); setNotice('已将全部提示词的默认文本填入本地草稿。请保存草稿；此操作不会发布。') }}>恢复全部默认文本</button></div>
          </aside>
          <div className="pm-main">
            {prompt && <section hidden={view !== 'edit'} aria-label="提示词编辑" className="pm-editor-card">
              <div className="pm-card-heading"><div><span className="pm-eyebrow">{selected.startsWith('role.') ? '角色指令' : '任务指令'}</span><h3>{prompt.name}</h3></div><span className={`pm-status ${dirty ? 'is-dirty' : ''}`}><i />{dirty ? '未保存' : state.draft ? '草稿已保存' : '与发布同步'}</span></div>
              <p className="pm-purpose">{prompt.purpose}</p>
              <div className="pm-editor-toolbar"><span>业务指令 <span className="pm-subtle">· 可编辑</span></span><button className={compare ? 'is-active' : ''} aria-pressed={compare} onClick={() => setCompare(!compare)}>{compare ? '收起对照' : '对照生效版本'}</button></div>
              <div className={`pm-editors ${compare ? 'is-comparing' : ''}`}>
                <label hidden={!compare} className="pm-field pm-baseline"><span>当前生效文本</span><textarea readOnly value={state.active.values[selected] ?? prompt.default} /></label>
                <label className="pm-field pm-draft"><span className="pm-sr-only">草稿业务文本</span><textarea spellCheck={false} disabled={busy} placeholder="在这里补充业务指令，例如表达风格、审核重点…" value={values[selected] ?? ''} onChange={(event) => setValues((previous) => ({ ...previous, [selected]: event.target.value }))} /></label>
              </div>
              <div className="pm-editor-meta"><span>{(values[selected] ?? '').length.toLocaleString()} 字符</span><span>{(values[selected] ?? '') === (state.active.values[selected] ?? prompt.default) ? '与生效版本一致' : '已调整，发布后生效'}</span></div>
              <details className="pm-disclosure"><summary><Icon name="lock" />受保护的规则（只读）</summary><p>输出结构、必需变量、工具权限与审核隔离由程序管理。</p><p>规则来源：<code>{prompt.source}</code></p><pre>{protectedText(prompt.protected)}</pre></details>
              <div className="pm-editor-actions"><span>保存整套编辑，前往测试当前模块</span><button className="btn" disabled={busy || conflict || !prompt || !dirty} title={!dirty ? '无未保存修改' : undefined} onClick={() => void saveDraft()}>保存草稿</button><button className="btn btn-primary" disabled={busy || conflict || testing || (!state.draft && !dirty)} onClick={() => void saveAndTrial()}>{dirty || !state.draft ? '保存并前往测试' : '前往样例试运行'}</button></div>
              {state.draft && <details className="pm-disclosure"><summary>查看服务器已保存的草稿</summary><pre>{state.draft.values[selected] ?? prompt.default}</pre></details>}
            </section>}
            <section hidden={view !== 'trial'} className="pm-trial-card" aria-labelledby="pm-trial-title">
              <div className="pm-trial-navigation"><button className="btn" onClick={() => setHistoryOpen(true)}>试运行历史</button>{historicalId && <button className="btn" onClick={() => setHistoricalId('')}>返回当前测试{testing ? ' · 运行中' : ''}</button>}</div>
              <div hidden={!!historicalId}>
              <div className="pm-card-heading"><div><span className="pm-eyebrow">发布前验证</span><h3 id="pm-trial-title">单任务样例试运行</h3></div><span className="pm-model">{models.models.find((model) => model.id === models.selectedModelId)?.label ?? models.selectedModelId}</span></div>
              <p className="pm-purpose">使用相同样例，对比生效版本与草稿的输出。</p>
              <div className="pm-trial-target"><Icon name="edit" /><span>当前测试</span><strong>{prompt?.name ?? '请选择提示词'}</strong></div>
              <label className="pm-field pm-sample"><span>输入样例</span><textarea placeholder="输入一段场景、材料或修改建议，观察两套指令的表现…" value={input} disabled={busy || testing || !!pendingTrial} onChange={(event) => setInput(event.target.value)} /></label>
              <div className="pm-trial-actions"><span>{pendingTrial ? '上次提交结果尚未确认，重试会找回同一测试。' : dirty || !state.draft ? '请先保存草稿，再运行对比。' : '独立测试，不修改正式材料。'}</span><button className="btn btn-primary" disabled={busy || testing || (!pendingTrial && (conflict || dirty || !state.draft || !prompt || !input.trim() || !models.selectedModelId))} onClick={() => void startTrial()}><Icon name="trial" />{testing ? '试运行中…' : pendingTrial ? '重试确认同一测试' : '运行对比'}</button></div>
              <details className="pm-disclosure"><summary>了解试运行范围</summary><p>这不是完整材料生成流程：仅使用样例直接调用 Agent，不执行完整 Skill 与校验流程，不采纳任何结果。试运行成功不代表后续生成质量保证。</p></details>
              {trialError && <div role="alert" className="pm-alert pm-error">{trialError} <button className="btn" onClick={() => setPollAttempt((value) => value + 1)}>重试查询结果</button></div>}
              {trial && trialContext ? <div aria-label="试运行结果" aria-live="polite" className="pm-trial-results">
                <div className="pm-result-heading"><strong>{trial.status === 'running' ? '正在运行独立对比…' : trial.status === 'completed' ? '试运行完成' : '试运行失败'}</strong><span>{trialContext.prompt} · {trial.model_id ?? trialContext.model}</span></div>
                <TrialResults key={trialId} trial={trial} id={trialId} />
              </div> : <div className="pm-trial-empty"><Icon name="trial" /><strong>看看调整带来了什么变化</strong><span>运行后，两份输出会并排显示在这里。</span></div>}
              </div>
              {historicalId && <div className="pm-historical-main">
                <div className="pm-result-heading"><strong>历史测试 · {state.catalogue.find((item) => item.id === historical?.prompt_id)?.name ?? historical?.prompt_id ?? '正在读取'}</strong><span>{historical?.model_id}</span></div>
                <p className="pm-purpose">以下为当时的测试快照，不代表当前生效版本。当前测试的输入和进度已保留。</p>
                {historicalError && <p role="alert">{historicalError}<button className="btn" onClick={() => setHistoricalReload((n) => n + 1)}>重试读取历史测试</button></p>}
                {!historical && !historicalError && <p>正在读取测试详情…</p>}
                {historical && <>
                  <div className="pm-output-heading"><h4>历史输入样例</h4><button className="btn" disabled={busy || testing || !!pendingTrial || historical.input === undefined || !state.catalogue.some((item) => item.id === historical.prompt_id)} onClick={() => {
                    clearTrial(); setInput(historical.input ?? ''); setSelected(historical.prompt_id ?? ''); setSearch('')
                    setNotice('已复用历史输入。下一次运行将对比当前生效版本与当前已保存的草稿。')
                  }}>复用输入</button></div><pre>{historical.input ?? '该记录未保存输入。'}</pre>
                  <TrialResults key={historicalId} trial={historical} id={historicalId} />
                </>}
              </div>}
            </section>
          </div>
        </div>
        <div hidden={view !== 'scenario'}>{scenarioOpened && <ScenarioComparison state={state} dirty={dirty} />}</div>
        <WorkspaceDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} title="试运行历史">
          {historyOpen && <TrialHistoryPanel catalogue={state.catalogue} selected={historicalId} onSelect={(id) => {
            setHistorical(null); setHistoricalId(id); setHistoricalReload((n) => n + 1); setHistoryOpen(false)
          }} />}
        </WorkspaceDrawer>
        <section hidden={view !== 'history'} className="pm-library pm-versions" aria-labelledby="pm-history-title">
          <div className="pm-card-heading"><div><h3 id="pm-history-title">发布历史 <span className="pm-count">{state.history.length}</span></h3><p className="pm-purpose">查看每次发布的修改，或回到之前的版本。回滚仅影响新任务。</p></div></div>
          <div className="pm-version-list">{state.history.map((release, index) => {
            const current = release.release_id === state.active.release_id
            return <article key={release.release_id} className={`pm-release-entry${current ? ' is-current' : ''}`}>
              <ReleaseChanges release={release} catalogue={state.catalogue}
                heading={<div className="pm-release-info">
                  <div><strong>{`版本 ${state.history.length - index}`}</strong>{current && <span className="pm-status"><i />当前生效</span>}<code title={release.release_id}>{shortVersion(release.release_id)}</code></div>
                  <p><time>{release.created_at ? displayDate(release.created_at) : '随应用提供'}</time><span>·</span><span className="pm-release-actor" title={release.actor}>{release.actor}</span></p>
                </div>}
                actions={!current && <button className="btn pm-rollback" aria-label={`回滚至 ${release.release_id}`} disabled={busy || conflict || dirty} onClick={() => { setError(''); setRollbackTarget(release.release_id) }}>回滚至此版本</button>}
              />
            </article>
          })}</div>
          {!state.history.length && <div className="pm-version-empty"><Icon name="history" /><strong>还没有发布记录</strong><p>首次发布后，可在这里查看修改和历史版本。</p></div>}
          {state.bundled && <div className="pm-default-restore">
            <div className="pm-output-heading"><div><strong>内置默认提示词</strong><p className="pm-purpose">恢复整套业务提示词，保留所有发布历史。</p></div><button className="btn" disabled={busy || conflict || dirty} onClick={() => { setError(''); setShowDefaults(true) }}>恢复内置默认提示词</button></div>
            <WorkspaceDrawer centered open={showDefaults} onClose={() => { if (!busy) setShowDefaults(false) }} title="确认恢复默认提示词">
              <p>请核对以下默认内容。确认后将立即发布默认提示词，并将共享草稿同步为默认内容。旧草稿内容将被替换，所有发布历史保留；仅影响新任务。</p>
              {state.catalogue.map((item) => <details className="pm-disclosure" key={item.id}><summary>{item.name}</summary><pre>{state.bundled!.values[item.id] || '（未设置业务补充）'}</pre></details>)}
              <button className="btn" disabled={busy} onClick={() => setShowDefaults(false)}>取消</button>{' '}
              <button className="btn btn-primary" disabled={busy || conflict || dirty} onClick={() => void mutate(() => adminPrompts.restoreDefaults(state.bundled!.release_id, state.active_etag, state.draft_etag), '已恢复默认提示词并发布，共享草稿也已同步，发布历史保留。')}>确认恢复并发布</button>
              {error && <p role="alert" className="pm-error">{error}</p>}
            </WorkspaceDrawer>
          </div>}
        </section>
        <WorkspaceDrawer open={rulesOpen} onClose={() => setRulesOpen(false)} title="规则参考"><section className="pm-library" aria-labelledby="pm-skills-title"><div className="pm-card-heading"><div><span className="pm-eyebrow">规则参考</span><h3 id="pm-skills-title">Skills 规则来源（只读）</h3><p className="pm-purpose">查看各环节遵循的规范。当前版本暂不支持在线修改。</p></div><span className="pm-admin"><Icon name="lock" />只读资料</span></div><div className="pm-skill-grid">{state.skills.map((skill) => <details key={skill.path} className="pm-skill"><summary><Icon name="skills" /><span><strong>{skill.path.split('/').slice(-1)[0]}</strong><small>{skill.path.split('/').slice(0, -1).join(' / ')}</small></span><span className="pm-open">查看</span></summary><pre>{skill.text}</pre></details>)}</div></section></WorkspaceDrawer>

        <WorkspaceDrawer centered open={rollbackTarget !== null} onClose={() => { if (!busy) setRollbackTarget(null) }} title="确认回滚提示词">
          <p>将当前生效版本 <code>{state.active.release_id}</code> 回滚为以下历史版本的内容：</p>
          <p><strong>{rollbackTarget}</strong></p>
          <p>确认后立即创建新的生效版本，仅影响之后开始的新任务。已有任务和发布历史保留。</p>
          <p>已保存的共享草稿不会随回滚改变；它可能与回滚后的生效版本存在差异。</p>
          <div className="pm-editor-actions"><button className="btn" disabled={busy} onClick={() => setRollbackTarget(null)}>取消回滚</button><button className="btn btn-primary" disabled={busy || conflict || dirty || !rollbackTarget} onClick={() => void mutate(() => adminPrompts.rollback(rollbackTarget!, state.active_etag), '已回滚并生效，仅影响新任务。共享草稿已保留，请留意它与生效版本的差异。')}>{busy ? '正在回滚…' : '确认回滚'}</button></div>
          {error && <p role="alert" className="pm-error">{error}</p>}
        </WorkspaceDrawer>

        <WorkspaceDrawer centered open={publishOpen} onClose={() => { if (!busy) setPublishOpen(false) }} title="确认发布整套草稿">
          <p className="pm-purpose">本次发布包含以下 {changedCount} 项提示词修改，仅影响新任务。不会只发布当前测试的模块。</p>
          {state.catalogue.filter((item) => (values[item.id] ?? item.default) !== (state.active.values[item.id] ?? item.default)).map((item) => <details className="pm-disclosure" key={item.id}><summary>{item.name}</summary><h4>当前生效</h4><pre>{state.active.values[item.id] ?? item.default}</pre><h4>已保存草稿</h4><pre>{values[item.id] || '（空）'}</pre></details>)}
          <div className="pm-editor-actions"><button className="btn" disabled={busy} onClick={() => setPublishOpen(false)}>暂不发布</button><button className="btn btn-primary" disabled={busy || conflict || dirty || !state.draft || changedCount === 0} onClick={() => void mutate(() => adminPrompts.publish(state.active_etag, state.draft_etag), '整套配置已发布，仅对新任务生效。')}>{busy ? '正在发布…' : '确认发布'}</button></div>
          {error && <p role="alert" className="pm-error">{error}</p>}
        </WorkspaceDrawer>
      </>}
    </main>
  )
}
