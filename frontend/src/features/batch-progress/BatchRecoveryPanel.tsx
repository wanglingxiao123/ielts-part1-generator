import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/endpoints'
import { scenarioMeta } from '@/config/scenarioMeta'
import { ApiError, userMessage } from '@/api/http'

import type { BatchRecoveryPlan } from '@/contracts/batchRecovery'

export function BatchRecoveryPanel({ batchId, onStarted }: { batchId: string; onStarted: () => void }) {
  const [plan, setPlan] = useState<BatchRecoveryPlan | null>(null)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)
  const storageKey = `ielts:batch-recovery:${batchId}`
  const [pending, setPending] = useState(() => {
    try { return sessionStorage.getItem(storageKey) ?? '' } catch { return '' }
  })
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    setLoading(true)
    async function read() {
      try {
        const next = await api.batchRecovery(batchId)
        if (cancelled) return
        setPlan(next); setReadError('')
        if (next.status === 'running') timer = setTimeout(() => void read(), 5000)
      } catch (err) {
        if (!cancelled) setReadError(userMessage(err, '断点状态暂时读取失败，请重试查询。'))
      } finally { if (!cancelled) setLoading(false) }
    }
    void read()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [batchId, reload])

  function remember(id: string) {
    setPending(id)
    try {
      if (id) sessionStorage.setItem(storageKey, id)
      else sessionStorage.removeItem(storageKey)
    } catch { /* The mounted panel still retains an uncertain request ID. */ }
  }
  async function start() {
    if (busy) return
    const id = pending || Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (byte) => byte.toString(16).padStart(2, '0')).join('')
    remember(id)
    setBusy(true); setError('')
    try {
      const response = await api.recoverBatch(batchId, id)
      if (!alive.current) return
      remember('')
      if (response.status === 'failed') {
        setError('上次恢复未能启动，请重新发起。')
        setReload((n) => n + 1)
      } else onStarted()
    } catch (err) {
      if (!alive.current) return
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) remember('')
      setError(userMessage(err, '提交结果尚未确认，请点击“确认同一次恢复”找回任务。'))
    } finally { if (alive.current) setBusy(false) }
  }

  const resumable = plan?.targets.filter((target) => target.action === 'resume').length ?? 0
  const restart = plan?.targets.filter((target) => target.action === 'restart').length ?? 0
  const retry = plan?.targets.filter((target) => target.action === 'retry').length ?? 0
  return <section className="banner banner-info" aria-label="批次恢复">
    <strong>继续完成这一批</strong>
    <div>
      {loading && <p role="status">正在检查剩余版位的断点…</p>}
      {plan && <p>已交付 {plan.delivered} / {plan.requested} 套，已交付的材料保持不变。</p>}
      {plan?.status === 'complete' && <p>服务器已记录全部交付；若页面仍缺少材料，请刷新读取已交付结果。</p>}
      {plan?.status === 'running' && <p>后台仍在执行或确认停止状态，进度会自动更新，无需重复提交。</p>}
      {plan?.status === 'readonly' && <p>这一批已提交或已过保留期，不能继续生成。</p>}
      {plan?.status === 'available' && <>
        {!!retry && <p>{retry} 套因模型调用失败而停止，可以重试。已通过的听力材料会保留，重新运行题目生成与审核。</p>}
        {!!resumable && <p>{resumable} 套可从原断点继续，不会重新生成已经通过的材料。</p>}
        {!!restart && <p>{restart} 套没有可继续的断点，将只对这些缺失版位补生成。</p>}
        {plan.targets.filter((target) => target.action === 'blocked').map((target) =>
          <p key={target.slot_id}>{scenarioMeta(target.scenario_key).titleZh} · 第 {target.index + 1} 套：{target.reason}</p>)}
      </>}
      {(error || readError) && <p role="alert">{error || readError}</p>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {(pending || (plan?.status === 'available' && resumable + restart + retry > 0)) && <button
          className="btn btn-primary" disabled={busy || (!pending && (loading || !!readError))}
          onClick={() => void start()}>
          {busy ? '正在提交…' : pending ? '确认同一次恢复' : retry ? `重试生成剩余 ${resumable + restart + retry} 套` : resumable && restart ? `继续断点并补生成剩余 ${resumable + restart} 套` : resumable ? `继续生成剩余 ${resumable} 套` : `补生成这 ${restart} 套`}
        </button>}
        {plan?.status === 'complete' && <button className="btn" disabled={busy} onClick={onStarted}>刷新已交付材料</button>}
        <button className="btn" disabled={busy || loading} onClick={() => setReload((n) => n + 1)}>重新检查断点</button>
      </div>
    </div>
  </section>
}
