/**
 * 定位规则本身。
 *
 * 这一层为什么值得单独测：它是 `backend/deterministic/anchors.py` 的移植，而两边一旦
 * 各写一套判据，页面和后端就会对「这条旁注贴对了没有」给出不同答案。下面每一条都对应
 * 后端那个模块里已有的一条行为，用同一份真实脚本（material_valid）跑。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Blueprint, BlueprintItem } from '@/contracts'
import { BASE_BLUEPRINT, BASE_MATERIAL } from '@/mocks/fixtures'
import {
  anchorHolds,
  findEvidenceTurns,
  locateEvidence,
  reportAnchorProblems,
  resetAnchorReporting,
  resolveAnchors,
} from './anchors'

const TURNS = BASE_MATERIAL.listening_material_parts[0].script.turns

const itemsWith = (patch: Partial<BlueprintItem> & { number: number }): BlueprintItem[] => {
  const bp = structuredClone(BASE_BLUEPRINT) as Blueprint
  const item = bp.items.find((i) => i.number === patch.number)!
  Object.assign(item, patch)
  return bp.items
}

describe('locateEvidence', () => {
  it('returns offsets into the ORIGINAL text when only the case differs', () => {
    const text = "It's Anna Woods."
    const span = locateEvidence(text, "it's anna woods.")!
    expect(span).toEqual({ start: 0, end: 16 })
    // 关键：切出来的是原文，不是小写副本。
    expect(text.slice(span.start, span.end)).toBe("It's Anna Woods.")
  })

  it('keeps the offset correct when the match is not at position 0', () => {
    const text = 'I will give you my mobile number. IT IS 07840051963.'
    const span = locateEvidence(text, 'it is 07840051963.')!
    expect(text.slice(span.start, span.end)).toBe('IT IS 07840051963.')
  })

  it('prefers the exact match, so identical text never goes down the case path', () => {
    // 'a' 出现两次，只有第二个大小写相同。精确匹配优先，所以命中的是那一个。
    const span = locateEvidence('A a', 'a')!
    expect(span).toEqual({ start: 2, end: 3 })
  })

  /**
   * 折叠会改变长度的串（'İ'.toLowerCase() 是两个码元）当作找不到。宁可少一条旁注，也不能
   * 把一个错位的下标当成对的——那正是「旁注贴在错的句子上」的字符级版本。
   */
  it('refuses a match whose lowercase form changes length', () => {
    expect(locateEvidence('the İstanbul office', 'i̇stanbul')).toBeNull()
  })

  it('ignores an empty or whitespace-only evidence', () => {
    expect(locateEvidence('anything', '')).toBeNull()
    expect(locateEvidence('anything', '   ')).toBeNull()
  })
})

describe('anchorHolds / findEvidenceTurns', () => {
  it('holds for every item of the aligned blueprint', () => {
    for (const item of BASE_BLUEPRINT.items) {
      expect(anchorHolds(TURNS, item.turn_index, item.evidence), `item ${item.number}`).toBe(true)
    }
  })

  it('is case-insensitive, exactly like anchor_ok and _carries', () => {
    expect(anchorHolds(TURNS, 4, "IT'S ANNA WOODS.")).toBe(true)
  })

  /** 旁白不是合格落点：contract 要求锚点指向非 speaker1 轮，答案不能出自旁白。 */
  it('never holds on a narrator turn, even when the text matches', () => {
    expect(TURNS[0]!.speaker).toBe('speaker1')
    expect(TURNS[0]!.text).toContain('Part one.')
    expect(anchorHolds(TURNS, 0, 'Part one.')).toBe(false)
    expect(findEvidenceTurns(TURNS, 'Part one.')).toEqual([])
  })

  it('rejects an out-of-range or non-integer index instead of throwing', () => {
    expect(anchorHolds(TURNS, 999, "It's Anna Woods.")).toBe(false)
    expect(anchorHolds(TURNS, -1, "It's Anna Woods.")).toBe(false)
    expect(anchorHolds(TURNS, 4.5, "It's Anna Woods.")).toBe(false)
  })

  it('lists every eligible turn carrying the evidence', () => {
    expect(findEvidenceTurns(TURNS, "It's BT14 9BJ.")).toEqual([10])
    expect(findEvidenceTurns(TURNS, 'do you have').length).toBeGreaterThan(1)
    expect(findEvidenceTurns(TURNS, 'nowhere in this script')).toEqual([])
  })
})

describe('resolveAnchors', () => {
  it('leaves a correct blueprint completely alone', () => {
    const r = resolveAnchors(TURNS, BASE_BLUEPRINT.items)
    expect(r.repairs).toEqual([])
    expect(r.omissions).toEqual([])
    expect(r.placements).toHaveLength(10)
    for (const p of r.placements) {
      const item = BASE_BLUEPRINT.items.find((i) => i.number === p.itemNumber)!
      expect(p.turnIndex).toBe(item.turn_index)
      expect(TURNS[p.turnIndex]!.text.slice(p.span.start, p.span.end)).toBe(item.evidence)
    }
  })

  /** 恰好一处命中 → 挪正。后端 `repair_anchors` 的 `len(hits) == 1` 分支。 */
  it('relocates when the evidence sits in exactly one turn', () => {
    const r = resolveAnchors(TURNS, itemsWith({ number: 3, turn_index: 14 }))
    expect(r.omissions).toEqual([])
    expect(r.repairs).toEqual([
      { itemNumber: 3, declaredTurnIndex: 14, turnIndex: 10, evidence: "It's BT14 9BJ." },
    ])
    expect(r.placements.find((p) => p.itemNumber === 3)!.turnIndex).toBe(10)
  })

  /** 零处命中 → 不猜。 */
  it('omits when the evidence is nowhere in the script', () => {
    const r = resolveAnchors(TURNS, itemsWith({ number: 3, evidence: 'not in this script' }))
    expect(r.repairs).toEqual([])
    expect(r.omissions.map((o) => [o.itemNumber, o.reason])).toEqual([[3, 'not-found']])
    expect(r.placements).toHaveLength(9)
  })

  /**
   * 两处以上命中 → 不猜。后端注释写得很清楚：重复出现的句子正是锚点存在的理由，命中多轮时
   * 猜一个等于把锚点的全部价值丢掉，而且是悄悄丢掉。
   */
  it('omits rather than guessing when the evidence matches several turns', () => {
    const r = resolveAnchors(
      TURNS,
      itemsWith({ number: 3, turn_index: 0, evidence: 'do you have' }),
    )
    expect(r.repairs).toEqual([])
    expect(r.omissions).toHaveLength(1)
    const o = r.omissions[0]!
    expect(o.reason).toBe('ambiguous')
    expect(o.matches.length).toBeGreaterThan(1)
    // 命中的轮次都留在记录里，开发者一眼看出是哪几句重复了。
    for (const t of o.matches) expect(TURNS[t]!.text.toLowerCase()).toContain('do you have')
  })

  /** 声明的位置成立时不去别处找，即使 evidence 在别处也出现过。 */
  it('does not touch an anchor that already holds', () => {
    const items = itemsWith({ number: 3, turn_index: 10 })
    const r = resolveAnchors(TURNS, items)
    expect(r.repairs).toEqual([])
    expect(r.placements.find((p) => p.itemNumber === 3)!.turnIndex).toBe(10)
  })

  it('omits an item whose evidence is empty rather than matching everything', () => {
    const r = resolveAnchors(TURNS, itemsWith({ number: 5, evidence: '   ' }))
    expect(r.omissions.map((o) => o.itemNumber)).toEqual([5])
  })
})

/**
 * 开发者通道。用户看不到定位问题（客户的底线），但一条挪不了的锚点说明我们自己的流水线产出了
 * 自相矛盾的构件——全方向咽下去就再没人会发现。所以这里钉的是「确实有人被告知了」。
 */
describe('reportAnchorProblems', () => {
  beforeEach(() => {
    resetAnchorReporting()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('warns a developer about an omission, naming the material', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    reportAnchorProblems({
      materialId: 'mat-x',
      anchorRepairs: [],
      anchorOmissions: [
        { itemNumber: 3, declaredTurnIndex: 14, reason: 'not-found', evidence: 'x', matches: [] },
      ],
    })
    expect(warn).toHaveBeenCalledTimes(1)
    const message = String(warn.mock.calls[0]![0])
    expect(message).toContain('mat-x')
    // 说清了「只影响显示」，免得下一个人以为 blueprint 被改了。
    expect(message).toContain('still has all ten items')
  })

  it('logs a repair at debug level, not as a warning', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    reportAnchorProblems({
      materialId: 'mat-y',
      anchorRepairs: [{ itemNumber: 3, declaredTurnIndex: 14, turnIndex: 10, evidence: 'x' }],
      anchorOmissions: [],
    })
    expect(debug).toHaveBeenCalledTimes(1)
    expect(warn).not.toHaveBeenCalled()
  })

  /** view 会随每次勾选重新 join，所以同一套材料不能刷满控制台。 */
  it('says it once per material, however often the view is rebuilt', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const view = {
      materialId: 'mat-z',
      anchorRepairs: [],
      anchorOmissions: [
        { itemNumber: 3, declaredTurnIndex: 14, reason: 'not-found' as const, evidence: 'x', matches: [] },
      ],
    }
    reportAnchorProblems(view)
    reportAnchorProblems(view)
    reportAnchorProblems(view)
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('stays silent when nothing needed fixing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    reportAnchorProblems({ materialId: 'ok', anchorRepairs: [], anchorOmissions: [] })
    expect(warn).not.toHaveBeenCalled()
    expect(debug).not.toHaveBeenCalled()
  })
})
