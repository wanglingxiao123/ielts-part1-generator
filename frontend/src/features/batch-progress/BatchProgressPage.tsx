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
 *    「待审核」、圆形勾选框、信息点时间轴缩略图、预览两行、一个「阅读全文」。
 *    评价方的内部评级（PASS / MINOR_EDITS / FAIL）不做徽章——客户明确要求统一
 *    「待审核」。缺陷通过黄点和缺陷小结说出来，材料照样可选。
 *
 * 3. **没有等待页。** 提交后直接进这一页，并在**第一个 material 事件之前**就把
 *    全部卡位铺成灰色骨架卡（场景名 + 生成中… + shimmer）。每套材料到达时把它
 *    自己那张骨架**替换**成真卡并淡入。卡位形状来自用户提交时选的每场景数量
 *    （store 的 `requested`，刷新后退回按快照 items 反推）——见
 *    domain/resultSlots.ts。客户明确否掉的三样：单独的加载页、白屏、等整批跑完
 *    才显示。已到的卡必须立刻可读。
 *
 *    骨架**不区分**「排队中 / 生成中」：后端对某个版位的重试（`refilling`）是静默的，
 *    而客户要的就是用户察觉不到重试，所以未到达的卡位一律是同一个「生成中…」。
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
import { buildCardPreview, type CardPreview } from '@/domain/cardPreview'
import { computeDistribution, type DistributionMetrics } from '@/domain/distribution'
import { analyseFormGroups, type FormGroupAnalysis } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { arrivedByScenario, buildResultGroups, type ResultSlot } from '@/domain/resultSlots'
import type { ViewMaterial } from '@/domain/types'
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
import { DistributionThumb } from './DistributionThumb'
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

/* ── 骨架卡 / 异常卡 ────────────────────────────────────────────────────────── */

/**
 * 未到达的卡位。提交后立刻铺满，每套材料到达时被真卡**替换**（key 相同）。
 *
 * 只有一句「生成中…」，不分「排队中 / 生成中 / 修改中」：后端对判不了的版位是静默
 * 重跑的（`refilling`），客户要的就是用户察觉不到重试，所以卡位状态越少越诚实。
 * 想知道整批到哪一段的，看顶部那一条四段进度。
 */
function SkeletonCard({ scenarioTitle, label }: { scenarioTitle: string; label: string }) {
  return (
    <div className="mat-card skel-card" aria-busy="true" aria-label={`${scenarioTitle} ${label} 生成中`}>
      <div className="mat-card-top">
        <span className="mat-card-label">{label}</span>
        <span className="status-badge skel-badge">生成中…</span>
      </div>
      <div className="skel-scn">{scenarioTitle}</div>
      <div className="skel-axis" />
      <div className="skel-line" />
      <div className="skel-line short" />
    </div>
  )
}

/*
 * 这里原来有一个 `ErrorCard`——红色的「生成异常」空卡片。它被整个删掉了，不是改文案。
 *
 * 客户的两句话决定了这件事：
 *
 *   「前端不再渲染任何『生成异常』空卡片。只要模型返回了文本就必须展示」
 *   「如果是 API 调用本身失败（网络超时等真正没内容的情况），后台静默补跑，补不上就
 *     少返回一套，不放空卡片」
 *
 * 它原本几乎全部是被**校验**触发的：三次校验都没过 → `validation_exhausted` → 材料被吞
 * → 这张空卡。而模型每一次都正常返回了完整脚本。现在校验是质检报告不是门卫，材料照样
 * 交付（校验意见挂在阅读页上），这条路径不存在了。
 *
 * 剩下的唯一情况是真的没有内容（模型调用失败、slot 崩了）。那种情况后端现在会静默补跑
 * （batch.py 的 `REFILLABLE_FAILURES`），补不上就是这个场景**少一套**——组头上的
 * 「2/3」如实说出了这件事，页面顶部还有一条整批的「补生成这 N 套」。少一张卡是诚实的，
 * 一张写着「生成异常」的空卡只是把我们自己的故障当成内容摆给用户看。
 */

/* ── 一张材料卡 ─────────────────────────────────────────────────────────────── */

interface CardProps {
  preview: CardPreview
  view: ViewMaterial
  metrics: DistributionMetrics
  groups: FormGroupAnalysis
  selected: boolean
  compareMode: boolean
  /** 'a' | 'b' | null —— 对比模式下这张卡是 A 还是 B。 */
  pickSide: 'a' | 'b' | null
  onToggle: () => void
}

function MaterialCard({
  preview,
  view,
  metrics,
  groups,
  selected,
  compareMode,
  pickSide,
  onToggle,
}: CardProps) {
  const label = `第 ${preview.index + 1} 套`
  // A selected card keeps looking selected inside compare mode. Hiding it there
  // reads as "entering compare mode threw my选择 away" — it does not, and the
  // bottom bar's count would then contradict the cards.
  const className = [
    'mat-card',
    // 骨架被替换成真卡时淡入。CSS 动画，不是 JS 定时器：卡的到达时机由 SSE 决定。
    'fade-in',
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

      {/* 十个编号圆点换成时间轴缩略图：圆点只说明「有十个点」——每套都成立，等于
          什么也没说。缩略图说的是十个点落在对话的哪里。 */}
      <DistributionThumb
        view={view}
        metrics={metrics}
        groups={groups}
        flagged={preview.flaggedPoints}
      />

      <div className="mat-preview">
        {preview.firstLine && <q>{preview.firstLine}</q>}
        <span>— {preview.summary}</span>
      </div>

      {/* 这里原来有一段缺陷小结。客户明确否掉了：「结果页卡片上只展示：场景名 + 信息点
          时间轴图 + 预览第一句话 + 操作按钮。不展示任何评价文字。……阅读全文页面里可以
          展示评价建议（如『⑤⑥之间空了 6 轮，可考虑补细节或压缩闲聊』），因为用户在看
          全文时才有上下文理解这个建议的含义。」
          文案本身没有删，它在 domain/usability.ts，由阅读页的 DistributionStrip 渲染。
          上面那张时间轴留着：黄点是「先看这一段」的指路，不是一句评价——客户点名表扬过
          这张图。 */}

      {/* 原型里这个按钮是「图标 + 一个词」的行内胶囊（docs/ui/progressive-loading-prototype.html
          的 .card-actions .btn），不是一段带边框的文字链。文案仍是「阅读全文」——原型写「阅读」，
          但页面上另有「对比本场景」这类动作，说清读的是全文才不会被当成展开摘要。 */}
      <div className="mat-actions">
        <Link className="btn btn-card" to={`/materials/${preview.materialId}`}>
          <span aria-hidden="true">📖</span> 阅读全文
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

  /**
   * 全部卡位（不只是已到达的），按场景分组、组内按第 N 套排序。
   *
   * 这是「没有等待页」的关键：卡位形状来自用户提交时选的每场景数量，所以在**任何
   * material 事件之前**就已经知道要铺几张骨架卡。规则本身在
   * domain/resultSlots.ts，纯函数、可单测。
   */
  const batchFinished = store.status === 'done' || store.status === 'partial'
  const groups = useMemo(
    () =>
      buildResultGroups({
        requested: store.requested,
        items: store.itemOrder.map((id) => store.items[id]).filter((i) => i !== undefined),
        materials: store.materials,
        batchFinished,
      }),
    [store.requested, store.itemOrder, store.items, store.materials, batchFinished],
  )

  /**
   * 每张真卡要画的东西：预览 + 分布指标 + form_group 分析。
   *
   * join、分布、分组计算都不便宜，而一批最多 6 套却会随每次勾选重渲染，所以按材料
   * 集合缓存——勾选、进对比模式都不会重算。分布指标只算一次并同时喂给缺陷小结和
   * 时间轴缩略图，两处因此不可能不一致。
   */
  const cards = useMemo(() => {
    const out = new Map<
      string,
      { preview: CardPreview; view: ViewMaterial; metrics: DistributionMetrics; groups: FormGroupAnalysis }
    >()
    for (const record of Object.values(store.materials)) {
      const view = joinFromRecord(record)
      const metrics = computeDistribution(view, thresholds)
      out.set(record.material_id, {
        preview: buildCardPreview(record, view, metrics),
        view,
        metrics,
        groups: analyseFormGroups(view, thresholds),
      })
    }
    return out
  }, [store.materials, thresholds])

  const idsByScenario = useMemo(() => arrivedByScenario(groups), [groups])

  const rule = useMemo(
    () => evaluateSelection({ byScenario: idsByScenario, selected }),
    [idsByScenario, selected],
  )

  const completed = groups.reduce((n, g) => n + g.arrived, 0)
  const finished = batchFinished
  const scenarioCount = groups.length
  const perScenario = groups[0]?.slots.length ?? 0
  /** 计划总套数。骨架期 store.total 已经有值，所以进度条一开始就说得出分母。 */
  const plannedTotal = store.total > 0 ? store.total : groups.reduce((n, g) => n + g.slots.length, 0)

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
        const preview = cards.get(materialId)?.preview
        if (!preview) return []
        return [
          {
            materialId,
            batchId: batchId ?? '',
            scenarioKey: preview.scenarioKey,
            index: preview.index,
            submittedAt: at,
            summary: preview.summary,
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
            style={{ width: `${plannedTotal > 0 ? (completed / plannedTotal) * 100 : 0}%` }}
          />
        </div>
        <div className="results-stats">
          <span>
            {scenarioCount > 0 ? `${scenarioCount} 场景 × ${perScenario} 套 = ` : ''}
            {plannedTotal} 套材料
          </span>
          {/* 「已完成 M/N」。骨架期 N 已经是计划总数，所以进度条一开始就有分母。
              这是这一行里唯一的 M/N——describeProgress 不再重复它（见 progressStages.ts）。 */}
          <span className="progress-count">
            已完成 {completed}/{plannedTotal}
          </span>
          {/* 「全部完成」只在真的一套不缺时出现——旁边还有红色「生成异常」卡片却
              打一个绿勾，读起来就是页面在骗人。而它出现时那句「全部生成完毕」就是同一件事说两遍，
              所以两者互斥：跑完且跑齐用绿勾，其余情况用那句话 + 四段进度。 */}
          {finished && completed >= plannedTotal ? (
            <span className="done-badge">✓ 全部完成</span>
          ) : (
            <>
              <span>
                {describeProgress({ completed, total: plannedTotal, phase: activePhase, finished })}
              </span>
              {!finished && <PhaseTrack phase={activePhase} finished={false} />}
            </>
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

      {/* 唯一还剩的空态：整批跑完了却一套都没出来。「正在生成，第一套完成后会出现
          在这里」那句话没有了——现在页面从第一秒就是结果页的结构本身。 */}
      {completed === 0 && finished && !snapshotError && (
        <div className="panel panel-pad muted">本批次没有生成出材料。</div>
      )}

      {groups.map((group) => {
        const meta = scenarioMeta(group.scenarioKey)
        const comparing = compareScenario === group.scenarioKey
        return (
          <section className="scn-group" key={group.scenarioKey}>
            <div className="scn-group-head">
              <span className="scn-group-icon" aria-hidden="true">
                {meta.icon}
              </span>
              <span className="scn-group-title">{meta.titleZh}</span>
              <span className="scn-group-tag">{meta.categoryZh}</span>
              {/* 「2/3」就是「少返回一套」这件事本身的说法。空卡片删掉之后，这个计数是
                  用户唯一需要知道的：这个场景要了 3 套、到了 2 套。 */}
              <span className="scn-group-count">
                {group.arrived}/{group.slots.length}
              </span>
              <span className="spacer" />
              {/* 对比要两张**真卡**：拿一张骨架去比没有意义。 */}
              {group.arrived >= 2 && (
                <button
                  type="button"
                  className={`btn btn-sm${comparing ? '' : ' btn-compare'}`}
                  onClick={() => (comparing ? leaveCompare() : enterCompare(group.scenarioKey))}
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
              {group.slots.map((slot: ResultSlot) => {
                const label = `第 ${slot.index + 1} 套`
                // key 是 (场景, 第 N 套)，骨架期和真卡期同一个值：React 因此把骨架
                // **替换**成真卡，而不是在旁边多挂一张。
                //
                // 没有出来的版位**不渲染**。后端已经静默补跑过了（batch.py 的
                // `REFILLABLE_FAILURES`），到这一步还是空的就是这个场景少一套——组头的
                // 「N/M」已经说了，顶部还有整批的补生成入口。渲染一张空卡等于把我们自己的
                // 故障当成一份材料摆出来，客户点名不要。
                if (slot.state === 'error') return null
                const card = slot.materialId ? cards.get(slot.materialId) : undefined
                if (!card || !slot.materialId) {
                  return <SkeletonCard key={slot.key} scenarioTitle={meta.titleZh} label={label} />
                }
                const materialId = slot.materialId
                const pickSide =
                  comparing && pick[0] === materialId
                    ? 'a'
                    : comparing && pick[1] === materialId
                      ? 'b'
                      : null
                return (
                  <MaterialCard
                    key={slot.key}
                    preview={card.preview}
                    view={card.view}
                    metrics={card.metrics}
                    groups={card.groups}
                    selected={selected.has(materialId)}
                    compareMode={comparing}
                    pickSide={pickSide}
                    onToggle={() =>
                      comparing
                        ? setPick((prev) => pickForCompare(prev, materialId))
                        : setSelected((prev) => toggleSelection(prev, materialId))
                    }
                  />
                )
              })}
            </div>
          </section>
        )
      })}

      {/* 底栏和卡位一起在位。「提交审核」在第一张真卡到达之前是禁用的——按钮跳着
          出现会让人以为功能刚刚才有。 */}
      {groups.length > 0 && (
        <div className="results-bar">
          <div className="bar-left">
            <span>
              已选择 <span className="count">{rule.selectedCount}</span> 套材料
            </span>
            <span className="muted">
              {completed === 0 ? '（等第一套到达后即可勾选）' : '（每场景至少选 1 套）'}
            </span>
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
