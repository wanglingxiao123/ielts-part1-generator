/**
 * Distribution metrics (design.md §3.4). Pure, unit-testable.
 *
 *   gaps       = [d1 - d_start, d2 - d1, ..., d10 - d9, d_end - d10]
 *   CV         = stdev(gaps) / mean(gaps)
 *   uniformity = clamp(round(100 * (1 - CV / CV_FAIL)), 0, 100)
 *
 * The leading and trailing boundary gaps are included on purpose: ten points
 * crammed into the middle with eight empty turns at each end is a real defect
 * that point-to-point gaps alone would score as perfect.
 */
import type { Thresholds } from '@/config/runtimeConfig'
import type { ViewMaterial } from './types'

export interface PointPosition {
  number: number
  turnIndex: number
  /** Position on the density axis (narration excluded). */
  ordinal: number
  group: 1 | 2
}

export interface Cluster {
  /** Item numbers, ascending. */
  numbers: number[]
  turnStart: number
  turnEnd: number
  ordinalStart: number
  ordinalEnd: number
}

export interface DistributionMetrics {
  points: PointPosition[]
  /** Points whose anchor was out of range / unresolvable, excluded from math. */
  unplacedNumbers: number[]
  dialogueTurnCount: number
  gaps: number[]
  meanGap: number
  maxGap: number
  minGap: number
  /** gaps index of maxGap, so the strip can mark the right segment. */
  maxGapIndex: number
  cv: number
  uniformity: number
  firstHalfCount: number
  secondHalfCount: number
  splitAfter: number
  /** Ordinal boundary between question group 1 and 2, for the ╫ marker. */
  splitOrdinal: number | null
  clusters: Cluster[]
  /** Gaps at least 2x the mean — the "empty stretch" callouts. */
  wideGaps: Array<{ index: number; size: number; fromOrdinal: number; toOrdinal: number }>
  /**
   * Adjacent pairs where a HIGHER question number is spoken before a lower one
   * (spec §4B-2 线性顺序性: 音频出现顺序必须等于题号顺序，几乎不回跳).
   *
   * Pure ordering of the already-placed points — no threshold involved. Points
   * sharing one ordinal are tie-broken by number, so a single turn carrying two
   * points is never reported as a jump-back.
   */
  outOfOrder: Array<{
    spokenFirst: number
    spokenSecond: number
    turnFirst: number
    turnSecond: number
  }>
  cvWarn: boolean
  balanced: boolean
  notes: string[]
}

function mean(xs: number[]): number {
  if (xs.length === 0) return 0
  return xs.reduce((a, b) => a + b, 0) / xs.length
}

function stdevPopulation(xs: number[]): number {
  if (xs.length === 0) return 0
  const m = mean(xs)
  return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)))
}

export function computeDistribution(
  view: ViewMaterial,
  thresholds: Thresholds,
): DistributionMetrics {
  // 点位读的是 ViewTurn 上**已经解出来**的位置，不是 blueprint 声明的 turn_index。
  // joinArtifacts 已经按 domain/anchors.ts 那条规则把能确定挪正的挪正、确定不了的
  // 不予显示，所以分布图、旁注列和缩略图三处必然指向同一批句子。直接读 blueprint 会
  // 让分布图把一个已经挪正的点画在旧位置上。
  const points: PointPosition[] = []
  const placed = new Set<number>()

  for (const turn of view.turns) {
    if (turn.dialogueOrdinal === null) continue
    for (const item of turn.items) {
      placed.add(item.number)
      points.push({
        number: item.number,
        turnIndex: turn.index,
        ordinal: turn.dialogueOrdinal,
        group: item.group,
      })
    }
  }
  points.sort((a, b) => a.ordinal - b.ordinal || a.number - b.number)

  // 定位不出来的点。它们不画在图上、也不参与任何度量——把一个位置未知的点按声明的
  // 下标画上去，等于让图替一条我们并不相信的坐标背书。
  const unplacedNumbers = view.blueprint.items
    .map((i) => i.number)
    .filter((n) => !placed.has(n))
    .sort((a, b) => a - b)

  const dialogueTurnCount = view.dialogueTurnCount
  const dStart = 0
  const dEnd = Math.max(0, dialogueTurnCount - 1)

  const gaps: number[] = []
  if (points.length > 0) {
    gaps.push(points[0]!.ordinal - dStart)
    for (let i = 1; i < points.length; i += 1) {
      gaps.push(points[i]!.ordinal - points[i - 1]!.ordinal)
    }
    gaps.push(dEnd - points[points.length - 1]!.ordinal)
  }

  const m = mean(gaps)
  const cv = m > 0 ? stdevPopulation(gaps) / m : 0
  const uniformity =
    thresholds.CV_FAIL > 0
      ? Math.min(100, Math.max(0, Math.round(100 * (1 - cv / thresholds.CV_FAIL))))
      : 0

  let maxGap = 0
  let maxGapIndex = -1
  let minGap = gaps.length > 0 ? Number.POSITIVE_INFINITY : 0
  gaps.forEach((g, i) => {
    if (g > maxGap) {
      maxGap = g
      maxGapIndex = i
    }
    if (g < minGap) minGap = g
  })
  if (!Number.isFinite(minGap)) minGap = 0

  const splitAfter = view.blueprint.split_after
  const firstHalfCount = view.blueprint.items.filter((i) => i.number <= splitAfter).length
  const secondHalfCount = view.blueprint.items.length - firstHalfCount
  const lastOfFirst = points.find((p) => p.number === splitAfter)
  const firstOfSecond = points.find((p) => p.number === splitAfter + 1)
  const splitOrdinal =
    lastOfFirst && firstOfSecond
      ? (lastOfFirst.ordinal + firstOfSecond.ordinal) / 2
      : (lastOfFirst?.ordinal ?? null)

  // Clusters: maximal runs of >= CLUSTER_MIN_POINTS consecutive points spanning
  // <= CLUSTER_SPAN TURN indexes. Turn span (not ordinal span) is what §3.5
  // specifies, and it is also what the reviewer reads off the script.
  //
  // CLUSTER_MIN_POINTS defaults to 3 rather than 2 because the real balanced
  // fixture legitimately has three 2-point pairs at turn distance <= 3
  // (a point often spans ask/answer/confirm turns, §3.1). At 2 the balanced
  // material would report three clusters and the signal would be worthless.
  const clusters: Cluster[] = []
  let runStart = 0
  const extend = (endExclusive: number) => {
    const run = points.slice(runStart, endExclusive)
    if (run.length >= thresholds.CLUSTER_MIN_POINTS) {
      clusters.push({
        numbers: run.map((p) => p.number).sort((a, b) => a - b),
        turnStart: Math.min(...run.map((p) => p.turnIndex)),
        turnEnd: Math.max(...run.map((p) => p.turnIndex)),
        ordinalStart: run[0]!.ordinal,
        ordinalEnd: run[run.length - 1]!.ordinal,
      })
    }
  }
  for (let i = 1; i <= points.length; i += 1) {
    const atEnd = i === points.length
    const stillTight =
      !atEnd && points[i]!.turnIndex - points[runStart]!.turnIndex <= thresholds.CLUSTER_SPAN
    if (!stillTight) {
      extend(i)
      runStart = i
    }
  }

  const wideGaps = gaps
    .map((size, index) => ({ size, index }))
    .filter((g) => m > 0 && g.size >= 2 * m && g.size >= 4)
    .map((g) => {
      const fromOrdinal = g.index === 0 ? dStart : points[g.index - 1]!.ordinal
      const toOrdinal = g.index === points.length ? dEnd : points[g.index]!.ordinal
      return { index: g.index, size: g.size, fromOrdinal, toOrdinal }
    })

  // Linearity (spec §4B-2). `points` is sorted by ordinal, so any place where
  // the item number decreases is an audible jump back to an earlier question.
  const outOfOrder: DistributionMetrics['outOfOrder'] = []
  for (let i = 1; i < points.length; i += 1) {
    const prev = points[i - 1]!
    const cur = points[i]!
    if (cur.number < prev.number && cur.ordinal > prev.ordinal) {
      outOfOrder.push({
        spokenFirst: prev.number,
        spokenSecond: cur.number,
        turnFirst: prev.turnIndex,
        turnSecond: cur.turnIndex,
      })
    }
  }

  const notes: string[] = []
  for (const c of clusters) {
    notes.push(
      `${c.numbers.map((n) => `#${n}`).join('')} 集中于 turn ${c.turnStart}–${c.turnEnd}`,
    )
  }
  for (const g of wideGaps) {
    notes.push(`对话轮次 ${g.fromOrdinal}→${g.toOrdinal} 存在 ${g.size} 轮空档`)
  }
  for (const o of outOfOrder) {
    notes.push(
      `第 ${o.spokenSecond} 题的信息（turn ${o.turnSecond}）出现在第 ${o.spokenFirst} 题` +
        `（turn ${o.turnFirst}）之后，题号回跳`,
    )
  }
  if (unplacedNumbers.length > 0) {
    notes.push(`${unplacedNumbers.map((n) => `#${n}`).join('')} 锚点无法定位，未计入分布指标`)
  }

  return {
    points,
    unplacedNumbers,
    dialogueTurnCount,
    gaps,
    meanGap: m,
    maxGap,
    minGap,
    maxGapIndex,
    cv,
    uniformity,
    firstHalfCount,
    secondHalfCount,
    splitAfter,
    splitOrdinal,
    clusters,
    wideGaps,
    outOfOrder,
    cvWarn: cv > thresholds.CV_WARN,
    balanced: Math.abs(firstHalfCount - secondHalfCount) <= 1,
    notes,
  }
}
