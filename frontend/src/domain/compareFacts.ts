/**
 * 对比两套材料时，出题人真正要看的那几件事——以及那句「所以选哪套」。
 *
 * ## 为什么不复用 `domain/compare.ts`
 *
 * 那个模块（`compareCandidates`）算的是总分差、倾向、以及「听不出来的点」「计划外的可考细节」
 * 这类**评价方内部指标**。客户明确否掉了它们在对比页的存在：
 *
 *   「当前对比页展示了大量出题人不关心的内部评价指标（100 分、听不出来、计划外细节、出题就绪度）。
 *     出题人对比两套材料时只关心规范（§2-§4、§6）里定义的维度。」
 *
 * 所以这里另起一份，判据只用规范维度：类型覆盖、干扰机制、篇幅、前后两组分配。它不给分、不判
 * 「哪套赢」，只说清两套**差在哪**，选择权留给出题人。`compare.ts` 保持原样服务它自己的调用方。
 *
 * ## 篇幅只标硬线
 *
 * 450-750 词 / 20-48 轮是规范的禁止线，越过即不合格。规范里另有「600-650 词」，那是 20 套真题的
 * **观测典型值**，不是命制门槛（这个判断在项目里栽过一次：`validate_part1.py` 曾把它当硬门槛，
 * warning 也返回失败码）。所以这里只判硬线，不提典型值——把 660 词标成异常会让出题人以为一套
 * 完全合格的材料有问题。
 */
import { DISTRACTION_LABEL, contentFacts, type DistractionKind } from './pointFacts'
import type { DistributionMetrics } from './distribution'
import type { ItemType } from '@/contracts'
import { ITEM_TYPE_LABEL, type ViewMaterial } from './types'

/* ── 篇幅 ─────────────────────────────────────────────────────────────────── */

/** 规范的禁止线。越过即不合格；区间内一律合格，不再细分。 */
export const WORD_RANGE = [450, 750] as const
export const TURN_RANGE = [20, 48] as const

export interface LengthFacts {
  words: number
  turns: number
  wordsInRange: boolean
  turnsInRange: boolean
  /** 两项都在区间内。 */
  ok: boolean
  /** 一行说法：「535 词 / 40 轮」，越线的那一项带上区间。 */
  text: string
}

export function lengthFacts(view: ViewMaterial): LengthFacts {
  const words = view.audit.metrics.dialogue_words
  const turns = view.audit.metrics.dialogue_turns
  const wordsInRange = words >= WORD_RANGE[0] && words <= WORD_RANGE[1]
  const turnsInRange = turns >= TURN_RANGE[0] && turns <= TURN_RANGE[1]
  const wordText = wordsInRange
    ? `${words} 词`
    : `${words} 词（须 ${WORD_RANGE[0]}-${WORD_RANGE[1]}）`
  const turnText = turnsInRange
    ? `${turns} 轮`
    : `${turns} 轮（须 ${TURN_RANGE[0]}-${TURN_RANGE[1]}）`
  return {
    words,
    turns,
    wordsInRange,
    turnsInRange,
    ok: wordsInRange && turnsInRange,
    text: `${wordText} / ${turnText}`,
  }
}

/* ── 干扰机制 ─────────────────────────────────────────────────────────────── */

export interface DistractionCount {
  kind: DistractionKind
  label: string
  count: number
  /** 用了这个机制的点号，升序。 */
  numbers: number[]
}

/**
 * 干扰机制按种类计数：「先说后改 ×3」。
 *
 * 之前只有「标签 + 一排点号」，没有 ×N 形式。对比两套时数量本身就是差异——一套用了 3 次同义替换、
 * 另一套 1 次，那是两种难度，而让人去数两排圆圈来得出这件事是没必要的。点号仍然留着（要跳转）。
 *
 * 顺序固定为 先说后改 → 同义替换 → 干扰，不按出现次数排：两侧并排时顺序必须一致，否则同一行
 * 对不上同一个机制。
 */
const KIND_ORDER: DistractionKind[] = ['correction', 'paraphrase', 'unspecified']

export function distractionCounts(view: ViewMaterial): DistractionCount[] {
  const { distractions } = contentFacts(view.blueprint)
  const byKind = new Map<DistractionKind, number[]>()
  for (const { number, kind } of distractions) {
    byKind.set(kind, [...(byKind.get(kind) ?? []), number])
  }
  return KIND_ORDER.filter((kind) => byKind.has(kind)).map((kind) => {
    const numbers = [...byKind.get(kind)!].sort((a, b) => a - b)
    return { kind, label: DISTRACTION_LABEL[kind], count: numbers.length, numbers }
  })
}

/* ── 对比摘要 ─────────────────────────────────────────────────────────────── */

export interface CompareSummary {
  /** 两套都成立的事。空数组表示没有值得一说的共同点。 */
  shared: string[]
  /** 核心差异，一条一句。空数组表示这两套在规范维度上没有可说的差别。 */
  differences: string[]
  /** 一句选择建议。没有差异时说的是「按别的标准挑」，而不是硬造一个理由。 */
  advice: string
}

interface Side {
  label: string
  view: ViewMaterial
  metrics: DistributionMetrics
}

/** 两套共有的信息点类型，按规范的八类顺序。 */
function sharedTypes(a: ViewMaterial, b: ViewMaterial): ItemType[] {
  const inB = new Set(b.blueprint.items.map((i) => i.type))
  const seen = new Set<ItemType>()
  const out: ItemType[] = []
  for (const item of a.blueprint.items) {
    if (inB.has(item.type) && !seen.has(item.type)) {
      seen.add(item.type)
      out.push(item.type)
    }
  }
  return out
}

/** A 有而 B 没有的类型。 */
function typesOnlyIn(a: ViewMaterial, b: ViewMaterial): ItemType[] {
  const inB = new Set(b.blueprint.items.map((i) => i.type))
  const seen = new Set<ItemType>()
  const out: ItemType[] = []
  for (const item of a.blueprint.items) {
    if (!inB.has(item.type) && !seen.has(item.type)) {
      seen.add(item.type)
      out.push(item.type)
    }
  }
  return out
}

function labels(types: readonly ItemType[]): string {
  return types.map((t) => ITEM_TYPE_LABEL[t]).join('、')
}

/**
 * 两三行总结：共同点、核心差异、一句建议。
 *
 * 规则模板拼接，不调模型——同一份结构化数据同时驱动这段话和下面两栏的每个模块，所以两者不可能
 * 互相矛盾。措辞上不出现分数、不出现「倾向 A」这种判决口气：客户要的是「说清差在哪，我自己选」。
 */
export function compareSummary(left: Side, right: Side): CompareSummary {
  const shared: string[] = []
  const differences: string[] = []

  const lLen = lengthFacts(left.view)
  const rLen = lengthFacts(right.view)

  // ── 共同点 ──
  const common = sharedTypes(left.view, right.view)
  if (common.length > 0) {
    shared.push(`两套都考了${labels(common)}`)
  }
  if (lLen.ok && rLen.ok) {
    shared.push('篇幅都在合格区间内')
  }

  // ── 差异 ──
  // 1. 类型覆盖。哪套多考了什么，是出题人第一眼要问的。
  const onlyLeft = typesOnlyIn(left.view, right.view)
  const onlyRight = typesOnlyIn(right.view, left.view)
  if (onlyLeft.length > 0 || onlyRight.length > 0) {
    const parts: string[] = []
    if (onlyLeft.length > 0) parts.push(`${left.label}多了${labels(onlyLeft)}`)
    if (onlyRight.length > 0) parts.push(`${right.label}多了${labels(onlyRight)}`)
    differences.push(`考点类型不同：${parts.join('，')}`)
  }

  // 2. 干扰机制的数量差。同一机制用 3 次和用 1 次是两种难度。
  const lDist = new Map(distractionCounts(left.view).map((d) => [d.kind, d]))
  const rDist = new Map(distractionCounts(right.view).map((d) => [d.kind, d]))
  for (const kind of KIND_ORDER) {
    const l = lDist.get(kind)?.count ?? 0
    const r = rDist.get(kind)?.count ?? 0
    if (l === r) continue
    const label = DISTRACTION_LABEL[kind]
    if (l === 0 || r === 0) {
      const has = l > 0 ? left.label : right.label
      const n = Math.max(l, r)
      differences.push(`只有${has}用了${label}（${n} 处）`)
    } else {
      differences.push(`${label}：${left.label} ${l} 处 / ${right.label} ${r} 处`)
    }
  }

  // 3. 篇幅。越线的要单独点出来——那是不合格，不是风格差别。
  if (!lLen.ok || !rLen.ok) {
    const bad = [!lLen.ok ? `${left.label}（${lLen.text}）` : null,
                 !rLen.ok ? `${right.label}（${rLen.text}）` : null]
      .filter(Boolean)
      .join('、')
    differences.push(`篇幅超出规范区间：${bad}`)
  } else if (Math.abs(lLen.words - rLen.words) >= 60) {
    // 都合格时只在差得明显时才说，且说成风格差别。60 词约等于两三轮对话。
    const longer = lLen.words > rLen.words ? left : right
    differences.push(
      `篇幅长短不同：${left.label} ${lLen.words} 词 / ${right.label} ${rLen.words} 词，` +
        `${longer.label}更长（都合格，长短只是风格差别）`,
    )
  }

  // 4. 前后两组分配。切分位置不同会影响两组题量是否均衡。
  const lSplit = `${left.metrics.firstHalfCount}/${left.metrics.secondHalfCount}`
  const rSplit = `${right.metrics.firstHalfCount}/${right.metrics.secondHalfCount}`
  if (lSplit !== rSplit) {
    differences.push(
      `前后两组题量不同：${left.label} 第 1 组 ${left.metrics.firstHalfCount} 题 / 第 2 组 ` +
        `${left.metrics.secondHalfCount} 题；${right.label} 是 ` +
        `${right.metrics.firstHalfCount} / ${right.metrics.secondHalfCount}`,
    )
  }

  // ── 建议 ──
  // 只在有明确规范依据时才给方向；否则如实说「按别的标准挑」，不硬造理由。
  let advice: string
  if (!lLen.ok && rLen.ok) {
    advice = `${right.label}的篇幅合规，${left.label}越线了——除非愿意改篇幅，否则用${right.label}。`
  } else if (!rLen.ok && lLen.ok) {
    advice = `${left.label}的篇幅合规，${right.label}越线了——除非愿意改篇幅，否则用${left.label}。`
  } else {
    const lTypes = new Set(left.view.blueprint.items.map((i) => i.type)).size
    const rTypes = new Set(right.view.blueprint.items.map((i) => i.type)).size
    const lTricks = distractionCounts(left.view).reduce((n, d) => n + d.count, 0)
    const rTricks = distractionCounts(right.view).reduce((n, d) => n + d.count, 0)
    if (lTypes !== rTypes) {
      const wider = lTypes > rTypes ? left.label : right.label
      advice = `要考点更杂就用${wider}（覆盖 ${Math.max(lTypes, rTypes)} 类）；两套都合规，按这一讲想练什么挑。`
    } else if (lTricks !== rTricks) {
      const harder = lTricks > rTricks ? left.label : right.label
      advice = `要更难就用${harder}（干扰 ${Math.max(lTricks, rTricks)} 处）；两套都合规，按学生水平挑。`
    } else if (differences.length === 0) {
      advice = '两套在规范维度上几乎一样，按话题内容和语言风格挑即可。'
    } else {
      advice = '两套都合规，差别不影响出题，按话题内容挑即可。'
    }
  }

  return { shared, differences, advice }
}
