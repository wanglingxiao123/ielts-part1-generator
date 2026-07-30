/**
 * 考点小结：这一套「考什么、难在哪、哪几个点听不出来」。
 *
 * 客户的要求是「把『拼读、先说后改、同义替换』这些考点抽取出来，然后用高亮块标注」。所以这一层
 * 只做归拢：把已经分散在各处的判断按考点聚成几块，页面直接渲染。
 *
 * 一律复用现成判据，不新造：
 *   · 一行简述        cardPreview.previewSummary（与结果卡、后端 cards.preview_summary 同源）
 *   · 干扰机制        pointFacts.distractionOf —— 只认 blueprint 自己声明的 correction /
 *                     indirect_confirmation 两处，其余 distractor 点落到「机制未声明」
 *   · 拼读 / 复述确认  pointFacts.contentFacts（规范 §3）
 *   · 八类信息点      ITEM_TYPE_LABEL，措辞取自规范 §4B-3 的表格
 *
 * 为什么不去检测规范 §4B-4 的另外三种机制（多选项陷阱 / 否定排除 / 条件限定）：blueprint 没有
 * 声明它们的字段，从原文猜是新造判据，而猜错的后果是页面对命题人断言一个不存在的考点。声明了
 * `distractor` 却对不上两处声明的点，如实说成「机制未声明，请对照原文」——那本身就是有用的提示。
 *
 * 盲评（cross_check）在这里只保留**点号**：哪几个点试听的人没听出来、哪几个听着有歧义。
 * 「计划 10 个 / 听出 10 个」这类计数是内部核对口径，命题人读不出该做什么，所以不出现。
 */
import type { ItemType } from '@/contracts'
import { previewSummary } from './cardPreview'
import { displayTurns } from './joinArtifacts'
import { DISTRACTION_HINT, DISTRACTION_LABEL, contentFacts, distractionOf } from './pointFacts'
import type { DistractionKind } from './pointFacts'
import { ITEM_TYPE_LABEL, type ViewMaterial } from './types'

/** 一个考点块：一句话说清「这是什么考点」+ 涉及哪几个点，点号可跳原文。 */
export interface ExamPointBlock {
  key: string
  /** 高亮块标题，用规范的词：拼读 / 先说后改 / 同义替换 / …… */
  label: string
  /** 鼠标悬停的一句解释；块本身只放标题和点号。 */
  hint: string
  /** 涉及的点号，升序。 */
  numbers: number[]
  /** 点号 → 原文 turn，用于跳转。 */
  turnOf: Record<number, number>
  /** 'good' 是能力，'warn' 是要人看一眼的问题。 */
  tone: 'good' | 'warn' | 'bad'
}

export interface TypeCoverageRow {
  type: ItemType
  label: string
  numbers: number[]
}

export interface ExamPointSummary {
  /** 一行简述，和结果卡上那一行同一个函数算出来，两处不可能不一致。 */
  headline: string
  /** 考点块：拼读、各种干扰机制、复述确认，以及听不出来 / 有歧义的点。 */
  blocks: ExamPointBlock[]
  /** 八类信息点里这一套用到的几类（规范 §4B-3）。 */
  typeCoverage: TypeCoverageRow[]
  /** 八类中覆盖到的类数。 */
  typeKindCount: number
  /** 点号 → blueprint 声明的原文 turn。跳转坐标，全页共用一份。 */
  turnOf: Record<number, number>
}

/** 规范 §4B-3 的八类，按表格顺序——覆盖率要按固定顺序读才看得出缺哪类。 */
const TYPE_ORDER: readonly ItemType[] = [
  'name',
  'number',
  'address',
  'price',
  'datetime',
  'quantity',
  'condition',
  'option',
] as const

function block(
  key: string,
  label: string,
  hint: string,
  tone: ExamPointBlock['tone'],
  numbers: number[],
  turnOf: Record<number, number>,
): ExamPointBlock {
  return { key, label, hint, tone, numbers: [...numbers].sort((a, b) => a - b), turnOf }
}

export function summariseExamPoints(view: ViewMaterial): ExamPointSummary {
  const items = view.blueprint.items
  const facts = contentFacts(view.blueprint)

  // 跳转坐标取**显示位置**，不取 blueprint 声明的 turn_index：一个被挪正过的点，声明的坐标
  // 会把人跳到一句不含这个考点的台词上——考点小结的全部用处就是「从考点直接看到句子」，跳错了
  // 它比没有更糟。解不出来的点没有显示位置，退回声明坐标（那个点在正文里本来也没有高亮）。
  const shown = displayTurns(view)
  const turnOf: Record<number, number> = {}
  for (const item of items) turnOf[item.number] = shown.get(item.number) ?? item.turn_index

  const blocks: ExamPointBlock[] = []

  // 拼读（§3：至少一处姓名/专有名词拼读）。拼读点没人复述是真问题——once-only 下几乎必错，
  // 所以它单独成一块，而不是缩在拼读块里当个脚注。
  if (facts.spellingNumbers.length > 0) {
    blocks.push(
      block(
        'spelling',
        '拼读',
        '姓名/专名逐字母给出，考生须听写字母（规范 §3）',
        'good',
        facts.spellingNumbers,
        turnOf,
      ),
    )
  }
  if (facts.spellingUnconfirmed.length > 0) {
    blocks.push(
      block(
        'spelling-unconfirmed',
        '拼读却没人复述',
        '拼读信息只播一次且无人复述，考生极易听错（规范 §3 要求关键信息复述确认）',
        'warn',
        facts.spellingUnconfirmed,
        turnOf,
      ),
    )
  }

  // 干扰机制（§4B-4）。按机制归组，一个机制一块，而不是逐点重复同一个标签。
  const byKind = new Map<DistractionKind, number[]>()
  for (const item of items) {
    const kind = distractionOf(item, view.blueprint)
    if (!kind) continue
    byKind.set(kind, [...(byKind.get(kind) ?? []), item.number])
  }
  for (const kind of ['correction', 'paraphrase', 'unspecified'] as const) {
    const numbers = byKind.get(kind)
    if (!numbers || numbers.length === 0) continue
    blocks.push(
      block(
        `distraction-${kind}`,
        DISTRACTION_LABEL[kind],
        DISTRACTION_HINT[kind],
        kind === 'unspecified' ? 'warn' : 'good',
        numbers,
        turnOf,
      ),
    )
  }

  // 复述确认（§3）。它是「一遍能不能听清」的保障，所以放在能力一侧。
  if (facts.confirmedNumbers.length > 0) {
    blocks.push(
      block(
        'confirmed',
        '有复述确认',
        '对话中复述或确认过，一遍就能听清并定位（规范 §3）',
        'good',
        facts.confirmedNumbers,
        turnOf,
      ),
    )
  }

  // 这里原来还有两块盲评结论：「听不出来」（unrecoverable）和「听着有歧义」（ambiguous）。
  // 移走的理由，按交付物的定位：
  //
  //  * 交付的是听力材料，不是试卷。这两块判断的是「据此出题可不可行」，规范 §3 / §6 讲的是
  //    材料本身该具备什么，不含这一层。
  //  * 层级不对。旁边的拼读 / 先说后改 / 干扰 / 有复述确认都是「这套材料有什么」，是正面描述；
  //    这两块是「某个点可能有毛病」，是负面警告。混在一排标签里，读者无法判断自己在看什么。
  //  * 红色「听不出来」会被读成「这套材料不能用」，而它也可能只是盲评方的提取没覆盖某种信息
  //    类型——HGR482 就是：证据文本完全一致，只因对照只比轮次和 type 而误判（见
  //    shared/cross_check.py）。一个可能来自我们自己 bug 的信号，不该长在成品的能力清单里。
  //
  // 盲评本身没有削弱：`unrecoverable` 仍进修改指令、仍参与取分，只是不再摆到用户面前当结论。
  const byType = new Map<ItemType, number[]>()
  for (const item of items) byType.set(item.type, [...(byType.get(item.type) ?? []), item.number])
  const typeCoverage: TypeCoverageRow[] = TYPE_ORDER.filter((t) => byType.has(t)).map((type) => ({
    type,
    label: ITEM_TYPE_LABEL[type],
    numbers: [...byType.get(type)!].sort((a, b) => a - b),
  }))

  return {
    headline: previewSummary(view),
    blocks,
    typeCoverage,
    typeKindCount: facts.typeKindCount,
    turnOf,
  }
}
