/**
 * The pure layers behind the results page: selection rules, compare picking,
 * card previews, and the internal-stage → user-facing progression mapping.
 *
 * The page's own DOM assertions live in BatchResults.test.tsx; everything that
 * can be decided without React is decided here, because these are the rules the
 * client stated in words and they should be checkable as rules.
 */
import { describe, expect, it } from 'vitest'
import { FALLBACK_CONFIG } from '@/config/runtimeConfig'
import { buildRecord } from '@/mocks/fixtures'
import { computeDistribution } from './distribution'
import { joinFromRecord } from './joinArtifacts'
import {
  buildCardPreview,
  firstDialogueLine,
  flaggedPointNumbers,
  previewSummary,
} from './cardPreview'
import {
  advancePhase,
  describeProgress,
  PHASE_LABEL,
  PHASE_SEQUENCE,
  phaseOfProgress,
  phaseOfStage,
} from './progressStages'
import {
  comparePairReady,
  EMPTY_PICK,
  evaluateSelection,
  pickForCompare,
  toggleSelection,
} from './selection'

const T = FALLBACK_CONFIG.thresholds
const O = { batchId: 'b1', scenarioKey: 'accommodation-rental', index: 0 }

const previewOf = (kind: 'balanced' | 'clustered' | 'failed' | 'anchorMismatch', id: string) => {
  const record = buildRecord(kind, { ...O, materialId: id })
  const view = joinFromRecord(record)
  return buildCardPreview(record, view, computeDistribution(view, T))
}

/* ── 勾选 + 「每场景至少选 1 套」 ─────────────────────────────────────────── */

describe('selection', () => {
  const byScenario = new Map<string, string[]>([
    ['accommodation-rental', ['m1', 'm2']],
    ['booking-hotel', ['m3', 'm4']],
  ])

  it('counts what is selected and names the scenarios still missing one', () => {
    const rule = evaluateSelection({ byScenario, selected: new Set(['m1']) })
    expect(rule.selectedCount).toBe(1)
    expect(rule.scenariosMissing).toEqual(['booking-hotel'])
    expect(rule.canSubmit).toBe(false)
  })

  it('blocks submission until every scenario has at least one pick', () => {
    expect(evaluateSelection({ byScenario, selected: new Set() }).canSubmit).toBe(false)
    expect(evaluateSelection({ byScenario, selected: new Set(['m1']) }).canSubmit).toBe(false)
    expect(evaluateSelection({ byScenario, selected: new Set(['m1', 'm3']) }).canSubmit).toBe(true)
  })

  it('allows more than one pick per scenario', () => {
    const rule = evaluateSelection({ byScenario, selected: new Set(['m1', 'm2', 'm3']) })
    expect(rule.selectedCount).toBe(3)
    expect(rule.canSubmit).toBe(true)
  })

  /**
   * The rule is "every scenario that HAS materials", not "every scenario the
   * user originally asked for". A scenario whose materials all failed to
   * generate must not deadlock the submit button — the user would have no way
   * out but to re-run the whole batch.
   */
  it('does not deadlock on a scenario that produced no materials', () => {
    const withEmpty = new Map<string, string[]>([
      ['accommodation-rental', ['m1']],
      ['booking-hotel', []],
    ])
    const rule = evaluateSelection({ byScenario: withEmpty, selected: new Set(['m1']) })
    expect(rule.canSubmit).toBe(true)
    expect(rule.scenariosMissing).toEqual([])
  })

  it('toggles without mutating the previous set', () => {
    const first = new Set(['m1'])
    const second = toggleSelection(first, 'm2')
    expect([...first]).toEqual(['m1'])
    expect([...second].sort()).toEqual(['m1', 'm2'])
    expect([...toggleSelection(second, 'm1')]).toEqual(['m2'])
  })
})

/* ── 对比模式的 A / B 点选 ────────────────────────────────────────────────── */

describe('compare picking', () => {
  it('assigns the first click to A and the second to B', () => {
    const a = pickForCompare(EMPTY_PICK, 'm1')
    expect(a).toEqual(['m1', null])
    expect(comparePairReady(a)).toBe(false)
    const ab = pickForCompare(a, 'm2')
    expect(ab).toEqual(['m1', 'm2'])
    expect(comparePairReady(ab)).toBe(true)
  })

  it('replaces B when a third card is clicked, keeping A', () => {
    const ab = pickForCompare(pickForCompare(EMPTY_PICK, 'm1'), 'm2')
    expect(pickForCompare(ab, 'm3')).toEqual(['m1', 'm3'])
  })

  it('deselects a card that is clicked again, promoting B to A', () => {
    const ab = pickForCompare(pickForCompare(EMPTY_PICK, 'm1'), 'm2')
    expect(pickForCompare(ab, 'm1')).toEqual(['m2', null])
    expect(pickForCompare(ab, 'm2')).toEqual(['m1', null])
  })

  it('never reports a pair ready with only one card chosen', () => {
    expect(comparePairReady(EMPTY_PICK)).toBe(false)
    expect(comparePairReady(['m1', null])).toBe(false)
    expect(comparePairReady([null, 'm2'])).toBe(false)
  })
})

/* ── 内部环节 → 用户看到的四段 ────────────────────────────────────────────── */

describe('progress phases', () => {
  it('labels exactly the four phases the client asked for', () => {
    expect(PHASE_SEQUENCE.map((p) => PHASE_LABEL[p])).toEqual(['生成', '校验', '修改', '复评'])
  })

  it('maps every retry stage onto the phase it belongs to, never its own step', () => {
    expect(phaseOfStage('regenerating')).toBe('writing')
    expect(phaseOfStage('refilling')).toBe('writing')
    expect(phaseOfStage('anchors_repaired')).toBe('checking')
    expect(phaseOfStage('audited')).toBe('checking')
  })

  it('leaves the phase untouched for stages that merely re-run another', () => {
    expect(phaseOfStage('infra_retry')).toBeNull()
    expect(phaseOfStage('refill_abandoned')).toBeNull()
    expect(advancePhase('revising', phaseOfStage('infra_retry'))).toBe('revising')
    expect(advancePhase('revising', phaseOfStage('refill_abandoned'))).toBe('revising')
  })

  it('never walks backwards, so a retry reads as continued progress', () => {
    // The real sequence: 生成 → 校验 → (校验没过) 重新生成 → 校验 → 评价.
    const sequence = ['generating', 'validating', 'regenerating', 'validating', 'auditing']
    let phase = advancePhase(null, phaseOfStage('queued'))
    const seen: Array<string | null> = []
    for (const name of sequence) {
      phase = advancePhase(phase, phaseOfStage(name))
      seen.push(phase)
    }
    // 生成 once, then 校验 — and `regenerating` in the middle does NOT drag the
    // display back to 生成.
    expect(seen).toEqual(['writing', 'checking', 'checking', 'checking', 'checking'])
  })

  it('prefers the backend raw name over the folded §8 stage', () => {
    // `refilling` is folded to `generating`, but it is the raw name that carries
    // the fact; both land on 生成 here, and the raw name is what decides.
    expect(phaseOfProgress({ stage: 'generating', rawStage: 'anchors_repaired' })).toBe('checking')
    expect(phaseOfProgress({ stage: 'generating', rawStage: null })).toBe('writing')
    // An unrecognised future stage falls back to the contract stage rather than
    // silently reporting no progress at all.
    expect(phaseOfProgress({ stage: 'revising', rawStage: 'some_future_step' })).toBe('revising')
  })

  it('describes progress without naming a retry, an attempt or a failure', () => {
    const running = describeProgress({ completed: 2, total: 6, phase: 'checking', finished: false })
    expect(running).toContain('已生成 2 / 6 套')
    expect(running).toContain('正在校验')
    expect(describeProgress({ completed: 6, total: 6, phase: null, finished: true })).toBe(
      '6 套材料已全部生成',
    )
    for (const text of [running, describeProgress({ completed: 6, total: 6, phase: null, finished: true })]) {
      expect(text).not.toMatch(/未过|重试|重新生成|失败|第 \d+ 次/)
    }
  })
})

/* ── 卡片预览 ─────────────────────────────────────────────────────────────── */

describe('card preview', () => {
  it('quotes the first real line of dialogue, not the narration', () => {
    const record = buildRecord('balanced', { ...O, materialId: 'm-bal' })
    const view = joinFromRecord(record)
    const line = firstDialogueLine(view)
    expect(line).toBeTruthy()
    const narration = view.turns.filter((t) => t.dialogueOrdinal === null).map((t) => t.text.trim())
    expect(narration.length).toBeGreaterThan(0)
    expect(narration).not.toContain(line)
    // It is the FIRST non-narration turn, not merely some non-narration turn.
    expect(line).toBe(view.turns.find((t) => t.dialogueOrdinal !== null)!.text.trim())
  })

  it('summarises as topic + distraction, in the spec vocabulary', () => {
    const view = joinFromRecord(buildRecord('balanced', { ...O, materialId: 'm-bal' }))
    const summary = previewSummary(view)
    // 租房咨询 comes from config/scenarios.yaml via codegen, not a hardcoded map.
    expect(summary).toContain('租房咨询')
    expect(summary).toMatch(/拼读|先说后改|同义替换|干扰/)
  })

  it('flags the clustered variant and leaves the balanced one clean', () => {
    const balanced = previewOf('balanced', 'm-bal')
    const clustered = previewOf('clustered', 'm-clu')
    expect(balanced.pointTotal).toBe(10)
    expect(balanced.pointNumbers).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    expect(balanced.flaggedPoints).toEqual([])
    expect(clustered.flaggedPoints.length).toBeGreaterThan(0)
    // Only real item numbers become dots.
    for (const n of clustered.flaggedPoints) expect(clustered.pointNumbers).toContain(n)
  })

  it('flags a point whose anchor cannot be located', () => {
    const record = buildRecord('anchorMismatch', { ...O, materialId: 'm-mis' })
    const view = joinFromRecord(record)
    const metrics = computeDistribution(view, T)
    const flagged = flaggedPointNumbers(view, metrics)
    for (const n of metrics.unplacedNumbers) expect(flagged).toContain(n)
  })

  /**
   * The client's rule: a flawed material is still returned and still selectable;
   * the UI states its shortcomings and the user decides. So the FAIL fixture must
   * produce shortcomings AND must not produce anything that reads as a verdict.
   */
  it('states a rejected material shortcomings without exposing the verdict', () => {
    const failed = previewOf('failed', 'm-fail')
    expect(failed.shortcomings.length).toBeGreaterThan(0)
    const text = failed.shortcomings.join(' ')
    expect(text).not.toMatch(/FAIL|NOT_ASSESSABLE|PASS|MINOR_EDITS/)
    expect(text).not.toMatch(/隔离|quarantin/i)
    // It says what is wrong in terms a question-writer can act on.
    expect(text).toMatch(/必须改|不达标|没有经过复核/)
  })

  it('reuses the readiness vocabulary rather than inventing a second one', () => {
    const clustered = previewOf('clustered', 'm-clu')
    const balanced = previewOf('balanced', 'm-bal')
    // 记录节奏 / 全篇覆盖 / 题号顺序 / 前后两组题量 are usability.ts's own labels;
    // every shortcoming line must be one of them (or the audit sentence), so the
    // card, the distribution strip and the compare view cannot disagree.
    for (const line of [...clustered.shortcomings, ...balanced.shortcomings]) {
      expect(line, line).toMatch(/^(记录节奏|全篇覆盖|题号顺序|前后两组题量|信息点定位)：/)
    }
    // The clustered fixture's defining problem IS the clustering; the balanced
    // one is clean on that axis. (It still reports its own wide gaps — a real
    // property of that fixture, not a second vocabulary.)
    expect(clustered.shortcomings.join(' ')).toContain('记录节奏')
    expect(balanced.shortcomings.join(' ')).not.toContain('记录节奏')
    expect(balanced.readiness).toBe('needsWork')
    expect(balanced.flaggedPoints).toEqual([])
  })

  it('reports the degraded material as having skipped revise + re-audit', () => {
    expect(previewOf('anchorMismatch', 'm-mis').shortcomings.join(' ')).toContain('复评')
  })
})
