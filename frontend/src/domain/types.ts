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
import type { AnchorOmission, AnchorRepair } from './anchors'

/**
 * 说话人标签。客户的原话：「是 speak1 和 speak2，而不是你现在的信息持有方和需求方什么的」。
 *
 * 反对的是我们自己编的角色名，不是「谁在旁白」这个事实。规范 §4B-5 把角色系在 speaker 编号上
 * （speaker1＝旁白，另两位一方发问、一方持有信息），材料 JSON 也用 speaker1/2/3，所以标签直接
 * 用编号——命题人对着 JSON 看时不用再做一次翻译。旁白仍标出来，因为它不参与对话、不计入轮次，
 * 是读稿时必须看得见的区别；写法是 `speaker1` 加一个「旁白」限定语，而不是换成别的名字。
 */
export type TurnRole = 'speaker1' | 'speaker2' | 'speaker3'

/** 旁白（§4B-5：speaker1 只出现在框架轮次，不参与对话）。 */
export const NARRATOR_SPEAKER: TurnRole = 'speaker1'

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

export interface ViewMaterial {
  materialId: string
  scenarioKey: string
  index: number
  verdict: Verdict
  /** Audit rejection, if any. A shortcoming to state — it gates nothing. */
  auditRejection: { code: string; message: string } | null
  degraded: boolean
  scenario: string
  turns: ViewTurn[]
  /** Non-narrator turn count; the x-axis extent of the overview strip. */
  dialogueTurnCount: number
  material: Material
  /**
   * 存档/发布用的 blueprint，**原封不动**。校验器要求恰好 10 个信息点
   * （`validate_part1.py`：`len(items) != 10` 直接是 error），所以剔除一条旁注只能
   * 发生在显示层。这个字段就是那条边界：读它的人拿到的永远是十个点。
   */
  blueprint: Blueprint
  audit: Audit
  crossCheck: CrossCheck
  /**
   * 静默修正过的定位（evidence 恰好只在另一轮里出现）。用户看不到——挪正一条能确定
   * 挪的旁注就是「修好再返回」，不是需要用户参与的事。留在这里给开发者。
   */
  anchorRepairs: AnchorRepair[]
  /**
   * 修不了、因此本次**不显示**的旁注（evidence 找不到，或命中多轮无法确定）。
   * 只影响显示：`blueprint` 仍是完整的十个点。用户看不到这一条；开发者从控制台
   * 与 /dev/fixtures 上看得到（见 domain/anchors.ts 的 reportAnchorProblems）。
   */
  anchorOmissions: AnchorOmission[]
}

export const ITEM_FORM_GLYPH: Record<ItemForm, string> = {
  form: '▤',
  table: '▦',
  note: '▭',
}

export const ITEM_FORM_LABEL: Record<ItemForm, string> = {
  form: '表单',
  table: '表格',
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
