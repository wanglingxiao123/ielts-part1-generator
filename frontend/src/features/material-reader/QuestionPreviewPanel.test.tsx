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
import { describe, expect, it, vi } from 'vitest'
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
  it('marks a commented question and selects its anchor without changing the question face', async () => {
    const onSelectQuestion = vi.fn()
    render(
      <QuestionPreviewPanel
        pkg={pkg}
        blueprint={BASE_BLUEPRINT}
        view={view}
        selectedQuestion={1}
        commentCounts={new Map([[1, 2]])}
        onSelectQuestion={onSelectQuestion}
      />,
    )
    const question = document.querySelector('[data-question="1"]')!
    expect(question.className).toContain('has-comments')
    expect(question.className).toContain('selected')
    expect(question.querySelector('.comment-count-badge')?.textContent).toBe('2')
    await userEvent.click(question)
    expect(onSelectQuestion).toHaveBeenCalledWith(1)
  })

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

  /**
   * 行标签与空前文重复时**照印**，不再合成一格。
   *
   * 原先这里断言的是相反的事：`row_label: Street` 与 `carrier_before: "Street:"` 合并成一格只印一次。
   * 那是前端替产物打补丁——而「标签与 carrier 说同一句话」正是出题规则现在禁止、审核 Agent 第 12 项要
   * 判断的缺陷（question-rules.md §4）。页面替它去重，复核的人就永远看不到那一行真实印出来的样子。
   */
  it('form 的行标签来自 structure.row_labels，与空前文重复时也照印', () => {
    renderPanel()
    const forms = [...document.querySelectorAll('.qp-form')]
    expect(forms[0]!.textContent).toContain('Full name')
    const streetCells = [...forms[1]!.querySelectorAll('.qp-form-label')].map((n) => n.textContent)
    expect(streetCells).toEqual(['Street', 'Postcode', 'Mobile'])
    // 两处都在页面上：标签一处，carrier 一处。重复是产物的问题，不是渲染的问题。
    expect(forms[1]!.textContent).toContain('Street:')
    expect(forms[1]!.textContent).toContain('Postcode:')
  })

  it('note 按 note_sections 的显式映射印标题，并显示题组 title', () => {
    renderPanel()
    const heads = [...document.querySelectorAll('.qp-note-head')].map((n) => n.textContent)
    // 顺序是包里的声明顺序：G3 的 `Child's education` 在 G4 的 `Location and lifestyle` 之前。
    expect(heads).toEqual(["Child's education", 'Location and lifestyle'])
    expect(screen.getByText('Family background')).toBeInTheDocument()
    expect(screen.getByText('Property preferences')).toBeInTheDocument()
  })

  it('note 标题与声明的题目保持在同一个 section', () => {
    renderPanel()
    const sections = [...document.querySelectorAll('.qp-note-section')]
    expect(sections.map((section) => section.querySelector('.qp-blank')?.textContent)).toEqual([
      '5',
      '6',
    ])
  })

  it('旧 note 没有题目映射时不猜标题归属', () => {
    const legacy = structuredClone(pkg)
    for (const group of legacy.question_face.groups) {
      if (group.layout === 'note') {
        delete group.structure.note_sections
        group.structure.hierarchy = ['Legacy heading that may not match']
      }
    }
    render(
      <QuestionPreviewPanel
        pkg={offContract<typeof pkg>(legacy)}
        blueprint={BASE_BLUEPRINT}
        view={view}
      />,
    )
    expect(screen.queryByText('Legacy heading that may not match')).toBeNull()
    expect(document.querySelectorAll('.qp-note .qp-blank')).toHaveLength(3)
  })

  it('table 的行标题列和内容列都有明确表头', () => {
    renderPanel()
    const table = document.querySelector('.qp-table')!
    const headers = [...table.querySelectorAll('thead th')].map((n) => n.textContent)
    expect(headers).toEqual(['Category', 'Requirement', 'Notes'])
    const rowHeads = [...table.querySelectorAll('tbody th')].map((n) => n.textContent)
    expect(rowHeads).toEqual(['Size', 'Extra space', 'Other'])
  })

  it('题面保留题号，并把数据里的点串渲染成实线答题位', () => {
    renderPanel()
    const blanks = [...document.querySelectorAll('.qp-blank')].map((n) => n.textContent)
    expect(blanks).toHaveLength(10)
    expect(blanks[0]).toBe('1')
    expect(blanks[1]).toBe('2')
    expect(document.querySelectorAll('.qp-blank-line')).toHaveLength(10)
    // JSX 节点之间也要有真实空格，复制文本时不能粘成 `Full name:1`。
    expect(document.querySelector('.qp-line')!.textContent).toContain('Full name: 1')
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

  it('signpost 是审核元数据，不印在考生题面上', async () => {
    renderPanel()
    expect(document.querySelector('.qp-paper')!.textContent).not.toContain(
      'Personal details taken by phone',
    )
    await userEvent.click(toggle())
    expect(document.querySelector('.qp-audit')!.textContent).toContain(
      '定位：Personal details taken by phone',
    )
  })
})

describe('显示答案和证据 开关', () => {
  it('默认关闭，只显示考生可见的题面', () => {
    renderPanel()
    expect(toggle()).not.toBeChecked()
    expect(document.querySelector('.qp-reveals')).toBeNull()
    expect(document.querySelector('.qp-audit')).toBeNull()
    expect(document.querySelectorAll('.qp-blank')).toHaveLength(10)
    expect(screen.getByText(/盲看模式/)).toBeInTheDocument()
  })

  it('主动打开后逐题给出绿色答案与灰色斜体原文', async () => {
    renderPanel()
    await userEvent.click(toggle())
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
    await userEvent.click(toggle())
    const turnButtons = [...document.querySelectorAll('.qp-turn')]
    expect(turnButtons).toHaveLength(10)
    // 显示的是「对话第 N 轮」而不是 turn_index：turn_index 把旁白也算在内。
    expect(turnButtons[0]!.textContent).toMatch(/^对话第 \d+ 轮$/)
    await userEvent.click(turnButtons[0]!)
    expect(jumps).toEqual([pkg.evidence[0]!.turn_index])
  })

  it('默认页面上没有任何 answer key、evidence、blueprint 或审核字样', () => {
    renderPanel()
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

    // 版式名、旁白窗口、group_id 同样是内部信息：真实试卷上没有哪一组标题旁边印着「表格」。
    expect(document.querySelector('.qp-audit')).toBeNull()
    expect(haystack).not.toMatch(/旁白窗口/)
    for (const label of ['表单 Form', '笔记 Note', '表格 Table']) {
      expect(haystack, label).not.toContain(label)
    }
    for (const group of pkg.question_face.groups) {
      expect(haystack, group.group_id).not.toContain(group.group_id)
    }
  })

  it('打开开关时内部信息在独立审核区里，不混在题面之中', async () => {
    renderPanel()
    await userEvent.click(toggle())
    const strips = [...document.querySelectorAll('.qp-audit')]
    expect(strips).toHaveLength(pkg.question_face.groups.length)
    expect(strips[0]!.textContent).toContain('旁白窗口')
    expect(strips[0]!.textContent).toContain(pkg.question_face.groups[0]!.group_id)
    // 关键是位置：审核带在 .qp-paper 之外，所以考生题面那一块从上到下没有内部字段。
    for (const paper of document.querySelectorAll('.qp-paper')) {
      expect(paper.querySelector('.qp-audit')).toBeNull()
      expect(paper.textContent).not.toContain('旁白窗口')
    }
  })

  it('打开后再关闭，答案随开关出现和消失', async () => {
    renderPanel()
    expect(document.querySelectorAll('.qp-inline-answer')).toHaveLength(0)
    await userEvent.click(toggle())
    expect(document.querySelectorAll('.qp-inline-answer')).toHaveLength(10)
    await userEvent.click(toggle())
    expect(document.querySelectorAll('.qp-inline-answer')).toHaveLength(0)
  })
})

describe('查看题解', () => {
  it('默认折叠，展开后只显示后端已有的事实', async () => {
    renderPanel()
    await userEvent.click(toggle())
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
    await userEvent.click(toggle())
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
  it('缺字段时题解只少几行，绝不由前端补一段解析', async () => {
    render(
      <QuestionPreviewPanel
        pkg={offContract<typeof pkg>({ ...pkg, answer_key: [], evidence: [] })}
        blueprint={null}
        view={view}
      />,
    )
    await userEvent.click(toggle())
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
    await userEvent.click(toggle())
    await userEvent.click(screen.getAllByRole('button', { name: '查看题解' })[0]!)
    const labels = [...document.querySelectorAll('.qp-facts dt')].map((n) => n.textContent)
    // 只剩题面自己声明得出的那一条。考点类型（要 blueprint）与计分口径（要 answer_key）都不见了，
    // 而不是被一句「本题考查听力细节」之类的话顶上。
    expect(labels).toEqual(['答案形态'])
  })
})

/**
 * booking-hotel 的回归夹具。
 *
 * 取自线上那一份 `20260808-booking-hotel-5ee1cbb2` 的题面，精简到两组四题——保留的是这次要修的四处
 * 缺陷各自的形状，去掉的是与排版无关的六道题。**只有题面**：答案、证据、题号顺序都不在这次改动范围内，
 * 所以这个夹具连 `answer_key` / `evidence` 都不带（组件对缺失如实显示，见上面「缺字段」那组测试）。
 *
 * 修改前（线上那一份）与修改后各一份，同一批断言跑两遍——「对照」如果只写在提交说明里，下一次改动就
 * 没有任何东西能说出它退回去了。
 */
const BH_BEFORE = {
  reference: 'Part 1',
  test_package: 'Test 1',
  material_id: 'seabrook-hotel-reservation',
  question_face: {
    instructions: [
      {
        group_id: 'booking_details',
        question_range: '1-2',
        // 非标准措辞：真实试卷只说 "Complete the form below."
        instruction_text:
          'Complete the booking record. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.',
        word_limit: 'NO MORE THAN TWO WORDS AND/OR A NUMBER',
        numeral_allowance: 1,
      },
      {
        group_id: 'hotel_information',
        question_range: '3-4',
        instruction_text:
          'Complete the hotel information table. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.',
        word_limit: 'NO MORE THAN TWO WORDS AND/OR A NUMBER',
        numeral_allowance: 1,
      },
    ],
    groups: [
      {
        group_id: 'booking_details',
        narrator_window_id: 1,
        layout: 'form',
        // 泛化 signpost：这一句能原样搬到任何一份 Part 1 材料上。
        signposts: ['Reservation details for the city break are confirmed.'],
        structure: { row_labels: ['Family name', 'Arrival date'] },
      },
      {
        group_id: 'hotel_information',
        narrator_window_id: 2,
        // 伪 Table：只有一个内容列，列标签还是 `Detail` 这种填充词。
        layout: 'table',
        signposts: ['Details for arrival and use of the hotel are explained.'],
        structure: {
          row_header_label: 'Topic',
          row_labels: ['Included item', 'Guest facility'],
          column_labels: ['Detail'],
        },
      },
    ],
    questions: [
      // label 与 carrier 重复：`Family name` / `Family name for the reservation:`。
      {
        number: 1,
        group_id: 'booking_details',
        carrier_before: 'Family name for the reservation:',
        blank: '1 ................',
        carrier_after: '',
        blank_position: 'final',
        answer_category: 'person_name',
        response_form: 'word',
      },
      {
        number: 2,
        group_id: 'booking_details',
        carrier_before: 'Arrival is scheduled for',
        blank: '2 ................',
        carrier_after: 'at the hotel.',
        blank_position: 'medial',
        answer_category: 'date',
        response_form: 'phrase',
      },
      {
        number: 3,
        group_id: 'hotel_information',
        carrier_before: 'Included with the rate:',
        blank: '3 ................',
        carrier_after: '',
        blank_position: 'final',
        answer_category: 'service',
        response_form: 'word',
      },
      {
        number: 4,
        group_id: 'hotel_information',
        carrier_before: 'Facility available to guests:',
        blank: '4 ................',
        carrier_after: '',
        blank_position: 'final',
        answer_category: 'facility',
        response_form: 'phrase',
      },
    ],
  },
  answer_key: [],
  evidence: [],
}

/** 同一份题面按新规则改过之后：标准 instruction、全大写标题、具体 signpost、Q3-Q4 改 note、label 与 carrier 分工。 */
const BH_AFTER = {
  ...BH_BEFORE,
  question_face: {
    instructions: [
      {
        ...BH_BEFORE.question_face.instructions[0]!,
        instruction_text:
          'Complete the form below. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.',
      },
      {
        ...BH_BEFORE.question_face.instructions[1]!,
        instruction_text:
          'Complete the notes below. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.',
      },
    ],
    groups: [
      {
        ...BH_BEFORE.question_face.groups[0]!,
        title: 'HOTEL BOOKING',
        signposts: ['The receptionist takes the details of the booking.'],
      },
      {
        group_id: 'hotel_information',
        narrator_window_id: 2,
        layout: 'note',
        title: 'ARRIVAL AND FACILITIES',
        signposts: ['The receptionist goes through what the rate includes.'],
        // 两层：主项 + 子项，名字取自对话本身。
        structure: {
          note_sections: [
            { heading: 'Included in the rate', question_numbers: [3] },
            { heading: 'For guests to use', question_numbers: [4] },
          ],
        },
      },
    ],
    questions: [
      // 标签负责命名字段，carrier 只补这一行还需要的东西——所以空格落在行首。
      {
        ...BH_BEFORE.question_face.questions[0]!,
        carrier_before: '',
        carrier_after: '(in block capitals)',
        blank_position: 'initial',
      },
      BH_BEFORE.question_face.questions[1]!,
      {
        ...BH_BEFORE.question_face.questions[2]!,
        carrier_before: 'Included with the room rate:',
      },
      {
        ...BH_BEFORE.question_face.questions[3]!,
        carrier_before: 'Open to guests all day:',
      },
    ],
  },
}

describe('booking-hotel 回归夹具：修改前后对照', () => {
  const renderBH = (pkg: unknown) =>
    render(
      <QuestionPreviewPanel pkg={offContract<typeof QUESTION_PACKAGE>(pkg)} blueprint={null} view={null} />,
    )

  it('修改前：伪 Table 画成 <table>，label 与 carrier 印两遍，signpost 泛化', () => {
    renderBH(BH_BEFORE)
    // 一个内容列的 table 照样画成表格——这正是要靠审核规则而不是靠渲染去发现的东西。
    const table = document.querySelector('.qp-table')!
    expect(table.tagName).toBe('TABLE')
    expect([...table.querySelectorAll('thead th')].map((n) => n.textContent)).toEqual([
      'Topic',
      'Detail',
    ])
    // 重复：`Family name`（标签）+ `Family name for the reservation:`（carrier）。
    const form = document.querySelector('.qp-form')!
    expect(form.querySelector('.qp-form-label')!.textContent).toBe('Family name')
    expect(form.textContent).toContain('Family name for the reservation:')
    // 非标准 rubric 原样在页面上；泛化 signpost 是审核元数据，不印给考生。
    expect(screen.getByText(/Complete the booking record\./)).toBeInTheDocument()
    expect(document.querySelector('.qp-paper')!.textContent).not.toContain(
      'Reservation details for the city break are confirmed.',
    )
    // 题面上没有标题可印。
    expect(document.querySelector('.qp-title')).toBeNull()
  })

  it('修改后：Q3-Q4 是 note，标题全大写，rubric 用标准措辞，标签与 carrier 不重复', () => {
    renderBH(BH_AFTER)
    expect(document.querySelector('.qp-table')).toBeNull()
    expect(document.querySelectorAll('.qp-note-list')).toHaveLength(2)
    // 每个标题紧跟自己对应的题目，不再先列完标题再统一列题。
    const heads = [...document.querySelectorAll('.qp-note-head')]
    expect(heads.map((n) => n.textContent)).toEqual(['Included in the rate', 'For guests to use'])
    const sections = [...document.querySelectorAll('.qp-note-section')]
    expect(sections.map((n) => n.querySelector('.qp-blank')?.textContent)).toEqual(['3', '4'])

    const titles = [...document.querySelectorAll('.qp-title')].map((n) => n.textContent)
    expect(titles).toEqual(['HOTEL BOOKING', 'ARRIVAL AND FACILITIES'])
    // 标题里不带题型标签，也不带窗口编号。
    for (const title of titles) {
      expect(title!).not.toMatch(/form|table|note|window|窗口|Questions/i)
    }

    const rubrics = [...document.querySelectorAll('.qp-rubric')].map((n) => n.textContent)
    expect(rubrics).toEqual([
      'Complete the form below. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.',
      'Complete the notes below. Write NO MORE THAN TWO WORDS AND/OR A NUMBER for each answer.',
    ])

    // Q1：标签命名字段，carrier 只补限定语，所以空格在行首而不是行末。
    const form = document.querySelector('.qp-form')!
    expect(form.querySelector('.qp-form-label')!.textContent).toBe('Family name')
    expect(form.textContent).not.toContain('Family name for the reservation')
    expect(form.textContent).toContain('(in block capitals)')
  })

  it('修改后的题面默认只剩考生该读的四层，顺序即印刷顺序', () => {
    renderBH(BH_AFTER)

    const paper = document.querySelector('.qp-paper')!
    const order = [...paper.children].map((el) => el.className.split(' ')[0])
    expect(order).toEqual(['qp-range', 'qp-rubric', 'qp-title', 'qp-form'])
    expect(paper.querySelector('.qp-range')!.textContent).toBe('Questions 1-2')
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
