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

  it('warns at material level and flags the card when an anchor is stale', () => {
    render(<MaterialReader view={view('anchorMismatch', 'mis')} />)
    expect(screen.getByText(/旁注可能错位/)).toBeInTheDocument()
    expect(screen.getByText('旁注位置可疑')).toBeInTheDocument()
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
