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
import type { BatchItemSnapshot, MaterialRecord } from '@/contracts/api'
import { buildRecord, type FixtureKind } from '@/mocks/fixtures'
import {
  BACKEND_CONCURRENCY,
  describeBatchEstimate,
  estimateBatchSeconds,
  waveCount,
  WAVE_SECONDS,
} from './batchEstimate'
import { computeDistribution } from './distribution'
import { joinFromRecord } from './joinArtifacts'
import { arrivedByScenario, buildResultGroups } from './resultSlots'
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

const previewOf = (kind: FixtureKind, id: string) => {
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

  /**
   * 这句话原来带着「已生成 2 / 6 套」，而它右边紧挨着的 `.progress-count` 已经在说
   * 「已完成 2/6」——客户看到的是同一个数字在同一行出现两次。所以 M/N 归计数器，这句话只说
   * 计数器说不出来的部分：还在跑时是哪一段，跑完时是齐没齐。
   *
   * 原来的意图一条没丢：不出现重试 / 尝试次数 / 失败字样，而且「跑完但没跑齐」仍然不能说成
   * 「已全部生成」——那会和同一页上的红色「生成异常」卡片直接矛盾。
   */
  it('describes progress without naming a retry, an attempt or a failure', () => {
    const running = describeProgress({ completed: 2, total: 6, phase: 'checking', finished: false })
    expect(running).toBe('正在校验')
    const allDone = describeProgress({ completed: 6, total: 6, phase: null, finished: true })
    expect(allDone).toBe('全部生成完毕')
    // Finished but short: saying "全部生成完毕" here would contradict the 生成异常
    // cards sitting on the same page, and a self-contradicting page reads as a bug.
    expect(describeProgress({ completed: 4, total: 6, phase: null, finished: true })).toBe(
      '已结束，其余未能生成',
    )
    // The counter owns the numbers; this line must not repeat them.
    for (const text of [
      running,
      allDone,
      describeProgress({ completed: 4, total: 6, phase: null, finished: true }),
      describeProgress({ completed: 0, total: 2, phase: 'writing', finished: false }),
    ]) {
      expect(text).not.toMatch(/未过|重试|重新生成|失败|第 \d+ 次/)
      expect(text).not.toMatch(/\d/)
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

  /**
   * 客户的原话：
   *
   *   > 结果页卡片上只展示：场景名 + 信息点时间轴图 + 预览第一句话 + 操作按钮。
   *   > 不展示任何评价文字。
   *
   * 所以 CardPreview 这一层不再产出任何一句评价——连字段都没有了（留一个没人读的
   * `shortcomings` 只是等着有人再把它渲染回卡片上）。评价文案本身没删，它在
   * domain/usability.ts，由阅读页的 DistributionStrip 渲染；下面 usability 那一组
   * 测试仍然在钉它。
   */
  it('carries no evaluation prose at all, only what the client listed', () => {
    for (const kind of ['balanced', 'clustered', 'failed'] as const) {
      const preview = previewOf(kind, `m-${kind}`)
      expect(Object.keys(preview).sort()).toEqual([
        'firstLine',
        'flaggedPoints',
        'index',
        'materialId',
        'pointNumbers',
        'pointTotal',
        'scenarioKey',
        'summary',
      ])
      // 那一行简述是「话题 + 考点」，不是判断：不许出现结论/整改用语。
      for (const forbidden of ['建议', '须先改', '不达标', '缺陷', '影响', '来不及']) {
        expect(preview.summary, forbidden).not.toContain(forbidden)
      }
    }
  })

  /**
   * 黄点留下来了，而且判据没变——它是「先看这一段」的指路，不是结论。客户点名表扬过这张
   * 时间轴，一个有颜色的点不会替他判断材料好坏。
   */
  it('keeps flagging the points worth a look, from the same deterministic evidence', () => {
    const clustered = previewOf('clustered', 'm-clu')
    const record = buildRecord('clustered', { ...O, materialId: 'm-clu' })
    const view = joinFromRecord(record)
    const metrics = computeDistribution(view, T)
    // 扎堆的三个点必在其中；判据全部来自已经算好的确定性结果。
    for (const n of metrics.clusters.flatMap((c) => c.numbers)) {
      expect(clustered.flaggedPoints).toContain(n)
    }
    for (const row of view.crossCheck.unrecoverable) {
      expect(clustered.flaggedPoints).toContain(row.number)
    }
    // 均衡那一套在这个轴上是干净的，否则黄点等于什么也没说。
    expect(previewOf('balanced', 'm-bal').flaggedPoints).toEqual([])
  })

  /**
   * 定位不出来的点不标黄：它在图上根本画不出来（见 distribution.ts），给一个画不出来的
   * 点标黄没有意义；而且那是我们自己的标注问题，不是客户看一眼就能判断的材料属性。
   */
  it('does not flag a point whose anchor could not be resolved', () => {
    const record = buildRecord('anchorUnresolvable', { ...O, materialId: 'm-unres' })
    const view = joinFromRecord(record)
    const metrics = computeDistribution(view, T)
    expect(metrics.unplacedNumbers).toEqual([3])
    expect(flaggedPointNumbers(view, metrics)).not.toContain(3)
  })
})

/* ── 提交前的耗时预估：必须建模并发，不能按串行算 ──────────────────────────── */

describe('batch estimate models concurrency, not serial execution', () => {
  it('scales with the number of WAVES, not the number of sets', () => {
    // Up to MAX_CONCURRENCY sets run at once, so 1..6 sets is one wave.
    expect(waveCount(1)).toBe(1)
    expect(waveCount(BACKEND_CONCURRENCY)).toBe(1)
    expect(waveCount(BACKEND_CONCURRENCY + 1)).toBe(2)
    expect(waveCount(0)).toBe(0)
    // 2, 4 and 6 sets all take one wave: the estimate must therefore be equal
    // for all three. The old serial formula made 6 sets three times 2 sets.
    const [two, four, six] = [2, 4, 6].map((n) => estimateBatchSeconds(n))
    expect(two).toEqual(four)
    expect(four).toEqual(six)
    expect(six).toEqual([WAVE_SECONDS[0], WAVE_SECONDS[1]])
  })

  it('predicts about 3–4 minutes for a single wave, matching the measured batch', () => {
    // Measured on AWS: a real 4-material batch ran 182–230s end to end.
    for (const total of [2, 4, 6]) {
      const [min, max] = estimateBatchSeconds(total)
      expect(min).toBe(182)
      expect(max).toBe(230)
      expect(describeBatchEstimate(total)).toBe('约 3–4 分钟')
    }
  })

  /**
   * The bug this replaces: `total * 100/60 … total * 160/60` told a user
   * "7–11 分钟" for the batch that was actually measured at 182–230s. A user who
   * believes that trims scenarios they could in fact afford to submit.
   */
  it('never reports the serial figure the old formula produced', () => {
    const serialMin = Math.round((4 * 100) / 60) // 7
    const [min] = estimateBatchSeconds(4)
    expect(Math.round(min / 60)).toBeLessThan(serialMin)
    expect(describeBatchEstimate(4)).not.toContain('7')
    expect(describeBatchEstimate(4)).not.toContain('11')
  })

  it('reports a range rather than false precision, and nothing at all for an empty batch', () => {
    expect(describeBatchEstimate(0)).toBe('—')
    expect(describeBatchEstimate(4)).toMatch(/^约 \d+–\d+ 分钟$/)
    // A lower concurrency (the backend's IELTS_CONCURRENCY can be lowered on
    // 429s) means more waves, and the estimate has to follow.
    expect(waveCount(6, 3)).toBe(2)
    expect(estimateBatchSeconds(6, 3)).toEqual([364, 460])
  })
})

/* ── 骨架卡位：材料到达之前就得知道要铺几张 ────────────────────────────────── */

describe('result slots', () => {
  /** Mid-flight is the default; `batchFinished` is exercised on its own below. */
  const build = (input: Omit<Parameters<typeof buildResultGroups>[0], 'batchFinished'>) =>
    buildResultGroups({ ...input, batchFinished: false })

  const item = (
    scenarioKey: string,
    index: number,
    status: BatchItemSnapshot['status'] = 'pending',
  ): BatchItemSnapshot => ({
    material_id: `${scenarioKey}-${index}`,
    scenario_key: scenarioKey,
    index,
    status,
    stage: 'queued',
    attempt: 0,
  })

  const material = (scenarioKey: string, index: number, id: string): MaterialRecord =>
    buildRecord('balanced', { materialId: id, batchId: 'b1', scenarioKey, index })

  const REQUESTED = [
    { scenarioKey: 'accommodation-rental', count: 3 },
    { scenarioKey: 'booking-hotel', count: 3 },
  ]

  it('gives every scenario as many skeletons as the user asked for, before any event', () => {
    const groups = build({ requested: REQUESTED, items: [], materials: {} })
    expect(groups.map((g) => g.scenarioKey)).toEqual([
      'accommodation-rental',
      'booking-hotel',
    ])
    for (const g of groups) {
      expect(g.slots).toHaveLength(3)
      expect(g.slots.map((s) => s.state)).toEqual(['skeleton', 'skeleton', 'skeleton'])
      expect(g.slots.map((s) => s.index)).toEqual([0, 1, 2])
      expect(g.arrived).toBe(0)
    }
  })

  it('honours a per-scenario count of 1 and of the batch maximum alike', () => {
    for (const count of [1, 2, 6]) {
      const groups = build({
        requested: [{ scenarioKey: 'booking-hotel', count }],
        items: [],
        materials: {},
      })
      expect(groups[0]!.slots).toHaveLength(count)
    }
  })

  /** The whole point: a material REPLACES its skeleton rather than adding a card. */
  it('replaces one skeleton, keeping the slot count and the slot key stable', () => {
    const before = build({ requested: REQUESTED, items: [], materials: {} })
    const after = build({
      requested: REQUESTED,
      items: [item('accommodation-rental', 1, 'done')],
      materials: { 'mat-x': material('accommodation-rental', 1, 'mat-x') },
    })

    const group = after.find((g) => g.scenarioKey === 'accommodation-rental')!
    expect(group.slots).toHaveLength(3) // not 4
    expect(group.slots.map((s) => s.state)).toEqual(['skeleton', 'material', 'skeleton'])
    expect(group.arrived).toBe(1)
    expect(group.slots[1]!.materialId).toBe('mat-x')
    // Same React key before and after → the skeleton is replaced, not remounted
    // alongside the real card.
    const keysBefore = before.find((g) => g.scenarioKey === 'accommodation-rental')!.slots.map((s) => s.key)
    expect(group.slots.map((s) => s.key)).toEqual(keysBefore)
  })

  /**
   * A slot the backend silently re-runs (audit returned NOT_ASSESSABLE → the
   * `refilling` stage event, no `material_failed`) must keep showing a skeleton.
   * The user is not supposed to perceive the refill at all.
   */
  it('keeps a silently refilled slot as a skeleton, never as an error', () => {
    const groups = build({
      requested: [{ scenarioKey: 'booking-hotel', count: 2 }],
      // `running`, because that is all a refill produces: stage events.
      items: [item('booking-hotel', 0, 'running'), item('booking-hotel', 1, 'running')],
      materials: {},
    })
    expect(groups[0]!.slots.map((s) => s.state)).toEqual(['skeleton', 'skeleton'])
  })

  /** A terminal `material_failed` — the backend will NOT refill that one. */
  it('shows an error slot only for a terminal failure', () => {
    const groups = build({
      requested: [{ scenarioKey: 'booking-hotel', count: 2 }],
      items: [item('booking-hotel', 0, 'failed'), item('booking-hotel', 1, 'running')],
      materials: {},
    })
    expect(groups[0]!.slots.map((s) => s.state)).toEqual(['error', 'skeleton'])
  })

  /**
   * Once the batch reaches a terminal state, an unfilled slot will never be
   * filled — the time budget skipped it, or its failure event never arrived. A
   * skeleton shimmering forever is worse than saying the set did not come out.
   */
  it('stops shimmering an unfilled slot once the batch is done', () => {
    const groups = buildResultGroups({
      requested: [{ scenarioKey: 'booking-hotel', count: 2 }],
      items: [item('booking-hotel', 0, 'done')],
      materials: { 'mat-1': material('booking-hotel', 0, 'mat-1') },
      batchFinished: true,
    })
    expect(groups[0]!.slots.map((s) => s.state)).toEqual(['material', 'error'])
  })

  /**
   * After a reload `requested` is gone (it lives in memory), so the shape has to
   * come from the snapshot's items — otherwise a refresh mid-batch would show
   * fewer cards than the batch actually has.
   */
  it('reconstructs the shape from snapshot items when the request is gone', () => {
    const groups = build({
      requested: [],
      items: [
        item('booking-hotel', 0),
        item('booking-hotel', 1),
        item('booking-hotel', 2),
        item('accommodation-rental', 0),
      ],
      materials: {},
    })
    expect(groups.map((g) => [g.scenarioKey, g.slots.length])).toEqual([
      ['booking-hotel', 3],
      ['accommodation-rental', 1],
    ])
  })

  it('still shows a material that arrived outside the plan rather than dropping it', () => {
    const groups = build({
      requested: [{ scenarioKey: 'booking-hotel', count: 1 }],
      items: [],
      materials: {
        planned: material('booking-hotel', 0, 'planned'),
        surprise: material('booking-hotel', 4, 'surprise'),
        other: material('service-refund', 0, 'other'),
      },
    })
    const hotel = groups.find((g) => g.scenarioKey === 'booking-hotel')!
    expect(hotel.slots.map((s) => s.materialId)).toEqual(['planned', 'surprise'])
    expect(groups.find((g) => g.scenarioKey === 'service-refund')!.arrived).toBe(1)
  })

  it('feeds the 每场景至少选 1 套 rule with arrived materials only', () => {
    const groups = build({
      requested: REQUESTED,
      items: [],
      materials: { 'mat-a': material('accommodation-rental', 0, 'mat-a') },
    })
    const byScenario = arrivedByScenario(groups)
    expect(byScenario.get('accommodation-rental')).toEqual(['mat-a'])
    // A scenario with only skeletons contributes no ids, so the rule cannot be
    // deadlocked by cards that do not exist yet.
    expect(byScenario.get('booking-hotel')).toEqual([])
  })
})
