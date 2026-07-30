/**
 * 对比页那几个规范维度的判据，以及那段摘要。
 *
 * 这里最要紧的不是「函数算得对」，而是**它说的话里不能出现被删掉的那些概念**。客户的要求是对比页
 * 只讲规范维度（§2-§4、§6），不讲评价方的内部指标：分数、听不出来、计划外的可考细节、出题就绪度。
 * 摘要是唯一一段自由拼接的文字，所以它是最容易把那些词漏回来的地方——最后一组测试专门守这条。
 */
import { describe, expect, it } from 'vitest'
import { joinFromRecord } from './joinArtifacts'
import { computeDistribution } from './distribution'
import { getThresholds } from '@/config/runtimeConfig'
import { buildRecord, type FixtureKind } from '@/mocks/fixtures'
import type { ViewMaterial } from './types'
import { compareSummary, distractionCounts, lengthFacts, TURN_RANGE, WORD_RANGE } from './compareFacts'

const thresholds = getThresholds()

function viewOf(kind: FixtureKind): ViewMaterial {
  return joinFromRecord(
    buildRecord(kind, {
      materialId: `m-${kind}`,
      batchId: 'b1',
      scenarioKey: 'accommodation-rental',
      index: 0,
    }),
  )
}

function sideOf(label: string, kind: FixtureKind) {
  const view = viewOf(kind)
  return { label, view, metrics: computeDistribution(view, thresholds) }
}

/** 改掉篇幅数字，用来构造越线的一侧。 */
function withLength(view: ViewMaterial, words: number, turns: number): ViewMaterial {
  return {
    ...view,
    audit: { ...view.audit, metrics: { ...view.audit.metrics, dialogue_words: words, dialogue_turns: turns } },
  }
}

/* ── 篇幅 ─────────────────────────────────────────────────────────────────── */

describe('lengthFacts', () => {
  it('区间内一律合格，不再细分', () => {
    const view = viewOf('balanced')
    const at = (w: number, t: number) => lengthFacts(withLength(view, w, t))
    // 450 和 750 是**含**在合格区间里的，边界不能算越线。
    expect(at(450, 20).ok).toBe(true)
    expect(at(750, 48).ok).toBe(true)
    expect(at(600, 35).ok).toBe(true)
  })

  it('越线的那一项带上区间，没越线的不带', () => {
    const view = viewOf('balanced')
    const over = lengthFacts(withLength(view, 800, 35))
    expect(over.ok).toBe(false)
    expect(over.wordsInRange).toBe(false)
    expect(over.turnsInRange).toBe(true)
    expect(over.text).toContain(`须 ${WORD_RANGE[0]}-${WORD_RANGE[1]}`)
    // 轮次合格，所以那一半不该出现区间——否则读起来像两项都有问题。
    expect(over.text).not.toContain(`须 ${TURN_RANGE[0]}-${TURN_RANGE[1]}`)
  })

  /**
   * 600-650 是 20 套真题的**观测典型值**，不是命制门槛。
   *
   * 这个判断在项目里栽过一次：后端的 `validate_part1.py` 曾把它当硬门槛，warning 也返回失败码。
   * 把 660 词标成异常会让出题人以为一套完全合格的材料有问题。
   */
  it('不把 600-650 当门槛', () => {
    const view = viewOf('balanced')
    for (const words of [460, 660, 740]) {
      const facts = lengthFacts(withLength(view, words, 35))
      expect(facts.ok, `${words} 词应合格`).toBe(true)
      expect(facts.text).not.toContain('600')
      expect(facts.text).not.toContain('650')
    }
  })
})

/* ── 干扰机制 ─────────────────────────────────────────────────────────────── */

describe('distractionCounts', () => {
  it('按种类给出计数和点号', () => {
    const counts = distractionCounts(viewOf('balanced'))
    expect(counts.length).toBeGreaterThan(0)
    for (const c of counts) {
      expect(c.count).toBe(c.numbers.length)
      expect(c.count).toBeGreaterThan(0)
      expect(c.label).toBeTruthy()
      // 点号升序：并排看两侧时顺序不一致会对不上。
      expect([...c.numbers].sort((a, b) => a - b)).toEqual(c.numbers)
    }
  })

  it('种类顺序固定，不随出现次数变', () => {
    // 两侧并排时同一行必须是同一个机制，所以顺序是 先说后改 → 同义替换 → 干扰，
    // 而不是按次数排序。
    const order = ['correction', 'paraphrase', 'unspecified']
    for (const kind of ['balanced', 'clustered'] as const) {
      const kinds = distractionCounts(viewOf(kind)).map((c) => c.kind)
      const expected = order.filter((k) => kinds.includes(k as never))
      expect(kinds, kind).toEqual(expected)
    }
  })

  it('没有干扰点时返回空数组，而不是一堆 0', () => {
    const view = viewOf('balanced')
    // `items` 是定长 10 元组（契约要求恰好十个点），所以逐个改而不是 map——map 返回普通数组，
    // 类型上就不再是那个元组了。
    const items = view.blueprint.items.map((i) => ({ ...i, distractor: false })) as
      typeof view.blueprint.items
    // `correction` 是契约里的必填字段，删不掉；给一个对不上任何点的值，效果等同「没有干扰」。
    const noDistractor: ViewMaterial = {
      ...view,
      blueprint: {
        ...view.blueprint,
        items,
        correction: { earlier: 'zzz-nothing', final: 'zzz-nothing', marker: 'zzz' },
      },
    }
    // 「先说后改 ×0」不是信息，是噪音。
    expect(distractionCounts(noDistractor)).toEqual([])
  })
})

/* ── 对比摘要 ─────────────────────────────────────────────────────────────── */

describe('compareSummary', () => {
  it('说得出共同点和差异，并给一句建议', () => {
    const summary = compareSummary(sideOf('材料 A', 'balanced'), sideOf('材料 B', 'clustered'))
    expect(summary.shared.length + summary.differences.length).toBeGreaterThan(0)
    expect(summary.advice).toBeTruthy()
  })

  it('篇幅越线的那一侧被点名，并且建议指向合规的另一侧', () => {
    const a = sideOf('材料 A', 'balanced')
    const b = sideOf('材料 B', 'clustered')
    const over = { ...a, view: withLength(a.view, 900, 35) }
    const summary = compareSummary(over, b)
    expect(summary.differences.some((d) => d.includes('超出规范区间'))).toBe(true)
    expect(summary.differences.some((d) => d.includes('材料 A'))).toBe(true)
    // 越线是不合格，不是风格差别，所以建议必须给方向。
    expect(summary.advice).toContain('材料 B')
  })

  it('两套都合格时不说篇幅有问题', () => {
    const a = sideOf('材料 A', 'balanced')
    const b = sideOf('材料 B', 'clustered')
    const summary = compareSummary(a, b)
    expect(summary.differences.some((d) => d.includes('超出规范区间'))).toBe(false)
    expect(summary.shared.some((s) => s.includes('篇幅都在合格区间内'))).toBe(true)
  })

  it('篇幅差得不明显时不硬凑一条差异', () => {
    const a = sideOf('材料 A', 'balanced')
    const b = sideOf('材料 B', 'clustered')
    const near = { ...b, view: withLength(b.view, a.view.audit.metrics.dialogue_words + 10, 35) }
    const summary = compareSummary(a, near)
    // 10 词的差别在两栏并排时是看不出来的，说出来只是占一行。
    expect(summary.differences.some((d) => d.includes('篇幅长短不同'))).toBe(false)
  })

  it('同一套跟自己比时如实说「几乎一样」', () => {
    const a = sideOf('材料 A', 'balanced')
    const b = sideOf('材料 B', 'balanced')
    const summary = compareSummary(a, b)
    expect(summary.differences).toEqual([])
    // 没差异时不硬造一个选择理由。
    expect(summary.advice).toContain('几乎一样')
  })

  /**
   * 这是这个文件存在的主要原因。
   *
   * 摘要是对比页上唯一一段自由拼接的文字，所以它是最容易把被删掉的概念漏回来的地方。客户的原话：
   * 「当前对比页展示了大量出题人不关心的内部评价指标（100 分、听不出来、计划外细节、出题就绪度）」。
   */
  it('措辞里不出现任何被删掉的内部指标', () => {
    const forbidden = [
      '分', // 「总分高 2 分」——顺带也挡住「评分」「打分」
      '听不出来',
      '计划外',
      '就绪',
      '倾向', // compareCandidates 的判决口气
      'PASS',
      'FAIL',
      'verdict',
    ]
    const pairs: Array<[FixtureKind, FixtureKind]> = [
      ['balanced', 'clustered'],
      ['clustered', 'failed'],
      ['failed', 'balanced'],
      ['balanced', 'balanced'],
      ['anchorMismatch', 'clustered'],
    ]
    for (const [x, y] of pairs) {
      const s = compareSummary(sideOf('材料 A', x), sideOf('材料 B', y))
      const text = [...s.shared, ...s.differences, s.advice].join(' | ')
      for (const word of forbidden) {
        expect(text, `${x} vs ${y} 的摘要里出现了「${word}」：${text}`).not.toContain(word)
      }
    }
  })

  it('即使一侧被评价判为不达标，摘要也不提这件事', () => {
    // `failed` 这一套带着 audit_rejection。那是评价方的判断，对比页不承载它——
    // 卡片和阅读页各自有说法，这里只讲规范维度。
    const summary = compareSummary(sideOf('材料 A', 'failed'), sideOf('材料 B', 'balanced'))
    const text = [...summary.shared, ...summary.differences, summary.advice].join(' ')
    expect(text).not.toContain('不达标')
    expect(text).not.toContain('缺陷')
  })
})
