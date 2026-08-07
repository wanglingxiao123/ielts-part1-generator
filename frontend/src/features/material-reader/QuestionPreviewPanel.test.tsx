/**
 * 「题目预览」页签的渲染。
 *
 * 两件事必须钉在 DOM 上，因为它们的失败方式在数据层看不见：
 *
 *   · **真实版式**。form/note/table 各画成什么，只有渲染出来才算数——`buildQuestionPreview` 对
 *     「十张问答卡片」和「一张带列标签的表」返回的是同一个对象。
 *   · **关掉开关之后页面上确实没有答案**。数据层已经保证对象里没有（见 questionPreview.test.ts），
 *     这里保证的是没人从别处（title、aria-label、blueprint）把它带回页面。
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BASE_BLUEPRINT, buildRecord, QUESTION_PACKAGE } from '@/mocks/fixtures'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { QuestionPreviewPanel } from './QuestionPreviewPanel'

const pkg = QUESTION_PACKAGE
const view = joinFromRecord(
  buildRecord('balanced', {
    materialId: '20260729-accommodation-rental-11aa22bb',
    batchId: 'b1',
    scenarioKey: 'accommodation-rental',
    index: 0,
  }),
)

function renderPanel(onJump?: (turnIndex: number) => void) {
  return render(
    <QuestionPreviewPanel pkg={pkg} blueprint={BASE_BLUEPRINT} view={view} onJump={onJump} />,
  )
}

const toggle = () => screen.getByRole('checkbox', { name: /显示答案和证据/ })

/**
 * 契约把三块都定成「恰好十项」的元组，所以缺项的包在类型上构造不出来；运行时照样可能出现，因为
 * 端点是把 `_questions/` 里存着的东西原样交出来的（见 web/app.py 的注释）。这几处刻意造出契约
 * 不允许的形状，检验的正是页面遇到它时说实话，而不是画出一个看起来完整的十题。
 */
const offContract = <T,>(x: unknown): T => x as T

describe('题目预览的真实版式', () => {
  it('三种版式各按自己的结构画，不统一成十张问答卡片', () => {
    renderPanel()
    // form：左列行标签 + 右列印刷行。
    expect(document.querySelectorAll('.qp-form').length).toBe(2)
    // note：层级标题 + 笔记行。
    expect(document.querySelectorAll('.qp-note-list').length).toBe(2)
    // table：真的 <table>，有表头。
    const table = document.querySelector('.qp-table')!
    expect(table.tagName).toBe('TABLE')
    expect(table.querySelector('thead')).not.toBeNull()
  })

  it('form 的行标签来自 structure.row_labels，重复的那一格只印一次', () => {
    renderPanel()
    const forms = [...document.querySelectorAll('.qp-form')]
    expect(forms[0]!.textContent).toContain('Full name')
    // G2 的 row_label 是 `Street`，而 carrier_before 是 `Street:` —— 同一句话，印两遍会像产物出了错。
    const streetCells = [...forms[1]!.querySelectorAll('.qp-form-label')].map((n) => n.textContent)
    expect(streetCells).not.toContain('Street')
    expect(forms[1]!.textContent).toContain('Street:')
    expect(forms[1]!.textContent).toContain('Postcode:')
  })

  it('note 按 hierarchy 的声明顺序印标题，并显示题组 title', () => {
    renderPanel()
    const heads = [...document.querySelectorAll('.qp-note-head')].map((n) => n.textContent)
    // 顺序是包里的声明顺序：G3 的 `Child's education` 在 G4 的 `Location and lifestyle` 之前。
    expect(heads).toEqual(["Child's education", 'Location and lifestyle'])
    expect(screen.getByText('Family background')).toBeInTheDocument()
    expect(screen.getByText('Property preferences')).toBeInTheDocument()
  })

  it('table 的列标签进表头、行标签进首列，表头左上角留空', () => {
    renderPanel()
    const table = document.querySelector('.qp-table')!
    const headers = [...table.querySelectorAll('thead th')].map((n) => n.textContent)
    // 第一格是空的角，那一格在真实试卷上就是空的。
    expect(headers).toEqual(['', 'Requirement', 'Notes'])
    const rowHeads = [...table.querySelectorAll('tbody th')].map((n) => n.textContent)
    expect(rowHeads).toEqual(['Size', 'Extra space', 'Other'])
  })

  it('题面文字逐字照印，包括空格里那串点和题号', () => {
    renderPanel()
    const blanks = [...document.querySelectorAll('.qp-blank')].map((n) => n.textContent)
    expect(blanks).toHaveLength(10)
    expect(blanks[0]).toBe(pkg.question_face.questions[0]!.blank)
    expect(blanks[1]).toContain('2')
    // carrier_after 也在：`, Ballysillan` 是这一行读起来通不通的一半。
    expect(document.body.textContent).toContain(', Ballysillan')
  })

  it('rubric 与题号范围逐字取自 instruction', () => {
    renderPanel()
    expect(
      screen.getByText(pkg.question_face.instructions[1]!.instruction_text),
    ).toBeInTheDocument()
    expect(screen.getByText('Questions 2-4')).toBeInTheDocument()
    expect(screen.getByText('共 10 题')).toBeInTheDocument()
  })

  it('signpost 印在题面之上，不混进题里', () => {
    renderPanel()
    const signposts = [...document.querySelectorAll('.qp-signposts li')].map((n) => n.textContent)
    expect(signposts).toContain('Personal details taken by phone')
  })
})

describe('显示答案和证据 开关', () => {
  it('内部审核页面默认开启，逐题给出绿色答案与灰色斜体原文', () => {
    renderPanel()
    expect(toggle()).toBeChecked()
    // 绿色答案印在空格后面——复核的人读的是「这一行填进去通不通」。
    const inline = [...document.querySelectorAll('.qp-inline-answer')].map((n) => n.textContent)
    expect(inline).toEqual(pkg.answer_key.map((a) => a.canonical))
    const quotes = [...document.querySelectorAll('.qp-quote')].map((n) => n.textContent)
    expect(quotes).toHaveLength(10)
    expect(quotes[0]).toContain(pkg.evidence[0]!.quote)
  })

  it('轮次按对话第几轮显示，并能跳回原文那一句', async () => {
    const jumps: number[] = []
    renderPanel((turnIndex) => jumps.push(turnIndex))
    const turnButtons = [...document.querySelectorAll('.qp-turn')]
    expect(turnButtons).toHaveLength(10)
    // 显示的是「对话第 N 轮」而不是 turn_index：turn_index 把旁白也算在内。
    expect(turnButtons[0]!.textContent).toMatch(/^对话第 \d+ 轮$/)
    await userEvent.click(turnButtons[0]!)
    expect(jumps).toEqual([pkg.evidence[0]!.turn_index])
  })

  it('关闭后页面上没有任何 answer key、evidence、blueprint 或审核字样', async () => {
    renderPanel()
    await userEvent.click(toggle())
    expect(toggle()).not.toBeChecked()

    // 整棵 DOM 的文本 + 每一个属性值一起查：一个 title 或 aria-label 同样是泄露。
    const attrs = [...document.querySelectorAll('*')]
      .flatMap((el) => [...el.attributes].map((a) => a.value))
      .join(' ')
    const haystack = `${document.body.textContent} ${attrs}`
    for (const row of pkg.answer_key) {
      expect(haystack, row.canonical).not.toContain(row.canonical)
    }
    for (const row of pkg.evidence) {
      expect(haystack, row.quote).not.toContain(row.quote)
      // 同义改写关系、指代实体这些也是审核信息，不是考生可见的。
      expect(haystack, row.carrier_entity).not.toContain(row.carrier_entity)
    }
    // blueprint 的干扰机制与它被改口的旧值同样不该出现。
    if (BASE_BLUEPRINT.correction?.earlier) {
      expect(haystack).not.toContain(BASE_BLUEPRINT.correction.earlier)
    }
    // 「查看题解」整块不在——题解含答案信息。
    expect(screen.queryByRole('button', { name: /查看题解/ })).toBeNull()
    expect(document.querySelector('.qp-reveals')).toBeNull()
    // 题面还在：这才是考生看到的那张纸。
    expect(document.querySelectorAll('.qp-blank')).toHaveLength(10)
    expect(document.querySelector('.qp-table')).not.toBeNull()
    expect(screen.getByText(/盲看模式/)).toBeInTheDocument()
  })

  it('再打开一次答案又回来：开关是可逆的', async () => {
    renderPanel()
    await userEvent.click(toggle())
    expect(document.querySelectorAll('.qp-inline-answer')).toHaveLength(0)
    await userEvent.click(toggle())
    expect(document.querySelectorAll('.qp-inline-answer')).toHaveLength(10)
  })
})

describe('查看题解', () => {
  it('默认折叠，展开后只显示后端已有的事实', async () => {
    renderPanel()
    const buttons = screen.getAllByRole('button', { name: '查看题解' })
    expect(buttons).toHaveLength(10)
    expect(document.querySelector('.qp-facts')).toBeNull()

    await userEvent.click(buttons[0]!)
    const facts = document.querySelector('.qp-facts')!
    const labels = [...facts.querySelectorAll('dt')].map((n) => n.textContent)
    expect(labels).toContain('考点类型')
    expect(labels).toContain('计分口径')
    // 每一条都来自一个真实字段：计分口径就是 answer_key 自己的原话。
    expect(facts.textContent).toContain(pkg.answer_key[0]!.counting_rule)
    // 展开一题不会把别的九题也展开。
    expect(document.querySelectorAll('.qp-facts')).toHaveLength(1)
  })

  it('收起之后事实不再在页面上', async () => {
    renderPanel()
    await userEvent.click(screen.getAllByRole('button', { name: '查看题解' })[0]!)
    await userEvent.click(screen.getByRole('button', { name: '收起题解' }))
    expect(document.querySelector('.qp-facts')).toBeNull()
  })

  /**
   * 「缺少字段时不得由前端编造，可先隐藏对应内容」——这一条钉的就是隐藏那一半。
   *
   * 组件的条件是 `facts.length > 0`，而不是「有没有 blueprint」：答案形态（response_form）是题面
   * 自己的字段，所以即便 blueprint 与 answer_key 全缺，仍有一条真实事实可搬，题解照样该开。真正
   * 该消失的是那些没有来源的行——下面断言的正是「事实少了，题解里的行也少了」，而不是补一段话。
   */
  it('缺字段时题解只少几行，绝不由前端补一段解析', () => {
    render(
      <QuestionPreviewPanel
        pkg={offContract<typeof pkg>({ ...pkg, answer_key: [], evidence: [] })}
        blueprint={null}
        view={view}
      />,
    )
    // 答案缺失如实说出来，而不是画一个空的绿框。
    expect(screen.getAllByText(/这一题的答案或证据在包里缺失/)).toHaveLength(10)
    expect(document.querySelectorAll('.qp-inline-answer')).toHaveLength(0)
    expect(document.querySelectorAll('.qp-quote')).toHaveLength(0)
  })

  it('缺字段时题解里只剩有来源的那几行', async () => {
    render(
      <QuestionPreviewPanel
        pkg={offContract<typeof pkg>({ ...pkg, answer_key: [], evidence: [] })}
        blueprint={null}
        view={view}
      />,
    )
    await userEvent.click(screen.getAllByRole('button', { name: '查看题解' })[0]!)
    const labels = [...document.querySelectorAll('.qp-facts dt')].map((n) => n.textContent)
    // 只剩题面自己声明得出的那一条。考点类型（要 blueprint）与计分口径（要 answer_key）都不见了，
    // 而不是被一句「本题考查听力细节」之类的话顶上。
    expect(labels).toEqual(['答案形态'])
  })
})

describe('不完整的包', () => {
  it('题目不足十道时说出来，而不是安静地画九道', () => {
    render(
      <QuestionPreviewPanel
        pkg={offContract<typeof pkg>({
          ...pkg,
          question_face: {
            ...pkg.question_face,
            questions: pkg.question_face.questions.slice(0, 9),
          },
        })}
        blueprint={BASE_BLUEPRINT}
        view={view}
      />,
    )
    expect(screen.getByText('共 9 题')).toBeInTheDocument()
    expect(screen.getByText(/题目不足十道/)).toBeInTheDocument()
  })
})
