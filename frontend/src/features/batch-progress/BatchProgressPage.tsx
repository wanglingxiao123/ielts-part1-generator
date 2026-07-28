import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { getConfig } from '@/config/runtimeConfig'
import type { MaterialStage } from '@/contracts/api'
import { useBatchStore } from '@/stores/batchStore'
import { useBatchStream } from './useBatchStream'

const STAGE_LABEL: Record<MaterialStage, string> = {
  queued: '排队',
  generating: '生成中',
  validating: '校验中',
  auditing: '评价中',
  revising: '修改中',
  re_auditing: '复评中',
}
const STAGE_ORDER: MaterialStage[] = [
  'queued',
  'generating',
  'validating',
  'auditing',
  'revising',
  're_auditing',
]

function ConnectionBanner({ onRetry }: { onRetry: () => void }) {
  const connection = useBatchStore((s) => s.connection)
  const attempt = useBatchStore((s) => s.reconnectAttempt)
  const lastError = useBatchStore((s) => s.lastError)
  const completed = useBatchStore((s) => Object.keys(s.materials).length)
  const degraded = useBatchStore((s) => s.degradedRecovery)

  if (connection === 'reconnecting') {
    return (
      <div className="banner banner-warn">
        <strong>连接中断，正在重连（第 {attempt}/8 次）</strong>
        <div>
          已到达的 {completed} 套完全不受影响，可继续阅读、对比、选定。后端批次是独立 job，
          不随连接中断而终止。
        </div>
      </div>
    )
  }
  if (connection === 'recovered') {
    return (
      <div className="banner banner-good">
        <strong>连接已恢复</strong>
        <div>已按 since_seq 补齐中断期间的事件，剩余材料将照常到达。</div>
      </div>
    )
  }
  if (connection === 'failed') {
    return (
      <div className="banner banner-bad">
        <strong>重连 8 次均失败，已停止自动重试</strong>
        <div>
          已完成的 {completed} 套不会丢失。{lastError && <span className="mono">{lastError}</span>}
          <div style={{ marginTop: 8 }}>
            <button type="button" className="btn" onClick={onRetry}>
              手动重新连接
            </button>
          </div>
        </div>
      </div>
    )
  }
  if (degraded && (connection === 'streaming' || connection === 'done')) {
    return (
      <div className="banner banner-info">
        <strong>降级恢复态</strong>
        <div>本批次曾发生连接中断，当前数据由快照对账 + 事件重放补齐。</div>
      </div>
    )
  }
  return null
}

export function BatchProgressPage() {
  const { batchId } = useParams<{ batchId: string }>()
  const stream = useBatchStream()
  const store = useBatchStore()
  const cfg = getConfig()
  const [now, setNow] = useState(Date.now())
  const [retryBusy, setRetryBusy] = useState(false)
  const [snapshotError, setSnapshotError] = useState<string | null>(null)

  // Refresh / revisit: reattach to the in-flight batch (prd R3).
  //
  // The store is empty after a reload, so we deliberately resume from seq 0 and
  // let the contract's replay guarantee (§8.2: since_seq returns every later
  // event INCLUDING full material payloads) refill it. The persisted cursor is
  // for mid-session reconnects, where the store still holds the materials and
  // re-sending them would be wasted bandwidth.
  useEffect(() => {
    if (!batchId || stream.isActive(batchId)) return
    const persisted = stream.resumePersisted()
    void (async () => {
      try {
        const snapshot = await api.getBatch(batchId)
        store.applySnapshot(snapshot)
        setSnapshotError(null)
        if (persisted?.batchId === batchId) store.setConnection('streaming')
      } catch (err) {
        // Surfaced, not just logged: against the real backend a reload genuinely
        // loses the batch (the job is bound to the POST), and a page that sits
        // at "已完成 0 / 0" with no explanation reads as a frontend bug.
        console.warn('[batch] snapshot failed', err)
        setSnapshotError(err instanceof Error ? err.message : String(err))
        return
      }
      stream.connect(batchId)
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId])

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  const elapsedMs = store.createdAt ? now - store.createdAt : 0
  const elapsed = `${Math.floor(elapsedMs / 60_000)}:${String(
    Math.floor((elapsedMs % 60_000) / 1000),
  ).padStart(2, '0')}`
  const nearLimit = elapsedMs / 1000 >= cfg.limits.warnAtSeconds
  const completed = Object.keys(store.materials).length
  const items = store.itemOrder.map((id) => store.items[id]).filter((i) => i !== undefined)
  const pending = items.filter((i) => i!.status !== 'done')

  const byScenario = useMemo(() => {
    const map = new Map<string, string[]>()
    for (const id of store.itemOrder) {
      const item = store.items[id]
      if (!item) continue
      const list = map.get(item.scenario_key) ?? []
      list.push(id)
      map.set(item.scenario_key, list)
    }
    return map
  }, [store.itemOrder, store.items])

  const doRetry = async () => {
    if (!batchId) return
    setRetryBusy(true)
    try {
      const res = await api.retryBatch(batchId, {
        material_ids: pending.map((i) => i!.material_id),
      })
      window.location.href = `/batches/${res.batch_id}`
    } finally {
      setRetryBusy(false)
    }
  }

  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>批次进度</h2>
        <span className="mono muted">{batchId}</span>
        <div className="progress-track" style={{ maxWidth: 260 }}>
          <div
            className="progress-fill"
            style={{ width: `${store.total > 0 ? (completed / store.total) * 100 : 0}%` }}
          />
        </div>
        <span>
          已完成 <strong className="mono">{completed}</strong> / {store.total} · 已用{' '}
          <strong className="mono">{elapsed}</strong>
        </span>
        <span className={`flag ${store.connection === 'failed' ? 'flag-bad' : 'flag-neutral'}`}>
          {store.connection}
        </span>
      </div>

      {snapshotError && (
        <div className="banner banner-bad">
          <strong>无法加载本批次</strong>
          <div>{snapshotError}</div>
          <div style={{ marginTop: 8 }}>
            <Link className="btn" to="/">
              返回场景选择，重新提交
            </Link>
          </div>
        </div>
      )}

      {nearLimit && store.status === 'running' && (
        <div className="banner banner-warn">
          <strong>接近 15 分钟硬限</strong>
          <div>已用 {elapsed}，剩余材料可能未完成；未完成部分可在结束后单独补生成。</div>
        </div>
      )}

      <ConnectionBanner onRetry={stream.retryNow} />

      {store.status === 'partial' && (
        <div className="banner banner-warn">
          <strong>批次以 partial 结束</strong>
          <div>
            以下 {pending.length} 套未完成：
            {pending.map((i) => (
              <span key={i!.material_id} className="flag flag-neutral" style={{ margin: '0 3px' }}>
                {i!.scenario_key} #{i!.index + 1}
                {i!.status === 'failed' ? ' · 生成失败' : ' · 未开始'}
              </span>
            ))}
            {/* Distinguishing the two matters: a material skipped by the time
                budget will probably succeed on retry, one that exhausted
                validation three times probably will not. */}
            {pending.some((i) => i!.status === 'failed') && (
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                标记为「生成失败」的是确定性校验连续未通过后放弃的，不是连接中断；
                补生成会重新调用模型，结果可能仍然失败。
              </div>
            )}
            <div style={{ marginTop: 8 }}>
              <button
                type="button"
                className="btn"
                disabled={retryBusy}
                onClick={() => void doRetry()}
              >
                补生成这 {pending.length} 套（新建小批次，不重跑全部）
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="batch-grid">
        {items.map((item) => {
          const ready = item!.status === 'done'
          // A failed material is terminal, not in flight. Without this it renders
          // "生成中 · 第 3 次尝试" next to a 失败 badge and an animated dot —
          // observed against real output, where giving up after three failed
          // validations is a normal outcome rather than an edge case.
          const failed = item!.status === 'failed'
          const stageIdx = STAGE_ORDER.indexOf(item!.stage)
          return (
            <div key={item!.material_id} className="mat-card">
              <header>
                <strong>
                  {item!.scenario_key} · 第 {item!.index + 1} 套
                </strong>
                {ready ? (
                  <span className={`flag ${item!.quarantined ? 'flag-bad' : 'flag-good'}`}>
                    {item!.verdict}
                  </span>
                ) : failed ? (
                  <span className="flag flag-bad">失败</span>
                ) : (
                  <span className="flag flag-neutral">{STAGE_LABEL[item!.stage]}</span>
                )}
              </header>
              <div className="stages">
                {STAGE_ORDER.map((stage, i) => (
                  <div
                    key={stage}
                    className={`stage-dot${
                      ready || (!failed && i < stageIdx)
                        ? ' done'
                        : i === stageIdx && !ready && !failed
                          ? ' active'
                          : ''
                    }`}
                    title={STAGE_LABEL[stage]}
                  />
                ))}
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                {ready
                  ? '已完成，可直接阅读'
                  : failed
                    ? `已放弃于「${STAGE_LABEL[item!.stage]}」· 共 ${item!.attempt} 次尝试`
                    : `${STAGE_LABEL[item!.stage]}${item!.attempt > 1 ? ` · 第 ${item!.attempt} 次尝试` : ''}`}
                {!ready && !failed && item!.subStage && (
                  <div style={{ color: 'var(--warn)' }}>{item!.subStage}</div>
                )}
                {item!.error && <div style={{ color: 'var(--bad)' }}>{item!.error}</div>}
              </div>
              {ready && (
                <div className="row" style={{ marginTop: 8 }}>
                  <Link className="btn btn-sm" to={`/materials/${item!.material_id}`}>
                    阅读
                  </Link>
                  {(byScenario.get(item!.scenario_key)?.length ?? 0) > 1 && (
                    <Link className="btn btn-sm" to={`/compare/${item!.scenario_key}`}>
                      对比本场景
                    </Link>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
