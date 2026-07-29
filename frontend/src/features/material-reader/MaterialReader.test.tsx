import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { buildRecord } from '@/mocks/fixtures'
import { MaterialReader } from './MaterialReader'

const O = { batchId: 'b', scenarioKey: 's', index: 0 }
const view = (kind: Parameters<typeof buildRecord>[0], id: string) =>
  joinFromRecord(buildRecord(kind, { ...O, materialId: id }))

describe('MaterialReader', () => {
  it('renders every turn with its index and role', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const turns = document.querySelectorAll('[data-turn]')
    expect(turns).toHaveLength(43)
    expect(document.querySelector('[data-turn="0"]')?.className).toContain('narration')
    expect(document.querySelector('[data-turn="4"]')?.textContent).toContain('需求方')
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
    expect(screen.getByText(/点挨在一起/)).toBeInTheDocument()

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
