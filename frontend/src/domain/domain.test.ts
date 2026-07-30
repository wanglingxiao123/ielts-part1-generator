import { describe, expect, it } from 'vitest'
import { FALLBACK_CONFIG } from '@/config/runtimeConfig'
import { displayTurns, joinFromRecord } from './joinArtifacts'
import { previewSummary } from './cardPreview'
import { computeDistribution } from './distribution'
import { summariseExamPoints } from './examPoints'
import { analyseFormGroups } from './formGroups'
import { buildFacts, compareCandidates } from './compare'
import { assessUsability } from './usability'
import { CONTENT_RULES, contentFacts, distractionMap, distractionOf } from './pointFacts'
import { buildPlaylist, entryForTurn, nextPlayable } from './playlist'
import { buildRecord, mockManifest } from '@/mocks/fixtures'
import type { Blueprint } from '@/contracts'

const T = FALLBACK_CONFIG.thresholds
const O = { batchId: 'b1', scenarioKey: 'accommodation-rental', index: 0 }

const balanced = joinFromRecord(buildRecord('balanced', { ...O, materialId: 'm-bal' }))
const clustered = joinFromRecord(buildRecord('clustered', { ...O, materialId: 'm-clu' }))
const failed = joinFromRecord(buildRecord('failed', { ...O, materialId: 'm-fail' }))
const mismatch = joinFromRecord(buildRecord('anchorMismatch', { ...O, materialId: 'm-mis' }))
const caseDiffers = joinFromRecord(
  buildRecord('anchorCaseDiffers', { ...O, materialId: 'm-case' }),
)
const unresolvable = joinFromRecord(
  buildRecord('anchorUnresolvable', { ...O, materialId: 'm-unres' }),
)

/**
 * 换一份 blueprint 之后必须**重新 join**，不能只把 blueprint 字段替换掉。
 *
 * 分布指标现在读的是 ViewTurn 上已经解出来的位置（见 distribution.ts），而 `turns` 是
 * join 出来的——只换 blueprint 字段就会让两者对不上，测出来的是一个页面上不可能出现的状态。
 */
const viewWith = (blueprint: Blueprint, materialId = 'm-var') =>
  joinFromRecord({ ...buildRecord('balanced', { ...O, materialId }), blueprint })

describe('joinArtifacts', () => {
  it('anchors all ten annotations to the turn_index the blueprint states', () => {
    for (const item of balanced.blueprint.items) {
      const turn = balanced.turns[item.turn_index]
      expect(turn).toBeDefined()
      expect(turn!.items.map((i) => i.number)).toContain(item.number)
    }
  })

  it('highlights evidence as character ranges inside the anchored turn', () => {
    for (const item of balanced.blueprint.items) {
      const turn = balanced.turns[item.turn_index]!
      const covering = turn.highlights.find((h) => h.itemNumbers.includes(item.number))
      expect(covering, `item ${item.number}`).toBeDefined()
      expect(turn.text.slice(covering!.start, covering!.end)).toContain(item.evidence)
    }
  })

  it('excludes narration from dialogueOrdinal', () => {
    const narrators = balanced.turns.filter((t) => t.speaker === 'speaker1')
    expect(narrators.length).toBe(3)
    for (const n of narrators) expect(n.dialogueOrdinal).toBeNull()
    expect(balanced.dialogueTurnCount).toBe(balanced.turns.length - 3)
  })

  /**
   * 这条断言原来钉的是相反的行为：「报出失配、绝不按字符串搜索挪正」。客户的规则把它推翻了
   *
   *   > 如果能程序化定位并修正（如锚点偏移一位）→ 修好再返回，用户不需要知道
   *
   * 挪正的判据不是「按字符串搜索」这么宽——它就是 `deterministic/anchors.py` 那一条：
   * evidence **恰好只在一轮**里出现才挪，零处或两处以上不猜。所以旧断言里真正要守的东西
   * （不许瞎猜、不许把旁注挪到一个碰巧撞上的句子边上）一条没丢，只是「确定的那一种」现在
   * 归入修好而不是报警。
   */
  it('silently relocates an anchor whose evidence sits in exactly one other turn', () => {
    // blueprint_bad_anchor 把第 3 题标在 turn 14，而 "It's BT14 9BJ." 只在 turn 10 出现。
    expect(mismatch.anchorRepairs).toHaveLength(1)
    const r = mismatch.anchorRepairs[0]!
    expect(r.itemNumber).toBe(3)
    expect(r.declaredTurnIndex).toBe(14)
    expect(r.turnIndex).toBe(10)
    expect(mismatch.anchorOmissions).toEqual([])

    // 旁注和高亮都落在真正带着这句话的那一轮，声明的那一轮什么也不留。
    expect(mismatch.turns[10]!.items.map((i) => i.number)).toEqual([3])
    expect(mismatch.turns[14]!.items).toEqual([])
    const h = mismatch.turns[10]!.highlights.find((x) => x.itemNumbers.includes(3))!
    expect(mismatch.turns[10]!.text.slice(h.start, h.end)).toBe("It's BT14 9BJ.")
    // 存档侧不受影响：blueprint 仍是十个点，且 turn_index 一个字没改。
    expect(mismatch.blueprint.items).toHaveLength(10)
    expect(mismatch.blueprint.items.find((i) => i.number === 3)!.turn_index).toBe(14)
  })

  /**
   * 后端两处实现（`validate_part1.py` 的 `anchor_ok`、`anchors.py` 的 `_carries`）都对两侧
   * casefold，所以只差大小写的 evidence 是**合法**的。前端原来用 `indexOf` 精确匹配，把这样
   * 一套材料报成「标错位置」——虚报，而且很可能是常见情形。高亮下标必须仍然对着原文。
   */
  it('accepts an evidence that differs only in case, with offsets into the original text', () => {
    expect(caseDiffers.anchorRepairs).toEqual([])
    expect(caseDiffers.anchorOmissions).toEqual([])
    const item = caseDiffers.blueprint.items.find((i) => i.number === 1)!
    expect(item.evidence).toBe("it's anna woods.") // 小写，和原文首字母不同
    const turn = caseDiffers.turns[item.turn_index]!
    const h = turn.highlights.find((x) => x.itemNumbers.includes(1))!
    // 切出来的是**原文**那一段（大写 I），不是小写副本里的一段，也没有错位。
    expect(turn.text.slice(h.start, h.end)).toBe("It's Anna Woods.")
  })

  /**
   * 挪不了的那一条：evidence 一处都不存在。客户的规则：
   *
   *   > 如果不确定怎么修 → 直接去掉这条旁注，返回干净的材料
   *
   * 关键约束是这只影响**显示**：校验器要求恰好 10 个信息点，所以 blueprint 必须仍是十个。
   */
  it('omits an unresolvable annotation from the display while keeping ten in the blueprint', () => {
    expect(unresolvable.anchorRepairs).toEqual([])
    expect(unresolvable.anchorOmissions).toHaveLength(1)
    const o = unresolvable.anchorOmissions[0]!
    expect(o.itemNumber).toBe(3)
    expect(o.reason).toBe('not-found')

    // 九条旁注照常显示，第 3 条哪里都不出现。
    const shown = unresolvable.turns.flatMap((t) => t.items.map((i) => i.number)).sort((a, b) => a - b)
    expect(shown).toEqual([1, 2, 4, 5, 6, 7, 8, 9, 10])
    for (const turn of unresolvable.turns) {
      for (const h of turn.highlights) expect(h.itemNumbers).not.toContain(3)
    }
    // 存档 / 发布侧不变：十个点，`validate_part1.py` 的 `len(items) != 10` 仍然满足。
    expect(unresolvable.blueprint.items).toHaveLength(10)
    expect(unresolvable.blueprint.items.map((i) => i.number)).toContain(3)
  })

  /** evidence 命中两轮以上时不猜——重复出现的句子正是锚点存在的理由。 */
  it('refuses to guess when the evidence matches more than one turn', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const item = bp.items.find((i) => i.number === 6)!
    item.evidence = 'do you have' // turn 17 / 22 / 28 / 31 都有
    item.turn_index = 0 // 旁白，因此声明的位置本身也不成立
    const view = viewWith(bp, 'm-amb')
    expect(view.anchorRepairs).toEqual([])
    expect(view.anchorOmissions).toHaveLength(1)
    expect(view.anchorOmissions[0]!.reason).toBe('ambiguous')
    expect(view.anchorOmissions[0]!.matches.length).toBeGreaterThan(1)
    expect(view.blueprint.items).toHaveLength(10)
  })

  /** 指向旁白的锚点不成立：contract 要求锚点指向非 speaker1 轮，答案不能出自旁白。 */
  it('does not accept a narrator turn as an anchor', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const item = bp.items.find((i) => i.number === 2)!
    item.turn_index = 0
    item.evidence = 'Part one.' // 只在旁白 turn 0 里出现
    const view = viewWith(bp, 'm-narr')
    expect(view.anchorOmissions.map((o) => o.itemNumber)).toEqual([2])
    expect(view.turns[0]!.items).toEqual([])
  })

  it('needs neither a repair nor an omission on the balanced or clustered fixtures', () => {
    for (const view of [balanced, clustered]) {
      expect(view.anchorRepairs).toEqual([])
      expect(view.anchorOmissions).toEqual([])
    }
  })

  /**
   * 挪正之后，「这个点在原文哪一句」必须只有一个答案。
   *
   * 每一处坐标都走 `displayTurns`，因为直接读 `blueprint.items[].turn_index` 的地方会把点画在
   * 声明的老位置上：分布图的点、form_group 括号、考点小结的跳转按钮就会各指一句，而这三样都
   * 声称说的是同一件事。这条断言钉的就是它们不可能打架。
   */
  it('gives every channel one and the same location for a relocated point', () => {
    const shown = displayTurns(mismatch)
    expect(shown.get(3)).toBe(10) // 不是 blueprint 写的 14

    // 分布图。
    const d = computeDistribution(mismatch, T)
    expect(d.points.find((p) => p.number === 3)!.turnIndex).toBe(10)
    // 考点小结的跳转坐标。
    expect(summariseExamPoints(mismatch).turnOf[3]).toBe(10)
    // form_group 括号：第 3 题所在那一组的起止轮次里不许再出现 14。
    const group = analyseFormGroups(mismatch, T).groups.find((g) => g.numbers.includes(3))!
    expect(group.turnStart).toBeLessThanOrEqual(10)
    expect(group.turnEnd).toBeLessThan(14)

    // 解不出来的点不进这张表：它在页面上不显示，也就没有可跳转的位置。
    expect(displayTurns(unresolvable).has(3)).toBe(false)
  })

  it('joins findings and blind map entries by turn_index', () => {
    expect(clustered.turns[27]!.findings.map((f) => f.severity)).toEqual(['major'])
    expect(balanced.turns[4]!.blindHits.map((b) => b.seq)).toEqual([1])
  })
})

describe('distribution', () => {
  const dBal = computeDistribution(balanced, T)
  const dClu = computeDistribution(clustered, T)

  it('computes gaps including the leading and trailing boundary', () => {
    expect(dBal.gaps).toEqual([3, 4, 2, 2, 8, 8, 3, 3, 2, 3, 1])
    expect(dBal.gaps.length).toBe(11)
    expect(dClu.gaps).toEqual([3, 4, 2, 2, 0, 14, 0, 2, 8, 3, 1])
  })

  it('scores the clustered variant markedly less uniform', () => {
    expect(dBal.cv).toBeCloseTo(0.629, 2)
    expect(dClu.cv).toBeCloseTo(1.105, 2)
    expect(dBal.uniformity).toBeGreaterThan(dClu.uniformity + 20)
    expect(dBal.cvWarn).toBe(false)
    expect(dClu.cvWarn).toBe(true)
  })

  it('reports max gap and its position', () => {
    expect(dBal.maxGap).toBe(8)
    expect(dClu.maxGap).toBe(14)
    expect(dClu.gaps[dClu.maxGapIndex]).toBe(14)
  })

  it('detects the 6/7/8 cluster only in the clustered fixture', () => {
    expect(dBal.clusters).toEqual([])
    expect(dClu.clusters).toHaveLength(1)
    const c = dClu.clusters[0]!
    expect(c.numbers).toEqual([6, 7, 8])
    expect(c.turnStart).toBe(27)
    expect(c.turnEnd).toBe(29)
    expect(dClu.notes.some((n) => n.includes('turn 27–29'))).toBe(true)
  })

  it('reports front/back balance from split_after', () => {
    expect(dBal.splitAfter).toBe(5)
    expect(dBal.firstHalfCount).toBe(5)
    expect(dBal.secondHalfCount).toBe(5)
    expect(dBal.balanced).toBe(true)
  })

  it('judges "all ten in the middle, eight empty turns at each end" uneven', () => {
    // The point of including boundary gaps: point-to-point gaps here are a
    // perfectly even 1,1,1..., yet the material is badly distributed.
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const middle = [14, 15, 16, 17, 18, 19, 20, 22, 23, 24]
    bp.items.forEach((item, i) => {
      item.turn_index = middle[i]!
      item.evidence = balanced.turns[middle[i]!]!.text
    })
    const d = computeDistribution(viewWith(bp), T)
    expect(d.gaps[0]).toBeGreaterThanOrEqual(8)
    expect(d.gaps[d.gaps.length - 1]!).toBeGreaterThanOrEqual(8)
    expect(d.cvWarn).toBe(true)
    expect(d.uniformity).toBeLessThan(dBal.uniformity)
  })

  /**
   * 原来这条把 `turn_index` 改成 999 就算「定位不到」。现在不算了：evidence 还在
   * turn 4 好好地待着，恰好只有一处，所以那是能确定挪正的一种，页面会静默挪回去
   * （domain/anchors.ts，与后端同一条规则）。真正定位不到的是 evidence 本身不存在。
   */
  it('excludes a genuinely unresolvable anchor from the metrics rather than crashing', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.items[0]!.evidence = 'a sentence that appears nowhere in this script'
    const d = computeDistribution(viewWith(bp), T)
    expect(d.unplacedNumbers).toEqual([1])
    expect(d.points).toHaveLength(9)
    expect(d.notes.some((n) => n.includes('锚点无法定位'))).toBe(true)
  })

  /** 一个只是下标写歪了的锚点会被挪回去，因此不进 unplacedNumbers。 */
  it('counts a relocatable anchor as placed, at the turn that carries the evidence', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.items[0]!.turn_index = 999
    const d = computeDistribution(viewWith(bp), T)
    expect(d.unplacedNumbers).toEqual([])
    expect(d.points.find((p) => p.number === 1)!.turnIndex).toBe(4)
  })

  /**
   * Linearity (spec §4B-2 线性顺序性). Both fixtures are in order; the check has
   * to be able to see a violation, or it is decoration.
   */
  it('reports no jump-back on either fixture', () => {
    expect(dBal.outOfOrder).toEqual([])
    expect(dClu.outOfOrder).toEqual([])
  })

  it('catches a question whose information is spoken after a later question', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    // Swap points 2 and 3: #3 now speaks at turn 8, #2 at turn 10 → 题号回跳.
    const i2 = bp.items.find((i) => i.number === 2)!
    const i3 = bp.items.find((i) => i.number === 3)!
    i2.turn_index = 10
    i2.evidence = balanced.turns[10]!.text
    i3.turn_index = 8
    i3.evidence = balanced.turns[8]!.text
    const d = computeDistribution(viewWith(bp), T)
    expect(d.outOfOrder).toEqual([
      { spokenFirst: 3, spokenSecond: 2, turnFirst: 8, turnSecond: 10 },
    ])
    expect(d.notes.some((n) => n.includes('题号回跳'))).toBe(true)
  })

  it('does not call two points on one turn a jump-back', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    // #5 moved onto #4's turn: same ordinal, so there is no audible reordering.
    const i5 = bp.items.find((i) => i.number === 5)!
    i5.turn_index = 12
    i5.evidence = balanced.turns[12]!.text
    expect(computeDistribution(viewWith(bp), T).outOfOrder).toEqual([])
  })
})

/**
 * The reviewer-facing translation layer. These assertions are about the WORDS a
 * question-writer reads, because that is the thing that was broken: the metrics
 * were correct all along and still unusable.
 */
describe('usability verdict', () => {
  const vBal = assessUsability(computeDistribution(balanced, T))
  const vClu = assessUsability(computeDistribution(clustered, T))

  it('reads out a conclusion, never a coefficient', () => {
    const all = [vBal, vClu].flatMap((v) => [v.headline, ...v.checks.map((c) => c.detail)])
    for (const text of all) {
      for (const jargon of ['CV', '均匀度', 'uniformity', '间隔', '阈值']) {
        expect(text, text).not.toContain(jargon)
      }
    }
  })

  it('turns the 6/7/8 cluster into "考生来不及记" rather than a cluster count', () => {
    const pace = vClu.checks.find((c) => c.key === 'pace')!
    expect(pace.level).toBe('needsWork')
    expect(pace.detail).toContain('⑥⑦⑧ 挤在 turn 27–29')
    expect(pace.detail).toContain('来不及记')
    // The balanced fixture has no cluster, so its pace check must pass — the two
    // must not both read as "有问题" or the signal is worthless.
    expect(vBal.checks.find((c) => c.key === 'pace')!.level).toBe('ready')
  })

  it('blocks question writing when the question order jumps back', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const i2 = bp.items.find((i) => i.number === 2)!
    const i3 = bp.items.find((i) => i.number === 3)!
    i2.turn_index = 10
    i2.evidence = balanced.turns[10]!.text
    i3.turn_index = 8
    i3.evidence = balanced.turns[8]!.text
    const v = assessUsability(computeDistribution(viewWith(bp), T))
    expect(v.level).toBe('blocked')
    expect(v.headline).toContain('暂不能直接出题')
    expect(v.checks.find((c) => c.key === 'order')!.detail).toContain('题号回跳')
  })

  /**
   * 原来这里钉的是「有点定位不到时要多出一行『信息点定位』」。客户的规则把这一行整个否掉了：
   * 锚点对不上是我们自己的标注 bug，不是材料的质量问题，不该拿给用户看，更不该让他自查
   * （「不要把『我可能标错了你自己检查一下』这种话展示给用户」）。
   *
   * 原意图仍然守着——「不为常态挂一行」：定位正常时本来就没有这一行。变的是异常时也没有这一行，
   * 因为异常已经在显示层处理掉了（挪正或不显示），而信号走开发者通道。
   */
  it('never says anything about anchors, placed or not', () => {
    expect(vBal.checks.map((c) => c.key)).toEqual(['order', 'pace', 'coverage', 'groups'])

    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.items[0]!.evidence = 'a sentence that appears nowhere in this script'
    const v = assessUsability(computeDistribution(viewWith(bp), T))
    expect(v.checks.map((c) => c.key)).toEqual(['order', 'pace', 'coverage', 'groups'])
    const text = [v.headline, ...v.checks.map((c) => c.detail)].join(' ')
    for (const forbidden of ['定位', '标错', '锚点', '旁注', '核对']) {
      expect(text, forbidden).not.toContain(forbidden)
    }
  })

  it('agrees with the metrics it was derived from', () => {
    // The whole point of deriving the words from DistributionMetrics: the strip's
    // shading and the sentence beneath it cannot contradict each other.
    const d = computeDistribution(clustered, T)
    const paceProblem = vClu.checks.find((c) => c.key === 'pace')!.level !== 'ready'
    expect(paceProblem).toBe(d.clusters.length > 0 || d.cvWarn)
    const coverageProblem = vClu.checks.find((c) => c.key === 'coverage')!.level !== 'ready'
    expect(coverageProblem).toBe(d.wideGaps.length > 0)
  })
})

/** 干扰机制必须说出是哪一种（规范 §4B-4），而不是一个 boolean。 */
describe('pointFacts', () => {
  it('resolves each distractor to the mechanism the blueprint declares', () => {
    const bp = balanced.blueprint
    const kinds = distractionMap(bp)
    // #5 is the correction target ("still in primary school"), #7's answer word
    // ("house") is the one referred to indirectly as "the latter".
    expect(kinds.get(5)).toBe('correction')
    expect(kinds.get(7)).toBe('paraphrase')
    // Non-distractors get no entry at all — 「非干扰」 is not a finding.
    for (const item of bp.items.filter((i) => !i.distractor)) {
      expect(kinds.has(item.number)).toBe(false)
    }
  })

  it('falls back to 干扰 when a distractor matches neither declared mechanism', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const item = bp.items.find((i) => i.number === 9)!
    expect(item.distractor).toBe(true) // declared, but not the correction/paraphrase one
    expect(distractionOf(item, bp)).toBe('unspecified')
  })

  it('counts the content requirements the spec states, not invented ones', () => {
    const f = contentFacts(balanced.blueprint)
    expect(f.spellingNumbers).toEqual([1]) // §3: ≥1 处姓名拼读
    expect(f.spellingUnconfirmed).toEqual([]) // 拼读点已被复述确认
    expect(f.confirmedNumbers.length).toBeGreaterThanOrEqual(CONTENT_RULES.MIN_CONFIRMED)
    expect(f.distractions.length).toBeGreaterThanOrEqual(CONTENT_RULES.MIN_DISTRACTORS)
    expect(f.typeKindCount).toBeGreaterThanOrEqual(CONTENT_RULES.MIN_TYPE_KINDS)
  })
})

/**
 * 考点小结（examPoints）。客户要的是「把拼读、先说后改、同义替换这些考点抽取出来」，所以这一层
 * 唯一要证明的事是：它归拢的是规范里已有的考点，且判据仍然是现成的那几个，没有新造。
 */
describe('examPoints', () => {
  it("names the spec's own exam points, reusing the existing predicates", () => {
    const s = summariseExamPoints(balanced)
    const labels = s.blocks.map((b) => b.label)
    // §3 的拼读、§4B-4 的两种已声明机制。措辞与旁注、结果卡完全一致。
    expect(labels).toContain('拼读')
    expect(labels).toContain('先说后改')
    expect(labels).toContain('同义替换')
    expect(labels).toContain('有复述确认')

    // 点号直接取自 pointFacts / blueprint，不是这一层重算的。
    const facts = contentFacts(balanced.blueprint)
    expect(s.blocks.find((b) => b.label === '拼读')!.numbers).toEqual(facts.spellingNumbers)
    expect(s.blocks.find((b) => b.label === '先说后改')!.numbers).toEqual([5])
    expect(s.blocks.find((b) => b.label === '同义替换')!.numbers).toEqual([7])
    // 每个点号都带得到跳转坐标，否则小结只是一串不能行动的数字。
    for (const block of s.blocks) {
      for (const n of block.numbers) expect(block.turnOf[n]).toBeTypeOf('number')
    }
  })

  it("covers the eight detail types in the spec's order and counts the kinds", () => {
    const s = summariseExamPoints(balanced)
    expect(s.typeKindCount).toBe(contentFacts(balanced.blueprint).typeKindCount)
    // §4B-3 的表格顺序，姓名在最前——固定顺序才看得出缺哪一类。
    expect(s.typeCoverage[0]!.type).toBe('name')
    const listed = s.typeCoverage.flatMap((r) => r.numbers).sort((a, b) => a - b)
    expect(listed).toEqual(balanced.blueprint.items.map((i) => i.number).sort((a, b) => a - b))
  })

  it('keeps blind-audit conclusions out of the summary entirely', () => {
    // `clustered` 用的是 CROSS_CHECK_WITH_GAP：盲评没能复原第 5 题。这一块曾经渲染成红色
    // 「听不出来」，现在一个都不该出现——考点小结只说「这套材料具备什么」，而「据此能不能出题」
    // 是另一层判断，且它可能来自我们自己的对照 bug（HGR482）。
    const labels = summariseExamPoints(clustered).blocks.map((b) => b.label)
    expect(labels).not.toContain('听不出来')
    expect(labels).not.toContain('听着有歧义')
    // 连带的口径也不出现：没有计数，也没有把盲评说成结论的措辞。
    for (const block of summariseExamPoints(clustered).blocks) {
      expect(block.label).not.toMatch(/计划|听出\s*\d|\d+\s*个/)
      expect(block.tone).not.toBe('bad')
    }
    // 但盲评本身没有被削弱：`unrecoverable` 仍在 view 上，仍进后端的修改指令。
    expect(clustered.crossCheck.unrecoverable.map((r) => r.number)).toEqual([5])
  })

  it('shares its headline with the result card, so the two cannot disagree', () => {
    expect(summariseExamPoints(balanced).headline).toBe(previewSummary(balanced))
  })
})

describe('formGroups', () => {
  const gBal = analyseFormGroups(balanced, T)
  const gClu = analyseFormGroups(clustered, T)

  it('groups by (item_form, form_group) composite key', () => {
    const a = gBal.groups.find((g) => g.name === 'A')!
    expect(a.itemForm).toBe('form')
    expect(a.numbers).toEqual([1, 2, 3, 4])
    expect(a.canFormQuestion).toBe(true)
    const b = gBal.groups.find((g) => g.name === 'B')!
    expect(b.numbers).toEqual([8, 9, 10])
    expect(b.turnSpan).toBe(5)
    expect(b.spanWarn).toBe(false)
  })

  it('widens the table group span when its first point is pulled into the cluster', () => {
    const b = gClu.groups.find((g) => g.name === 'B')!
    expect(b.turnSpan).toBe(11) // 29 → 40, vs 5 when balanced
    expect(b.spanWarn).toBe(false) // still under GROUP_SPAN_WARN = 12
  })

  it('flags a group whose turn span exceeds GROUP_SPAN_WARN', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const item8 = bp.items.find((i) => i.number === 8)!
    item8.turn_index = 24
    item8.evidence = balanced.turns[24]!.text
    const g = analyseFormGroups(viewWith(bp), T)
    const b = g.groups.find((x) => x.name === 'B')!
    expect(b.turnSpan).toBe(16)
    expect(b.spanWarn).toBe(true)
  })

  it('confirms question_type_coverage covers 1..10 and agrees with item_form', () => {
    expect(gBal.consistency.coversAllTen).toBe(true)
    expect(gBal.consistency.disagreeingNumbers).toEqual([])
    expect(gBal.consistency.consistent).toBe(true)
    expect(gBal.hasViableQuestionGroup).toBe(true)
    expect(gBal.multipleChoiceCount).toBe(2)
  })

  it('names the specific item numbers when the two views disagree', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.items[9]!.item_form = 'note' // coverage still lists 10 under `table`
    const g = analyseFormGroups(viewWith(bp), T)
    expect(g.consistency.disagreeingNumbers).toEqual([10])
    expect(g.consistency.consistent).toBe(false)
    // Coverage flattening itself is still complete; only the views disagree.
    expect(g.consistency.coversAllTen).toBe(true)
  })

  it('detects a missing number in question_type_coverage', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.question_type_coverage.note = []
    const g = analyseFormGroups(viewWith(bp), T)
    expect(g.consistency.missingNumbers).toEqual([5])
    expect(g.consistency.coversAllTen).toBe(false)
  })

  /**
   * Regression from live output. Real blueprints leave `form_group: null` on
   * standalone points, so the null bucket collects points that were never
   * claimed to belong together. Treating that bucket as a group flagged a
   * turn-7 and a turn-20 multiple choice as "跨度过大 · 不足以单独成题" — a
   * fabricated defect. Every fixture had at most one null-group point per
   * item_form, so the bucket never held two and the bug stayed invisible.
   */
  it('does not judge span or viability for form_group=null points', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    // Two ungrouped multiple_choice points 20 turns apart, as real output has.
    const mc = bp.items.filter((i) => i.item_form === 'multiple_choice')
    expect(mc.length).toBeGreaterThanOrEqual(2)
    for (const i of mc) i.form_group = null
    mc[0]!.turn_index = 7
    mc[0]!.evidence = balanced.turns[7]!.text
    mc[1]!.turn_index = 27
    mc[1]!.evidence = balanced.turns[27]!.text

    const g = analyseFormGroups(viewWith(bp), T)
    const nullBucket = g.groups.find((x) => x.itemForm === 'multiple_choice')!
    expect(nullBucket.ungrouped).toBe(true)
    expect(nullBucket.turnSpan).toBe(20) // still reported as raw data
    expect(nullBucket.spanWarn).toBe(false) // but NOT flagged as a defect
    expect(nullBucket.canFormQuestion).toBe(false)
  })

  it('still flags span on a DECLARED group of the same shape', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    const mc = bp.items.filter((i) => i.item_form === 'multiple_choice')
    for (const i of mc) i.form_group = 'C'
    mc[0]!.turn_index = 7
    mc[0]!.evidence = balanced.turns[7]!.text
    mc[1]!.turn_index = 27
    mc[1]!.evidence = balanced.turns[27]!.text
    const g = analyseFormGroups(viewWith(bp), T)
    const declared = g.groups.find((x) => x.name === 'C')!
    expect(declared.ungrouped).toBe(false)
    expect(declared.spanWarn).toBe(true)
  })
})

describe('compare', () => {
  const factsFor = (view: typeof balanced, label: string) =>
    buildFacts(label, view, computeDistribution(view, T), analyseFormGroups(view, T))

  it('prefers the material without unrecoverable points even when it scores lower', () => {
    // A = balanced, 88 分, 0 unrecoverable. B = higher score but 1 unrecoverable.
    const a = factsFor(balanced, '候选 A')
    const b = { ...factsFor(clustered, '候选 B'), unrecoverable: 1, total: 92 }
    const r = compareCandidates(a, b, T)
    expect(r.lean).toBe('A')
    expect(r.decidedBy).toBe(1)
    // The reason names the CONSEQUENCE ("写不成题"), not the internal term
    // 不可回收点 — that phrase meant nothing to the reviewer.
    expect(r.summary).toContain('没听出来，写不成题')
    expect(r.summary).toContain('倾向 候选 A')
  })

  it('labels a sub-threshold score gap as not significant and does not decide on it', () => {
    const a = factsFor(balanced, '候选 A')
    const b = { ...factsFor(balanced, '候选 B'), total: a.total + 4 }
    const r = compareCandidates(a, b, T)
    expect(r.scoreDiff).toBe(-4)
    expect(r.scoreDiffSignificant).toBe(false)
    expect(r.reasons.some((x) => x.includes('分差不显著'))).toBe(true)
    expect(r.lean).toBe('tie')
  })

  it('falls through to distribution when the fatal signals tie', () => {
    const r = compareCandidates(factsFor(balanced, 'A'), factsFor(clustered, 'B'), T)
    expect(r.lean).toBe('A')
    // clustered has 1 unrecoverable in its cross_check fixture, so priority 1
    // decides; assert the distribution reason is still surfaced — and phrased as
    // what it costs the candidate, not as a CV comparison.
    expect(r.reasons.some((x) => x.includes('铺得更开，考生有时间记录'))).toBe(true)
    expect(r.reasons.some((x) => x.includes('信息点连着给'))).toBe(true)
    expect(r.reasons.every((x) => !x.includes('CV'))).toBe(true)
  })

  it('lists only dimensions differing by at least DIMENSION_DIFF_SHOWN', () => {
    const r = compareCandidates(factsFor(balanced, 'A'), factsFor(clustered, 'B'), T)
    for (const d of r.dimensionDeltas) {
      expect(Math.abs(d.delta)).toBeGreaterThanOrEqual(T.DIMENSION_DIFF_SHOWN)
    }
    expect(r.dimensionDeltas.map((d) => d.key)).toContain('information_map_quality')
    expect(r.hiddenDimensionCount).toBeGreaterThan(0)
  })

  it('counts defects by severity', () => {
    const f = factsFor(failed, 'F')
    expect(f.defects).toEqual({ critical: 1, major: 1, minor: 0 })
  })
})

describe('playlist', () => {
  const manifest = mockManifest('m-bal', (i) => `blob:seg-${i}`)
  const playlist = buildPlaylist(manifest)

  it('accumulates the timeline from duration_ms + gap_after_ms', () => {
    expect(playlist.entries).toHaveLength(manifest.segments.length)
    expect(playlist.entries[0]!.startMs).toBe(0)
    for (let i = 1; i < playlist.entries.length; i += 1) {
      const prev = playlist.entries[i - 1]!
      expect(playlist.entries[i]!.startMs).toBe(prev.endMs + prev.gapAfterMs)
    }
    expect(playlist.totalMs).toBe(playlist.declaredTotalMs)
  })

  it('maps turn_index to segment in both directions with no gaps in numbering', () => {
    expect(playlist.orderingProblems).toEqual([])
    expect(entryForTurn(playlist, 7)!.segmentIndex).toBe(7)
    expect(playlist.entries[7]!.turnIndex).toBe(7)
  })

  it('marks a url:null segment unplayable and skips it', () => {
    expect(playlist.unplayableTurnIndexes).toEqual([30])
    expect(playlist.entries[30]!.playable).toBe(false)
    expect(nextPlayable(playlist, 30)!.turnIndex).toBe(31)
  })

  it('reports ordering problems instead of silently misaligning', () => {
    const broken = structuredClone(manifest)
    broken.segments.splice(5, 2)
    const p = buildPlaylist(broken)
    expect(p.orderingProblems.length).toBeGreaterThan(0)
  })
})
