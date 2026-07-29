/**
 * material × blueprint × audit → ViewMaterial (design.md §2).
 *
 * Pure. No React. The one thing that must never go wrong here is the anchor:
 * an annotation next to the wrong sentence does not throw, it just makes the
 * reviewer believe something false.
 *
 * 定位规则不在这个文件里：它是 domain/anchors.ts，那是 backend/deterministic/anchors.py
 * 的移植（大小写不敏感匹配、恰好一处命中才修正、否则不猜）。这里只负责把解出来的位置
 * 挂到 ViewTurn 上。
 *
 * **显示 / 存档的边界就在这一层。** 解不出来的旁注不进 `viewTurns`，因此页面不显示它；
 * 但 `blueprint` 字段原封不动地透传出去，因为校验器要求恰好 10 个信息点，任何存档或
 * 发布路径读到的必须还是十个点。剔除只发生在这里往下的显示侧。
 */
import type { Audit, AuditFinding, BlindMapEntry, Blueprint, Material, SpeakerId } from '@/contracts'
import type { CrossCheck, MaterialRecord } from '@/contracts/api'
import { reportAnchorProblems, resolveAnchors } from './anchors'
import type { HighlightRange, TurnRole, ViewMaterial, ViewTurn } from './types'

/**
 * 说话人编号原样用作标签，不再映射成「信息持有方 / 需求方」。
 *
 * 那两个名字是我们自己起的：规范 §4B-5 把分工系在编号上（speaker1＝旁白；另两位一方发问推进、
 * 一方分点给出细节），而哪一位是哪一种在场景之间会互换（平等关系场景里分工本身就可轮换）。所以
 * 一个固定的 speaker2→信息持有方 映射既对不上材料 JSON，也不总是对的。编号是唯一稳定的事实。
 */
const ROLE_BY_SPEAKER: Record<SpeakerId, TurnRole> = {
  speaker1: 'speaker1',
  speaker2: 'speaker2',
  speaker3: 'speaker3',
}

export interface JoinInput {
  materialId: string
  scenarioKey: string
  index: number
  material: Material
  blueprint: Blueprint
  audit: Audit
  crossCheck: CrossCheck
  verdict?: Audit['verdict']
  auditRejection?: { code: string; message: string } | null
  degraded?: boolean
}

export function joinFromRecord(record: MaterialRecord): ViewMaterial {
  return joinArtifacts({
    materialId: record.material_id,
    scenarioKey: record.scenario_key,
    index: record.index,
    material: record.material,
    blueprint: record.blueprint,
    audit: record.audit,
    crossCheck: record.cross_check,
    verdict: record.verdict,
    auditRejection: record.audit_rejection ?? null,
    degraded: record.degraded ?? false,
  })
}

/** Merges overlapping evidence spans so nested <mark> elements never nest. */
function mergeHighlights(ranges: HighlightRange[]): HighlightRange[] {
  const sorted = [...ranges].sort((a, b) => a.start - b.start || a.end - b.end)
  const out: HighlightRange[] = []
  for (const r of sorted) {
    const last = out[out.length - 1]
    if (last && r.start <= last.end) {
      last.end = Math.max(last.end, r.end)
      for (const n of r.itemNumbers) if (!last.itemNumbers.includes(n)) last.itemNumbers.push(n)
    } else {
      out.push({ start: r.start, end: r.end, itemNumbers: [...r.itemNumbers] })
    }
  }
  return out
}

export function joinArtifacts(input: JoinInput): ViewMaterial {
  const part = input.material.listening_material_parts[0]
  const turns = part.script.turns

  /**
   * 定位。修正得了的静默修正，修不了的这一次不显示——两者都不出现在页面上。
   * 规则本身在 domain/anchors.ts，与 backend/deterministic/anchors.py 同一条。
   */
  const anchors = resolveAnchors(turns, input.blueprint.items)
  const itemByNumber = new Map(input.blueprint.items.map((i) => [i.number, i]))

  const itemsByTurn = new Map<number, Blueprint['items'][number][]>()
  const highlightsByTurn = new Map<number, HighlightRange[]>()

  for (const placement of anchors.placements) {
    const item = itemByNumber.get(placement.itemNumber)
    if (!item) continue
    const list = highlightsByTurn.get(placement.turnIndex) ?? []
    list.push({
      start: placement.span.start,
      end: placement.span.end,
      itemNumbers: [item.number],
    })
    highlightsByTurn.set(placement.turnIndex, list)

    // 旁注挂在**解出来的**那一轮，而不是 blueprint 声明的那一轮：两者不同时，声明的
    // 那一轮就是「贴错位置」本身。item 对象照原样带过去（含它自己的 turn_index），
    // 因为旁注里那行 `turn N` 是给人对着 JSON 看的坐标。
    const bucket = itemsByTurn.get(placement.turnIndex) ?? []
    bucket.push(placement.turnIndex === item.turn_index ? item : { ...item, turn_index: placement.turnIndex })
    itemsByTurn.set(placement.turnIndex, bucket)
  }

  const findingsByTurn = new Map<number, AuditFinding[]>()
  for (const f of input.audit.findings) {
    if (f.turn_index == null) continue
    const list = findingsByTurn.get(f.turn_index) ?? []
    list.push(f)
    findingsByTurn.set(f.turn_index, list)
  }

  const blindByTurn = new Map<number, BlindMapEntry[]>()
  for (const b of input.audit.blind_information_map) {
    const list = blindByTurn.get(b.turn_index) ?? []
    list.push(b)
    blindByTurn.set(b.turn_index, list)
  }

  let ordinal = 0
  const viewTurns: ViewTurn[] = turns.map((turn, index) => {
    const isNarration = turn.speaker === 'speaker1'
    const dialogueOrdinal = isNarration ? null : ordinal++
    const items = (itemsByTurn.get(index) ?? []).slice().sort((a, b) => a.number - b.number)
    return {
      index,
      speaker: turn.speaker,
      role: ROLE_BY_SPEAKER[turn.speaker],
      text: turn.text,
      dialogueOrdinal,
      items,
      findings: findingsByTurn.get(index) ?? [],
      blindHits: blindByTurn.get(index) ?? [],
      highlights: mergeHighlights(highlightsByTurn.get(index) ?? []),
    }
  })

  const view: ViewMaterial = {
    materialId: input.materialId,
    scenarioKey: input.scenarioKey,
    index: input.index,
    verdict: input.verdict ?? input.audit.verdict,
    auditRejection: input.auditRejection ?? null,
    degraded: input.degraded ?? false,
    scenario: part.scenario,
    turns: viewTurns,
    dialogueTurnCount: ordinal,
    material: input.material,
    // 原封不动透传：显示侧可能少一条旁注，存档侧永远是十个点。
    blueprint: input.blueprint,
    audit: input.audit,
    crossCheck: input.crossCheck,
    anchorRepairs: anchors.repairs,
    anchorOmissions: anchors.omissions,
  }

  // 用户看不到定位问题；开发者看得到。剔除一条旁注说明我们自己的流水线产出了自相矛盾的
  // 构件，全方向咽下去就再没人会发现。
  reportAnchorProblems(view)

  return view
}

/** turn_index → dialogueOrdinal, for the overview strip and playback pointer. */
export function ordinalOf(view: ViewMaterial, turnIndex: number): number | null {
  return view.turns[turnIndex]?.dialogueOrdinal ?? null
}

/**
 * 点号 → **显示用**的 turn。
 *
 * 每一处「这个点在原文哪一句」都必须走这里，不许直接读 `blueprint.items[].turn_index`：
 * 那是 blueprint 声明的坐标，而声明可能是歪的（这正是这一轮要解决的问题）。挪正过的点在
 * 这里给出的是真正带着 evidence 的那一轮，因此分布图的点位、form_group 括号、考点小结的
 * 跳转坐标必然落在同一句上。
 *
 * 解不出来的点在这个表里**没有条目**——它在页面上不显示，所以也没有可跳转的位置。调用方
 * 据此跳过它，而不是跳到一个我们并不相信的坐标。
 */
export function displayTurns(view: ViewMaterial): Map<number, number> {
  const out = new Map<number, number>()
  for (const turn of view.turns) {
    for (const item of turn.items) out.set(item.number, turn.index)
  }
  return out
}
