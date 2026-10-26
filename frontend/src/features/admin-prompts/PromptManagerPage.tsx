import { useEffect, useRef, useState } from 'react'
import { adminPrompts, type PromptState, type PromptTrial, type PromptValues } from '@/api/adminPrompts'
import { ApiError, userMessage } from '@/api/http'
import { useSession } from '@/auth/useSession'
import { useModelPreference } from '@/stores/modelPreference'
import './prompt-manager.css'

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
  return <PromptEditor key={session.email} />
}

function PromptEditor() {
  const [view, setView] = useState('edit')
  const [search, setSearch] = useState('')
  const [compare, setCompare] = useState(false)
  const [state, setState] = useState<PromptState | null>(null)
  const [values, setValues] = useState<PromptValues>({})
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
      setValues(valuesFor(next))
      setSelected(next.catalogue[0]?.id ?? '')
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(userMessage(err, '提示词读取失败，请重试。'))
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false)
    })
    return () => { mounted.current = false; controller.abort() }
  }, [])

  const saved = state ? valuesFor(state) : {}
  const dirty = state !== null && state.catalogue.some((prompt) => values[prompt.id] !== saved[prompt.id])
  const prompt = state?.catalogue.find((item) => item.id === selected)
  const testing = !!trialId && trial?.status === 'running'

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

  function fail(err: unknown) {
    if (err instanceof ApiError && err.status === 409) {
      setConflict(true)
      setError('版本冲突：其他管理员已更新配置。本地编辑已保留，请刷新服务器版本，核对后重新保存。')
    } else {
      setError(userMessage(err, '操作失败，本地编辑已保留，请重试。'))
    }
  }

  async function mutate(operation: () => Promise<PromptState>, message: string) {
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await operation()
      if (!mounted.current) return
      setState(next); setValues(valuesFor(next)); setConflict(false); setNotice(message)
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
      setState(next)
      // A refresh updates concurrency tokens without overwriting unsaved business text.
      if (!dirty) setValues(valuesFor(next))
      setSelected((id) => next.catalogue.some((item) => item.id === id) ? id : next.catalogue[0]?.id ?? '')
      setConflict(false)
      setNotice(dirty ? '已刷新服务器版本，本地编辑已保留。请核对当前生效和服务器草稿后再保存。' : '已刷新服务器版本。')
    } catch (err) {
      if (mounted.current) fail(err)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function startTrial() {
    if (!state || !prompt) return
    setBusy(true); setError(''); setNotice(''); setTrialError('')
    try {
      const context = {
        prompt: prompt.name, model: models.selectedModelId,
        release: state.active.release_id, etag: state.draft_etag,
      }
      const result = await adminPrompts.test({
        prompt_id: selected, input, model_id: context.model, draft_etag: state.draft_etag,
      })
      if (!mounted.current) return
      setTrialContext(context); setTrial({ status: 'running' }); setTrialId(result.test_id)
    } catch (err) {
      if (mounted.current) fail(err)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const changedCount = state?.catalogue.filter((item) => (values[item.id] ?? item.default) !== (state.active.values[item.id] ?? item.default)).length ?? 0
  const visiblePrompts = state?.catalogue.filter((item) => `${item.name} ${item.purpose}`.toLowerCase().includes(search.toLowerCase())) ?? []
  const tabs = [{ id: 'edit', label: '提示词编辑' }, { id: 'trial', label: '试运行' }, { id: 'history', label: '发布记录' }, { id: 'skills', label: 'Skill 资料' }]
  return (
    <main className="page prompt-manager">
      <header className="pm-header">
        <div className="pm-heading"><span className="pm-brand"><Icon name="edit" /></span><div><h2>提示词管理</h2><p>调整 AI 的工作指令，让每一次生成更符合你的要求。</p></div></div>
        <span className="pm-admin"><Icon name="lock" />管理员工作区</span>
      </header>
      {error && <div role="alert" className="pm-alert pm-error">{error}</div>}
      {notice && <div role="status" className="pm-alert pm-notice">{notice}</div>}
      <div className="pm-toolbar">
        <nav aria-label="提示词管理导航" className="pm-tabs">{tabs.map((tab) => <button key={tab.id} aria-current={view === tab.id ? 'page' : undefined} className={view === tab.id ? 'is-active' : ''} onClick={() => setView(tab.id)}><Icon name={tab.id} />{tab.label}{tab.id === 'trial' && testing && <span className="pm-dot" />}</button>)}</nav>
        <button className="pm-refresh" disabled={busy} aria-label="刷新服务器版本（保留本地编辑）" title="刷新服务器版本，保留本地编辑" onClick={() => void refresh()}><Icon name="refresh" />刷新</button>
      </div>
      {!state && <div className="pm-empty">{busy ? '正在加载工作区…' : '暂时无法加载配置，请点击刷新重试。'}</div>}
      {state && <>
        <div className="pm-workspace" hidden={view !== 'edit' && view !== 'trial'}>
          <aside className="pm-sidebar" aria-label="提示词目录">
            <div className="pm-sidebar-head"><strong>提示词目录</strong><span>{state.catalogue.length}</span></div>
            <label className="pm-search"><Icon name="search" /><input aria-label="搜索提示词" placeholder="搜索名称或用途…" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
            <div className="pm-prompt-list">
              {[{ label: '角色指令', role: true }, { label: '任务指令', role: false }].map((group) => {
                const items = visiblePrompts.filter((item) => item.id.startsWith('role.') === group.role)
                return items.length > 0 && <div key={group.label} className="pm-group"><h3>{group.label}<span>{items.length}</span></h3>{items.map((item) => <button key={item.id} aria-label={`选择提示词：${item.name}`} aria-pressed={selected === item.id} className={selected === item.id ? 'is-selected' : ''} disabled={busy} onClick={() => setSelected(item.id)}><span className="pm-prompt-symbol">{group.role ? 'A' : 'T'}</span><span>{item.name}</span>{(values[item.id] ?? item.default) !== (state.active.values[item.id] ?? item.default) && <i className="pm-changed-dot" title="与生效版本不同" />}</button>)}</div>
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
              {state.draft && <details className="pm-disclosure"><summary>查看服务器已保存的草稿</summary><pre>{state.draft.values[selected] ?? prompt.default}</pre></details>}
            </section>}
            <section hidden={view !== 'trial'} className="pm-trial-card" aria-labelledby="pm-trial-title">
              <div className="pm-card-heading"><div><span className="pm-eyebrow">发布前验证</span><h3 id="pm-trial-title">单任务样例试运行</h3></div><span className="pm-model">{models.models.find((model) => model.id === models.selectedModelId)?.label ?? models.selectedModelId}</span></div>
              <p className="pm-purpose">使用相同样例，对比生效版本与草稿的输出。</p>
              <div className="pm-trial-target"><Icon name="edit" /><span>当前测试</span><strong>{prompt?.name ?? '请选择提示词'}</strong></div>
              <label className="pm-field pm-sample"><span>输入样例</span><textarea placeholder="输入一段场景、材料或修改建议，观察两套指令的表现…" value={input} disabled={busy || testing} onChange={(event) => setInput(event.target.value)} /></label>
              <div className="pm-trial-actions"><span>{dirty || !state.draft ? '请先保存草稿，再运行对比。' : '独立测试，不修改正式材料。'}</span><button className="btn btn-primary" disabled={busy || testing || conflict || dirty || !state.draft || !prompt || !input.trim() || !models.selectedModelId} onClick={() => void startTrial()}><Icon name="trial" />{testing ? '试运行中…' : '运行对比'}</button></div>
              <details className="pm-disclosure"><summary>了解试运行范围</summary><p>这不是完整材料生成流程：仅使用样例直接调用 Agent，不执行完整 Skill 与校验流程，不采纳任何结果。试运行成功不代表后续生成质量保证。</p></details>
              {trialError && <div role="alert" className="pm-alert pm-error">{trialError} <button className="btn" onClick={() => setPollAttempt((value) => value + 1)}>重试查询结果</button></div>}
              {trial && trialContext ? <div aria-label="试运行结果" aria-live="polite" className="pm-trial-results">
                <div className="pm-result-heading"><strong>{trial.status === 'running' ? '正在运行独立对比…' : trial.status === 'completed' ? '试运行完成' : '试运行失败'}</strong><span>{trialContext.prompt} · {trial.model_id ?? trialContext.model}</span></div>
                {trial.error && <p role="alert" className="pm-error">{trial.error}</p>}
                <div className="pm-columns"><div><h4><span className="pm-dot neutral" />生效版本输出</h4><pre>{trial.baseline ?? '等待输出…'}</pre></div><div><h4><span className="pm-dot" />草稿版本输出</h4><pre>{trial.draft ?? '等待输出…'}</pre></div></div>
                <details className="pm-disclosure"><summary>查看测试版本与编号</summary><p>对比版本：{trial.baseline_release ?? '正在确认'}<br />草稿版本：{trial.draft_release ?? '正在确认'}<br />测试编号：{trialId}</p></details>
              </div> : <div className="pm-trial-empty"><Icon name="trial" /><strong>看看调整带来了什么变化</strong><span>运行后，两份输出会并排显示在这里。</span></div>}
            </section>
          </div>
        </div>
        <section hidden={view !== 'history'} className="pm-library" aria-labelledby="pm-history-title">
          <div className="pm-card-heading"><div><span className="pm-eyebrow">版本管理</span><h3 id="pm-history-title">发布历史</h3><p className="pm-purpose">回滚仅影响新任务，已有材料保持不变。</p></div><span className="pm-count">{state.history.length} 个版本</span></div>
          <div className="pm-history-list">{state.history.map((release, index) => <div key={release.release_id} className="pm-release"><span className={`pm-release-icon ${release.release_id === state.active.release_id ? 'current' : ''}`}><Icon name="history" /></span><div className="pm-release-info"><div><strong>{release.release_id === state.active.release_id ? '当前生效版本' : `历史版本 ${state.history.length - index}`}</strong><code title={release.release_id}>{shortVersion(release.release_id)}</code></div><p>{release.actor} <span>·</span> {release.created_at ? new Date(typeof release.created_at === 'number' ? release.created_at * 1000 : release.created_at).toLocaleString() : '随应用提供'}</p></div><button className="btn" aria-label={`回滚至 ${release.release_id}`} disabled={busy || conflict || dirty || release.release_id === state.active.release_id} onClick={() => void mutate(() => adminPrompts.rollback(release.release_id, state.active_etag), '已创建回滚发布记录，仅对新任务生效。')}>{release.release_id === state.active.release_id ? '使用中' : '回滚至此版本'}</button></div>)}</div>
        </section>
        <section hidden={view !== 'skills'} className="pm-library" aria-labelledby="pm-skills-title"><div className="pm-card-heading"><div><span className="pm-eyebrow">规则参考</span><h3 id="pm-skills-title">Skills 规则来源（只读）</h3><p className="pm-purpose">查看各环节遵循的规范。当前版本暂不支持在线修改。</p></div><span className="pm-admin"><Icon name="lock" />只读资料</span></div><div className="pm-skill-grid">{state.skills.map((skill) => <details key={skill.path} className="pm-skill"><summary><Icon name="skills" /><span><strong>{skill.path.split('/').slice(-1)[0]}</strong><small>{skill.path.split('/').slice(0, -1).join(' / ')}</small></span><span className="pm-open">查看</span></summary><pre>{skill.text}</pre></details>)}</div></section>
        <footer className="pm-publish-bar"><div className="pm-publish-state"><span className="pm-status"><i />已生效</span><code title={state.active.release_id}>{shortVersion(state.active.release_id)}</code><span className="pm-publish-note">发布仅影响新任务</span></div><div className="pm-publish-actions"><span>{busy ? '处理中…' : dirty ? '有未保存的编辑' : `${changedCount} 项待发布`}</span><button className="btn" disabled={busy || conflict || !prompt} onClick={() => void mutate(() => adminPrompts.save(values, state.draft_etag), '草稿已保存，尚未发布。')}>保存草稿</button><button className="btn btn-primary" disabled={busy || conflict || dirty || !state.draft} onClick={() => void mutate(() => adminPrompts.publish(state.active_etag, state.draft_etag), '整套配置已发布，仅对新任务生效。')}>发布整套草稿 <span aria-hidden="true">↗</span></button></div></footer>
      </>}
    </main>
  )
}
