/**
 * 对比详情页的两处交互修复。
 *
 * 这个模块以前没有测试，于是两个缺陷一直活着：
 *
 * 1. **「切换对比」的三元判断两个分支返回同一个值**
 *    `setPair(([a]) => (a === i ? [a, i] : [a, i]))`——写着一个条件，两边一模一样，所以它实际
 *    的行为是「永远换右栏」。点左栏自己那个按钮会把右栏也换成它，两栏于是同一套材料自己跟自己比。
 *    这种 bug 类型检查抓不到（两个分支类型相同），只有断言行为才能抓到。
 *
 * 2. **没有回去的路**
 *    用户从结果页点进来，这一页却没有任何返回入口，只剩浏览器后退键——而它是全宽布局、和结果页
 *    长得不像，读起来像是离开了那个批次。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { MaterialRecord } from '@/contracts/api'
import { buildRecord } from '@/mocks/fixtures'
import { useBatchStore } from '@/stores/batchStore'
import { ComparePage } from './ComparePage'

const SCENARIO = 'accommodation-rental'
const BATCH = 'web-1785400000000-1'

/** 这一页会去查音频状态（选定确认框的文案要知道有没有音频）。离线跑，答一个「没有」。 */
vi.mock('@/api/endpoints', () => ({
  api: {
    getAudio: () => Promise.resolve({ status: 'none' }),
    listMaterials: () => Promise.resolve({ materials: [] }),
    selectMaterial: () => Promise.resolve({ ok: true }),
  },
}))

function records(n: number): MaterialRecord[] {
  const kinds = ['balanced', 'clustered', 'failed', 'balanced'] as const
  return Array.from({ length: n }, (_, i) =>
    buildRecord(kinds[i] ?? 'balanced', {
      materialId: `m${i + 1}`,
      batchId: BATCH,
      scenarioKey: SCENARIO,
      index: i,
    }),
  )
}

/** 把材料放进 store，就像它们刚从 SSE 到达一样——这一页优先读 store。 */
function seed(list: MaterialRecord[]) {
  const store = useBatchStore.getState()
  store.initBatch({
    batchId: BATCH,
    total: list.length,
    requested: [{ scenarioKey: SCENARIO, count: list.length }],
    items: list.map((r) => ({
      material_id: r.material_id,
      scenario_key: r.scenario_key,
      index: r.index,
      status: 'done' as const,
      stage: 're_auditing' as const,
      attempt: 1,
    })),
  })
  for (const record of list) {
    useBatchStore.getState().applyEvent({
      event: 'material',
      seq: record.index + 2,
      material_id: record.material_id,
      scenario_key: record.scenario_key,
      index: record.index,
      verdict: record.verdict,
      audit_rejection: record.audit_rejection ?? null,
      degraded: record.degraded ?? false,
      material: record.material,
      blueprint: record.blueprint,
      audit: record.audit,
      cross_check: record.cross_check,
    } as never)
  }
}

function renderPage(query = '') {
  return render(
    <MemoryRouter initialEntries={[`/compare/${SCENARIO}${query}`]}>
      <Routes>
        <Route path="/compare/:scenarioKey" element={<ComparePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

/**
 * 当前并排的两栏各是哪套材料，按栏的顺序（即 `pair` 的顺序）。
 *
 * 读 `data-material` 而不是找「候选 X」那几个字：每栏内部到处都出现它——结论句、选定按钮、
 * 顶部的切换按钮，`getByText` 会直接报 "Found multiple elements"。
 */
function shownPair(): string[] {
  return [...document.querySelectorAll('.cmp-col')].map(
    (col) => (col as HTMLElement).dataset.material!,
  )
}

beforeEach(() => {
  useBatchStore.getState().reset()
})

afterEach(() => {
  useBatchStore.getState().reset()
})

describe('返回批次', () => {
  it('顶部有回到这一批的入口，指向材料自己的 batch_id', async () => {
    seed(records(2))
    renderPage()

    const back = await screen.findByRole('link', { name: /返回批次/ })
    // 用材料自带的 batch_id：从历史批次点进来时 store 装的是当前活批次，用它会把用户送回另一批。
    expect(back.getAttribute('href')).toBe(`/batches/${BATCH}`)
  })

  it('一套材料都没有时也给得出出路', async () => {
    // 这一屏最需要返回入口：页面上除了「本场景暂无材料」什么都没有。
    seed([])
    useBatchStore.setState({ batchId: BATCH })
    renderPage()

    await waitFor(() => expect(screen.getByText('本场景暂无材料。')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: /返回批次/ }).getAttribute('href')).toBe(
      `/batches/${BATCH}`,
    )
  })
})

describe('切换对比', () => {
  it('点右栏那个候选换掉右栏', async () => {
    seed(records(3))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    await userEvent.click(screen.getByRole('button', { name: '候选 C' }))
    expect(shownPair()).toEqual(['m1', 'm3'])
  })

  /**
   * 这一条就是那个 bug。
   *
   * 原来的三元两个分支都返回 `[a, i]`，所以点左栏自己那个按钮也会去改右栏——右栏被换成左栏那一套，
   * 两栏同一份材料，对比结论变成「跟自己比，完全一样」。
   */
  it('点左栏自己那个候选不会把右栏也变成它', async () => {
    seed(records(3))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    await userEvent.click(screen.getByRole('button', { name: '候选 A' }))
    // 原地不动：A 已经在对比里了。
    expect(shownPair()).toEqual(['m1', 'm2'])
  })

  it('点已在右栏的那个候选也不动', async () => {
    seed(records(3))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    await userEvent.click(screen.getByRole('button', { name: '候选 B' }))
    expect(shownPair()).toEqual(['m1', 'm2'])
  })

  it('两栏永远不是同一套材料', async () => {
    // 上面三条的共同不变式，单独说一遍：一套材料跟自己比得不出任何结论，而页面会照样渲染出
    // 一张「两边一模一样」的对照表，看起来像功能正常。
    seed(records(4))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toHaveLength(2))
    for (const name of ['候选 A', '候选 B', '候选 C', '候选 D']) {
      await userEvent.click(screen.getByRole('button', { name }))
      const [left, right] = shownPair()
      expect(left, `点了 ${name} 之后两栏相同`).not.toBe(right)
    }
  })
})
