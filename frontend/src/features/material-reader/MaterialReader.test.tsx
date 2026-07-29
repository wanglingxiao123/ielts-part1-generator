import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { buildRecord } from '@/mocks/fixtures'
import { MaterialReader } from './MaterialReader'

const O = { batchId: 'b', scenarioKey: 's', index: 0 }
const view = (kind: Parameters<typeof buildRecord>[0], id: string) =>
  joinFromRecord(buildRecord(kind, { ...O, materialId: id }))

describe('MaterialReader', () => {
  /**
   * 说话人标签就是材料 JSON 里的 speaker 编号。
   *
   * 这条断言原来钉的是「需求方」——我们自己起的角色名。客户明确要求「是 speak1 和 speak2，而不是
   * 你现在的信息持有方和需求方什么的」，所以钉的东西换成编号，意图不变：每一轮都得标出**是谁在
   * 说**，标签必须和 JSON 对得上。旁白仍要看得出来（它不参与对话、不计入轮次），写法是
   * `speaker1` 加一个「旁白」限定语，而不是换成另一个编造的名字。
   */
  it('labels every turn with its speaker id, marking the narrator as such', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const turns = document.querySelectorAll('[data-turn]')
    expect(turns).toHaveLength(43)

    const narration = document.querySelector('[data-turn="0"]')!
    expect(narration.className).toContain('narration')
    expect(narration.querySelector('.role')?.textContent).toContain('speaker1')
    expect(narration.querySelector('.role')?.textContent).toContain('旁白')

    const dialogue = document.querySelector('[data-turn="4"]')!
    expect(dialogue.querySelector('.role')?.textContent).toContain('speaker3')
    // 对话轮标轮次序号，不标「旁白」。
    expect(dialogue.querySelector('.role')?.textContent).not.toContain('旁白')

    // 编造的角色名一个都不该再出现在页面上。
    const body = document.body.textContent!
    for (const invented of ['信息持有方', '需求方']) {
      expect(body).not.toContain(invented)
    }
  })

  it('highlights evidence inside the anchored turn only', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const turn4 = document.querySelector('[data-turn="4"]')!
    const mark = turn4.querySelector('mark')
    expect(mark?.textContent).toContain("It's Anna Woods.")
    expect(mark?.getAttribute('data-items')).toBe('1')
    // A turn with no information point must carry no highlight.
    expect(document.querySelector('[data-turn="5"] mark')).toBeNull()
  })

  it('shows one cluster card labelled with the turn range for the clustered fixture', () => {
    render(<MaterialReader view={view('clustered', 'clu')} />)
    const clusters = document.querySelectorAll('.ann-card[data-cluster="true"]')
    expect(clusters).toHaveLength(1)
    expect(clusters[0]!.textContent).toContain('3 点集中于 turn 27–29')
  })

  it('shows no cluster card for the balanced fixture', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    expect(document.querySelectorAll('.ann-card[data-cluster="true"]')).toHaveLength(0)
    expect(document.querySelectorAll('.ann-card').length).toBeGreaterThan(0)
  })

  /**
   * 这一条原来钉的是相反的行为：材料级红色横幅「⚠ 本材料旁注可能错位，请勿据此判断」加上
   * 逐条旁注的「旁注位置可疑」。客户明确否掉了这种做法：
   *
   *   > 底线：用户看到的永远是「成品」，不是「带已知 bug 的半成品 + 修复建议」。
   *
   * 原意图（旁注不许贴在错的句子旁边）一点没丢，只是实现方式换了：能确定挪正的静默挪正，
   * 确定不了的那一条不显示。这里钉的是同一件事的新形态。
   */
  it('silently anchors a relocatable annotation to the right sentence, saying nothing', () => {
    const v = view('anchorMismatch', 'mis')
    render(<MaterialReader view={v} />)

    // 第 3 题的 evidence 只在 turn 10 出现，旁注和高亮都在那里，且旁注上的坐标也是 10。
    const mark = document.querySelector('[data-turn="10"] mark')!
    expect(mark.getAttribute('data-items')).toBe('3')
    expect(mark.textContent).toContain("It's BT14 9BJ.")
    expect(document.querySelector('[data-turn="14"] mark')).toBeNull()
    const card = [...document.querySelectorAll('.ann-card')].find((c) =>
      c.textContent!.includes("It's BT14 9BJ."),
    )!
    expect(card.textContent).toContain('turn 10')
    expect(card.textContent).not.toContain('turn 14')

    // 十条旁注一条不少（这一条挪正了，不是被剔除）。
    const numbered = new Set(
      [...document.querySelectorAll('.ann-item .num')].map((n) => n.textContent),
    )
    expect(numbered.size).toBe(10)

    // 页面上没有任何一句「我可能标错了」。
    const body = document.body.textContent!
    for (const forbidden of [
      '旁注可能错位',
      '旁注位置可疑',
      '标错了位置',
      '不相干的句子',
      '请核对高亮位置',
      '请勿据此判断',
      '退回重新生成',
    ]) {
      expect(body, forbidden).not.toContain(forbidden)
    }
  })

  /**
   * 挪不了的那一条：evidence 在脚本里根本不存在，任何落点都是猜。客户的规则是
   * 「直接去掉这条旁注，返回干净的材料」。另外九条照常显示，页面上一句告警都没有。
   */
  it('drops an unresolvable annotation and shows the other nine, with no warning', () => {
    const v = view('anchorUnresolvable', 'unres')
    render(<MaterialReader view={v} />)

    const numbers = [...document.querySelectorAll('.ann-item .num')].map((n) => n.textContent)
    expect(numbers).toHaveLength(9)
    expect(numbers).not.toContain('③')
    // 高亮里也不会出现第 3 题。
    for (const mark of document.querySelectorAll('mark')) {
      expect(mark.getAttribute('data-items')!.split(',')).not.toContain('3')
    }
    // 存档侧仍是十个点：剔除只发生在显示层（校验器要求恰好 10 个）。
    expect(v.blueprint.items).toHaveLength(10)

    const body = document.body.textContent!
    for (const forbidden of ['旁注', '标错', '定位', '错位', '核对', '可疑']) {
      expect(body, forbidden).not.toContain(forbidden)
    }
  })

  /**
   * The strip's numeric readout (CV / 均匀度 / 间隔序列) was replaced by a
   * plain-language verdict: a question-writer needs the conclusion, not the
   * coefficient. This test now pins the conclusion — and, crucially, that the
   * jargon is GONE, since a stray metric row would put the app right back where
   * the client found it.
   */
  it('states the distribution verdict in plain language, with no metric jargon', () => {
    render(<MaterialReader view={view('clustered', 'clu')} />)
    // 说明文字重写过：原来那句「点挨在一起，就是原文里真的挨在一起」意思对但是口语。这里钉的仍是
    // 同一个意思——点位不做避让，所以重叠是真实信号，不是画错了。
    expect(screen.getByText(/点位不作避让，重叠即原文中相邻/)).toBeInTheDocument()
    expect(document.querySelector('.strip')!.textContent).not.toContain('就是原文里真的')

    const strip = document.querySelector('.strip')!.textContent!
    // The clustered fixture bunches 6/7/8 → "能出题，但…建议先改", and the
    // cluster is named where the reviewer can act on it.
    expect(strip).toContain('建议先改')
    expect(strip).toContain('⑥⑦⑧ 挤在 turn 27–29')
    expect(strip).toContain('考生来不及记')
    // Its 14-turn hole is reported as an empty stretch, not as "最大间隔 14".
    expect(strip).toContain('14 轮')
    // Front/back balance is still surfaced, in question-group terms.
    expect(strip).toContain('第 1 组 5 题 / 第 2 组 5 题')
    // Order is correct in this fixture, so that check reads as passing.
    expect(strip).toContain('没有回跳')

    for (const jargon of ['CV', '均匀度', '间隔序列', '阈值待校准', '最大间隔']) {
      expect(strip).not.toContain(jargon)
    }
    expect(document.querySelector('.strip-metrics')).toBeNull()
    expect(screen.getByText(/3 点挤在 turn 27–29/)).toBeInTheDocument()
  })

  /**
   * The balanced fixture must clear the two checks it actually passes — order and
   * pace — rather than being tarred by its one real defect. A verdict that says
   * "有问题" without saying WHICH is the jargon problem in a new costume.
   *
   * Its remaining complaint is genuine and independently corroborated: the
   * fixture has two 8-turn stretches with no point in them, which its own audit
   * records as `minor: 中段存在较长信息空档`.
   */
  it('clears the checks the balanced fixture passes and names only its real gap', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const strip = document.querySelector('.strip')!.textContent!
    expect(strip).toContain('没有回跳')
    expect(strip).toContain('考生有时间写下答案')
    expect(strip).not.toContain('挤在')
    // The one finding, located so a writer can act on it.
    expect(strip).toContain('④ 与 ⑤ 之间空了 8 轮')
  })

  /**
   * 评价建议现在**只**在这一页。客户的原话就是拿这句话举的例：
   *
   *   > 阅读全文页面里可以展示评价建议（如「⑤⑥之间空了 6 轮，可考虑补细节或压缩闲聊」），
   *   > 因为用户在看全文时才有上下文理解这个建议的含义。
   *
   * 卡片那边已经删空（BatchResults.test.tsx 逐字钉了它不出现），所以这一条是那段文案
   * 唯一的落脚处——它要是也丢了，审阅者就再没有地方看到这些建议了。
   */
  it('is where the quality advice lives, in the wording the client quoted', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const strip = document.querySelector('.strip')!
    const text = strip.textContent!
    // 客户举例那句的形状：点号 + 空了 N 轮 + 该怎么办。
    expect(text).toMatch(/[①-⑩] 与 [①-⑩] 之间空了 \d+ 轮/)
    expect(text).toContain('补一个可考细节或压缩闲聊')
    // 四条判断各占一行，标签是命题人的说法。
    const labels = [...strip.querySelectorAll('.verdict-label')].map((l) => l.textContent)
    expect(labels).toEqual(['题号顺序', '记录节奏', '全篇覆盖', '前后两组题量'])
    // 一句话总结在最上面，所以不必逐行读完才知道能不能出题。
    expect(strip.querySelector('.verdict-headline')!.textContent).toMatch(/出题/)
  })

  /**
   * The annotation leads with what the point tests and its answer, and carries a
   * badge only when the badge says something. 非干扰 / 未确认 / 第 N 组 were
   * pure negatives or internal coordinates.
   */
  it('annotates a point with its type, answer and only informative badges', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const card = document.querySelector('.ann-card')!
    const text = card.textContent!
    expect(text).toContain('姓名/专名') // 考什么, not `name`
    expect(text).toContain('Anna Woods') // 答案
    expect(text).toContain('须拼读')
    expect(text).toContain('有复述确认')
    expect(text).toContain('turn 4') // navigation coordinate earns its place
    for (const dropped of ['非干扰', '未确认', '第 1 组', 'item_form']) {
      expect(text).not.toContain(dropped)
    }
  })

  /** A distractor names WHICH mechanism, in the spec's own vocabulary (§4B-4). */
  it('names the distraction mechanism rather than a bare 干扰项 flag', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const cards = [...document.querySelectorAll('.ann-card')].map((c) => c.textContent!)
    // Point 5 is the correction target (先说后改); point 7's answer word is the
    // one referred to indirectly (同义替换).
    expect(cards.some((t) => t.includes('先说后改'))).toBe(true)
    expect(cards.some((t) => t.includes('同义替换'))).toBe(true)
    expect(cards.every((t) => !t.includes('干扰项'))).toBe(true)
  })

  it('places overview strip point marks at true position without avoidance', () => {
    // Points 6 and 7 both sit on turn 27 → identical left offset. The strip must
    // NOT spread them horizontally; that would fake an even distribution.
    render(<MaterialReader view={view('clustered', 'clu')} />)
    const marks = [...document.querySelectorAll<HTMLElement>('.axis-point')]
    expect(marks).toHaveLength(10)
    const lefts = marks.map((m) => m.style.left)
    const p6 = marks.find((m) => m.textContent === '⑥')!
    const p7 = marks.find((m) => m.textContent === '⑦')!
    expect(p6.style.left).toBe(p7.style.left)
    // ...but they are stacked vertically so neither is fully occluded.
    expect(p6.style.top).not.toBe(p7.style.top)
    // The clustered fixture must show fewer distinct x positions than points.
    expect(new Set(lefts).size).toBeLessThan(marks.length)
  })

  it('hides the annotation column in narrow (compare) mode and shows no audio', () => {
    render(<MaterialReader view={view('balanced', 'bal')} narrow />)
    expect(document.querySelectorAll('.ann-card')).toHaveLength(0)
    expect(document.querySelector('.reader-body')?.className).toContain('narrow')
    // Inline numbered badges remain, so points are still locatable.
    expect(document.querySelectorAll('.inline-badge').length).toBeGreaterThan(0)
    expect(document.querySelector('audio')).toBeNull()
    expect(document.querySelector('.player')).toBeNull()
  })
})
