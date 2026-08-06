import type {
  Audit,
  AuditFinding,
  BlindMapEntry,
  Blueprint,
  BlueprintItem,
  FindingSeverity,
  ItemType,
  Material,
  SpeakerId,
  Verdict,
} from '@/contracts'
import type { CrossCheck } from '@/contracts/api'
import type { AnchorOmission, AnchorRepair } from './anchors'
import { isCurrentLayout } from './blueprintVersion'
import type { CurrentLayout } from './blueprintVersion'

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

/**
 * 只给**本产品还在出的三种版式**配字形和名字，键是 `CurrentLayout` 而不是 `ItemForm`。
 *
 * 这两个类型不一样，差别就是这里要表达的：`ItemForm` 有四个值，因为读侧 schema 必须收下带
 * `multiple_choice` 的历史记录；能渲染的只有三种。若按 `ItemForm` 建表，TS 会要求补一个
 * `multiple_choice` 的字形和中文名——那等于把客户已经删掉的版式重新摆到命题人面前，看着像仍可选。
 * 历史值不是「缺一个标签」，而是「不该有标签」，所以走下面的 `layoutLabel` 回退。
 */
export const ITEM_FORM_GLYPH: Record<CurrentLayout, string> = {
  form: '▤',
  table: '▦',
  note: '▭',
}

export const ITEM_FORM_LABEL: Record<CurrentLayout, string> = {
  form: '表单',
  table: '表格',
  note: '填空',
}

/**
 * 标签查找必须走这里，不要直接索引 `ITEM_FORM_LABEL`。
 *
 * v1 记录里的 `item_form` 可能是 `multiple_choice`——真实数据里就有，因此在 `ItemForm` union 内，
 * 但不在上面两张表内。直接索引会渲染出 `undefined：①②`。回退用原字符串而不是「未知」：读的人需要
 * 知道这份历史记录声明的到底是什么，才判断得出面板说的对不对。
 */
export function layoutLabel(layout: string): string {
  return isCurrentLayout(layout) ? ITEM_FORM_LABEL[layout] : layout
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
