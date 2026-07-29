import { describe, expect, it } from 'vitest'
import { FALLBACK_CONFIG } from '@/config/runtimeConfig'
import { joinFromRecord } from './joinArtifacts'
import { computeDistribution } from './distribution'
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

  it('reports a mismatched anchor instead of relocating it by string search', () => {
    expect(mismatch.anchorMismatches).toHaveLength(1)
    const m = mismatch.anchorMismatches[0]!
    expect(m.itemNumber).toBe(3)
    expect(m.reason).toBe('evidence-not-in-turn')
    // The item stays anchored where the blueprint said, and no other turn
    // silently acquired the annotation.
    expect(mismatch.turns[14]!.items.map((i) => i.number)).toEqual([3])
    expect(mismatch.turns[10]!.items).toEqual([])
    expect(mismatch.turns[14]!.highlights).toEqual([])
  })

  it('has no anchor mismatch on the balanced or clustered fixtures', () => {
    expect(balanced.anchorMismatches).toEqual([])
    expect(clustered.anchorMismatches).toEqual([])
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
    const view = { ...balanced, blueprint: bp }
    const d = computeDistribution(view, T)
    expect(d.gaps[0]).toBeGreaterThanOrEqual(8)
    expect(d.gaps[d.gaps.length - 1]!).toBeGreaterThanOrEqual(8)
    expect(d.cvWarn).toBe(true)
    expect(d.uniformity).toBeLessThan(dBal.uniformity)
  })

  it('excludes unresolvable anchors from the metrics rather than crashing', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.items[0]!.turn_index = 999
    const d = computeDistribution({ ...balanced, blueprint: bp }, T)
    expect(d.unplacedNumbers).toEqual([1])
    expect(d.points).toHaveLength(9)
    expect(d.notes.some((n) => n.includes('锚点无法定位'))).toBe(true)
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
    const d = computeDistribution({ ...balanced, blueprint: bp }, T)
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
    expect(computeDistribution({ ...balanced, blueprint: bp }, T).outOfOrder).toEqual([])
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
    const v = assessUsability(computeDistribution({ ...balanced, blueprint: bp }, T))
    expect(v.level).toBe('blocked')
    expect(v.headline).toContain('暂不能直接出题')
    expect(v.checks.find((c) => c.key === 'order')!.detail).toContain('题号回跳')
  })

  it('says nothing about anchors when every point is placed', () => {
    // A row reading "10 个点都定位到了" is the kind of noise this redesign removes.
    expect(vBal.checks.some((c) => c.key === 'anchor')).toBe(false)
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.items[0]!.turn_index = 999
    const v = assessUsability(computeDistribution({ ...balanced, blueprint: bp }, T))
    const anchor = v.checks.find((c) => c.key === 'anchor')!
    expect(anchor.level).toBe('blocked')
    expect(anchor.detail).toContain('①')
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
    const g = analyseFormGroups({ ...balanced, blueprint: bp }, T)
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
    const g = analyseFormGroups({ ...balanced, blueprint: bp }, T)
    expect(g.consistency.disagreeingNumbers).toEqual([10])
    expect(g.consistency.consistent).toBe(false)
    // Coverage flattening itself is still complete; only the views disagree.
    expect(g.consistency.coversAllTen).toBe(true)
  })

  it('detects a missing number in question_type_coverage', () => {
    const bp = structuredClone(balanced.blueprint) as Blueprint
    bp.question_type_coverage.note = []
    const g = analyseFormGroups({ ...balanced, blueprint: bp }, T)
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

    const g = analyseFormGroups({ ...balanced, blueprint: bp }, T)
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
    const g = analyseFormGroups({ ...balanced, blueprint: bp }, T)
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
