import type {
  Audit,
  AuditFinding,
  BlindMapEntry,
  Blueprint,
  BlueprintItem,
  ItemForm,
  Material,
  SpeakerId,
  Verdict,
} from '@/contracts'
import type { CrossCheck } from '@/contracts/api'

export type TurnRole = '旁白' | '信息持有方' | '需求方'

export interface HighlightRange {
  start: number
  end: number
  /** Blueprint item numbers whose evidence covers this range. */
  itemNumbers: number[]
}

export interface ViewTurn {
  /** turns array index — shown verbatim in the UI, and the only join key. */
  index: number
  speaker: SpeakerId
  role: TurnRole
  text: string
  /** Ordinal among non-narrator turns; null for narration. Density axis. */
  dialogueOrdinal: number | null
  items: BlueprintItem[]
  findings: AuditFinding[]
  blindHits: BlindMapEntry[]
  highlights: HighlightRange[]
}

export interface AnchorMismatch {
  itemNumber: number
  turnIndex: number
  reason: 'evidence-not-in-turn' | 'turn-out-of-range' | 'narrator-turn'
  evidence: string
  actualTurnText: string | null
}

export interface ViewMaterial {
  materialId: string
  scenarioKey: string
  index: number
  verdict: Verdict
  quarantined: boolean
  quarantineReason: { code: string; message: string } | null
  degraded: boolean
  scenario: string
  turns: ViewTurn[]
  /** Non-narrator turn count; the x-axis extent of the overview strip. */
  dialogueTurnCount: number
  material: Material
  blueprint: Blueprint
  audit: Audit
  crossCheck: CrossCheck
  /**
   * Non-empty means at least one annotation may sit beside the wrong sentence.
   * Never silently repaired by string search: an auto-"fix" hides a defect the
   * backend already persisted to S3 (design.md §2.1).
   */
  anchorMismatches: AnchorMismatch[]
}

export const ITEM_FORM_GLYPH: Record<ItemForm, string> = {
  form: '▤',
  table: '▦',
  multiple_choice: '◉',
  note: '▭',
}

export const ITEM_FORM_LABEL: Record<ItemForm, string> = {
  form: '表单',
  table: '表格',
  multiple_choice: '多选',
  note: '填空',
}

export const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩'] as const

export function circled(n: number): string {
  return CIRCLED[n - 1] ?? `(${n})`
}
