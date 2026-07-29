import type {
  Audit,
  AuditFinding,
  BlindMapEntry,
  Blueprint,
  BlueprintItem,
  FindingSeverity,
  ItemForm,
  ItemType,
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

/**
 * 八类可考信息点，措辞取自《Part1 选材命制规范》§4B-3 的表格，让 UI 和客户团队自己
 * 的说法对得上。contract 里的 `name`/`number`/… 是内部枚举，命题人不认。
 */
export const ITEM_TYPE_LABEL: Record<ItemType, string> = {
  name: '姓名/专名',
  number: '电话/编号',
  address: '地址/门牌',
  price: '金额/价格',
  datetime: '日期/时间',
  quantity: '数量/上限',
  condition: '条件/要求',
  option: '选择/偏好',
}

/**
 * 需要拼读的类型（§3「至少包含一处姓名/专有名词拼读」）。拼读点必须被确认，否则
 * once-only 下考生根本抓不住，所以这两个字段要一起看。
 */
export function needsSpelling(type: ItemType): boolean {
  return type === 'name'
}

/**
 * `critical`/`major`/`minor` 是评价方的内部枚举。审阅者要的是「这条我该不该管」，
 * 所以按后果说话，而不是按级别名。
 */
export const SEVERITY_LABEL: Record<FindingSeverity, string> = {
  critical: '必须改',
  major: '影响出题',
  minor: '可斟酌',
}

export const SEVERITY_FLAG: Record<FindingSeverity, string> = {
  critical: 'flag-bad',
  major: 'flag-warn',
  minor: 'flag-neutral',
}

export const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩'] as const

export function circled(n: number): string {
  return CIRCLED[n - 1] ?? `(${n})`
}
