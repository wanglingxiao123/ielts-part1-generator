/**
 * 「题目预览」的数据层。这一组测试真正要守住的是**一条**性质：
 *
 *   开关关掉时，答案与证据不是「存在但没画出来」，而是根本不在返回的对象里。
 *
 * 之所以钉在这一层而不只钉在 DOM 上：DOM 断言只能证明当时那几个组件没把它印出来，而下一个组件、
 * 一个 `title=`、一个 `aria-label` 都能重新把它带回页面。数据里没有，是所有渲染路径共同的保证。
 */
import { describe, expect, it } from 'vitest'
import { BASE_BLUEPRINT, QUESTION_PACKAGE } from '@/mocks/fixtures'
import { buildQuestionPreview, dialogueOrdinalOf, LAYOUT_LABEL } from './questionPreview'
import { joinFromRecord } from './joinArtifacts'
import { buildRecord } from '@/mocks/fixtures'

const pkg = QUESTION_PACKAGE

/**
 * 契约把三块都定成「恰好十项」的元组，所以一个缺项的包在类型上根本构造不出来。它在**运行时**照样
 * 可能出现：端点是把 `_questions/` 里存着的东西原样交出来的，没有第二次校验（web/app.py 的注释说明
 * 了为什么——那里不出关于交付性的第二个意见）。所以这几处用 cast 造出契约不允许的形状，正是为了
 * 检验页面遇到它时说实话而不是画一个看起来完整的十题。
 */
const offContract = <T,>(x: unknown): T => x as T

describe('buildQuestionPreview 的三块分离', () => {
  it('关闭答案时，返回值里没有任何 answer_key 或 evidence 的内容', () => {
    const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, false)
    expect(preview.count).toBe(10)
    for (const q of preview.questions) {
      // `reveal` 不存在，而不是存在但字段为空——空字符串同样会被某个组件印成一个空框。
      expect(q.reveal).toBeUndefined()
      // 题解也含答案信息（同样算对的写法、被改口的旧值），所以盲看时一条都不给。
      expect(q.facts).toEqual([])
    }
    // 序列化整棵树来找答案原文：任何一个新加的字段只要携带了答案，这一条就会失败。
    const dumped = JSON.stringify(preview)
    for (const row of pkg.answer_key) {
      expect(dumped, row.canonical).not.toContain(row.canonical)
    }
    for (const row of pkg.evidence) {
      expect(dumped, row.quote).not.toContain(row.quote)
    }
  })

  it('开启答案时，逐题给出正确答案、原文与轮次', () => {
    const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, true)
    const first = preview.questions[0]!
    expect(first.reveal?.canonical).toBe(pkg.answer_key[0].canonical)
    expect(first.reveal?.quote).toBe(pkg.evidence[0].quote)
    expect(first.reveal?.turnIndex).toBe(pkg.evidence[0].turn_index)
    expect(preview.questions.every((q) => q.reveal)).toBe(true)
  })

  it('答案与证据缺了任何一半就都不给：半个答案比没有更容易被误读', () => {
    const halved = offContract<typeof pkg>({
      ...pkg,
      evidence: pkg.evidence.filter((e) => e.number !== 3),
    })
    const preview = buildQuestionPreview(halved, BASE_BLUEPRINT, true)
    const third = preview.questions.find((q) => q.number === 3)!
    expect(third.reveal).toBeUndefined()
    // 别的题不受影响：一条缺失不该让整套的答案都消失。
    expect(preview.questions.find((q) => q.number === 4)!.reveal).toBeDefined()
  })
})

describe('buildQuestionPreview 的题组与版式', () => {
  it('按题组分，版式取自题组自己声明的 layout', () => {
    const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, true)
    expect(preview.groups.map((g) => g.group.group_id)).toEqual(['G1', 'G2', 'G3', 'G4', 'G5'])
    expect(preview.groups.map((g) => g.group.layout)).toEqual([
      'form',
      'form',
      'note',
      'note',
      'table',
    ])
    // 去重后按出现顺序。页顶那行标签用的就是它。
    expect(preview.layouts).toEqual(['form', 'note', 'table'])
    for (const layout of preview.layouts) expect(LAYOUT_LABEL[layout]).toBeTruthy()
  })

  it('题组内的题按题号升序，并且十道题一道不落', () => {
    const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, true)
    const numbers = preview.groups.flatMap((g) => g.questions.map((q) => q.number))
    expect(numbers).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
  })

  it('rubric 与字数限制挂在 instruction 上，逐字取自包里', () => {
    const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, true)
    const g2 = preview.groups.find((g) => g.group.group_id === 'G2')!
    expect(g2.instruction?.question_range).toBe('2-4')
    expect(g2.instruction?.instruction_text).toBe(pkg.question_face.instructions[1]!.instruction_text)
  })

  it('题面文字原样保留，包括空格里那串点和题号', () => {
    const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, true)
    const second = preview.questions.find((q) => q.number === 2)!
    expect(second.face.blank).toBe(pkg.question_face.questions[1].blank)
    expect(second.face.blank).toContain('2')
    expect(second.face.carrier_after).toBe(', Ballysillan')
  })

  it('题目不足十道时 count 如实变小，而不是补齐', () => {
    const short = offContract<typeof pkg>({
      ...pkg,
      question_face: {
        ...pkg.question_face,
        questions: pkg.question_face.questions.slice(0, 9),
      },
    })
    expect(buildQuestionPreview(short, BASE_BLUEPRINT, true).count).toBe(9)
  })
})

describe('题解事实只搬运后端已有的字段', () => {
  const preview = buildQuestionPreview(pkg, BASE_BLUEPRINT, true)

  it('考点类型用 blueprint 的 item.type，不用题面的 answer_category', () => {
    const first = preview.questions[0]!
    const type = first.facts.find((f) => f.key === 'type')
    expect(type).toBeDefined()
    // `answer_category` 是 13 类答案微类别，不是规范 §4B-3 的八类考点；印错会说出规范里没有的词。
    expect(type!.text).not.toBe(pkg.question_face.questions[0].answer_category)
  })

  it('计分口径逐字来自 answer_key 的 word_limit 与 counting_rule', () => {
    const fact = preview.questions[0]!.facts.find((f) => f.key === 'counting')!
    expect(fact.text).toContain(pkg.answer_key[0].word_limit)
    expect(fact.text).toContain(pkg.answer_key[0].counting_rule)
  })

  it('易错点只在 evidence 自己说是改写时才出现；signpost 不算易错', () => {
    // 第 1 题 exact，第 5 题 paraphrase，第 8 题 signpost（QR-034 要求保留定位标签，不是缺陷）。
    const has = (n: number) =>
      preview.questions.find((q) => q.number === n)!.facts.some(
        (f) => f.key === 'paraphrase-relation',
      )
    expect(has(1)).toBe(false)
    expect(has(5)).toBe(true)
    expect(has(8)).toBe(false)
  })

  it('没有可搬的事实时 facts 为空，页面据此隐藏「查看题解」', () => {
    // 没有 blueprint、没有 answer_key/evidence 的包：只剩题面自己声明的答案形态。
    const bare = offContract<typeof pkg>({ ...pkg, answer_key: [], evidence: [] })
    const facts = buildQuestionPreview(bare, null, true).questions[0]!.facts
    expect(facts.map((f) => f.key)).toEqual(['response-form'])
  })

  it('不生成任何自由文本「解析」——后端没有这个字段', () => {
    // 每一条事实都必须能指名它来自哪个字段。键名是那张清单。
    const allowed = new Set([
      'type',
      'distraction',
      'correction-earlier',
      'paraphrase-reference',
      'paraphrase-relation',
      'unconfirmed',
      'counting',
      'alternatives',
      'response-form',
    ])
    for (const q of preview.questions) {
      for (const fact of q.facts) expect(allowed, fact.key).toContain(fact.key)
    }
  })
})

describe('dialogueOrdinalOf', () => {
  const view = joinFromRecord(
    buildRecord('balanced', {
      materialId: 'm1',
      batchId: 'b1',
      scenarioKey: 'accommodation-rental',
      index: 0,
    }),
  )

  it('把 turn_index 翻成读者眼里的「对话第 N 轮」', () => {
    // turn_index 把旁白也算在内，而阅读页那一栏是按对话轮编号的。
    const ordinal = dialogueOrdinalOf(view, pkg.evidence[0].turn_index)
    expect(ordinal).not.toBeNull()
    expect(ordinal).toBeLessThanOrEqual(pkg.evidence[0].turn_index)
  })

  it('旁白轮返回 null，让调用方退回原始索引而不是印一个错的轮次', () => {
    const narrationIndex = view.turns.findIndex((t) => t.dialogueOrdinal === null)
    expect(narrationIndex).toBeGreaterThanOrEqual(0)
    expect(dialogueOrdinalOf(view, narrationIndex)).toBeNull()
    expect(dialogueOrdinalOf(null, 3)).toBeNull()
  })
})
