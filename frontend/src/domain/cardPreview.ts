/**
 * 结果卡片要显示的四样东西：第一句台词、一行简述、需要看一眼的点号、缺陷小结。
 *
 * 客户的版式里每张卡只有这么点信息量，但每一项都得当真：
 *
 * - **第一句台词**跳过旁白。旁白是「You will hear a conversation between…」这种
 *   套话，三套材料的旁白一模一样，用它当预览等于没有预览。
 * - **一行简述**只由 blueprint 自己声明的字段拼出来（`type` / `distractor` /
 *   `correction` / `indirect_confirmation`），术语沿用《Part1 选材命制规范》。
 *   不调模型、不猜，因此不可能和材料本身矛盾。
 * - **黄点**是「审阅时该看一眼的点号」，判据全部来自已有的确定性计算：锚点定位
 *   不到、扎堆、题号回跳、盲评没听出来。不引入新阈值。
 * - **缺陷小结**复用 domain/usability.ts 的结论文案。客户的规则是「有缺陷的材料
 *   照样返回、照样可选，只是把缺点说清楚让用户自己判断」，所以这里只描述缺点，
 *   不做「可选/不可选」的判断——那个判断不存在了。
 *
 * `verdict`（PASS / MINOR_EDITS / FAIL）在这一层是可读的，但只用来补一句
 * 「评价环节判为不合格」这类给审阅者的提示，绝不当作卡片上的状态徽章：客户明确
 * 要求徽章统一是「待审核」，Agent 的内部评级不出现在用户面前。
 */
import type { Verdict } from '@/contracts'
import type { MaterialRecord } from '@/contracts/api'
import { scenarioMeta } from '@/config/scenarioMeta'
import type { DistributionMetrics } from './distribution'
import { contentFacts, DISTRACTION_LABEL } from './pointFacts'
import type { ViewMaterial } from './types'
import { ITEM_TYPE_LABEL, SEVERITY_LABEL } from './types'
import { assessUsability, type Readiness } from './usability'

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
  /** 缺陷小结，每条一句话；无缺陷时为空数组。 */
  shortcomings: string[]
  /** 出题就绪度，只用于给缺陷小结排序/着色，不作为徽章文案。 */
  readiness: Readiness
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
 * 四个判据，全部来自已经算好的确定性结果，没有新阈值：
 *   1. 锚点定位不到（`unplacedNumbers`）——这个点无法据以出题；
 *   2. 扎堆（`clusters`）——连着给，考生来不及记；
 *   3. 题号回跳（`outOfOrder`）——音频顺序和题号不一致；
 *   4. 盲评没复原（`crossCheck.unrecoverable`）——试听的人没听出来。
 */
export function flaggedPointNumbers(
  view: ViewMaterial,
  metrics: DistributionMetrics,
): number[] {
  const flagged = new Set<number>(metrics.unplacedNumbers)
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

/** 评价方判为不合格时给审阅者的一句话。verdict 只在这里出声，不做徽章。 */
function verdictShortcoming(verdict: Verdict, criticalCount: number): string | null {
  if (verdict === 'FAIL') {
    return criticalCount > 0
      ? `评价环节认为有 ${criticalCount} 处必须改的问题，建议先读全文再决定是否选用。`
      : '评价环节认为本套整体不达标，建议先读全文再决定是否选用。'
  }
  if (verdict === 'NOT_ASSESSABLE') {
    return '评价环节未能给出结论，本套的质量没有经过复核，请务必先读全文。'
  }
  return null
}

/**
 * 缺陷小结。文案一律取自 usability.ts，不另造一套说法——同一份数据同时驱动
 * 卡片小结、分布预览和对比视图，三处不可能互相打架。
 */
export function shortcomingsOf(
  view: ViewMaterial,
  metrics: DistributionMetrics,
): { shortcomings: string[]; readiness: Readiness } {
  const verdictReport = assessUsability(metrics)
  const lines = verdictReport.checks
    .filter((c) => c.level !== 'ready')
    .map((c) => `${c.label}：${c.detail}`)

  const criticalCount = view.audit.findings.filter((f) => f.severity === 'critical').length
  const fromVerdict = verdictShortcoming(view.verdict, criticalCount)
  if (fromVerdict) lines.unshift(fromVerdict)

  // 「必须改」的 finding 里第一条带上原文位置：小结要能让人直接翻到那一句。
  const firstCritical = view.audit.findings.find((f) => f.severity === 'critical')
  if (firstCritical && criticalCount > 0) {
    lines.push(`${SEVERITY_LABEL.critical}：${firstCritical.rule}`)
  }

  if (view.anchorMismatches.length > 0) {
    lines.push(
      `有 ${view.anchorMismatches.length} 处旁注可能标在了错的句子上，读全文时请核对高亮位置。`,
    )
  }
  if (view.degraded) {
    lines.push('本套跳过了修改与复评环节，只经过一次评价。')
  }

  const readiness: Readiness =
    view.verdict === 'FAIL' || view.verdict === 'NOT_ASSESSABLE'
      ? 'blocked'
      : verdictReport.level
  return { shortcomings: lines, readiness }
}

export function buildCardPreview(
  record: MaterialRecord,
  view: ViewMaterial,
  metrics: DistributionMetrics,
): CardPreview {
  const { shortcomings, readiness } = shortcomingsOf(view, metrics)
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
    shortcomings,
    readiness,
  }
}
