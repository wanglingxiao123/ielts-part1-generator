/**
 * 并排对比两套材料。
 *
 * ## 这一页只讲规范维度
 *
 * 客户的原话决定了这个文件的形状：
 *
 *   「当前对比页展示了大量出题人不关心的内部评价指标（100 分、听不出来、计划外细节、出题就绪度）。
 *     出题人对比两套材料时只关心规范（§2-§4、§6）里定义的维度。单篇阅读详情页已有这些模块的数据
 *     和组件，对比视图直接复用并排展示即可。」
 *
 * 所以每侧是**单篇页那几个现成模块**的并排：话题简述、信息点时间轴（含前后切分线）、信息点类型
 * 列表、干扰机制标签、篇幅、前后两组分配、能否成表格题。顶部一张全宽的对比摘要。
 *
 * 删掉的东西（都是评价方的内部指标，客户点名不要）：
 *
 *   - `DecisionBar`（总分 100 分、分差、「听不出来的点」、「计划外的可考细节」、「出题就绪度」）
 *   - 「评分差别明显的方面」那组横条
 *   - 「需要看一眼的地方」——评价方 finding 的跳转按钮
 *   - `UsabilityCompare`（就绪度四行对照）
 *   - 「选定 A / 选定 B」按钮和底部操作栏
 *
 * ## 这一页不承载选稿
 *
 * 「选定操作放在主结果页的 checkbox，对比页不承载选稿功能」。所以 `SelectDialog`、`selectMaterial`、
 * 「已选定，语音合成已触发」那条横幅全部移除——一并移除了那次 `getAudio` 查询（它只为确认框的
 * 文案服务）。这一页现在是纯读的：不发任何写请求，不花钱。
 *
 * ## 摘要为什么不用 `domain/compare.ts`
 *
 * `compareCandidates` 算的是分数差和倾向，正是要去掉的那类指标。摘要另起一份
 * （`domain/compareFacts.ts` 的 `compareSummary`），判据只用规范维度，措辞里不出现分数、不出现
 * 「倾向 A」这种判决口气——客户要的是「说清差在哪，我自己选」。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { scenarioMeta } from '@/config/scenarioMeta'
import { getThresholds } from '@/config/runtimeConfig'
import type { MaterialRecord } from '@/contracts/api'
import { previewSummary } from '@/domain/cardPreview'
import { compareSummary, distractionCounts, lengthFacts } from '@/domain/compareFacts'
import { computeDistribution } from '@/domain/distribution'
import { summariseExamPoints } from '@/domain/examPoints'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { groupKeyOf } from '@/domain/resultSlots'
import { useBatchStore } from '@/stores/batchStore'
import { useHistoricalBatch } from '../batch-progress/useHistoricalBatch'
import { ExamPointPanel } from '../material-reader/ExamPointPanel'
import { MaterialReader } from '../material-reader/MaterialReader'
import { QuestionTypePanel } from '../material-reader/QuestionTypePanel'

const LABELS = ['材料 A', '材料 B', '材料 C', '材料 D']

export function ComparePage() {
  const { scenarioKey } = useParams<{ scenarioKey: string }>()
  /**
   * 匹配材料时要归一场景 key。
   *
   * 结果页按**归一后**的 key 分组（自定义场景是 `custom`），跳过来的 URL 里就是 `/compare/custom`；
   * 而材料自己带的是后端给的 `custom-<sha1(文本)[:8]>`。直接字面比较就永远匹配不上——自定义场景点
   * 「打开完整对比」进来是整屏「本场景暂无材料」，而目录场景全都正常，所以看起来像「有时候不显示」。
   * 复用 `groupKeyOf`，不在这里抄第二份规则：抄了就会和结果页的分组漂移。
   */
  const groupKey = groupKeyOf(scenarioKey ?? '')
  // ?a=&b= is how the results page hands over the two cards the user point-
  // selected. Absent (a direct link, a bookmark) it falls back to the first two.
  const [search] = useSearchParams()
  const thresholds = getThresholds()

  // Subscribe to the two stable references and derive with useMemo. A selector
  // that builds a new array on every call fails Zustand's snapshot caching and
  // loops forever ("The result of getSnapshot should be cached").
  const itemOrder = useBatchStore((s) => s.itemOrder)
  const materials = useBatchStore((s) => s.materials)
  const storeBatchId = useBatchStore((s) => s.batchId)
  const fromStore = useMemo(
    () =>
      itemOrder
        .map((id) => materials[id])
        .filter(
          (m): m is MaterialRecord =>
            m !== undefined && groupKeyOf(m.scenario_key) === groupKey,
        ),
    [itemOrder, materials, groupKey],
  )
  const [records, setRecords] = useState<MaterialRecord[]>(fromStore)
  const [syncScroll, setSyncScroll] = useState(true)
  /** 旁注默认折叠：并排两栏本来就窄，旁注一展开会把原文挤成细长条。 */
  const [showAnnotations, setShowAnnotations] = useState(false)
  const [jump, setJump] = useState<Record<string, { turnIndex: number; nonce: number }>>({})

  /**
   * 材料从哪来：先看 batchStore，没有就取 `?batch=` 那一批的历史记录。
   *
   * 这里原来的退路是 `GET /materials?scenario_key=`——而真实后端**没有这条路由**（web 层只有
   * `/api/batch-history`，见 `api/agentcore.ts` 顶部那张对照表）。store 只装当前活批次，所以
   * 任何跑完又刷新过的批次点进这一页都是整屏「本场景暂无材料」，从结果页点「打开完整对比」
   * 过来也一样——结果页的历史批次数据本来就不在 store 里。这条路径在真实部署上是 100% 坏的，
   * 而所有测试都用 mock 答了那个不存在的接口，所以没人看见。
   *
   * 归一交给 `useHistoricalBatch`：那份记录的 `materials` 是宽松的 Partial 形状（sidecar 可能
   * 只有摘要没有构件），把它变成 `MaterialRecord` 有若干条判断，抄第二份就会和结果页漂移。
   */
  const historyBatchId = search.get('batch')
  const historical = useHistoricalBatch(historyBatchId ?? undefined, fromStore.length === 0)
  useEffect(() => {
    if (fromStore.length > 0) {
      setRecords(fromStore)
      return
    }
    if (!historical.batch) {
      setRecords([])
      return
    }
    setRecords(
      historical.batch.materialOrder
        .map((id) => historical.batch!.materials[id]!)
        .filter((m) => groupKeyOf(m.scenario_key) === groupKey),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupKey, historical.batch, fromStore.length])

  // Every material is comparable now, audit-rejected ones included: the client's
  // rule is that a flawed material is shown with its shortcomings stated, and
  // hiding it here would silently remove the very comparison a reviewer wants
  // ("is the flawed one still better than the alternative?").
  const candidates = records

  const views = useMemo(() => candidates.map(joinFromRecord), [candidates])

  const [pair, setPair] = useState<[number, number]>([0, 1])

  // Honour ?a=&b= once the records are in hand. Runs on id, not on index, so a
  // late-arriving material cannot shift the pair out from under the user.
  useEffect(() => {
    const a = candidates.findIndex((c) => c.material_id === search.get('a'))
    const b = candidates.findIndex((c) => c.material_id === search.get('b'))
    if (a >= 0 && b >= 0 && a !== b) setPair([a, b])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [records, search])

  /**
   * 「材料 A / B」这个名字按**当前摆放位置**给，不按 `records` 里的下标。
   *
   * 原来是按下标。那样一来名字取决于材料到达/被记录的顺序，而摆放取决于 `pair`，两者无关：用户在
   * 结果页点第一张卡（界面标 A）进来，左栏摆的确实是那一套，标题却写着「候选 B」——只要历史记录的
   * 顺序和他点的顺序相反就会这样，而那是一半的概率。名字是给人指位置用的，所以它必须跟着位置。
   */
  const labelOf = useMemo(() => {
    const map = new Map<number, string>()
    pair.forEach((idx, side) => map.set(idx, LABELS[side] ?? `材料 ${side + 1}`))
    let next = pair.length
    candidates.forEach((_, idx) => {
      if (!map.has(idx)) map.set(idx, LABELS[next++] ?? `材料 ${next}`)
    })
    return map
  }, [candidates, pair])

  /** 每侧要画的东西，一次算好：时间轴、题组、考点、干扰、篇幅。 */
  const sides = useMemo(
    () =>
      views.map((view, i) => ({
        view,
        label: labelOf.get(i) ?? `材料 ${i + 1}`,
        metrics: computeDistribution(view, thresholds),
        groups: analyseFormGroups(view, thresholds),
        examPoints: summariseExamPoints(view),
        distractions: distractionCounts(view),
        length: lengthFacts(view),
        topic: previewSummary(view),
      })),
    [views, labelOf, thresholds],
  )

  const showPair = sides.length >= 2 && pair.every((i) => sides[i] !== undefined)

  /** 顶部那张摘要。只有两套并排时才有意义。 */
  const summary = useMemo(() => {
    if (!showPair) return null
    const left = sides[pair[0]]!
    const right = sides[pair[1]]!
    return compareSummary(left, right)
  }, [showPair, sides, pair])

  // 优先用材料自己带的 batch_id：从历史批次点进来时 store 装的是当前活批次，用它会把用户送回
  // 另一批。一套材料都没有时（下面那个分支）只剩 store 可用，而那一屏最需要一个出路。
  const backBatchId = records.find((r) => r.batch_id)?.batch_id ?? historyBatchId ?? storeBatchId

  /**
   * 还在取历史记录 ≠ 没有材料。
   *
   * 这两个状态过去共用「本场景暂无材料。」那一屏：取记录要一两秒（一次 S3 GET 加 N 个 sidecar），
   * 那段时间页面在说一句**当时还不知道真假**的话，然后内容才冒出来。用户看到的是先被告知没有、
   * 再被推翻。加载中就说加载中。
   */
  if (records.length === 0 && historical.loading) {
    return (
      <div className="page">
        <div className="panel panel-pad">正在读取这一批的材料…</div>
      </div>
    )
  }

  if (records.length === 0) {
    return (
      <div className="page">
        <div className="panel panel-pad">
          {/* 读取失败和「这一批确实没有这个场景」要说成两句话：前者可以重试，后者重试没用。 */}
          <div>{historical.error ?? '本场景暂无材料。'}</div>
          {backBatchId && (
            <Link className="btn btn-sm" to={`/batches/${backBatchId}`} style={{ marginTop: 10 }}>
              ← 返回批次
            </Link>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="page-wide">
      <div className="row cmp-head">
        {/* 返回批次。这一页过去是**单向**的：用户从结果页点进来，页面上没有任何回去的入口，
            只剩浏览器的后退键——而这一页是全宽布局、跟结果页长得不像，读起来像是离开了那个批次。
            batchId 取自材料自己（`MaterialRecord.batch_id`），不靠 store：从历史批次点进来时
            store 里装的是当前活批次，用它会把用户送回**另一批**。 */}
        {backBatchId && (
          <Link className="btn btn-sm" to={`/batches/${backBatchId}`}>
            ← 返回批次
          </Link>
        )}
        <h2 style={{ margin: 0 }}>
          {/* 自定义场景用用户输入的原文，和结果页的分组标题同一个来源。没有它的话这里只会写
              「自定义场景」——用户刚从「餐厅点餐」那一组点进来，标题却换了个说法。 */}
          {scenarioMeta(scenarioKey ?? '', historical.batch?.customLabel).titleZh} —{' '}
          {candidates.length} 套候选
        </h2>
        <span className="spacer" style={{ flex: 1 }} />
        {candidates.length > 2 && (
          <span className="row" style={{ gap: 4 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              切换对比：
            </span>
            {candidates.map((candidate, i) => (
              <button
                key={i}
                type="button"
                className={`btn btn-sm${pair.includes(i) ? ' btn-primary' : ''}`}
                /* 按钮上写「第 N 套」而不是「材料 A/B/C」。
                 *
                 * A/B 现在跟着摆放位置（见 `labelOf`），而摆放会被这些按钮改变——用它们当按钮名，
                 * 按钮就会在用户手底下改名：点了「材料 C」，它当场变成「材料 B」，下一次想点回来
                 * 已经找不到刚才那个名字了。「第 N 套」是这套材料的固有身份，不随摆放变。
                 *
                 * 点一个候选换掉**右**栏，左边留着。原来写的是
                 * `([a]) => (a === i ? [a, i] : [a, i])`——两个分支返回同一个值，所以那个三元
                 * 判断根本没在判断任何东西，点左栏自己那个按钮会让两栏变成同一套材料。 */
                onClick={() =>
                  setPair(([a, b]) => (a === i ? [a, b] : b === i ? [a, b] : [a, i]))
                }
              >
                第 {(candidate.index ?? i) + 1} 套
                {pair.includes(i) ? `（${labelOf.get(i)?.replace('材料 ', '')}）` : ''}
              </button>
            ))}
          </span>
        )}
      </div>

      {candidates.length === 1 && (
        <div className="banner banner-warn">
          <strong>本场景只有 1 套材料，无法对比</strong>
          <div>这一批里这个场景只生成了一套。回到批次页可以「补生成」再来对比。</div>
        </div>
      )}

      {/* ── 对比摘要（全宽，两列上方）────────────────────────────────────────
          规则模板拼接，不调模型。同一份结构化数据同时驱动这段话和下面两栏的每个模块，所以两者
          不可能互相矛盾。 */}
      {summary && (
        <div className="cmp-summary">
          <div className="cmp-summary-head">对比摘要</div>
          {summary.shared.length > 0 && (
            <div className="cmp-summary-row">
              <span className="cmp-summary-tag shared">共同点</span>
              <span>{summary.shared.join('；')}。</span>
            </div>
          )}
          {summary.differences.length > 0 && (
            <div className="cmp-summary-row">
              <span className="cmp-summary-tag diff">主要差异</span>
              <ul className="cmp-summary-list">
                {summary.differences.map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="cmp-summary-row">
            <span className="cmp-summary-tag advice">怎么选</span>
            <span>{summary.advice}</span>
          </div>
        </div>
      )}

      {/* ── 全文区的两个开关 ────────────────────────────────────────────────
          放在一排：它们管的是同一件事——下面那两栏原文怎么看。 */}
      <div className="row cmp-toolbar">
        <label style={{ fontSize: 12 }}>
          <input
            type="checkbox"
            checked={syncScroll}
            onChange={() => setSyncScroll((v) => !v)}
          />{' '}
          同步滚动
        </label>
        {/* 两侧同步展开/折叠：分别控制的话，左右两栏的行高会错开，同一个信息点在两侧对不上。 */}
        <button
          type="button"
          className={`btn btn-sm${showAnnotations ? ' btn-primary' : ''}`}
          aria-pressed={showAnnotations}
          onClick={() => setShowAnnotations((v) => !v)}
        >
          {showAnnotations ? '折叠旁注' : '展开旁注'}
        </button>
        <span className="muted" style={{ fontSize: 11 }}>
          {showAnnotations
            ? '每个信息点的类型、答案、标签、引用原文都在右侧'
            : '折叠时只显示高亮和编号；展开看每个点的答案与标签'}
        </span>
      </div>

      <div
        className="cmp-cols"
        style={{ gridTemplateColumns: showPair ? '1fr 1fr' : '1fr' }}
      >
        {(showPair ? pair : [0]).map((idx) => {
          const side = sides[idx]
          const record = candidates[idx]
          if (!side || !record) return null
          return (
            <div
              key={record.material_id}
              className="cmp-col"
              /* 这一栏当前是哪套材料。给测试用：「两栏永远不是同一套」这条不变式没法靠文字断言——
                 每栏内部到处都出现「材料 A」这几个字。 */
              data-material={record.material_id}
            >
              <div className="panel panel-pad cmp-facts">
                <div className="cmp-col-head">
                  <strong>{side.label}</strong>
                  <span className="muted" style={{ fontSize: 12 }}>
                    第 {record.index + 1} 套
                  </span>
                </div>

                {/* 话题简述。一句话说清这一套具体讲什么——两套同场景的材料靠它区分。 */}
                <div className="cmp-topic">{side.topic}</div>

                {/* 篇幅。只标硬线（450-750 词 / 20-48 轮）；规范里的 600-650 是真题观测典型值，
                    不是命制门槛，标出来会让人以为 660 词的材料有问题。 */}
                <div className="cmp-row">
                  <span className="cmp-row-label">篇幅</span>
                  <span className={side.length.ok ? '' : 'cmp-row-warn'}>
                    {side.length.text}
                    {side.length.ok ? '' : ' ← 超出规范区间'}
                  </span>
                </div>

                {/* 前后两组分配。切分位置影响两组题量是否均衡（规范 §6）。 */}
                <div className="cmp-row">
                  <span className="cmp-row-label">前后两组</span>
                  <span>
                    第 1 组 {side.metrics.firstHalfCount} 题 / 第 2 组{' '}
                    {side.metrics.secondHalfCount} 题（按第 {side.metrics.splitAfter} 题分组）
                  </span>
                </div>

                {/* 干扰机制的**计数**。带编号的那份在下面的考点块里（可跳转），这里只给数量——
                    「先说后改 ×3」和「×1」是两种难度，而让人去数两排圆圈来得出这件事没必要。 */}
                <div className="cmp-row">
                  <span className="cmp-row-label">干扰机制</span>
                  <span className="cmp-tags">
                    {side.distractions.length === 0 ? (
                      <span className="muted">未使用</span>
                    ) : (
                      side.distractions.map((d) => (
                        <span key={d.kind} className="cmp-tag" title={`第 ${d.numbers.join('、')} 题`}>
                          {d.label} ×{d.count}
                        </span>
                      ))
                    )}
                  </span>
                </div>
              </div>

              {/* 信息点类型列表 + 干扰机制（都带编号，点一下跳到原文）。
                  `compact` 去掉重复的 headline（顶部已作话题简述）和质量提示块。 */}
              <ExamPointPanel
                compact
                summary={side.examPoints}
                onJump={(turnIndex) =>
                  setJump((prev) => ({
                    ...prev,
                    [record.material_id]: { turnIndex, nonce: Date.now() },
                  }))
                }
              />

              {/* 原文 + 时间轴（含前后切分线）。`showVerdict={false}` 关掉时间轴下面那块
                  「出题就绪度」——客户点名去掉这类内部指标。 */}
              <MaterialReader
                view={side.view}
                height={520}
                narrow
                showAnnotations={showAnnotations}
                showVerdict={false}
                jumpToTurn={jump[record.material_id] ?? null}
              />

              {/* 能否成表格/表单题。补充信息，所以放在最底部。自带 panel 外壳。 */}
              <QuestionTypePanel analysis={side.groups} />
            </div>
          )
        })}
      </div>

      {/* 这一页不承载选稿（客户：选定操作放在主结果页的 checkbox），所以底部没有操作栏。
          留一句话说清音频在哪弄，免得用户在这一页找试听。 */}
      <div className="muted cmp-foot">
        选稿在批次页勾选。想先听一遍的，在单套材料的阅读页点「生成音频」。
      </div>
    </div>
  )
}
