/**
 * 校验意见 → 命题人看得懂、能动手的一句话。
 *
 * 背景：校验从「门卫」改成了「质检报告」。三次生成都带着校验错误时，后端不再吞掉材料，
 * 而是把最后一次连同校验意见一起交付（backend/orchestration/loop.py）。所以现在有一批
 * 材料是**带着校验意见**到用户手上的，这一页得说清那是什么。
 *
 * 两条约束来自客户上一轮的反馈，这个模块整个是围着它们写的：
 *
 * 1. **评价文字只出现在阅读页，不上结果页卡片。** 卡片只有场景名 + 时间轴 + 第一句话 +
 *    按钮。一条「⑤的证据句不在它标的那一轮」离开原文就没有意义——用户看不到那一轮，
 *    只会被一句读不懂的话吓住。所以这里导出的东西只有 MaterialPage 用。
 *
 * 2. **不能写成「这份材料坏了，你去修」。** 校验器自己就可能是错的：本轮实测，它有 5 条
 *    规则会判掉真题（拼读序列一条就判掉 27 套里的 14 套）。所以措辞是「命题人看这里」，
 *    不是「此处有缺陷」——把校验意见当成一条待核对的线索，而不是一份判决。
 *
 * 校验器的原文是英文、是阈值口径的（`dialogue words outside 450-750: 812`），直接摆出来
 * 只会训练人忽略这一整块——上一轮就是因为这个把「提示（不影响采用）」从这一页删掉的。
 * 所以这里做翻译，且**不认识的一律保留原文**：一条读不懂的线索仍然比没有线索强，而悄悄
 * 丢掉一条校验意见会让人以为材料是干净的。
 */

/** 一条校验意见，翻成命题人的说法。 */
export interface ValidationNote {
  /** 稳定 key（原文），供 React 列表用。 */
  key: string
  /** 「命题人该看什么」，不是「哪里坏了」。 */
  text: string
  /** 涉及的题号，有就能跳到原文。 */
  numbers: number[]
}

export interface ValidationNoteSummary {
  notes: ValidationNote[]
  /** 面板标题下的一句话。 */
  headline: string
}

/**
 * 从校验原文里抠出题号。
 *
 * `blueprint.items[4]` 是**下标**，题号是 5；`blueprint must mark 2-3 distractor items` 里的
 * 2、3 不是题号。所以只认这一种模式，宁可少认不可错认——一个错的题号会把人跳到无关的句子上。
 */
function itemNumbers(message: string): number[] {
  const found: number[] = []
  for (const m of message.matchAll(/blueprint\.items\[(\d+)\]/g)) {
    const index = Number(m[1])
    if (Number.isInteger(index) && index >= 0 && index <= 9) found.push(index + 1)
  }
  return [...new Set(found)].sort((a, b) => a - b)
}

/**
 * 规则匹配表。每条都写成「看什么」。
 *
 * 顺序有意义：先匹配到的先用，所以具体的规则排在笼统的前面。
 */
const RULES: Array<{ match: RegExp; text: (m: RegExpMatchArray) => string }> = [
  {
    match: /turn_index .*? does not carry its evidence \(found at turn (\d+)\)/,
    text: (m) => `这个信息点标注的轮次与它实际出现的轮次不一致（实际在第 ${m[1]} 轮）。` +
      `出题时按原文的轮次定位，标注可以顺手改过来。`,
  },
  {
    match: /\.evidence not found in any dialogue turn/,
    text: () => '这个信息点的证据句在对话里找不到原样的对应。出题前先在原文确认答案词是怎么说的。',
  },
  {
    match: /\.evidence is in the wrong dialogue half/,
    text: () => '这个信息点落在了另一半对话里。读题指令把它分在前半段（或后半段），' +
      '而它的证据句在另一边——出题时按原文实际位置分组。',
  },
  {
    match: /item evidence must occur in strictly increasing, distinct dialogue turns/,
    text: () => '信息点的出现顺序与题号顺序不完全一致。按原文实际先后出题，' +
      '不必照题号顺序照抄标注。',
  },
  {
    match: /dialogue words outside 450-750: (\d+)/,
    text: (m) => `对话 ${m[1]} 词，在常见的 450-750 之外。偏短的通常是细节不够撑十道题，` +
      '偏长的可以压缩闲聊——读一遍看是否够写满十题。',
  },
  {
    match: /dialogue turns outside 20-48: (\d+)/,
    text: (m) => `对话 ${m[1]} 轮。轮次偏少时一轮里容易挤进两个考点，` +
      '出题时注意别让考生在一句话里记两样东西。',
  },
  {
    match: /each half needs \d+ turns; found (\d+)\/(\d+)/,
    text: (m) => `前后两段的轮次是 ${m[1]}/${m[2]}，一段明显偏薄。` +
      '那一段的题量可以适当调低。',
  },
  {
    match: /narration must split questions|blueprint\.split_after must equal/,
    text: () => '读题指令报的题号范围与标注的分组不一致。出题时以读题指令为准——' +
      '考生听到的是那句话。',
  },
  {
    match: /full narration must be \d+-\d+ words|short narration must be \d+-\d+ words/,
    text: () => '旁白篇幅偏离常见长度。旁白不承载答案，不影响出题，需要时可自行增删。',
  },
  {
    match: /no numeric information detected/,
    text: () => '没有检出数字信息（金额 / 门牌 / 电话 / 日期 / 时间）。' +
      '规范要求至少一处，出题前确认是否漏了。',
  },
  {
    match: /blueprint must mark at least \d+ confirmed items; found (\d+)/,
    text: (m) => `只有 ${m[1]} 处关键信息在对话里被复述或确认。一次性听力下，` +
      '没被确认的点容易听不出来——这几道题的难度可能偏高。',
  },
  {
    match: /blueprint must mark 2-3 distractor items; found (\d+)/,
    text: (m) => `标了 ${m[1]} 处干扰点（先说后改这类）。偏少时题目容易靠词面直接匹配，` +
      '偏多时会显得刻意。',
  },
  {
    match: /blueprint must use at least four detail types/,
    text: () => '信息点类型不足四类。题型适配会受影响——表格题需要可分条比较的同类信息。',
  },
  {
    match: /blueprint needs one homogeneous form\/table form_group|form_group .* mixes item_form/,
    text: () => '没有一组同类信息可以摆成表格或表单。十道题里通常要留出这样一组，' +
      '出题时看看哪几个点能并到一起。',
  },
  {
    match: /blueprint must record at least one (\S+) item|at least one (\S+) item must be confirmed/,
    text: () => '姓名类或数字类的关键信息没有被确认过一次。这两类最容易听错，' +
      '出题时留意这几道的难度。',
  },
  {
    match: /correction must contain earlier value/,
    text: () => '「先说后改」的三段（原值 → 改口 → 新值）在原文里没有按这个顺序出现。' +
      '出题按最终有效答案，先在原文核对一遍。',
  },
  {
    match: /indirect answer term must occur before its reference phrase|indirect_confirmation\.answer_term/,
    text: () => '同义替换的指代出现在答案词之前。答案词本身必须在音频里被说出来，' +
      '出题前确认它可以原样作为答案。',
  },
]

/**
 * 校验意见 → 面板内容。空数组进、空 notes 出，调用方据此不渲染面板。
 */
export function summariseValidationNotes(findings: readonly string[]): ValidationNoteSummary {
  const notes: ValidationNote[] = []
  for (const raw of findings) {
    if (typeof raw !== 'string' || !raw.trim()) continue
    const rule = RULES.find((r) => r.match.test(raw))
    const matched = rule ? raw.match(rule.match) : null
    notes.push({
      key: raw,
      // 认不出来的保留原文。翻不动的线索仍然是线索，悄悄丢掉才是错的。
      text: rule && matched ? rule.text(matched) : raw,
      numbers: itemNumbers(raw),
    })
  }
  return {
    notes,
    headline:
      notes.length === 0
        ? ''
        : `结构校验还有 ${notes.length} 条待核对的地方。材料本身完整可用，` +
          '以下是出题时值得先看一眼的位置。',
  }
}
