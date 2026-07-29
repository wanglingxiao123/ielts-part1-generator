/**
 * 每个信息点「考什么、答案是什么、难在哪」——旁注要说的三件事。
 *
 * 干扰机制取自 blueprint 自己声明的两个字段，不猜、不新增判据（规范 §4B-4）：
 *   correction{earlier, final, marker} → 先说后改
 *   indirect_confirmation{answer_term, reference_phrase} → 同义替换
 * 只有 `distractor: true` 却对不上这两处的点，才退回笼统的「干扰」——那本身也是
 * 有用的信息：命题人得自己去原文确认干扰是怎么做的。
 */
import type { Blueprint, BlueprintItem } from '@/contracts'

export type DistractionKind = 'correction' | 'paraphrase' | 'unspecified'

export const DISTRACTION_LABEL: Record<DistractionKind, string> = {
  correction: '先说后改',
  paraphrase: '同义替换',
  unspecified: '干扰',
}

export const DISTRACTION_HINT: Record<DistractionKind, string> = {
  correction: '先给一个值再改口，考最终有效答案',
  paraphrase: '用指代/释义绕过答案原词，考生须自己还原',
  unspecified: 'blueprint 标了干扰但未说明机制，请对照原文确认',
}

/** 干扰机制；非干扰点返回 null——「非干扰」不值得占一个 badge。 */
export function distractionOf(
  item: BlueprintItem,
  blueprint: Blueprint,
): DistractionKind | null {
  if (!item.distractor) return null

  // 先说后改：本点的最终值就是 correction 声明的最终值。
  const final = blueprint.correction?.final
  if (final && (item.target === final || item.evidence === final || final.includes(item.target))) {
    return 'correction'
  }

  // 同义替换：本点的答案原词正是被间接指代的那个词（命题铁律要求它在音频里出现过）。
  if (blueprint.indirect_confirmation?.answer_term === item.target) return 'paraphrase'

  return 'unspecified'
}

export function distractionMap(blueprint: Blueprint): Map<number, DistractionKind> {
  const out = new Map<number, DistractionKind>()
  for (const item of blueprint.items) {
    const kind = distractionOf(item, blueprint)
    if (kind) out.set(item.number, kind)
  }
  return out
}

/**
 * 内容层面「够不够出一套题」的清点，用于两套候选并排对比。
 *
 * 这里出现的每个数字都是规范 / blueprint schema 自己写明的要求，不是新造的阈值：
 *   §3 + §6      ≥1 处姓名/专名拼读
 *   §3 + schema  ≥3 个点有复述确认（拼读与数字点尤其必须）
 *   §4B-4        每套 2–3 个循环设置干扰即可，多了会超出 Part 1 的易度定位
 *   §4B-3        八类信息点中至少覆盖 4 类
 */
export interface ContentFacts {
  /** 需拼读的点号。 */
  spellingNumbers: number[]
  /** 需拼读但没人复述的点号——once-only 下几乎必错。 */
  spellingUnconfirmed: number[]
  confirmedNumbers: number[]
  /** 出现过的干扰机制，按点号。 */
  distractions: Array<{ number: number; kind: DistractionKind }>
  /** 用到的干扰机制种类。 */
  distractionKinds: DistractionKind[]
  /** 覆盖到的信息点类型数（八类中的几类）。 */
  typeKindCount: number
}

/** 规范自身写明的数量要求，集中在一处以免各页各写一遍。 */
export const CONTENT_RULES = {
  MIN_SPELLING: 1,
  MIN_CONFIRMED: 3,
  MIN_DISTRACTORS: 2,
  MAX_DISTRACTORS: 3,
  MIN_TYPE_KINDS: 4,
} as const

export function contentFacts(blueprint: Blueprint): ContentFacts {
  const items = blueprint.items
  const spellingNumbers = items.filter((i) => i.type === 'name').map((i) => i.number)
  const distractions = items
    .map((i) => ({ number: i.number, kind: distractionOf(i, blueprint) }))
    .filter((x): x is { number: number; kind: DistractionKind } => x.kind !== null)
  return {
    spellingNumbers,
    spellingUnconfirmed: items
      .filter((i) => i.type === 'name' && !i.confirmed)
      .map((i) => i.number),
    confirmedNumbers: items.filter((i) => i.confirmed).map((i) => i.number),
    distractions,
    distractionKinds: [...new Set(distractions.map((d) => d.kind))],
    typeKindCount: new Set(items.map((i) => i.type)).size,
  }
}
