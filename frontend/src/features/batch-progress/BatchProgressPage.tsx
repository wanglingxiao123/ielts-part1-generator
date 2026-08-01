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
 *
 * 4. **左侧历史批次面板。** 这一页现在是「面板 + 内容区」两栏：左边 260px 的历史列表
 *    （BatchHistoryPanel，可折叠），右边是当前选中批次的卡片。
 *
 *    它能存在是因为后端现在真的有批次记录了：web 层把每个批次随材料到达增量写进 S3
 *    （`web/batch_history.py`），`/api/batch-history` 读回来。在这之前批次只活在
 *    `api/agentcore.ts` 的一个 `Map` 里，刷新即失——所以「历史」过去不是没做，是没有数据。
 *
 *    **历史批次是只读的。** 客户的话：「已提交为只读视图——可看材料、可试听，但不能修改选稿」。
 *    只读与否由**后端**给（`read_only`），不在这里重新推导：已提交与候选过期两种只读的
 *    理由不同，前端再算一遍就多一个会算错的地方。落地成三件事：勾选框禁用、底栏换成一句说明、
 *    「提交审核」不出现。可读、可比、可试听（阅读页的「生成音频」）全都留着——试听走
 *    `preview_audio`，它按 id 直接读候选（`load` 不套 TTL，只有 `list_candidates` 套），
 *    所以一个上周的批次照样能听。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { userMessage } from '@/api/http'
import { scenarioMeta } from '@/config/scenarioMeta'
import { getThresholds } from '@/config/runtimeConfig'
import { describeBatchEstimate, estimateBatchSeconds } from '@/domain/batchEstimate'
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
import { buildFacts, compareCandidates } from '@/domain/compare'
import type { Thresholds } from '@/config/runtimeConfig'
import { UsabilityCompare } from '../compare/UsabilityCompare'
import { selectActivePhase, useBatchStore } from '@/stores/batchStore'
import { useReviewQueue } from '@/stores/reviewQueueStore'
import { BatchHistoryPanel } from './BatchHistoryPanel'
import { DistributionThumb } from './DistributionThumb'
import { useBatchStream } from './useBatchStream'
import { useHistoricalBatch } from './useHistoricalBatch'

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
  /**
   * 历史批次的只读视图（已提交，或候选已过保留期）。
   *
   * 只关掉**选稿**：勾选框 `disabled`。阅读全文、时间轴、对比点选一个都不动，因为客户的原话是
   * 「可看材料、可试听，但不能修改选稿」。用 `disabled` 而不是不渲染那个按钮：按钮消失会让人以为
   * 这张卡缺了什么，而一个明确禁用并带上原因的控件说的是「这里本来能点，只是现在不能」。
   */
  readOnly: boolean
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
  readOnly,
  onToggle,
}: CardProps) {
  const label = `第 ${preview.index + 1} 套`
  // 勾选状态在对比模式下**保留但不显示**：客户的原话是「之前的勾选状态保留但不可见，退出对比后
  // 恢复」。所以这里只是不加 `selected` 这个 class，state 一个字节都没动——退出对比后同一个
  // `selected` prop 原样回来。反过来（进对比就清空勾选）会让底栏的计数在用户没做任何选稿动作时
  // 归零，那是把「换个视角看」误当成「重新开始」。
  const className = [
    'mat-card',
    // 骨架被替换成真卡时淡入。CSS 动画，不是 JS 定时器：卡的到达时机由 SSE 决定。
    'fade-in',
    !compareMode && selected ? 'selected' : '',
    compareMode ? 'compare-pickable' : '',
    pickSide === 'a' ? 'pick-a' : '',
    pickSide === 'b' ? 'pick-b' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div
      className={className}
      data-material={preview.materialId}
      /* 对比模式下**整张卡**是点选区，不是右上角那个 20px 的圆点。
       *
       * 这是这次修复的核心：两种模式过去共用右上角同一个控件，用户在对比模式里点它，改的却是
       * 选稿——两件事抢同一个点击行为。现在勾选框只在选稿模式出现，对比模式下整张卡可点，
       * 「点第一张是 A、第二张是 B」这句话才在界面上成立。 */
      role={compareMode ? 'button' : undefined}
      tabIndex={compareMode ? 0 : undefined}
      aria-pressed={compareMode ? pickSide !== null : undefined}
      aria-label={
        compareMode
          ? `${label}：${pickSide ? `已选为材料 ${pickSide.toUpperCase()}` : '点选进入对比'}`
          : undefined
      }
      onClick={compareMode ? onToggle : undefined}
      onKeyDown={
        compareMode
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onToggle()
              }
            }
          : undefined
      }
    >
      <div className="mat-card-top">
        <span className="mat-card-label">{label}</span>
        <span className="row" style={{ gap: 8 }}>
          {/* 统一「待审核」。评价方的内部评级不出现在这里。 */}
          <span className="status-badge">待审核</span>
          {/* 勾选框只属于选稿模式。对比模式下它整个消失——留在那儿（哪怕换成 A/B 字样）就是把
              两个含义压在同一个控件上，而这正是用户报的冲突。对比时的 A/B 由卡片边框和角标说明。 */}
          {compareMode ? (
            pickSide && <span className={`pick-badge ${pickSide}`}>{pickSide.toUpperCase()}</span>
          ) : (
            <button
              type="button"
              className={`select-check${selected ? ' checked' : ''}`}
              disabled={readOnly}
              title={readOnly ? '历史批次是只读的，不能修改选稿' : undefined}
              aria-pressed={selected}
              aria-label={
                readOnly
                  ? `${label}：历史批次，不能修改选稿`
                  : `${label}：${selected ? '已选择' : '选择'}`
              }
              onClick={onToggle}
            >
              ✓
            </button>
          )}
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
        {/* 对比模式下整张卡是点选区，所以这个链接必须拦住冒泡：否则点「阅读全文」会在离开页面的
            同时顺手把这张卡选成 A 或 B，回来时发现点选状态自己变了。 */}
        <Link
          className="btn btn-card"
          to={`/materials/${preview.materialId}`}
          onClick={(e) => e.stopPropagation()}
        >
          <span aria-hidden="true">📖</span> 阅读全文
        </Link>
      </div>
    </div>
  )
}

/* ── 内联对比预览 ───────────────────────────────────────────────────────────── */

type CardBundle = {
  preview: CardPreview
  view: ViewMaterial
  metrics: DistributionMetrics
  groups: FormGroupAnalysis
}

/**
 * 选满两套后在**原地**展开的对比预览。
 *
 * 存在的理由是客户否掉了自动跳转：点第二张卡就把整页换掉，用户既没确认要走，也丢了刚才那一屏的
 * 上下文。这里先回答「这两套差在哪」——两列时间轴 + 那张可用性对照表，都是现成的确定性计算，
 * 不调模型也不发请求。要读全文再点「打开完整对比」，跳转从此是用户的动作。
 *
 * 表格用的是对比详情页那一个 `UsabilityCompare`，不是另写一份：同一个问题在两处给出不同答案，
 * 是这类「预览 + 详情」结构最容易出的错。
 */
function InlineCompare({
  scenarioKey,
  a,
  b,
  thresholds,
  onOpenFull,
}: {
  scenarioKey: string
  a: CardBundle | undefined
  b: CardBundle | undefined
  thresholds: Thresholds
  onOpenFull: () => void
}) {
  const comparison = useMemo(() => {
    if (!a || !b) return null
    return compareCandidates(
      buildFacts('材料 A', a.view, a.metrics, a.groups),
      buildFacts('材料 B', b.view, b.metrics, b.groups),
      thresholds,
    )
  }, [a, b, thresholds])

  if (!a || !b || !comparison) return null

  return (
    <div className="inline-compare" data-scenario={scenarioKey}>
      <div className="inline-compare-head">
        <strong>哪一套更好出题</strong>
        <span className="spacer" />
        <button type="button" className="btn btn-sm btn-compare" onClick={onOpenFull}>
          打开完整对比 →
        </button>
      </div>

      <div className="inline-compare-summary">{comparison.summary}</div>

      <div className="inline-compare-cols">
        {[
          { side: 'A', bundle: a },
          { side: 'B', bundle: b },
        ].map(({ side, bundle }) => (
          <div className={`inline-compare-col pick-${side.toLowerCase()}`} key={side}>
            <div className="inline-compare-col-head">
              <span className={`pick-badge ${side.toLowerCase()}`}>{side}</span>
              <span>第 {bundle.preview.index + 1} 套</span>
            </div>
            <DistributionThumb
              view={bundle.view}
              metrics={bundle.metrics}
              groups={bundle.groups}
              flagged={bundle.preview.flaggedPoints}
            />
          </div>
        ))}
      </div>

      <UsabilityCompare
        columns={[
          { label: '材料 A', metrics: a.metrics },
          { label: '材料 B', metrics: b.metrics },
        ]}
      />
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
  const thresholds = getThresholds()
  const [now, setNow] = useState(Date.now())
  const [retryBusy, setRetryBusy] = useState(false)
  const [snapshotError, setSnapshotError] = useState<string | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set<string>())
  /** 正在对比的场景 key；null = 不在对比模式。 */
  const [compareScenario, setCompareScenario] = useState<string | null>(null)
  const [pick, setPick] = useState<ComparePick>(EMPTY_PICK)
  /**
   * 让历史面板在批次跑完之后重取一次列表。
   *
   * 面板自己不知道批次什么时候结束，页面知道（它拿着 SSE 的终态），所以由这里递增。
   */
  const [historyToken, setHistoryToken] = useState(0)

  /**
   * 这一批是不是「当前活批次」。
   *
   * `store.batchId` 是本页会话里正在跑（或刚跑完）的那一批。URL 里的 batchId 与它不同，说明用户
   * 从历史面板点了另一批——那一批没有 SSE 可接、store 里也没有它的材料，只能从
   * `/api/batch-history/{id}` 取。这个判据是两条数据路径的唯一分岔点。
   */
  const isLiveBatch = Boolean(batchId) && store.batchId === batchId
  const historical = useHistoricalBatch(batchId, !isLiveBatch, historyToken)

  // Refresh / revisit: reattach to the in-flight batch (prd R3).
  //
  // The store is empty after a reload, so we deliberately resume from seq 0 and
  // let the contract's replay guarantee (§8.2: since_seq returns every later
  // event INCLUDING full material payloads) refill it. The persisted cursor is
  // for mid-session reconnects, where the store still holds the materials and
  // re-sending them would be wasted bandwidth.
  //
  // Skipped for a historical batch: there is no stream to attach to (it finished, possibly days
  // ago) and `getBatch` would 404 on a `sessions` Map that has never heard of it. Attempting it
  // anyway is how a perfectly good historical batch would render a red "无法加载本批次" banner.
  useEffect(() => {
    if (!batchId || !isLiveBatch || stream.isActive(batchId)) return
    const persisted = stream.resumePersisted()
    void (async () => {
      try {
        const snapshot = await api.getBatch(batchId)
        store.applySnapshot(snapshot)
        setSnapshotError(null)
        if (persisted?.batchId === batchId) store.setConnection('streaming')
      } catch (err) {
        // Surfaced, not just logged: against the real backend a reload genuinely
        // loses the LIVE stream (the job is bound to the POST), and a page that
        // sits at "已生成 0 / 0" with no explanation reads as a frontend bug.
        console.warn('[batch] snapshot failed', err)
        setSnapshotError(userMessage(err, '这一批的进度暂时读取不到，请稍后重试。'))
        return
      }
      stream.connect(batchId)
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId, isLiveBatch])

  // 批次跑完 → 让历史面板重取，这一批才会带着最终套数出现在列表里。
  useEffect(() => {
    if (store.status === 'done' || store.status === 'partial') {
      setHistoryToken((n) => n + 1)
    }
  }, [store.status])

  /**
   * 回到这个页签时重取历史批次。
   *
   * 撤回发生在审核队列页：那里改的是后端的 `submitted_*`，也就是这一页的 `read_only`。而
   * `useHistoricalBatch` 原来只依赖 `batchId`，切回来时 effect 不会再跑，用户看到的还是一个锁着
   * 的批次——checkbox 点不动、底栏写着「已提交审核，不能修改选稿」——只有手动刷新才恢复。
   *
   * 用 visibilitychange 而不是在撤回处跨页面推状态：两个页面之间没有共享的批次状态，而一个为此新增
   * 的全局 store 会是第二份 `read_only`，和后端那份迟早不一致。
   */
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') setHistoryToken((n) => n + 1)
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
  }, [])

  /**
   * 计时只在批次还在跑的时候走。
   *
   * 依赖数组原来是空的，所以定时器永不停：28/28 全部完成之后，「已用」还在一秒一秒往上加，看起来
   * 像是还有活没干完。终态由 `store.status` 决定（done / partial / error），到了就清掉定时器，
   * `now` 停在最后一次 tick，`elapsedMs` 随之冻结成这一批真正的耗时。
   */
  const running = store.status !== 'done' && store.status !== 'partial' && store.status !== 'failed'
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [running])

  /**
   * 冻结用 `finishedAt` 而不是最后一次 tick。差一秒是小事，要紧的是刷新之后：SSE 快照不带完成时间，
   * 恢复一个已完成的批次时 `finishedAt` 是 null，这时不显示计时，而不是拿此刻减 `createdAt` 算出
   * 「已用 3 天」。
   */
  const elapsedMs =
    store.createdAt && (running || store.finishedAt)
      ? (running ? now : store.finishedAt!) - store.createdAt
      : 0
  const elapsed = `${Math.floor(elapsedMs / 60_000)}:${String(
    Math.floor((elapsedMs % 60_000) / 1000),
  ).padStart(2, '0')}`
  const showElapsed = elapsedMs > 0
  /**
   * 「跑得比预估久」，而不是原来的「接近 15 分钟上限」。
   *
   * 那条横幅说的是一个已经不存在的约束：15 分钟同步硬限过去管的是整批，现在 web 层每套材料
   * 一次独立 invoke（`web/fanout.py`），它管的是单套。一个 20 套的批次跑 25 分钟完全正常，
   * 而旧文案会在第 12 分钟告诉用户「剩余材料可能来不及生成」——一句纯粹的假警报。
   *
   * 判据换成提交时那个预估区间的上界（`estimateBatchSeconds`，按并发算波数）加一成余量：
   * 超过它才说明这批确实比该跑的时间久，这句话才有信息量。
   */
  const estimateCeilingMs = useMemo(
    () => estimateBatchSeconds(store.total || 0)[1] * 1100,
    [store.total],
  )
  const runningLong = estimateCeilingMs > 0 && elapsedMs >= estimateCeilingMs
  const items = store.itemOrder.map((id) => store.items[id]).filter((i) => i !== undefined)
  const pending = items.filter((i) => i!.status !== 'done')

  /**
   * 只读。**后端给的**（`read_only`），不在这里按状态重算——见文件头。
   *
   * 活批次永远可写：它还在等这次选稿，这就是它存在的意义。
   */
  const readOnly = !isLiveBatch && (historical.batch?.readOnly ?? false)

  /**
   * 内容区的数据源：活批次用 store，历史批次用 `/api/batch-history/{id}`。
   *
   * 归一成 `buildResultGroups` 已经接受的两个字段（`requested` + `materials`），所以分组规则
   * （domain/resultSlots.ts）一份代码同时服务两条路径——历史视图和活视图的版式因此不可能长歪。
   * 历史批次没有 `items`：那是 SSE 事件填出来的，一个跑完的批次没有进行中的版位。
   */
  const source = useMemo(() => {
    if (isLiveBatch || !historical.batch) {
      return {
        requested: store.requested,
        items: store.itemOrder.map((id) => store.items[id]).filter((i) => i !== undefined),
        materials: store.materials,
        // 活批次跑完了才算完；历史批次按定义已经完了。
        finished: store.status === 'done' || store.status === 'partial',
        total: store.total,
      }
    }
    return {
      requested: historical.batch.requested,
      items: [],
      materials: historical.batch.materials,
      finished: true,
      total: historical.batch.requestedTotal,
    }
  }, [
    isLiveBatch,
    historical.batch,
    store.requested,
    store.itemOrder,
    store.items,
    store.materials,
    store.status,
    store.total,
  ])

  /**
   * 全部卡位（不只是已到达的），按场景分组、组内按第 N 套排序。
   *
   * 这是「没有等待页」的关键：卡位形状来自用户提交时选的每场景数量，所以在**任何
   * material 事件之前**就已经知道要铺几张骨架卡。规则本身在
   * domain/resultSlots.ts，纯函数、可单测。
   */
  const batchFinished = source.finished
  const groups = useMemo(
    () =>
      buildResultGroups({
        requested: source.requested,
        items: source.items,
        materials: source.materials,
        batchFinished,
      }),
    [source, batchFinished],
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
    for (const record of Object.values(source.materials)) {
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
  }, [source.materials, thresholds])

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
  const plannedTotal =
    source.total > 0 ? source.total : groups.reduce((n, g) => n + g.slots.length, 0)

  /**
   * 切换批次。
   *
   * 只改路由，不动 store：store 装的是**活批次**，把它清掉会让用户从历史看回当前批次时发现
   * 进度和已到达的卡都没了。`isLiveBatch` 那一行判据据此在两条数据路径间切换。
   *
   * 切换时顺手退出对比模式并清空勾选——勾选是「这一批里我选了哪几套」，带到另一批去就是一句谎话。
   */
  const switchBatch = (nextBatchId: string) => {
    if (nextBatchId === batchId) return
    setSelected(new Set())
    setCompareScenario(null)
    setPick(EMPTY_PICK)
    setSnapshotError(null)
    navigate(`/batches/${nextBatchId}`)
  }

  const doRetry = async () => {
    if (!batchId) return
    setRetryBusy(true)
    try {
      // Scenario keys, not material ids. Sending ids made the adapter resolve each one back to a
      // scenario through the in-memory `sessions` map, which is empty after a reload and for any
      // historical batch — so 补生成 threw 「没有可补生成的场景」 in exactly the situations where a
      // user most wants it. The page already knows each pending slot's scenario; there is nothing
      // to look up.
      const res = await api.retryBatch(batchId, {
        scenario_keys: pending
          .map((i) => i!.scenario_key)
          .filter((k): k is string => Boolean(k)),
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

  /**
   * 选满两张后**不跳转**，在原地展开内联预览。
   *
   * 这里原来是一个 `useEffect`，一凑满两张就 `navigate` 到 `/compare/...`。客户点名要去掉：
   * 点第二张卡的那一刻整页被换掉，用户既没确认过要走，也失去了刚才那一屏的上下文（其他场景、
   * 底栏计数、组头的 N/M）。改成先给一个内联预览（两列时间轴 + 关键指标），想看全文再点
   * 「打开完整对比」——跳转从此是用户的动作，不是页面的自作主张。
   */
  const comparePair = compareScenario && comparePairReady(pick) ? pick : null

  /**
   * 提交审核。
   *
   * 现在**同时**记到后端：`POST /api/batch-history/{id}/submit` 把这一批标成「已提交」，也就是历史
   * 面板那个蓝色状态。在这之前审核队列只在 localStorage 里，所以「已提交」是浏览器的私人意见，换
   * 台电脑就没了——那也是为什么这个状态过去不能出现在面板上（见 web/batch_history.py）。
   *
   * localStorage 的队列**保留**：队列页显示的是每条材料的摘要，那份数据后端没有。两者记的是同一件
   * 事的两个层面（哪一批 / 哪几套），不是重复。
   *
   * 后端记不上不阻塞跳转：队列已经写好了，为了一个状态标签把用户卡在这一页是不值得的。失败只会让
   * 面板上少一个「已提交」，下次提交会补上。
   */
  const doSubmit = () => {
    const at = Date.now()
    const materialIds = [...selected]
    submitToQueue(
      materialIds.flatMap((materialId) => {
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
    if (batchId) {
      void api
        .submitBatch(batchId, materialIds)
        .then(() => setHistoryToken((n) => n + 1))
        .catch((err) => console.warn('[batch] 记录已提交状态失败', err))
    }
    setSelected(new Set())
    navigate('/review-queue')
  }

  return (
    <div className="results-shell">
      {/* 左侧固定宽度面板 + 右侧内容区。面板在**外层**而不是 .results 内部，因为它要贴着页面
          左边、和内容区一起滚动之外各自滚动。 */}
      <BatchHistoryPanel
        activeBatchId={batchId}
        onSelect={switchBatch}
        reloadToken={historyToken}
      />
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
          {/* 跑完之后它描述的是一件已经结束的事，不是还在走的秒表，所以措辞跟着状态换。 */}
          {showElapsed && (
            <span className="muted">
              {running ? '已用' : '耗时'} {elapsed}
            </span>
          )}
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

      {historical.error && (
        <div className="banner banner-bad">
          <strong>无法加载这个历史批次</strong>
          <div>{historical.error}</div>
        </div>
      )}

      {/* 只读视图的说明。写成一句「为什么」而不只是「只读」：一个不说理由的禁用状态会被当成故障。
          同时点明还能做什么——看、听、对比——因为客户要的正是这三样留着。 */}
      {readOnly && historical.batch && (
        <div className="banner banner-info">
          <strong>
            {historical.batch.status === 'submitted'
              ? '这一批已经提交过审核'
              : '这一批不能再选稿了'}
            ，是只读的
          </strong>
          <div>
            {historical.batch.status === 'submitted'
              ? '选稿已经做过。想重新选，到「审核队列」把这一批撤回。'
              : '候选材料的保留期已过（30 天），所以不能再改选稿。'}
            材料照常可以阅读、并排对比；到阅读页还可以生成音频试听。
          </div>
        </div>
      )}

      {/* 任务中断过的历史批次。缺的套**不会**再补，所以不给「补生成」入口——那是一句做不到的承诺；
          要补就是重新提一批。 */}
      {historical.batch?.interrupted && (
        <div className="banner banner-warn">
          <strong>这一批的生成任务中途中断了</strong>
          <div>
            已到达的 {historical.batch.materials ? Object.keys(historical.batch.materials).length : 0}{' '}
            套是完整的、可以照常使用；缺的部分不会再补齐。需要补的话请重新提交一批。
          </div>
        </div>
      )}

      {runningLong && store.status === 'running' && (
        <div className="banner banner-warn">
          <strong>比预估慢</strong>
          <div>
            已用 {elapsed}，超过本批 {describeBatchEstimate(store.total || 0)}的预估。
            仍在生成中——每套材料是一次独立请求，慢的那几套不会影响已经完成的。
          </div>
        </div>
      )}

      {/* 连接状态只对活批次有意义：历史批次没有流可断。 */}
      {isLiveBatch && <ConnectionBanner onRetry={stream.retryNow} />}

      {/* 「有几套没生成出来」是结果，不是环节：这里只说数量和补生成的入口，
          不再逐套播报它卡在哪个内部环节、试了几次。 */}
      {isLiveBatch && store.status === 'partial' && pending.length > 0 && (
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

      {/* 历史批次取回来之前不铺骨架卡：那会先闪一屏「生成中…」再换成真卡，而这一批几天前就跑完了。 */}
      {historical.loading && (
        <div className="panel panel-pad muted">正在读取这个历史批次…</div>
      )}

      {/* 唯一还剩的空态：整批跑完了却一套都没出来。「正在生成，第一套完成后会出现
          在这里」那句话没有了——现在页面从第一秒就是结果页的结构本身。 */}
      {completed === 0 && finished && !snapshotError && !historical.loading && !historical.error && (
        <div className="panel panel-pad muted">本批次没有生成出材料。</div>
      )}

      {!historical.loading && groups.map((group) => {
        // 自定义场景的标题用**用户输入的原文**，由后端随批次记录一起给回来（`custom_label`）。
        //
        // 一度用的是材料自带的 `material.scenario`，那是错的：模型会把「餐厅点餐」扩写成一整句
        // "A customer phones a restaurant to book a family dinner..."，于是标题变成模型的改写而
        // 不是用户的话，还长得撑破侧栏。目录场景不需要它（有中文名），所以只在这里用。
        // 实时批次还没有历史记录可查（`useHistoricalBatch` 对活批次是关闭的），所以原文走 SSE 的
        // `batch_started` 帧进 store。两条路都拿不到时才退回材料自带的场景句。
        const customLabel =
          (isLiveBatch ? store.customLabel : '') ||
          historical.batch?.customLabel ||
          group.slots
            .map((slot) =>
              slot.materialId ? cards.get(slot.materialId)?.preview.scenarioText : null,
            )
            .find((text): text is string => Boolean(text && text.trim())) ||
          undefined
        const meta = scenarioMeta(group.scenarioKey, customLabel)
        const comparing = compareScenario === group.scenarioKey
        // 对比模式下**别的场景整块置灰不可操作**：对比是「在一个场景内部挑两套」，此时点另一个
        // 场景的卡没有任何合法含义。不置灰的话它们看着照旧可点，点下去却什么都不发生（或者更糟，
        // 改了选稿）——那就是用户报的那种「操作冲突」。
        const dimmed = compareScenario !== null && !comparing
        return (
          <section
            className={`scn-group${comparing ? ' comparing' : ''}${dimmed ? ' dimmed' : ''}`}
            key={group.scenarioKey}
            aria-hidden={dimmed || undefined}
          >
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
              {/* 对比要两张**真卡**：拿一张骨架去比没有意义。
                  置灰的场景不给按钮：同时进两个场景的对比模式没有意义。 */}
              {group.arrived >= 2 && !dimmed && (
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
                    readOnly={readOnly}
                    onToggle={() => {
                      // 置灰的场景不接受任何操作。CSS 的 `pointer-events: none` 已经拦了一层，
                      // 这里再拦一次：键盘和辅助技术不受 pointer-events 约束，而「其他场景不可
                      // 操作」是一条行为规则，不该只由样式来保证。
                      if (dimmed) return
                      if (comparing) {
                        setPick((prev) => pickForCompare(prev, materialId))
                        return
                      }
                      // 第二道闸，**在正常路径上不可达**（按钮已经 disabled）——这一行防的是
                      // 「以后有人把 disabled 那句改坏」。不变式属于持有状态的这一层，只靠一个
                      // 渲染属性来保护它，等于把它托付给样式。
                      //
                      // 也因此它没有对应的测试：任何能触发它的输入都需要先绕过 disabled，而那种
                      // 输入只能靠直接调 `onToggle` 造出来，测的就变成测试自己的调用而不是页面。
                      if (readOnly) return
                      setSelected((prev) => toggleSelection(prev, materialId))
                    }}
                  />
                )
              })}
            </div>

            {/* 选满两张后的内联预览。在卡片**下方**原地展开，而不是跳走整页——见 `comparePair`
                那段注释。想看全文再点里面的「打开完整对比」。 */}
            {comparing && comparePair && (
              <InlineCompare
                scenarioKey={group.scenarioKey}
                a={cards.get(comparePair[0])}
                b={cards.get(comparePair[1])}
                thresholds={thresholds}
                onOpenFull={() =>
                  navigate(
                    // `batch` 是必须带的：对比页对**历史**批次没有别的取材料的路。它优先读
                    // batchStore，而 store 只装当前活批次；一个跑完又刷新过的批次在 store 里
                    // 是空的，于是那一页整屏「本场景暂无材料」。旧的退路是
                    // `GET /materials?scenario_key=`，而真实后端没有这条路由（web 层只有
                    // batch-history），所以那条退路从来没生效过。
                    `/compare/${group.scenarioKey}?a=${comparePair[0]}&b=${comparePair[1]}&batch=${batchId ?? ''}`,
                  )
                }
              />
            )}
          </section>
        )
      })}

      {/* 对比模式的底栏。整条**换掉**选稿底栏，而不是把「提交审核」置灰：这两个模式互斥，而底栏
          是这件事最显眼的地方。留着「已选 N 套 · 提交审核」会让人以为在对比模式里还能提交，
          而此时卡片上连勾选框都没有——界面自己跟自己矛盾。
          勾选数一个字节都没动，退出对比后原样回来，所以这里说清「保留着」。 */}
      {groups.length > 0 && !readOnly && compareScenario !== null && (
        <div className="results-bar comparing">
          <div className="bar-left">
            <span className="legend-dot a" aria-hidden="true" />
            <span>
              对比模式：点第一张是材料 A，第二张是材料 B
              {comparePairReady(pick) ? '。下方已展开对比' : ''}
            </span>
            {rule.selectedCount > 0 && (
              <span className="muted">已勾选的 {rule.selectedCount} 套保留着，退出后恢复</span>
            )}
          </div>
          <div className="bar-right">
            <button type="button" className="btn btn-primary" onClick={leaveCompare}>
              退出对比
            </button>
          </div>
        </div>
      )}

      {/* 底栏和卡位一起在位。「提交审核」在第一张真卡到达之前是禁用的——按钮跳着
          出现会让人以为功能刚刚才有。 */}
      {groups.length > 0 && !readOnly && compareScenario === null && (
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

      {/* 只读批次的底栏。整条**换掉**而不是把「提交审核」置灰：一个永远点不了的提交按钮会让人一直
          找它为什么不能点。这里说的是这一批处在什么状态，以及已提交的那几套是哪几套。 */}
      {groups.length > 0 && readOnly && historical.batch && (
        <div className="results-bar readonly">
          <div className="bar-left">
            <span aria-hidden="true">🔒</span>
            <span>
              {historical.batch.status === 'submitted' ? '已提交审核' : '候选已过期'}
              ，不能修改选稿
            </span>
            {historical.batch.submittedMaterialIds.length > 0 && (
              <span className="muted">
                当时提交了 {historical.batch.submittedMaterialIds.length} 套
              </span>
            )}
          </div>
          <div className="bar-right">
            <Link className="btn" to="/review-queue">
              查看审核队列
            </Link>
          </div>
        </div>
      )}
      </div>
    </div>
  )
}
