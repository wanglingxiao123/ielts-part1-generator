/**
 * 生成结果页（客户 v2 版式）。
 *
 * 两条客户反馈决定了这个文件的形状：
 *
 * 1. **不把内部环节名给用户看。** 旧版每张卡片都在播报自己的处境
 *    （`booking-hotel · 第 1 套生成中 / 校验未过，重新生成`）。重生成是系统在
 *    重试自己，用户既管不了也不该被告知「校验没过」。现在整批只有顶部一条进度：
 *    进度条 + 生成→校验→修改→复评 四段。段的推进在 batchStore 里只前进不后退
 *    （见 progressStages.ts），所以重试看起来就是「还在生成」。
 *
 * 2. **版式。** 按场景分组，每组一行自适应卡片；卡片上只有第 N 套、统一的
 *    「待审核」、圆形勾选框、十个信息点圆点、预览两行、一个「阅读全文」。
 *    评价方的内部评级（PASS / MINOR_EDITS / FAIL）不做徽章——客户明确要求统一
 *    「待审核」。缺陷通过黄色圆点和缺陷小结说出来，材料照样可选。
 *
 * 保留下来的东西，一个都没动其行为：SSE 重连 + since_seq 补齐 + ConnectionBanner。
 * 批次是个长任务，连接断了不能丢结果，这条比版式重要。
 *
 * 卡片上没有「试听」：语音在选定之后才合成，选之前不存在音频（也不该为被弃用的
 * 材料付 Polly 的钱）。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { scenarioMeta } from '@/config/scenarioMeta'
import { getConfig, getThresholds } from '@/config/runtimeConfig'
import type { MaterialRecord } from '@/contracts/api'
import { buildCardPreview, type CardPreview } from '@/domain/cardPreview'
import { computeDistribution } from '@/domain/distribution'
import { joinFromRecord } from '@/domain/joinArtifacts'
import {
  comparePairReady,
  EMPTY_PICK,
  evaluateSelection,
  pickForCompare,
  toggleSelection,
  type ComparePick,
} from '@/domain/selection'
import {
  describeProgress,
  PHASE_LABEL,
  PHASE_SEQUENCE,
  type ProgressPhase,
} from '@/domain/progressStages'
import { selectActivePhase, useBatchStore } from '@/stores/batchStore'
import { useReviewQueue } from '@/stores/reviewQueueStore'
import { useBatchStream } from './useBatchStream'

/* ── 连接状态提示（行为原样保留） ───────────────────────────────────────────── */

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
          已到达的 {completed} 套完全不受影响，可继续阅读、对比、选定。生成任务在后端独立进行，
          不随连接中断而终止。
        </div>
      </div>
    )
  }
  if (connection === 'recovered') {
    return (
      <div className="banner banner-good">
        <strong>连接已恢复</strong>
        <div>中断期间的结果已补齐，剩余材料将照常到达。</div>
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
        <strong>本批次曾发生连接中断</strong>
        <div>当前结果由快照对账与事件补齐而来，内容完整。</div>
      </div>
    )
  }
  return null
}

/* ── 顶部进度：进度条 + 四段 ────────────────────────────────────────────────── */

function PhaseTrack({ phase, finished }: { phase: ProgressPhase | null; finished: boolean }) {
  const activeIdx = phase ? PHASE_SEQUENCE.indexOf(phase) : -1
  return (
    <div className="phase-track" aria-label="生成进度">
      {PHASE_SEQUENCE.map((p, i) => {
        const done = finished || i < activeIdx
        const active = !finished && i === activeIdx
        return (
          <span key={p} className="phase-step-wrap">
            {i > 0 && <span className="phase-sep">›</span>}
            <span className={`phase-step${done ? ' done' : ''}${active ? ' active' : ''}`}>
              <span className="dot" />
              {PHASE_LABEL[p]}
            </span>
          </span>
        )
      })}
    </div>
  )
}

/* ── 一张材料卡 ─────────────────────────────────────────────────────────────── */

interface CardProps {
  preview: CardPreview
  selected: boolean
  compareMode: boolean
  /** 'a' | 'b' | null —— 对比模式下这张卡是 A 还是 B。 */
  pickSide: 'a' | 'b' | null
  onToggle: () => void
}

function MaterialCard({ preview, selected, compareMode, pickSide, onToggle }: CardProps) {
  const flagged = new Set(preview.flaggedPoints)
  const label = `第 ${preview.index + 1} 套`
  // A selected card keeps looking selected inside compare mode. Hiding it there
  // reads as "entering compare mode threw my选择 away" — it does not, and the
  // bottom bar's count would then contradict the cards.
  const className = [
    'mat-card',
    selected ? 'selected' : '',
    pickSide === 'a' ? 'pick-a' : '',
    pickSide === 'b' ? 'pick-b' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={className} data-material={preview.materialId}>
      <div className="mat-card-top">
        <span className="mat-card-label">{label}</span>
        <span className="row" style={{ gap: 8 }}>
          {/* 统一「待审核」。评价方的内部评级不出现在这里。 */}
          <span className="status-badge">待审核</span>
          <button
            type="button"
            className={`select-check${pickSide || selected ? ' checked' : ''}${
              pickSide === 'b' ? ' pick-b' : ''
            }`}
            aria-pressed={compareMode ? pickSide !== null : selected}
            aria-label={
              compareMode
                ? `${label}：${pickSide ? `已选为材料 ${pickSide.toUpperCase()}` : '点选进入对比'}`
                : `${label}：${selected ? '已选择' : '选择'}`
            }
            onClick={onToggle}
          >
            {pickSide ? pickSide.toUpperCase() : '✓'}
          </button>
        </span>
      </div>

      <div className="point-section">
        <div className="point-label">
          信息点分布（{preview.pointTotal}/{preview.pointTotal}）
        </div>
        <div className="point-dots">
          {preview.pointNumbers.map((n) => (
            <span
              key={n}
              className={`point-dot${flagged.has(n) ? ' flagged' : ''}`}
              title={flagged.has(n) ? `第 ${n} 题的信息点需要看一眼` : `第 ${n} 题的信息点`}
            >
              {n}
            </span>
          ))}
        </div>
      </div>

      <div className="mat-preview">
        {preview.firstLine && <q>{preview.firstLine}</q>}
        <span>— {preview.summary}</span>
      </div>

      {/* 有缺陷的材料照样返回、照样可选；这里把缺点摆出来让用户自己判断。 */}
      {preview.shortcomings.length > 0 && (
        <ul className="mat-flaws">
          {preview.shortcomings.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      )}

      <div className="mat-actions">
        <Link className="btn btn-sm" to={`/materials/${preview.materialId}`}>
          阅读全文
        </Link>
      </div>
    </div>
  )
}

/* ── 页面 ───────────────────────────────────────────────────────────────────── */

export function BatchProgressPage() {
  const { batchId } = useParams<{ batchId: string }>()
  const navigate = useNavigate()
  const stream = useBatchStream()
  const store = useBatchStore()
  const activePhase = useBatchStore(selectActivePhase)
  const submitToQueue = useReviewQueue((s) => s.submit)
  const cfg = getConfig()
  const thresholds = getThresholds()
  const [now, setNow] = useState(Date.now())
  const [retryBusy, setRetryBusy] = useState(false)
  const [snapshotError, setSnapshotError] = useState<string | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set<string>())
  /** 正在对比的场景 key；null = 不在对比模式。 */
  const [compareScenario, setCompareScenario] = useState<string | null>(null)
  const [pick, setPick] = useState<ComparePick>(EMPTY_PICK)

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
        // at "已生成 0 / 0" with no explanation reads as a frontend bug.
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
  const items = store.itemOrder.map((id) => store.items[id]).filter((i) => i !== undefined)
  const pending = items.filter((i) => i!.status !== 'done')

  /** 已到达的材料，按场景分组，场景内按第 N 套排序。 */
  const groups = useMemo(() => {
    const map = new Map<string, MaterialRecord[]>()
    for (const id of store.itemOrder) {
      const record = store.materials[id]
      if (!record) continue
      const list = map.get(record.scenario_key) ?? []
      list.push(record)
      map.set(record.scenario_key, list)
    }
    for (const list of map.values()) list.sort((a, b) => a.index - b.index)
    return map
  }, [store.itemOrder, store.materials])

  /**
   * 每张卡的预览。join + 分布计算不便宜，而一批最多 6 套却会随每次勾选重渲染，
   * 所以按材料集合缓存——勾选、进对比模式都不会重算。
   */
  const previews = useMemo(() => {
    const out = new Map<string, CardPreview>()
    for (const list of groups.values()) {
      for (const record of list) {
        const view = joinFromRecord(record)
        out.set(record.material_id, buildCardPreview(record, view, computeDistribution(view, thresholds)))
      }
    }
    return out
  }, [groups, thresholds])

  const idsByScenario = useMemo(() => {
    const map = new Map<string, string[]>()
    for (const [key, list] of groups) map.set(key, list.map((r) => r.material_id))
    return map
  }, [groups])

  const rule = useMemo(
    () => evaluateSelection({ byScenario: idsByScenario, selected }),
    [idsByScenario, selected],
  )

  const completed = [...groups.values()].reduce((n, list) => n + list.length, 0)
  const finished = store.status === 'done' || store.status === 'partial'
  const scenarioCount = groups.size
  const perScenario = scenarioCount > 0 ? Math.round(store.total / scenarioCount) : 0

  const doRetry = async () => {
    if (!batchId) return
    setRetryBusy(true)
    try {
      const res = await api.retryBatch(batchId, {
        material_ids: pending.map((i) => i!.material_id),
      })
      navigate(`/batches/${res.batch_id}`)
    } finally {
      setRetryBusy(false)
    }
  }

  const enterCompare = (scenarioKey: string) => {
    setCompareScenario(scenarioKey)
    setPick(EMPTY_PICK)
  }

  const leaveCompare = () => {
    setCompareScenario(null)
    setPick(EMPTY_PICK)
  }

  // 并排对比是现成功能，这里只负责把它接上。
  useEffect(() => {
    if (compareScenario && comparePairReady(pick)) {
      navigate(`/compare/${compareScenario}?a=${pick[0]}&b=${pick[1]}`)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pick, compareScenario])

  const doSubmit = () => {
    const at = Date.now()
    submitToQueue(
      [...selected].flatMap((materialId) => {
        const preview = previews.get(materialId)
        if (!preview) return []
        return [
          {
            materialId,
            batchId: batchId ?? '',
            scenarioKey: preview.scenarioKey,
            index: preview.index,
            submittedAt: at,
            summary: preview.summary,
            shortcomingCount: preview.shortcomings.length,
          },
        ]
      }),
    )
    setSelected(new Set())
    navigate('/review-queue')
  }

  return (
    <div className="results">
      <div className="results-progress">
        <span className="batch-id">{batchId}</span>
        <div className="progress-track">
          <div
            className="progress-fill"
            style={{ width: `${store.total > 0 ? (completed / store.total) * 100 : 0}%` }}
          />
        </div>
        <div className="results-stats">
          <span>
            {scenarioCount > 0 ? `${scenarioCount} 场景 × ${perScenario} 套 = ` : ''}
            {store.total} 套材料
          </span>
          <span>{describeProgress({ completed, total: store.total, phase: activePhase, finished })}</span>
          {finished ? (
            <span className="done-badge">✓ 全部完成</span>
          ) : (
            <PhaseTrack phase={activePhase} finished={false} />
          )}
          <span className="muted">已用 {elapsed}</span>
        </div>
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
          <strong>接近 15 分钟上限</strong>
          <div>已用 {elapsed}，剩余材料可能来不及生成；未完成的部分可以在结束后单独补齐。</div>
        </div>
      )}

      <ConnectionBanner onRetry={stream.retryNow} />

      {/* 「有几套没生成出来」是结果，不是环节：这里只说数量和补生成的入口，
          不再逐套播报它卡在哪个内部环节、试了几次。 */}
      {store.status === 'partial' && pending.length > 0 && (
        <div className="banner banner-warn">
          <strong>有 {pending.length} 套未能生成</strong>
          <div>
            已生成的 {completed} 套可以照常选用。缺的部分可以单独补生成，不必重跑整批。
            <div style={{ marginTop: 8 }}>
              <button
                type="button"
                className="btn"
                disabled={retryBusy}
                onClick={() => void doRetry()}
              >
                补生成这 {pending.length} 套
              </button>
            </div>
          </div>
        </div>
      )}

      {completed === 0 && !snapshotError && (
        <div className="panel panel-pad muted">
          {finished ? '本批次没有生成出材料。' : '正在生成，第一套完成后会立刻出现在这里。'}
        </div>
      )}

      {[...groups.entries()].map(([scenarioKey, list]) => {
        const meta = scenarioMeta(scenarioKey)
        const comparing = compareScenario === scenarioKey
        return (
          <section className="scn-group" key={scenarioKey}>
            <div className="scn-group-head">
              <span className="scn-group-icon" aria-hidden="true">
                {meta.icon}
              </span>
              <span className="scn-group-title">{meta.titleZh}</span>
              <span className="scn-group-tag">{meta.categoryZh}</span>
              <span className="spacer" />
              {list.length >= 2 && (
                <button
                  type="button"
                  className={`btn btn-sm${comparing ? '' : ' btn-compare'}`}
                  onClick={() => (comparing ? leaveCompare() : enterCompare(scenarioKey))}
                >
                  {comparing ? '退出对比' : '对比本场景'}
                </button>
              )}
            </div>

            {comparing && (
              <div className="compare-banner">
                <span>对比模式：点选两套材料进行并排对比</span>
                <span className="legend">
                  <span className="legend-item">
                    <span className="legend-dot a" />
                    材料 A
                  </span>
                  <span className="legend-item">
                    <span className="legend-dot b" />
                    材料 B
                  </span>
                </span>
              </div>
            )}

            <div className="mat-row">
              {list.map((record) => {
                const preview = previews.get(record.material_id)
                if (!preview) return null
                const pickSide =
                  comparing && pick[0] === record.material_id
                    ? 'a'
                    : comparing && pick[1] === record.material_id
                      ? 'b'
                      : null
                return (
                  <MaterialCard
                    key={record.material_id}
                    preview={preview}
                    selected={selected.has(record.material_id)}
                    compareMode={comparing}
                    pickSide={pickSide}
                    onToggle={() =>
                      comparing
                        ? setPick((prev) => pickForCompare(prev, record.material_id))
                        : setSelected((prev) => toggleSelection(prev, record.material_id))
                    }
                  />
                )
              })}
            </div>
          </section>
        )
      })}

      {completed > 0 && (
        <div className="results-bar">
          <div className="bar-left">
            <span>
              已选择 <span className="count">{rule.selectedCount}</span> 套材料
            </span>
            <span className="muted">（每场景至少选 1 套）</span>
            {rule.scenariosMissing.length > 0 && rule.selectedCount > 0 && (
              <span className="muted">
                还差：
                {rule.scenariosMissing.map((k) => scenarioMeta(k).titleZh).join('、')}
              </span>
            )}
          </div>
          <div className="bar-right">
            <button
              type="button"
              className="btn"
              disabled={rule.selectedCount === 0}
              onClick={() => setSelected(new Set())}
            >
              取消选择
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!rule.canSubmit}
              onClick={doSubmit}
            >
              提交审核
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
