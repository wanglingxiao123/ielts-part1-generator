/**
 * Fixtures. The BASE material/blueprint/audit are the REAL artefacts from
 * 07-28-skill-contract (see generated.ts). The variants below are derived from
 * them by moving anchors / injecting findings, so the script text is always
 * real dialogue rather than invented prose.
 */
import type { Audit, Blueprint, Material } from '@/contracts'
import type { CrossCheck, MaterialRecord } from '@/contracts/api'
import type { AudioManifest } from '@/contracts/manifest'
import {
  AUDIT_ALIGNED,
  AUDIT_VALID,
  BLUEPRINT_BAD_ANCHOR,
  BLUEPRINT_V1_LEGACY,
  BLUEPRINT_VALID,
  MATERIAL_VALID,
} from './generated'

const clone = <T,>(x: T): T => structuredClone(x)

export const BASE_MATERIAL: Material = MATERIAL_VALID
export const BASE_BLUEPRINT: Blueprint = BLUEPRINT_VALID
/**
 * 真实 v1 记录：无版本字段、旧 coverage 名 `question_type_coverage`、三个点是
 * `item_form: "multiple_choice"`、这三个点的 `form_group` 为 null、`note` 是空数组。
 *
 * 与同一份 v2 夹具同源（`build_fixtures.py` 的 downgrade），所以脚本、轮次、evidence 全部相同，
 * 唯一的变量就是版本形态——前端读 v1 出问题时，可以确定不是材料换了。
 *
 * 前端此前是在测试里把 v2 夹具手工降级。那种写法只能包含「写测试的人当时记得改的字段」：真实
 * v1 的空 `note: []` 和 `multiple_choice` 都不在其中，兼容分支因此从未真正被测到。
 */
export const BASE_BLUEPRINT_V1: Blueprint = BLUEPRINT_V1_LEGACY
/** audit_valid has a genuine unrecoverable point (blind seq 5 sits at turn 16). */
export const BASE_AUDIT_WITH_GAP: Audit = AUDIT_VALID
/** audit_aligned matches the blueprint one-for-one. */
export const BASE_AUDIT_ALIGNED: Audit = AUDIT_ALIGNED

/**
 * cross_check is cross_check.py's output; the frontend cannot compute it (it
 * needs both maps and the ±1 tolerance rule). Values below are what that
 * script yields for these pairings:
 *   blueprint_valid × audit_aligned → 10 matched
 *   blueprint_valid × audit_valid   → point 5 (turn 20) unrecoverable,
 *                                     blind seq 5 (turn 16) unintended
 */
export const CROSS_CHECK_ALIGNED: CrossCheck = {
  ok: true,
  planned: 10,
  observed: 10,
  unrecoverable: [],
  unintended_target: [],
  ambiguous: [],
  matched: 10,
}
export const CROSS_CHECK_WITH_GAP: CrossCheck = {
  ok: false,
  planned: 10,
  observed: 10,
  unrecoverable: [
    {
      number: 5,
      type: 'name',
      target: 'Baker',
      turn_index: 20,
      evidence: "it's Baker, B-A-K-E-R",
      reason: 'auditor reading the script blind did not record this point',
    },
  ],
  unintended_target: [
    {
      audit_seq: 5,
      type: 'time',
      turn_index: 16,
      evidence: 'we open at half past eight',
      reason:
        'auditor recorded a detail the blueprint never planned; may create a second defensible answer',
    },
  ],
  ambiguous: [],
  matched: 9,
}

/* ── clustered variant ───────────────────────────────────────────────────── */

/**
 * Same script, same ten points — only the distribution changed:
 *   point 5 → turn 12 (shares a turn with point 4)
 *   point 6 → turn 27, point 7 → turn 27 (same turn), point 8 → turn 29
 * Measured against material_valid: gaps [3,4,2,2,0,14,0,2,8,3,1],
 * CV 1.11 (balanced 0.63), max gap 14 (balanced 8), one 3-point cluster at
 * turn 27–29 (balanced: none). Table group B turn span 11 vs 5.
 *
 * evidence is re-quoted from the destination turn so anchors stay honest: this
 * fixture must exercise clustering, not the anchor-mismatch path.
 */
const CLUSTER_MOVES: Record<number, { turn: number; evidence: string }> = {
  5: { turn: 12, evidence: 'you can get me on that anytime' },
  6: { turn: 27, evidence: 'he drives now' },
  7: { turn: 27, evidence: "he's not keen on buses" },
  8: { turn: 29, evidence: "he'd love a park nearby" },
}

export function clusteredBlueprint(): Blueprint {
  const bp = clone(BASE_BLUEPRINT)
  for (const item of bp.items) {
    const move = CLUSTER_MOVES[item.number]
    if (move) {
      item.turn_index = move.turn
      item.evidence = move.evidence
    }
  }
  return bp
}

export function clusteredAudit(): Audit {
  const audit = clone(BASE_AUDIT_ALIGNED)
  audit.verdict = 'PASS_WITH_MINOR_EDITS'
  audit.score.total = 69
  audit.score.dimensions.information_map_quality = 14
  audit.score.dimensions.naturalness_level = 14
  audit.findings = [
    {
      severity: 'major',
      rule: '信息点 6-8 过于集中，违反 §4A「前后均衡、分布均匀」',
      evidence: "Yes, he drives now, but I guess that's not going to be possible",
      turn_index: 27,
      fix: '将信息点 7 移到 turn 33 之后的房型讨论，拉开与 6、8 的间距',
    },
    {
      severity: 'minor',
      rule: '中段存在较长信息空档',
      evidence: 'Right. And do you have any children?',
      turn_index: null,
      fix: '在 turn 14–20 之间补一个可考细节',
    },
  ]
  return audit
}

/* ── anchor variants ─────────────────────────────────────────────────────── */

/**
 * 大小写不同的 evidence。
 *
 * 这一套的锚点是**对的**：校验器的 `anchor_ok` 与 `deterministic/anchors.py` 的 `_carries`
 * 都对两侧 casefold，所以 `"it's Anna Woods."` 落在 `"It's Anna Woods."` 这一轮上完全合法。
 * 前端原来用 `indexOf` 精确匹配，于是同一份材料在页面上被报成「标错位置」——纯属虚报，而且
 * 很可能是常见情形（模型给 evidence 时未必照抄首字母大小写）。
 *
 * 这个夹具钉两件事：不算失配，且高亮下标仍然对着**原文**（不是小写副本）。
 */
export function caseDifferingBlueprint(): Blueprint {
  const bp = clone(BASE_BLUEPRINT)
  const item = bp.items.find((i) => i.number === 1)!
  item.evidence = item.evidence.toLowerCase()
  return bp
}

/**
 * 修不了的锚点：evidence 一处都不存在。
 *
 * `deterministic/anchors.py` 的规则是「恰好命中一轮才挪正，零处或两处以上不猜」。这一套
 * 走的是零处那条分支：第 3 题的 evidence 被改成脚本里没有的句子，因此这条旁注**不显示**，
 * 另外九条照常显示。页面上不出现任何「可能标错了」的字样，而 blueprint 仍是十个点。
 */
export function unresolvableAnchorBlueprint(): Blueprint {
  const bp = clone(BASE_BLUEPRINT)
  const item = bp.items.find((i) => i.number === 3)!
  item.evidence = "It's ZZ99 0QQ."
  return bp
}

/* ── FAIL variant ────────────────────────────────────────────────────────── */

export function failedAudit(): Audit {
  const audit = clone(BASE_AUDIT_WITH_GAP)
  audit.verdict = 'FAIL'
  audit.score.total = 44
  audit.score.dimensions.information_map_quality = 9
  audit.score.dimensions.difficulty_distractor_control = 6
  audit.findings = [
    {
      severity: 'critical',
      rule: '答案原词未在对话中出现，违反命题铁律',
      evidence: "We have friends in an amazing flat ... we'll stick with the latter.",
      turn_index: 32,
      fix: "在 turn 32 之后补一句 'So, a house then.'，让答案原词被说出",
    },
    {
      severity: 'major',
      rule: '拼读类信息点未被确认',
      evidence: "It's 118 Fordyce.",
      turn_index: 8,
      fix: '让接线员回读门牌号并请对方确认',
    },
  ]
  return audit
}

/* ── manifest ────────────────────────────────────────────────────────────── */

/**
 * Synthetic manifest. `urlFor` supplies locally generated silent clips because
 * Polly is unreachable — the timeline, ordering, highlight sync and
 * double-buffer handoff are exercised; the voice audio is not.
 *
 * Turn 30 deliberately has `url: null`: the player must skip it and mark the
 * turn rather than rejecting the whole manifest (design.md §8.3).
 */
export function mockManifest(
  materialId: string,
  urlFor: (turnIndex: number) => string | null,
): AudioManifest {
  const turns = BASE_MATERIAL.listening_material_parts[0].script.turns
  let total = 0
  const segments = turns.map((turn, turnIndex) => {
    const words = turn.text.trim().split(/\s+/).length
    // ~160 wpm, floor 900ms.
    const durationMs = Math.max(900, Math.round((words / 160) * 60_000))
    const gapAfterMs = turn.speaker === 'speaker1' ? 1200 : 500
    total += durationMs + gapAfterMs
    const url = turnIndex === 30 ? null : urlFor(turnIndex)
    return {
      turn_index: turnIndex,
      speaker: turn.speaker,
      url,
      duration_ms: durationMs,
      gap_after_ms: gapAfterMs,
      bytes: durationMs * 4,
      error: url === null ? 'Polly ThrottlingException after 3 attempts' : null,
    }
  })
  const lastGap = segments[segments.length - 1]?.gap_after_ms ?? 0
  return {
    material_id: materialId,
    generated_at: new Date().toISOString(),
    engine: 'neural',
    format: 'mp3',
    sample_rate_hz: 24_000,
    voice_map: { speaker1: 'Brian', speaker2: 'Amy', speaker3: 'Arthur' },
    total_duration_ms: total - lastGap,
    url_expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    segments,
  }
}

export const MANIFEST_TURN_COUNT =
  BASE_MATERIAL.listening_material_parts[0].script.turns.length

/* ── material records ────────────────────────────────────────────────────── */

export type FixtureKind =
  | 'balanced'
  | 'clustered'
  | 'failed'
  /** blueprint_bad_anchor：第 3 题的 evidence 恰好只在另一轮出现 → 静默挪正。 */
  | 'anchorMismatch'
  /** evidence 只有大小写不同 → 后端认为合法，前端也必须认为合法。 */
  | 'anchorCaseDiffers'
  /** evidence 在脚本里不存在 → 挪不了，这一条旁注不显示。 */
  | 'anchorUnresolvable'
  /** 真实 v1 归档记录：旧 coverage 名 + `multiple_choice` + nullable group（见 BASE_BLUEPRINT_V1）。 */
  | 'v1Legacy'

export interface RecordOverrides {
  materialId: string
  batchId: string
  scenarioKey: string
  index: number
}

export function buildRecord(kind: FixtureKind, o: RecordOverrides): MaterialRecord {
  const base = {
    material_id: o.materialId,
    batch_id: o.batchId,
    scenario_key: o.scenarioKey,
    index: o.index,
    status: 'done' as const,
    created_at: new Date().toISOString(),
    material: BASE_MATERIAL,
  }
  switch (kind) {
    case 'balanced':
      return {
        ...base,
        verdict: 'PASS',
        blueprint: BASE_BLUEPRINT,
        audit: BASE_AUDIT_ALIGNED,
        cross_check: CROSS_CHECK_ALIGNED,
      }
    case 'clustered':
      return {
        ...base,
        verdict: 'PASS_WITH_MINOR_EDITS',
        blueprint: clusteredBlueprint(),
        audit: clusteredAudit(),
        cross_check: CROSS_CHECK_WITH_GAP,
      }
    case 'failed':
      return {
        ...base,
        verdict: 'FAIL',
        // Delivered and selectable like any other: the card states the
        // shortcoming, the user decides.
        audit_rejection: {
          code: 'VERDICT_FAIL',
          message: '评价环节判为不达标：答案原词未在对话中出现',
        },
        blueprint: BASE_BLUEPRINT,
        audit: failedAudit(),
        cross_check: {
          ok: false,
          planned: 10,
          observed: 9,
          matched: 8,
          unrecoverable: [
            {
              number: 7,
              type: 'option',
              target: 'house',
              turn_index: 32,
              evidence: "we'll stick with the latter",
              reason: 'auditor reading the script blind did not record this point',
            },
          ],
          unintended_target: [
            {
              audit_seq: 2,
              type: 'address',
              turn_index: 8,
              evidence: "It's 118 Fordyce.",
              reason:
                'auditor recorded a detail the blueprint never planned; may create a second defensible answer',
            },
          ],
          ambiguous: [],
        },
      }
    case 'anchorMismatch':
      return {
        ...base,
        verdict: 'PASS',
        degraded: true,
        blueprint: BLUEPRINT_BAD_ANCHOR,
        audit: BASE_AUDIT_ALIGNED,
        cross_check: CROSS_CHECK_ALIGNED,
      }
    case 'anchorCaseDiffers':
      return {
        ...base,
        verdict: 'PASS',
        blueprint: caseDifferingBlueprint(),
        audit: BASE_AUDIT_ALIGNED,
        cross_check: CROSS_CHECK_ALIGNED,
      }
    case 'anchorUnresolvable':
      return {
        ...base,
        verdict: 'PASS',
        blueprint: unresolvableAnchorBlueprint(),
        audit: BASE_AUDIT_ALIGNED,
        cross_check: CROSS_CHECK_ALIGNED,
      }
    case 'v1Legacy':
      // 归档记录本来就是 PASS 过的成品，审核结论沿用 aligned 那一份：这一套要检验的是「旧形态还
      // 读得对吗」，掺进 verdict 或 cross_check 的差异只会让失败原因变得难分辨。
      return {
        ...base,
        verdict: 'PASS',
        blueprint: BASE_BLUEPRINT_V1,
        audit: BASE_AUDIT_ALIGNED,
        cross_check: CROSS_CHECK_ALIGNED,
      }
  }
}
