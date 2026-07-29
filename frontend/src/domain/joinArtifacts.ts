/**
 * material × blueprint × audit → ViewMaterial (design.md §2).
 *
 * Pure. No React. The one thing that must never go wrong here is the anchor:
 * an annotation next to the wrong sentence does not throw, it just makes the
 * reviewer believe something false.
 */
import type { Audit, AuditFinding, BlindMapEntry, Blueprint, Material, SpeakerId } from '@/contracts'
import type { CrossCheck, MaterialRecord } from '@/contracts/api'
import type { AnchorMismatch, HighlightRange, TurnRole, ViewMaterial, ViewTurn } from './types'

const ROLE_BY_SPEAKER: Record<SpeakerId, TurnRole> = {
  speaker1: '旁白',
  speaker2: '信息持有方',
  speaker3: '需求方',
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

  const anchorMismatches: AnchorMismatch[] = []
  const itemsByTurn = new Map<number, Blueprint['items'][number][]>()
  const highlightsByTurn = new Map<number, HighlightRange[]>()

  for (const item of input.blueprint.items) {
    const turn = turns[item.turn_index]
    if (!turn) {
      anchorMismatches.push({
        itemNumber: item.number,
        turnIndex: item.turn_index,
        reason: 'turn-out-of-range',
        evidence: item.evidence,
        actualTurnText: null,
      })
      continue
    }
    const at = turn.text.indexOf(item.evidence)
    if (at < 0) {
      // Deliberately NOT falling back to a fuzzy search over other turns:
      // that would relocate the annotation and hide the defect.
      anchorMismatches.push({
        itemNumber: item.number,
        turnIndex: item.turn_index,
        reason: 'evidence-not-in-turn',
        evidence: item.evidence,
        actualTurnText: turn.text,
      })
    } else {
      const list = highlightsByTurn.get(item.turn_index) ?? []
      list.push({ start: at, end: at + item.evidence.length, itemNumbers: [item.number] })
      highlightsByTurn.set(item.turn_index, list)
    }
    if (turn.speaker === 'speaker1') {
      anchorMismatches.push({
        itemNumber: item.number,
        turnIndex: item.turn_index,
        reason: 'narrator-turn',
        evidence: item.evidence,
        actualTurnText: turn.text,
      })
    }
    // The card is still anchored where the blueprint says, mismatch or not.
    const bucket = itemsByTurn.get(item.turn_index) ?? []
    bucket.push(item)
    itemsByTurn.set(item.turn_index, bucket)
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

  return {
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
    blueprint: input.blueprint,
    audit: input.audit,
    crossCheck: input.crossCheck,
    anchorMismatches,
  }
}

/** turn_index → dialogueOrdinal, for the overview strip and playback pointer. */
export function ordinalOf(view: ViewMaterial, turnIndex: number): number | null {
  return view.turns[turnIndex]?.dialogueOrdinal ?? null
}
