/**
 * 结果卡片要显示的三样东西：第一句台词、一行简述、需要看一眼的点号。
 *
 * 客户的版式里每张卡只有这么点信息量，但每一项都得当真：
 *
 * - **第一句台词**跳过旁白。旁白是「You will hear a conversation between…」这种
 *   套话，三套材料的旁白一模一样，用它当预览等于没有预览。
 * - **一行简述**只由 blueprint 自己声明的字段拼出来（`type` / `distractor` /
 *   `correction` / `indirect_confirmation`），术语沿用《Part1 选材命制规范》。
 *   不调模型、不猜，因此不可能和材料本身矛盾。
 * - **黄点**是「审阅时该看一眼的点号」，判据全部来自已有的确定性计算：扎堆、
 *   题号回跳、盲评没听出来。不引入新阈值。
 *
 * **卡片上没有评价文字。** 客户的原话：「结果页卡片上只展示：场景名 + 信息点时间轴图
 * + 预览第一句话 + 操作按钮。不展示任何评价文字。……质量评价建议 → 放在『阅读全文』
 * 详情页里」。理由是建议要有上下文才读得懂——「⑤⑥之间空了 6 轮」这句话，只有正在看全文
 * 的人才知道它指哪一段。所以那些文案留在 domain/usability.ts，由阅读页的
 * DistributionStrip 渲染；这一层不再产出 `shortcomings`。
 *
 * 黄点留下来了，而且仍然由同一批判据驱动。它是「看这里」的指路，不是结论：一个有颜色的
 * 点不会替客户判断材料好坏，只会让他在时间轴上先看那一段。客户点名表扬过这张时间轴。
 *
 * `verdict`（PASS / MINOR_EDITS / FAIL）在这一层不再被读取：它唯一的用处是那句
 * 「评价环节判为不合格」的提示，而提示已经随评价文字一起搬到阅读页
 * （MaterialPage 的 audit_rejection 横幅）。
 */
import type { MaterialRecord } from '@/contracts/api'
import { scenarioMeta } from '@/config/scenarioMeta'
import type { DistributionMetrics } from './distribution'
import { contentFacts, DISTRACTION_LABEL } from './pointFacts'
import type { ViewMaterial } from './types'
import { ITEM_TYPE_LABEL } from './types'

export interface CardPreview {
  materialId: string
  scenarioKey: string
  /** 第 N 套里的 N-1。 */
  index: number
  /** 对话第一句（跳过旁白）；没有对话轮次时为 null。 */
  firstLine: string | null
  /** 一行简述：话题 + 干扰类型。 */
  summary: string
  /** blueprint 声明的信息点数，即圆点个数（正常是 10）。 */
  pointTotal: number
  /** 所有点号，升序，用来画圆点。 */
  pointNumbers: number[]
  /** 需要看一眼的点号（黄点）。 */
  flaggedPoints: number[]
}

/** 旁白是 speaker1。跳过它，返回第一句真实台词。 */
export function firstDialogueLine(view: ViewMaterial): string | null {
  const turn = view.turns.find((t) => t.dialogueOrdinal !== null && t.text.trim().length > 0)
  return turn ? turn.text.trim() : null
}

/**
 * 一行简述。形如「租房咨询，含拼读 + 先说后改 + 日期/时间」。
 *
 * 优先级是「拼读 → 干扰机制 → 信息点类型」：前两样是 Part 1 的难度来源（规范 §3、
 * §4B-4），第三样只是话题成分，所以在前两样撑不满时才补位。
 */
export function previewSummary(view: ViewMaterial): string {
  const topic = scenarioMeta(view.scenarioKey).titleZh
  const facts = contentFacts(view.blueprint)
  const parts: string[] = []

  if (facts.spellingNumbers.length > 0) parts.push('拼读')
  for (const kind of facts.distractionKinds) parts.push(DISTRACTION_LABEL[kind])

  if (parts.length < 2) {
    // 按出现次数补两类信息点，让两套同场景的材料仍然可区分。
    const counts = new Map<string, number>()
    for (const item of view.blueprint.items) {
      const label = ITEM_TYPE_LABEL[item.type]
      counts.set(label, (counts.get(label) ?? 0) + 1)
    }
    const top = [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([label]) => label)
      .filter((label) => !parts.includes(label))
      .slice(0, 2 - parts.length)
    parts.push(...top)
  }

  return parts.length > 0 ? `${topic}，含${parts.join(' + ')}` : topic
}

/**
 * 需要看一眼的点号。
 *
 * 三个判据，全部来自已经算好的确定性结果，没有新阈值：
 *   1. 扎堆（`clusters`）——连着给，考生来不及记；
 *   2. 题号回跳（`outOfOrder`）——音频顺序和题号不一致；
 *   3. 盲评没复原（`crossCheck.unrecoverable`）——试听的人没听出来。
 *
 * 不含 `unplacedNumbers`：定位不出来的点在图上根本不画（见 distribution.ts），
 * 给一个画不出来的点标黄没有意义；而且那是我们自己的标注问题，不是让客户看一眼就能
 * 判断的材料属性。它走开发者通道。
 */
export function flaggedPointNumbers(
  view: ViewMaterial,
  metrics: DistributionMetrics,
): number[] {
  const flagged = new Set<number>()
  for (const cluster of metrics.clusters) for (const n of cluster.numbers) flagged.add(n)
  for (const jump of metrics.outOfOrder) {
    flagged.add(jump.spokenFirst)
    flagged.add(jump.spokenSecond)
  }
  for (const row of view.crossCheck.unrecoverable) flagged.add(row.number)
  // 只保留 blueprint 真有的点号：cross_check 是另一个进程算的，点号对不上时
  // 画一个不存在的圆点会让人以为材料多了一个点。
  const known = new Set(view.blueprint.items.map((i) => i.number))
  return [...flagged].filter((n) => known.has(n)).sort((a, b) => a - b)
}

export function buildCardPreview(
  record: MaterialRecord,
  view: ViewMaterial,
  metrics: DistributionMetrics,
): CardPreview {
  const pointNumbers = view.blueprint.items.map((i) => i.number).sort((a, b) => a - b)
  return {
    materialId: record.material_id,
    scenarioKey: record.scenario_key,
    index: record.index,
    firstLine: firstDialogueLine(view),
    summary: previewSummary(view),
    pointTotal: pointNumbers.length,
    pointNumbers,
    flaggedPoints: flaggedPointNumbers(view, metrics),
  }
}
