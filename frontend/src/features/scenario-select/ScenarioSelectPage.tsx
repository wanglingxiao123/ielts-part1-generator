import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { ApiError } from '@/api/http'
import { useCan } from '@/auth/useSession'
import { SCENARIO_CATALOG } from '@/config/scenarios.generated'
import { CUSTOM_SCENARIO_KEY } from '@/config/scenarioTypes'
import type { CreateBatchRequest } from '@/contracts/api'
import { useBatchStore } from '@/stores/batchStore'
import { useBatchStream } from '../batch-progress/useBatchStream'

const CATALOG = SCENARIO_CATALOG

export function ScenarioSelectPage() {
  const navigate = useNavigate()
  const canCreate = useCan('batch.create')
  const stream = useBatchStream()
  const store = useBatchStore()

  const [counts, setCounts] = useState<Record<string, number>>({})
  const [customText, setCustomText] = useState('')
  const [customCount, setCustomCount] = useState(CATALOG.defaultCount)
  const [customOn, setCustomOn] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const selectedKeys = Object.keys(counts).filter((k) => (counts[k] ?? 0) > 0)
  const total =
    selectedKeys.reduce((n, k) => n + (counts[k] ?? 0), 0) + (customOn ? customCount : 0)
  const scenarioCount = selectedKeys.length + (customOn ? 1 : 0)
  const overLimit = total > CATALOG.maxBatch

  const estimate = useMemo(() => {
    // Same envelope the backend returns; shown before submit so the reviewer
    // can judge whether to trim the batch.
    const min = Math.round((total * 100) / 60)
    const max = Math.round((total * 160) / 60)
    return `${min}–${max} 分钟`
  }, [total])

  const toggle = (key: string) => {
    setCounts((prev) => {
      const next = { ...prev }
      if ((next[key] ?? 0) > 0) delete next[key]
      else next[key] = CATALOG.defaultCount
      return next
    })
  }

  const bump = (key: string, delta: number) => {
    setCounts((prev) => ({
      ...prev,
      [key]: Math.max(1, Math.min(CATALOG.maxBatch, (prev[key] ?? CATALOG.defaultCount) + delta)),
    }))
  }

  const submit = async () => {
    setError(null)
    // Intercepted BEFORE submit (prd R2): failing after submit would waste a
    // 15-minute window.
    if (overLimit) {
      setError(
        `本批共 ${total} 套，超过单批上限 ${CATALOG.maxBatch} 套。` +
          '上限来自后端 AgentCore Runtime 的 15 分钟同步硬限，前端不得放宽。请减少场景或数量。',
      )
      return
    }
    if (total === 0) {
      setError('请至少勾选一个场景，或填写自定义场景。')
      return
    }
    if (customOn && customText.trim().length === 0) {
      setError('自定义场景已勾选但内容为空。')
      return
    }

    const requests: CreateBatchRequest['requests'] = selectedKeys.map((key) => ({
      scenario_key: key,
      count: counts[key] ?? CATALOG.defaultCount,
    }))
    if (customOn) {
      requests.push({
        scenario_key: CUSTOM_SCENARIO_KEY,
        scenario_text: customText.trim(),
        count: customCount,
      })
    }

    setBusy(true)
    store.startCreating()
    try {
      const created = await api.createBatch({ requests, options: { narration_mode: 'full' } })
      store.initBatch({
        batchId: created.batch_id,
        total: created.total,
        items: created.items.map((i) => ({
          ...i,
          status: 'pending',
          stage: 'queued',
          attempt: 0,
        })),
      })
      stream.connect(created.batch_id)
      navigate(`/batches/${created.batch_id}`)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'BATCH_LIMIT_EXCEEDED') {
        setError(`${err.message}（上限 ${err.detail?.limit}，请求 ${err.detail?.requested}）`)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
      store.setConnection('idle')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="page">
        <h2>选择场景生成材料</h2>
        {error && (
          <div className="banner banner-bad">
            <strong>无法提交</strong>
            <div>{error}</div>
          </div>
        )}

        {CATALOG.categories.map((cat) => (
          <div key={cat.id} className="scn-cat">
            <h3>
              {cat.titleZh}
              <span className="muted mono" style={{ fontSize: 11 }}>
                {cat.id}
              </span>
            </h3>
            <div className="scn-grid">
              {cat.scenarios.map((scn) => {
                const on = (counts[scn.key] ?? 0) > 0
                return (
                  <div key={scn.key} className={`scn-row${on ? ' on' : ''}`}>
                    <label>
                      <input type="checkbox" checked={on} onChange={() => toggle(scn.key)} />
                      <span>
                        {scn.titleZh} <span className="key">{scn.key}</span>
                        <div className="muted" style={{ fontSize: 11 }} title={scn.hint}>
                          {scn.hint.slice(0, 62)}…
                        </div>
                      </span>
                    </label>
                    {on && (
                      <div className="stepper">
                        <button type="button" onClick={() => bump(scn.key, -1)}>
                          −
                        </button>
                        <span className="mono">{counts[scn.key]}</span>
                        <button type="button" onClick={() => bump(scn.key, 1)}>
                          +
                        </button>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        ))}

        {CATALOG.customScenario.enabled && (
          <div className="scn-cat">
            <h3>自定义场景</h3>
            <div className={`scn-row${customOn ? ' on' : ''}`} style={{ alignItems: 'flex-start' }}>
              <label style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
                <span>
                  <input
                    type="checkbox"
                    checked={customOn}
                    onChange={() => setCustomOn((v) => !v)}
                  />{' '}
                  与勾选项共存，可同时提交
                </span>
                <textarea
                  rows={2}
                  maxLength={CATALOG.customScenario.maxLength}
                  placeholder="例如：A student phones a bike shop about repairing a bicycle…"
                  value={customText}
                  onChange={(e) => setCustomText(e.target.value)}
                  onFocus={() => setCustomOn(true)}
                  style={{ font: 'inherit', padding: 6, width: '100%' }}
                />
                <span className="muted" style={{ fontSize: 11 }}>
                  {customText.length}/{CATALOG.customScenario.maxLength} ·
                  scenario_key=<span className="mono">custom</span>
                </span>
              </label>
              {customOn && (
                <div className="stepper">
                  <button
                    type="button"
                    onClick={() => setCustomCount((c) => Math.max(1, c - 1))}
                  >
                    −
                  </button>
                  <span className="mono">{customCount}</span>
                  <button
                    type="button"
                    onClick={() => setCustomCount((c) => Math.min(CATALOG.maxBatch, c + 1))}
                  >
                    +
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="summary-bar">
        <div>
          本批 <strong className="mono">{scenarioCount}</strong> 个场景 / 共{' '}
          <strong className="mono" style={{ color: overLimit ? 'var(--bad)' : undefined }}>
            {total}
          </strong>{' '}
          套 / 预计 <strong>{total > 0 ? estimate : '—'}</strong>
        </div>
        {overLimit && (
          <span className="flag flag-bad">
            超过单批上限 {CATALOG.maxBatch}（后端 15 分钟同步硬限）
          </span>
        )}
        <div className="spacer" style={{ flex: 1 }} />
        <button
          type="button"
          className="btn btn-primary"
          disabled={overLimit || total === 0 || busy || !canCreate}
          onClick={() => void submit()}
          title={!canCreate ? '当前角色无生成权限' : undefined}
        >
          {busy ? '提交中…' : `提交生成 ${total} 套`}
        </button>
      </div>
    </>
  )
}
