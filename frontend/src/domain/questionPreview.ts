/**
 * 「题目预览」要显示什么：把题目包的三块 + blueprint 拼成按组、按题的可渲染结构。
 *
 * 两条规矩，都是这一层存在的理由。
 *
 * **一、三块的分离在这里继续保持。** `question_face` 是考生可见的全部；`answer_key` 与 `evidence`
 * 各自挂在 `reveal` 下面，是一个**可选**字段。所以「关掉答案开关」不是渲染时少画几个 div，而是
 * 拿到的对象里根本没有那两块——泄露需要有人改这一层的类型，而不是漏写一个 `{showAnswers && …}`。
 *
 * **二、题解只搬运，不推断。** 客户要的是「真实解析、干扰机制、易错点、考点类型」。后端确实有的：
 *   · 干扰机制    blueprint 的 correction / indirect_confirmation，复用 pointFacts.distractionOf
 *   · 考点类型    blueprint item 的 type（规范 §4B-3 八类），复用 ITEM_TYPE_LABEL
 *   · 易错点      evidence 的 paraphrase_relation（同义改写就是考生错的地方）、item.confirmed
 *                （没人复述 → once-only 下极易听错）、answer_key 的 word_limit / counting_rule
 *   · 答案变体    answer_key.alternatives
 * 后端**没有**的：一段自由文本的「解析」。所以这里不生成那种句子，`hasExplanation` 为假时页面把
 * 「查看题解」整块隐藏——宁可少一块，也不由前端编一段命题人会当成后端结论去信的话。
 */
import type {
  AnswerKeyRow,
  Blueprint,
  BlueprintItem,
  EvidenceRow,
  QuestionFaceItem,
  QuestionGroup,
  QuestionInstruction,
  QuestionLayout,
  QuestionPackage,
} from '@/contracts'
import { DISTRACTION_HINT, DISTRACTION_LABEL, distractionOf } from './pointFacts'
import type { DistractionKind } from './pointFacts'
import { ITEM_TYPE_LABEL } from './types'
import type { ViewMaterial } from './types'

/** 一条题解事实：一个标签 + 一句话。全部来自后端字段，没有一条是这里推断出来的。 */
export interface SolutionFact {
  key: string
  label: string
  text: string
  tone: 'neutral' | 'good' | 'warn'
}

/** 答案与证据。只在「显示答案和证据」开启时存在——不存在，而不是存在但不画。 */
export interface QuestionReveal {
  /** 绿色正确答案。 */
  canonical: string
  /** 同样算对的写法；空数组是正常答案，不是遗漏（AR-004）。 */
  alternatives: string[]
  /** 灰色斜体原文。 */
  quote: string
  /** 证据所在的 turn 编号，与阅读页的轮次编号同一套坐标。 */
  turnIndex: number
}

export interface PreviewQuestion {
  number: number
  /** 考生可见的题面：空前后文 + 带题号的空格。 */
  face: QuestionFaceItem
  /** 开关关闭时为 undefined。类型上就拿不到答案，不是渲染时跳过。 */
  reveal?: QuestionReveal
  /** 「查看题解」里的内容。开关关闭时为空数组——题解含答案信息，不该在盲看时出现。 */
  facts: SolutionFact[]
}

/** 一个题组：一种版式、一条 rubric、若干题。Form/Note/Table 的真实排版按这个结构画。 */
export interface PreviewGroup {
  group: QuestionGroup
  instruction: QuestionInstruction | null
  questions: PreviewQuestion[]
  /** Derived from member evidence; unlike the legacy group scalar, this can truthfully be [1, 2]. */
  narratorWindows: Array<1 | 2>
}

export interface QuestionPreview {
  groups: PreviewGroup[]
  /** 十道题按题号升序，给「逐题」那一列用（题组内可能只覆盖一部分题号）。 */
  questions: PreviewQuestion[]
  /** 用到的版式，按 groups 出现顺序去重。页顶那行标签。 */
  layouts: QuestionLayout[]
  /** 面上有几道题。正常是 10；少于 10 说明包不完整，页面据此提示而不是假装完整。 */
  count: number
}

/**
 * 页面显示答案时，把 answer_key / evidence 也挂上去。
 *
 * `showAnswers` 是参数而不是返回值上的开关，正是为了让「关掉时对象里没有答案」成为类型能保证的事。
 */
export function buildQuestionPreview(
  pkg: QuestionPackage,
  blueprint: Blueprint | null,
  showAnswers: boolean,
): QuestionPreview {
  const face = pkg.question_face
  const byNumberAnswer = new Map<number, AnswerKeyRow>()
  for (const row of pkg.answer_key ?? []) byNumberAnswer.set(row.number, row)
  const byNumberEvidence = new Map<number, EvidenceRow>()
  for (const row of pkg.evidence ?? []) byNumberEvidence.set(row.number, row)
  const byNumberItem = new Map<number, BlueprintItem>()
  for (const item of blueprint?.items ?? []) byNumberItem.set(item.number, item)

  const instructionOf = new Map<string, QuestionInstruction>()
  for (const entry of face.instructions ?? []) instructionOf.set(entry.group_id, entry)

  const build = (item: QuestionFaceItem): PreviewQuestion => {
    const answer = byNumberAnswer.get(item.number)
    const evidence = byNumberEvidence.get(item.number)
    if (!showAnswers) return { number: item.number, face: item, facts: [] }
    return {
      number: item.number,
      face: item,
      // 缺了任何一半就不给 reveal：半个答案（有原文没答案、或反过来）比没有更容易被误读。
      reveal:
        answer && evidence
          ? {
              canonical: answer.canonical,
              alternatives: answer.alternatives ?? [],
              quote: evidence.quote,
              turnIndex: evidence.turn_index,
            }
          : undefined,
      facts: solutionFacts(item, answer, evidence, byNumberItem.get(item.number), blueprint),
    }
  }

  const questions = [...(face.questions ?? [])]
    .sort((a, b) => a.number - b.number)
    .map(build)
  const byNumber = new Map(questions.map((q) => [q.number, q]))

  const groups: PreviewGroup[] = (face.groups ?? []).map((group) => {
    const groupQuestions = (face.questions ?? [])
      .filter((item) => item.group_id === group.group_id)
      .sort((a, b) => a.number - b.number)
      .map((item) => byNumber.get(item.number)!)
      .filter(Boolean)
    const narratorWindows = [
      ...new Set(
        groupQuestions
          .map((question) => byNumberEvidence.get(question.number)?.narrator_window_id)
          .filter((window): window is 1 | 2 => window === 1 || window === 2),
      ),
    ].sort()
    if (narratorWindows.length === 0 && group.narrator_window_id) {
      narratorWindows.push(group.narrator_window_id)
    }
    return {
      group,
      instruction: instructionOf.get(group.group_id) ?? null,
      questions: groupQuestions,
      narratorWindows,
    }
  })

  return {
    groups,
    questions,
    layouts: [...new Set(groups.map((g) => g.group.layout))],
    count: questions.length,
  }
}

/**
 * 一道题的题解事实。每一条都指名它来自哪个字段——加一条新的必须先有一个真实字段可搬。
 */
function solutionFacts(
  face: QuestionFaceItem,
  answer: AnswerKeyRow | undefined,
  evidence: EvidenceRow | undefined,
  item: BlueprintItem | undefined,
  blueprint: Blueprint | null,
): SolutionFact[] {
  const facts: SolutionFact[] = []

  // 考点类型（规范 §4B-3 八类）。用 blueprint 的 type，不用题面的 answer_category：前者是规范的
  // 八类考点，后者是 13 类答案微类别，用途不同，混用会让「考点」这一行说出一个规范里没有的词。
  if (item) {
    facts.push({
      key: 'type',
      label: '考点类型',
      text: ITEM_TYPE_LABEL[item.type],
      tone: 'neutral',
    })
  }

  // 干扰机制。只认 blueprint 自己声明的两处（§4B-4），声明了 distractor 却对不上就如实说「未声明」。
  const kind: DistractionKind | null =
    item && blueprint ? distractionOf(item, blueprint) : null
  if (kind) {
    facts.push({
      key: 'distraction',
      label: DISTRACTION_LABEL[kind],
      text: DISTRACTION_HINT[kind],
      tone: kind === 'unspecified' ? 'warn' : 'good',
    })
    // 先说后改：把被改掉的那个值也摆出来——它就是考生会写下的错答案。
    if (kind === 'correction' && blueprint?.correction?.earlier) {
      facts.push({
        key: 'correction-earlier',
        label: '会被写成',
        text: `${blueprint.correction.earlier}（先说的值，已被改口${
          blueprint.correction.marker ? `：${blueprint.correction.marker}` : ''
        }）`,
        tone: 'warn',
      })
    }
    // 同义替换：答案原词与用来指代它的说法，是这道题唯一的难点所在。
    if (kind === 'paraphrase' && blueprint?.indirect_confirmation?.reference_phrase) {
      facts.push({
        key: 'paraphrase-reference',
        label: '指代说法',
        text: blueprint.indirect_confirmation.reference_phrase,
        tone: 'warn',
      })
    }
  }

  // 易错点：题面与原文不同词。`signpost` 不算易错——保留定位标签是 QR-034 要求的写法。
  if (evidence?.paraphrase_relation === 'paraphrase') {
    facts.push({
      key: 'paraphrase-relation',
      label: '易错点',
      text: '题面是原文的改写，考生不能靠找原词定位',
      tone: 'warn',
    })
  }

  // 没人复述的点，在 once-only 下几乎必错（规范 §3）。拼读点尤其。
  if (item && item.confirmed === false) {
    facts.push({
      key: 'unconfirmed',
      label: '易错点',
      text: '只播一次且无人复述，听漏就没有第二次机会',
      tone: 'warn',
    })
  }

  // 字数限制与计分口径。QR-017 要求报告点明计数依据，所以这是后端写下的原话，不是这里的措辞。
  if (answer) {
    facts.push({
      key: 'counting',
      label: '计分口径',
      text: `${answer.word_limit}；${answer.counting_rule}`,
      tone: 'neutral',
    })
    if ((answer.alternatives ?? []).length > 0) {
      facts.push({
        key: 'alternatives',
        label: '同样算对',
        text: answer.alternatives.join('、'),
        tone: 'good',
      })
    }
  }

  // 答案形态：题面自己声明的 response_form，按 token 数分类（数字 / 单词 / 短语）。
  facts.push({
    key: 'response-form',
    label: '答案形态',
    text: RESPONSE_FORM_LABEL[face.response_form],
    tone: 'neutral',
  })

  return facts
}

/** 三种版式的中文名。用在题组标题那一行，也用在页顶「本套用到的版式」。 */
export const LAYOUT_LABEL: Record<QuestionLayout, string> = {
  form: '表单 Form',
  note: '笔记 Note',
  table: '表格 Table',
}

/** `response_form` 的中文。措辞取自 schema 对该字段的定义（按 token 数，不是按字符组成）。 */
const RESPONSE_FORM_LABEL: Record<QuestionFaceItem['response_form'], string> = {
  numeric: '纯数字/时间/金额',
  word: '一个单词（连字符复合词算一个）',
  phrase: '多个单词',
}

/**
 * 证据的 turn 编号 → 阅读页那一栏的第几句话，让「原文第 N 轮」和对话原文页对得上。
 *
 * 走 `view.turns[i].dialogueOrdinal` 而不是直接显示 turn_index：turn_index 把旁白也算在内，
 * 而「原文第 3 轮」在读者眼里指的是对话的第 3 轮。旁白轮返回 null，调用方退回显示原始索引。
 */
export function dialogueOrdinalOf(view: ViewMaterial | null, turnIndex: number): number | null {
  return view?.turns[turnIndex]?.dialogueOrdinal ?? null
}
