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

/**
 * 离线替身。
 *
 * `batchHistoryDetail` 是这一页取历史批次材料的**唯一**通道，所以它在这里是可编程的：
 * `historyDetail` 由每个测试自己摆。以前这一页用的是 `listMaterials`（`GET /materials`），
 * 而真实后端没有那条路由——测试里 mock 答得好好的，线上则是 100% 空白。
 */
let historyDetail: unknown = null
vi.mock('@/api/endpoints', () => ({
  api: {
    getAudio: () => Promise.resolve({ status: 'none' }),
    selectMaterial: () => Promise.resolve({ ok: true }),
    batchHistoryDetail: () =>
      historyDetail
        ? Promise.resolve(historyDetail)
        : Promise.reject(new Error('没有这一批的历史记录')),
  },
}))

/** 把材料摆成 `/api/batch-history/{id}` 的响应形状（web/batch_history.py 的 `derive`）。 */
function asHistoryDetail(list: MaterialRecord[]) {
  return {
    batch_id: BATCH,
    created_at: 1785400000,
    status: 'submitted',
    read_only: true,
    interrupted: false,
    requested_total: list.length,
    submitted_material_ids: [],
    scenarios: [{ scenario_key: SCENARIO, count: list.length }],
    materials: list.map((r) => ({
      material_id: r.material_id,
      scenario_key: r.scenario_key,
      index: r.index,
      verdict: r.verdict,
      audit_rejection: r.audit_rejection ?? null,
      degraded: r.degraded ?? false,
      material: r.material,
      blueprint: r.blueprint,
      audit: r.audit,
      cross_check: r.cross_check,
    })),
  }
}

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
  historyDetail = null
  useBatchStore.getState().reset()
})

afterEach(() => {
  historyDetail = null
  useBatchStore.getState().reset()
})

/**
 * 历史批次的材料必须取得到——这一页在真实部署上**整屏空白**，就是因为取不到。
 *
 * store 只装当前活批次。一个跑完又刷新过的批次（以及从结果页点「打开完整对比」进来的历史批次）
 * 在 store 里是空的，而旧代码的退路是 `GET /materials?scenario_key=`，那条路由真实后端没有。
 * 于是「本场景暂无材料。」是这一页在线上唯一的样子，而所有测试都 mock 答了那个不存在的接口。
 */
describe('历史批次（store 是空的）', () => {
  it('带 ?batch= 时从历史记录取材料', async () => {
    historyDetail = asHistoryDetail(records(2))
    renderPage('?batch=' + BATCH)

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    // 这一行是缺陷本身：以前到这里永远是这句话。
    expect(screen.queryByText('本场景暂无材料。')).not.toBeInTheDocument()
  })

  it('?a=&b= 在历史批次上同样生效', async () => {
    historyDetail = asHistoryDetail(records(3))
    renderPage(`?batch=${BATCH}&a=m1&b=m3`)

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm3']))
  })

  it('没带 ?batch= 且 store 为空时说清没材料，并给出返回入口', async () => {
    // 直接粘链接或收藏夹进来的情形。取不到就如实说，不假装有内容。
    useBatchStore.setState({ batchId: BATCH })
    renderPage()

    await waitFor(() => expect(screen.getByText('本场景暂无材料。')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: /返回批次/ })).toBeInTheDocument()
  })

  it('历史记录读不到时不崩，仍给得出返回入口', async () => {
    historyDetail = null // batchHistoryDetail 会 reject
    renderPage('?batch=' + BATCH)

    await waitFor(() => expect(screen.getByText('本场景暂无材料。')).toBeInTheDocument())
    // batchId 从 URL 里就拿得到，所以这一屏仍然有出路。
    expect(screen.getByRole('link', { name: /返回批次/ }).getAttribute('href')).toBe(
      `/batches/${BATCH}`,
    )
  })
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

/**
 * 「候选 A / B」必须跟着**摆放位置**，不跟材料到达顺序。
 *
 * 用户在结果页点第一张卡（那里明确标 A）进来，左栏摆的确实是那一套，标题却可能写「候选 B」——
 * 原来的标签按 `records` 下标给，而摆放由 `?a=&b=` 决定，两者无关。历史记录的顺序和用户点的顺序
 * 相反就会这样，那是一半的概率。名字是给人指位置用的，所以它必须跟着位置。
 */
describe('候选标签跟着摆放位置', () => {
  it('?a= 指定的那一套在左栏且标 A，哪怕它在记录里排第二', async () => {
    historyDetail = asHistoryDetail(records(2))
    // 故意反着传：m2 当 A、m1 当 B。记录里 m1 在前。
    renderPage(`?batch=${BATCH}&a=m2&b=m1`)

    await waitFor(() => expect(shownPair()).toEqual(['m2', 'm1']))
    const cols = [...document.querySelectorAll('.cmp-col')]
    expect(cols[0]!.querySelector('strong')!.textContent).toBe('候选 A')
    expect(cols[1]!.querySelector('strong')!.textContent).toBe('候选 B')
  })

  it('换掉右栏后标签重新对位', async () => {
    historyDetail = asHistoryDetail(records(3))
    renderPage(`?batch=${BATCH}&a=m1&b=m2`)

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    await userEvent.click(screen.getByRole('button', { name: /^第 3 套/ }))
    expect(shownPair()).toEqual(['m1', 'm3'])
    const cols = [...document.querySelectorAll('.cmp-col')]
    // m3 现在摆在右栏，所以栏标题是 B。
    expect(cols[0]!.querySelector('strong')!.textContent).toBe('候选 A')
    expect(cols[1]!.querySelector('strong')!.textContent).toBe('候选 B')
  })
})

describe('切换对比', () => {
  it('点右栏那个候选换掉右栏', async () => {
    seed(records(3))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    await userEvent.click(screen.getByRole('button', { name: /^第 3 套/ }))
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
    await userEvent.click(screen.getByRole('button', { name: /^第 1 套/ }))
    // 原地不动：它已经在左栏了。
    expect(shownPair()).toEqual(['m1', 'm2'])
  })

  it('点已在右栏的那个候选也不动', async () => {
    seed(records(3))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toEqual(['m1', 'm2']))
    await userEvent.click(screen.getByRole('button', { name: /^第 2 套/ }))
    expect(shownPair()).toEqual(['m1', 'm2'])
  })

  it('两栏永远不是同一套材料', async () => {
    // 上面三条的共同不变式，单独说一遍：一套材料跟自己比得不出任何结论，而页面会照样渲染出
    // 一张「两边一模一样」的对照表，看起来像功能正常。
    seed(records(4))
    renderPage('?a=m1&b=m2')

    await waitFor(() => expect(shownPair()).toHaveLength(2))
    for (const name of [/^第 1 套/, /^第 2 套/, /^第 3 套/, /^第 4 套/]) {
      await userEvent.click(screen.getByRole('button', { name }))
      const [left, right] = shownPair()
      expect(left, `点了 ${name} 之后两栏相同`).not.toBe(right)
    }
  })
})
