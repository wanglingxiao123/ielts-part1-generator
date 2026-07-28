import { describe, expect, it } from 'vitest'
import { FALLBACK_CONFIG } from '@/config/runtimeConfig'
import { joinFromRecord } from './joinArtifacts'
import { computeDistribution } from './distribution'
import { analyseFormGroups } from './formGroups'
import { buildFacts, compareCandidates } from './compare'
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
    expect(r.summary).toContain('不可回收点')
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

  it('falls through to uniformity when the fatal signals tie', () => {
    const r = compareCandidates(factsFor(balanced, 'A'), factsFor(clustered, 'B'), T)
    expect(r.lean).toBe('A')
    // clustered has 1 unrecoverable in its cross_check fixture, so priority 1
    // decides; assert the uniformity reason is still surfaced to the reviewer.
    expect(r.reasons.some((x) => x.includes('分布更均匀'))).toBe(true)
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
