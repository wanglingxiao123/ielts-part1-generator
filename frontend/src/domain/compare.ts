/**
 * Rule-based candidate comparison (design.md §4.1/§4.2).
 *
 * The summary sentence influences a decision that costs money and cannot be
 * undone, so it is a deterministic template — reproducible, auditable, unit
 * testable. No model call.
 *
 * Signal priority (deliberately NOT score-first):
 *   1 unrecoverable  2 unintended_target  3 uniformity  4 grouping viability
 *   5 defect counts by severity  6 total score  7 length metrics
 */
import type { DimensionKey, FindingSeverity } from '@/contracts'
import type { Thresholds } from '@/config/runtimeConfig'
import type { DistributionMetrics } from './distribution'
import type { FormGroupAnalysis } from './formGroups'
import { SEVERITY_LABEL } from './types'
import type { ViewMaterial } from './types'

export interface CandidateFacts {
  materialId: string
  label: string
  view: ViewMaterial
  distribution: DistributionMetrics
  groups: FormGroupAnalysis
  unrecoverable: number
  unintendedTarget: number
  defects: Record<FindingSeverity, number>
  total: number
}

export const DIMENSION_LABEL: Record<DimensionKey, string> = {
  scenario_purpose_frame: '场景与目的框架',
  information_map_quality: '信息图谱质量',
  role_consistency: '角色一致性',
  naturalness_level: '自然度',
  difficulty_distractor_control: '难度与干扰控制',
  transcript_readiness: '转写就绪度',
}

export interface DimensionDelta {
  key: DimensionKey
  label: string
  a: number
  b: number
  delta: number
}

export type Lean = 'A' | 'B' | 'tie'

export interface CompareResult {
  a: CandidateFacts
  b: CandidateFacts
  lean: Lean
  /** The priority level (1-7) that decided the lean; null when tied. */
  decidedBy: number | null
  reasons: string[]
  /** Only |Δ| >= DIMENSION_DIFF_SHOWN; the rest is noise. */
  dimensionDeltas: DimensionDelta[]
  hiddenDimensionCount: number
  scoreDiff: number
  scoreDiffSignificant: boolean
  summary: string
}

export function buildFacts(
  label: string,
  view: ViewMaterial,
  distribution: DistributionMetrics,
  groups: FormGroupAnalysis,
): CandidateFacts {
  const defects: Record<FindingSeverity, number> = { critical: 0, major: 0, minor: 0 }
  for (const f of view.audit.findings) defects[f.severity] += 1
  return {
    materialId: view.materialId,
    label,
    view,
    distribution,
    groups,
    unrecoverable: view.crossCheck.unrecoverable.length,
    unintendedTarget: view.crossCheck.unintended_target.length,
    defects,
    total: view.audit.score.total,
  }
}

/**
 * Widest span among DECLARED groups only.
 *
 * The form_group=null bucket holds points that were never claimed to belong
 * together, so its span says nothing about whether a table question is
 * answerable. Including it let an unrelated pair of standalone points 20 turns
 * apart decide priority 4 between two candidates.
 */
function widestGroupSpan(f: CandidateFacts): number {
  return f.groups.groups
    .filter((g) => !g.ungrouped)
    .reduce((max, g) => Math.max(max, g.turnSpan), 0)
}

export function compareCandidates(
  a: CandidateFacts,
  b: CandidateFacts,
  thresholds: Thresholds,
): CompareResult {
  const reasons: string[] = []
  // Held in an object so TS does not narrow it to 'tie' after the closure runs.
  const verdict: { lean: Lean; decidedBy: number | null } = { lean: 'tie', decidedBy: null }

  const decide = (level: number, favour: Lean, reason: string) => {
    reasons.push(reason)
    if (verdict.lean === 'tie' && favour !== 'tie') {
      verdict.lean = favour
      verdict.decidedBy = level
    }
  }

  // 1 unrecoverable — a point even a blind reader cannot hear is fatal.
  if (a.unrecoverable !== b.unrecoverable) {
    const favour: Lean = a.unrecoverable < b.unrecoverable ? 'A' : 'B'
    const worse = favour === 'A' ? b : a
    decide(
      1,
      favour,
      `${worse.label} 有 ${worse.unrecoverable} 个信息点连试听的人都没听出来，写不成题；` +
        `${favour === 'A' ? a.label : b.label} 为 ${Math.min(a.unrecoverable, b.unrecoverable)} 个`,
    )
  }

  // 2 unintended targets — unplanned recordable detail breeds ambiguous answers.
  if (a.unintendedTarget !== b.unintendedTarget) {
    const favour: Lean = a.unintendedTarget < b.unintendedTarget ? 'A' : 'B'
    const worse = favour === 'A' ? b : a
    decide(
      2,
      favour,
      `${worse.label} 有 ${worse.unintendedTarget} 处计划外的可考细节，可能出现第二个说得通的答案`,
    )
  }

  // 3 uniformity.
  const uniDiff = a.distribution.uniformity - b.distribution.uniformity
  if (Math.abs(uniDiff) >= 5) {
    const favour: Lean = uniDiff > 0 ? 'A' : 'B'
    const better = favour === 'A' ? a : b
    const worse = favour === 'A' ? b : a
    // Say what the difference DOES to a question-writer, not the coefficient it
    // came from. The specific bunched/empty spots live in the usability table.
    const worseClusters = worse.distribution.clusters.length
    decide(
      3,
      favour,
      `${better.label} 的信息点铺得更开，考生有时间记录` +
        (worseClusters > 0
          ? `；${worse.label} 有 ${worseClusters} 处信息点连着给`
          : `；${worse.label} 疏密不均，有大段无考点的空白`),
    )
  }

  // 4 grouping viability.
  if (a.groups.hasViableQuestionGroup !== b.groups.hasViableQuestionGroup) {
    const favour: Lean = a.groups.hasViableQuestionGroup ? 'A' : 'B'
    const worse = favour === 'A' ? b : a
    decide(
      4,
      favour,
      `${worse.label} 出不了表格/表单题——同类信息点凑不满 3 个`,
    )
  } else {
    const spanA = widestGroupSpan(a)
    const spanB = widestGroupSpan(b)
    const aWarn = spanA > thresholds.GROUP_SPAN_WARN
    const bWarn = spanB > thresholds.GROUP_SPAN_WARN
    if (aWarn !== bWarn) {
      const favour: Lean = aWarn ? 'B' : 'A'
      const worse = aWarn ? a : b
      decide(
        4,
        favour,
        `${worse.label} 有一组表格题的信息点前后隔了 ${Math.max(spanA, spanB)} 轮，` +
          `考生要跨半篇回忆才能填完`,
      )
    }
  }

  // 5 defect counts by severity, not by wording.
  for (const sev of ['critical', 'major'] as const) {
    if (a.defects[sev] !== b.defects[sev]) {
      const favour: Lean = a.defects[sev] < b.defects[sev] ? 'A' : 'B'
      const worse = favour === 'A' ? b : a
      decide(5, favour, `${worse.label} 有 ${worse.defects[sev]} 处「${SEVERITY_LABEL[sev]}」的问题`)
      break
    }
  }

  // 6 total score, ranked this low on purpose.
  const scoreDiff = a.total - b.total
  const scoreDiffSignificant = Math.abs(scoreDiff) >= thresholds.SCORE_DIFF_SIGNIFICANT
  if (scoreDiff !== 0) {
    const higher = scoreDiff > 0 ? a : b
    if (scoreDiffSignificant) {
      decide(6, scoreDiff > 0 ? 'A' : 'B', `${higher.label} 总分高 ${Math.abs(scoreDiff)} 分`)
    } else {
      reasons.push(`${higher.label} 总分高 ${Math.abs(scoreDiff)} 分，但分差不显著`)
    }
  }

  // 7 length metrics — differences inside the hard limits are just style.
  const am = a.view.audit.metrics
  const bm = b.view.audit.metrics
  reasons.push(
    `篇幅 ${a.label} ${am.dialogue_words} 词 / ${am.dialogue_turns} 轮 · ` +
      `${b.label} ${bm.dialogue_words} 词 / ${bm.dialogue_turns} 轮（都在规定区间内，长短只是风格差别）`,
  )

  const dims = Object.keys(a.view.audit.score.dimensions) as DimensionKey[]
  const allDeltas: DimensionDelta[] = dims.map((key) => ({
    key,
    label: DIMENSION_LABEL[key],
    a: a.view.audit.score.dimensions[key],
    b: b.view.audit.score.dimensions[key],
    delta: a.view.audit.score.dimensions[key] - b.view.audit.score.dimensions[key],
  }))
  const dimensionDeltas = allDeltas
    .filter((d) => Math.abs(d.delta) >= thresholds.DIMENSION_DIFF_SHOWN)
    .sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta))

  const leaned =
    verdict.lean === 'A' ? a.label : verdict.lean === 'B' ? b.label : null
  const summary =
    reasons.slice(0, 3).join('；') + (leaned ? `。→ 倾向 ${leaned}。` : '。→ 两套无决定性差异。')

  return {
    a,
    b,
    lean: verdict.lean,
    decidedBy: verdict.decidedBy,
    reasons,
    dimensionDeltas,
    hiddenDimensionCount: allDeltas.length - dimensionDeltas.length,
    scoreDiff,
    scoreDiffSignificant,
    summary,
  }
}
