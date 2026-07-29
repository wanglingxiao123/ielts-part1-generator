/**
 * 分布图的坐标换算。横轴 = 对话进行到第几轮（不含旁白）。
 *
 * 阅读页的完整分布条（DistributionStrip）和结果卡上的缩略时间轴
 * （DistributionThumb）共用这里的每一个函数。抽出来的唯一目的就是让两处**不可能**
 * 画出不一致的点位：同一份 `DistributionMetrics` 进来，同一个百分比出去。
 *
 * 必须保留的设计性质（design.md §3.3）：点按真实 ordinal 落位，**不做避让**。
 * 挨在一起就是原文里真的挨在一起，重叠本身就是要看的信号，不能"修好"。
 * `pileIndex` 只把 ordinal **完全相同**的点垂直叠起来——横坐标一动不动——否则同
 * 一轮里的两个点会被像素级遮住，把最坏的情况藏在看起来正常的那个点后面。
 */
import type { DistributionMetrics, PointPosition } from './distribution'
import type { FormGroupAnalysis } from './formGroups'
import { ITEM_FORM_LABEL, circled, type ViewMaterial } from './types'

/** 刻度间隔（轮）。客户要的是 0 / 8 / 16 / 24 / 32 这几个位置有标注。 */
export const TICK_STEP = 8

/** 横轴跨度（轮）。至少 1，避免只有一轮对话时除以 0。 */
export function axisSpan(metrics: Pick<DistributionMetrics, 'dialogueTurnCount'>): number {
  return Math.max(1, metrics.dialogueTurnCount - 1)
}

/** ordinal → CSS 左偏移百分比。等比，所以轴宽随容器变化而不需要重算。 */
export function axisPercent(ordinal: number, span: number): string {
  return `${(ordinal / span) * 100}%`
}

/** 一段区间占轴的百分比宽度。 */
export function axisWidthPercent(fromOrdinal: number, toOrdinal: number, span: number): number {
  return ((toOrdinal - fromOrdinal) / span) * 100
}

/** 0, 8, 16, … 直到覆盖整根轴。 */
export function axisTicks(span: number, step: number = TICK_STEP): number[] {
  const ticks: number[] = []
  for (let o = 0; o <= span; o += step) ticks.push(o)
  return ticks
}

/**
 * 第 i 个点在「同 ordinal 堆」里的第几层（0 = 最底层）。
 *
 * 只用于垂直错开，横坐标不受影响——这不是避让。
 */
export function pileIndex(points: readonly PointPosition[], i: number): number {
  const ordinal = points[i]?.ordinal
  if (ordinal === undefined) return 0
  let pile = 0
  for (let k = 0; k < i; k += 1) if (points[k]!.ordinal === ordinal) pile += 1
  return pile
}

export interface AxisBracket {
  key: string
  /** 左偏移，形如 `42.5%`。 */
  left: string
  /** 宽度百分比（可能为 0：整组落在同一轮）。 */
  widthPercent: number
  /** 一句话说明，用作 title/图例文字。 */
  label: string
  /** 跨度过宽（form_group）或密度过高（cluster）。 */
  warn: boolean
  numbers: number[]
}

/**
 * form_group 括号。**只有被声明过的组**才画括号：在 `form_group === null` 的桶上
 * 画一条括号等于断言那些点属于同一道题，而 null 恰恰否认了这件事
 * （见 formGroups.ts 的 `ungrouped`）。
 */
export function declaredGroupBrackets(
  view: ViewMaterial,
  groups: FormGroupAnalysis,
  span: number,
): AxisBracket[] {
  return groups.groups
    .filter((g) => !g.ungrouped && g.numbers.length > 1)
    .map((g) => {
      const startOrd = view.turns[g.turnStart]?.dialogueOrdinal ?? 0
      const endOrd = view.turns[g.turnEnd]?.dialogueOrdinal ?? startOrd
      return {
        key: `fg${g.name ?? 'null'}-${g.itemForm}`,
        left: axisPercent(startOrd, span),
        widthPercent: axisWidthPercent(startOrd, endOrd, span),
        label:
          `${ITEM_FORM_LABEL[g.itemForm]}${g.name ? ` ${g.name}` : ''}：` +
          `${g.numbers.map((n) => circled(n)).join('')}` +
          (g.spanWarn ? ' ⚠ 跨度太宽，考生要跨半篇回忆' : ''),
        warn: g.spanWarn,
        numbers: g.numbers,
      }
    })
}

/** 扎堆括号。同一份 `metrics.clusters`，所以和黄点、缺陷小结说的是同一件事。 */
export function clusterBrackets(metrics: DistributionMetrics, span: number): AxisBracket[] {
  return metrics.clusters.map((c) => ({
    key: `cl${c.turnStart}-${c.turnEnd}`,
    left: axisPercent(c.ordinalStart, span),
    widthPercent: axisWidthPercent(c.ordinalStart, c.ordinalEnd, span),
    label:
      `密度过高：${c.numbers.map((n) => circled(n)).join('')} 挤在 turn ` +
      `${c.turnStart}–${c.turnEnd}，考生来不及记`,
    warn: true,
    numbers: c.numbers,
  }))
}
