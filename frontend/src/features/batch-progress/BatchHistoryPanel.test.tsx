/**
 * 历史批次面板与只读视图，按渲染出来的东西测。
 *
 * 两组性质，分别对应客户的两句话：
 *
 * 1. 面板本身 —— 「按日期分组」「输入『酒店』只显示含酒店场景的批次」「状态 chip 点击切换」
 *    「当前选中的批次用蓝色左竖条标记 + 蓝色背景高亮」。
 *
 * 2. **只读**  —— 「历史批次（已提交/已归档）为只读视图——可看材料、可试听，但不能修改选稿」。
 *    这一组是重点，因为它是一个**不能发生**的性质：一个只是忘了传 `readOnly` 的实现会渲染出一个
 *    看起来完全正常的页面，然后允许用户改一个历史批次的选稿。所以测的是「点了勾选框之后什么都
 *    没发生」，而不只是「按钮上有 disabled 属性」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { BatchHistoryDetail, BatchHistoryEntry } from '@/contracts/api'
import { buildRecord } from '@/mocks/fixtures'
import { useBatchStore } from '@/stores/batchStore'
import { useReviewQueue } from '@/stores/reviewQueueStore'
import { api } from '@/api/endpoints'
import { BatchProgressPage } from './BatchProgressPage'

/* ── harness ─────────────────────────────────────────────────────────────── */

vi.mock('./useBatchStream', () => ({
  useBatchStream: () => ({
    connect: () => {},
    disconnect: () => {},
    retryNow: () => {},
    isActive: () => true,
    resumePersisted: () => null,
  }),
}))

const navigations: string[] = []
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => (to: string) => navigations.push(to) }
})

/** 每个端点被调了几次 / 传了什么，测「提交也记到后端」和「历史批次不去打 getBatch」用。 */
const calls = { history: 0, detail: [] as string[], submit: [] as Array<[string, string[]]>, getBatch: 0 }
let historyBatches: BatchHistoryEntry[] = []
let detailFor: (batchId: string) => BatchHistoryDetail

vi.mock('@/api/endpoints', () => ({
  api: {
    batchHistory: () => {
      calls.history += 1
      return Promise.resolve({ batches: historyBatches, next_cursor: null })
    },
    batchHistoryDetail: (batchId: string) => {
      calls.detail.push(batchId)
      // `try` 而不是直接调用：一个抛异常的 `detailFor` 模拟的是「请求失败」，那在真实调用里是一个
      // rejected promise，不是一个同步抛出。同步抛会直接炸穿 useEffect，测不到页面的错误分支。
      try {
        return Promise.resolve(detailFor(batchId))
      } catch (err) {
        return Promise.reject(err)
      }
    },
    submitBatch: (batchId: string, materialIds: string[]) => {
      calls.submit.push([batchId, materialIds])
      return Promise.resolve(detailFor(batchId))
    },
    getBatch: () => {
      calls.getBatch += 1
      return Promise.reject(new Error('BATCH_NOT_FOUND'))
    },
    retryBatch: () => Promise.resolve({ batch_id: 'x' }),
  },
}))

const HOUR = 3600
const nowSeconds = () => Date.now() / 1000

function historyEntry(over: Partial<BatchHistoryEntry> = {}): BatchHistoryEntry {
  return {
    batch_id: 'web-1-1',
    created_at: nowSeconds(),
    completed_at: nowSeconds(),
    status: 'pending_selection',
    read_only: false,
    interrupted: false,
    state: 'complete',
    requested_total: 2,
    arrived: 2,
    scenarios: [{ scenario_key: 'booking-hotel', count: 2 }],
    materials: [],
    ...over,
  }
}

/** 一个历史批次的详情，两套 booking-hotel 的真材料。 */
function detail(batchId: string, over: Partial<BatchHistoryDetail> = {}): BatchHistoryDetail {
  const materials = [0, 1].map((index) => {
    const record = buildRecord('balanced', {
      materialId: `${batchId}-m${index}`,
      batchId,
      scenarioKey: 'booking-hotel',
      index,
    })
    return { ...record, scenario_key: record.scenario_key, index }
  })
  return {
    ...historyEntry({ batch_id: batchId }),
    materials,
    ...over,
  }
}

function renderAt(batchId: string) {
  return render(
    <MemoryRouter initialEntries={[`/batches/${batchId}`]}>
      <Routes>
        <Route path="/batches/:batchId" element={<BatchProgressPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** 面板里的一行。 */
function row(batchId: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`.hist-row[data-batch="${batchId}"]`)
  if (!found) throw new Error(`没有找到批次 ${batchId} 那一行`)
  return found
}

function rowIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>('.hist-row')].map(
    (el) => el.dataset.batch ?? '',
  )
}

/**
 * 一个状态 chip。**限定在 chip 组里**查，不用 `screen.getByRole`。
 *
 * 因为「已提交」这几个字在页面上有两处：筛选 chip 和列表里的状态徽章。按名字全局查会撞上两个，
 * 而 chip 才是这里要点的那个。
 */
function chip(label: string): HTMLElement {
  const group = document.querySelector('.hist-chips')!
  return within(group as HTMLElement).getByRole('button', { name: new RegExp(label) })
}

beforeEach(() => {
  calls.history = 0
  calls.detail = []
  calls.submit = []
  calls.getBatch = 0
  navigations.length = 0
  historyBatches = []
  detailFor = (batchId) => detail(batchId)
  useBatchStore.getState().reset()
  useReviewQueue.getState().clear()
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

/* ── 1. 面板 ─────────────────────────────────────────────────────────────── */

describe('历史批次面板', () => {
  it('列出批次并显示总数量', async () => {
    historyBatches = [
      historyEntry({ batch_id: 'web-a' }),
      historyEntry({ batch_id: 'web-b' }),
    ]
    renderAt('web-a')
    await waitFor(() => expect(rowIds()).toEqual(['web-a', 'web-b']))
    expect(screen.getByText('历史批次')).toBeInTheDocument()
    expect(screen.getByText('2 批')).toBeInTheDocument()
  })

  it('按日期分组，标题是今天 / 昨天', async () => {
    historyBatches = [
      historyEntry({ batch_id: 'today' }),
      historyEntry({ batch_id: 'yday', created_at: nowSeconds() - 26 * HOUR }),
    ]
    renderAt('today')
    await waitFor(() => expect(rowIds()).toEqual(['today', 'yday']))
    const labels = [...document.querySelectorAll('.hist-group-label')].map((el) => el.textContent)
    expect(labels).toEqual(['今天', '昨天'])
  })

  it('每条显示场景标签、套数和状态标签', async () => {
    historyBatches = [
      historyEntry({
        batch_id: 'web-a',
        status: 'submitted',
        arrived: 6,
        requested_total: 6,
        scenarios: [{ scenario_key: 'booking-hotel', count: 6 }],
      }),
    ]
    renderAt('web-a')
    await waitFor(() => expect(rowIds()).toEqual(['web-a']))
    const el = row('web-a')
    expect(within(el).getByText('酒店预订')).toBeInTheDocument()
    expect(within(el).getByText('6 套')).toBeInTheDocument()
    expect(within(el).getByText('已提交')).toBeInTheDocument()
  })

  it('搜索按场景名过滤', async () => {
    historyBatches = [
      historyEntry({ batch_id: 'hotel', scenarios: [{ scenario_key: 'booking-hotel', count: 1 }] }),
      historyEntry({
        batch_id: 'job',
        scenarios: [{ scenario_key: 'employment-vacancy', count: 1 }],
      }),
    ]
    renderAt('hotel')
    await waitFor(() => expect(rowIds()).toEqual(['hotel', 'job']))

    await userEvent.type(screen.getByLabelText('按场景名搜索历史批次'), '酒店')
    await waitFor(() => expect(rowIds()).toEqual(['hotel']))
  })

  it('状态 chip 点击切换', async () => {
    historyBatches = [
      historyEntry({ batch_id: 'pending', status: 'pending_selection' }),
      historyEntry({ batch_id: 'submitted', status: 'submitted' }),
      historyEntry({ batch_id: 'archived', status: 'archived', read_only: true }),
    ]
    renderAt('pending')
    await waitFor(() => expect(rowIds()).toHaveLength(3))

    await userEvent.click(chip('已归档'))
    await waitFor(() => expect(rowIds()).toEqual(['archived']))

    // 再点「全部」回到三条：chip 是切换，不是单向筛。
    await userEvent.click(chip('全部'))
    await waitFor(() => expect(rowIds()).toHaveLength(3))
  })

  it('chip 计数跟着搜索走', async () => {
    // 输入之后 chip 若还显示全库的数量，点下去会只有一条——那是页面在骗人。
    historyBatches = [
      historyEntry({
        batch_id: 'hotel-sub',
        status: 'submitted',
        scenarios: [{ scenario_key: 'booking-hotel', count: 1 }],
      }),
      historyEntry({
        batch_id: 'job-sub',
        status: 'submitted',
        scenarios: [{ scenario_key: 'employment-vacancy', count: 1 }],
      }),
    ]
    renderAt('hotel-sub')
    await waitFor(() => expect(rowIds()).toHaveLength(2))
    expect(chip('已提交').textContent).toContain('2')

    await userEvent.type(screen.getByLabelText('按场景名搜索历史批次'), '酒店')
    await waitFor(() => expect(chip('已提交').textContent).toContain('1'))
  })

  it('当前批次那一行被标记为选中', async () => {
    historyBatches = [
      historyEntry({ batch_id: 'web-a' }),
      historyEntry({ batch_id: 'web-b' }),
    ]
    renderAt('web-b')
    await waitFor(() => expect(rowIds()).toEqual(['web-a', 'web-b']))
    // 蓝竖条 + 蓝底是 `.active` 这个类给的（见 styles.css）；`aria-current` 是同一件事的无障碍说法。
    expect(row('web-b').className).toContain('active')
    expect(row('web-b')).toHaveAttribute('aria-current', 'true')
    expect(row('web-a').className).not.toContain('active')
  })

  it('点另一条批次会切过去', async () => {
    historyBatches = [
      historyEntry({ batch_id: 'web-a' }),
      historyEntry({ batch_id: 'web-b' }),
    ]
    renderAt('web-a')
    await waitFor(() => expect(rowIds()).toEqual(['web-a', 'web-b']))
    await userEvent.click(row('web-b'))
    expect(navigations).toEqual(['/batches/web-b'])
  })

  it('可以折叠成一条窄图标条，并且记住这个选择', async () => {
    historyBatches = [historyEntry({ batch_id: 'web-a' })]
    const first = renderAt('web-a')
    await waitFor(() => expect(rowIds()).toEqual(['web-a']))

    await userEvent.click(screen.getByLabelText('收起历史批次面板'))
    await waitFor(() => expect(document.querySelector('.hist-rail')).toBeTruthy())
    expect(document.querySelector('.hist-panel')).toBeNull()

    // 折叠是一个偏好，不是一次会话里的临时状态：重新挂载之后还是收起的。
    first.unmount()
    renderAt('web-a')
    await waitFor(() => expect(document.querySelector('.hist-rail')).toBeTruthy())
  })

  /**
   * 空历史是一个**成功**的答案，不是失败态。
   *
   * 客户第一次打开这一页看到的是「历史记录读取失败 ModuleNotFoundError: No module named
   * 'audio_storage'」——而当时并没有任何失败，只是一批都还没生成过。所以这里断言的不只是「说了句话」，
   * 还有「没有把它说成失败」：一个把 `[]` 当错误的实现会同时满足前半句。
   */
  it('没有历史时说「暂无」，并且不呈现为失败', async () => {
    historyBatches = []
    renderAt('web-a')
    await waitFor(() => expect(screen.getByText(/暂无历史批次/)).toBeInTheDocument())
    expect(screen.queryByText('历史记录读取失败')).toBeNull()
    // chip 上的「全部 0」照样在：空列表要看得出是空的，不是加载中。
    expect(screen.getByText('0 批')).toBeInTheDocument()
  })

  it('搜不到时说「没有匹配」，而不是说「还没有历史批次」', async () => {
    historyBatches = [historyEntry({ batch_id: 'web-a' })]
    renderAt('web-a')
    await waitFor(() => expect(rowIds()).toEqual(['web-a']))
    await userEvent.type(screen.getByLabelText('按场景名搜索历史批次'), '不存在')
    await waitFor(() => expect(screen.getByText('没有匹配的批次。')).toBeInTheDocument())
  })
})

/* ── 2. 只读 ─────────────────────────────────────────────────────────────── */

describe('只读的历史批次', () => {
  /**
   * 「不能修改选稿」的真正断言。
   *
   * 只查 `disabled` 属性是不够的：把 `disabled` 挪走、或者把 `readOnly` 忘在某个分支里，页面看起来
   * 一切正常。所以这里点下去，然后断言**底栏那个计数根本不存在**——因为可写底栏被整条换掉了——并且
   * 卡片没有变成选中态。
   */
  it('勾选框被禁用，点它不会改变任何选稿', async () => {
    historyBatches = [historyEntry({ batch_id: 'old', status: 'archived', read_only: true })]
    detailFor = (batchId) =>
      detail(batchId, { status: 'archived', read_only: true, created_at: nowSeconds() - 40 * HOUR })
    renderAt('old')

    await waitFor(() => expect(document.querySelectorAll('.mat-card').length).toBe(2))
    const check = document.querySelector<HTMLButtonElement>('.mat-card .select-check')!
    expect(check.disabled).toBe(true)

    await userEvent.click(check)
    // 没有任何一张卡进入选中态。
    expect(document.querySelectorAll('.mat-card.selected')).toHaveLength(0)
    // 可写底栏（带「已选择 N 套材料」的那条）不在。
    expect(document.querySelector('.results-bar:not(.readonly)')).toBeNull()
  })

  it('「提交审核」不出现，换成一句说明这一批是什么状态', async () => {
    historyBatches = [historyEntry({ batch_id: 'sub', status: 'submitted', read_only: true })]
    detailFor = (batchId) =>
      detail(batchId, {
        status: 'submitted',
        read_only: true,
        submitted_material_ids: [`${batchId}-m0`],
      })
    renderAt('sub')

    await waitFor(() => expect(document.querySelectorAll('.mat-card').length).toBe(2))
    expect(screen.queryByRole('button', { name: '提交审核' })).toBeNull()
    expect(document.querySelector('.results-bar.readonly')).toBeTruthy()
    expect(screen.getByText(/已提交审核，不能修改选稿/)).toBeInTheDocument()
    expect(screen.getByText('当时提交了 1 套')).toBeInTheDocument()
  })

  it('说出为什么只读，不只是说「只读」', async () => {
    // 一个不说理由的禁用状态会被当成故障。
    historyBatches = [historyEntry({ batch_id: 'old', status: 'archived', read_only: true })]
    detailFor = (batchId) => detail(batchId, { status: 'archived', read_only: true })
    renderAt('old')
    await waitFor(() => expect(screen.getByText(/这一批已归档，是只读的/)).toBeInTheDocument())
    expect(screen.getByText(/24 小时/)).toBeInTheDocument()
  })

  it('材料照样能读、能试听 —— 阅读全文的入口在', async () => {
    // 客户的原话是「可看材料、可试听，但不能修改选稿」。试听在阅读页（生成音频），所以只读批次
    // 必须保留通往那里的链接。
    historyBatches = [historyEntry({ batch_id: 'old', status: 'archived', read_only: true })]
    detailFor = (batchId) => detail(batchId, { status: 'archived', read_only: true })
    renderAt('old')

    await waitFor(() => expect(document.querySelectorAll('.mat-card').length).toBe(2))
    const links = [...document.querySelectorAll<HTMLAnchorElement>('.mat-actions a')]
    expect(links).toHaveLength(2)
    for (const link of links) expect(link.getAttribute('href')).toContain('/materials/')
  })

  it('待选稿的历史批次仍然可以选稿', async () => {
    // 只读不是「凡是历史都只读」：一个几小时前、还没提交的批次正是在等这次选稿。
    historyBatches = [historyEntry({ batch_id: 'fresh', status: 'pending_selection' })]
    detailFor = (batchId) => detail(batchId, { status: 'pending_selection', read_only: false })
    renderAt('fresh')

    await waitFor(() => expect(document.querySelectorAll('.mat-card').length).toBe(2))
    const check = document.querySelector<HTMLButtonElement>('.mat-card .select-check')!
    expect(check.disabled).toBe(false)
    await userEvent.click(check)
    await waitFor(() => expect(document.querySelectorAll('.mat-card.selected')).toHaveLength(1))
    expect(screen.getByRole('button', { name: '提交审核' })).toBeInTheDocument()
  })

  it('中断过的批次说清缺的不会再补，并且不给补生成的按钮', async () => {
    historyBatches = [historyEntry({ batch_id: 'cut', interrupted: true })]
    detailFor = (batchId) =>
      detail(batchId, { interrupted: true, state: 'running', requested_total: 6 })
    renderAt('cut')

    await waitFor(() => expect(screen.getByText(/生成任务中途中断/)).toBeInTheDocument())
    expect(screen.getByText(/不会再补齐/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补生成/ })).toBeNull()
  })
})

/* ── 3. 两条数据路径 ─────────────────────────────────────────────────────── */

describe('历史批次与活批次是两条路径', () => {
  it('历史批次不去打 getBatch，因为那个端点只认识本页会话的批次', async () => {
    // 打了就会 404，页面于是显示红色的「无法加载本批次」——一个完全好的历史批次被说成坏的。
    historyBatches = [historyEntry({ batch_id: 'old', status: 'archived', read_only: true })]
    detailFor = (batchId) => detail(batchId, { status: 'archived', read_only: true })
    renderAt('old')

    await waitFor(() => expect(document.querySelectorAll('.mat-card').length).toBe(2))
    expect(calls.getBatch).toBe(0)
    expect(calls.detail).toEqual(['old'])
    expect(screen.queryByText('无法加载本批次')).toBeNull()
  })

  it('活批次走 store，不去取历史详情', async () => {
    const BATCH = 'live-1'
    useBatchStore.getState().initBatch({
      batchId: BATCH,
      total: 1,
      requested: [{ scenarioKey: 'booking-hotel', count: 1 }],
      items: [
        {
          material_id: 'm1',
          scenario_key: 'booking-hotel',
          index: 0,
          status: 'pending',
          stage: 'queued',
          attempt: 0,
        },
      ],
    })
    historyBatches = [historyEntry({ batch_id: BATCH })]
    renderAt(BATCH)

    await waitFor(() => expect(calls.history).toBeGreaterThan(0))
    expect(calls.detail).toEqual([])
  })

  /**
   * 失败要说出来，但**用中文说我们的话**，不是把异常摆出来。
   *
   * 客户看到过的那一行就是这么来的：一个 `ModuleNotFoundError: No module named 'audio_storage'` 从
   * S3 一路原样漏到 DOM。这里刻意抛一个英文技术串，断言它没有出现在页面上——一个直接渲染
   * `err.message` 的实现会通过上半句而挂在下半句。
   */
  it('取历史详情失败时用中文说出来，不把异常摆给用户', async () => {
    historyBatches = [historyEntry({ batch_id: 'broken' })]
    detailFor = () => {
      throw new Error("ModuleNotFoundError: No module named 'audio_storage'")
    }
    renderAt('broken')
    await waitFor(() =>
      expect(screen.getByText('无法加载这个历史批次')).toBeInTheDocument(),
    )
    expect(screen.getByText('这个历史批次暂时读取不到，请稍后重试。')).toBeInTheDocument()
    const body = document.body.textContent ?? ''
    expect(body).not.toContain('ModuleNotFoundError')
    expect(body).not.toContain('audio_storage')
  })

  /**
   * 面板列表读取失败同理。这是客户实际看到那一行的**源头**：面板顶上的 `/api/batch-history`。
   */
  it('历史列表读取失败时也只说中文', async () => {
    historyBatches = []
    const original = api.batchHistory
    ;(api as { batchHistory: () => Promise<unknown> }).batchHistory = () =>
      Promise.reject(new Error("ModuleNotFoundError: No module named 'audio_storage'"))
    try {
      renderAt('web-a')
      await waitFor(() =>
        expect(screen.getByText('历史记录读取失败')).toBeInTheDocument(),
      )
      expect(screen.getByText('历史记录暂时读取不到，请稍后重试。')).toBeInTheDocument()
      expect(document.body.textContent ?? '').not.toContain('ModuleNotFoundError')
    } finally {
      ;(api as { batchHistory: unknown }).batchHistory = original
    }
  })
})

/* ── 4. 提交也记到后端 ───────────────────────────────────────────────────── */

describe('提交审核', () => {
  it('把已提交状态记到后端，而不只是记进 localStorage 的队列', async () => {
    // 这是「已提交」这个状态能出现在面板上的全部原因：在这之前它只是浏览器的私人意见。
    const BATCH = 'live-2'
    useBatchStore.getState().initBatch({
      batchId: BATCH,
      total: 1,
      requested: [{ scenarioKey: 'booking-hotel', count: 1 }],
      items: [
        {
          material_id: 'm1',
          scenario_key: 'booking-hotel',
          index: 0,
          status: 'pending',
          stage: 'queued',
          attempt: 0,
        },
      ],
    })
    const record = buildRecord('balanced', {
      materialId: 'm1',
      batchId: BATCH,
      scenarioKey: 'booking-hotel',
      index: 0,
    })
    useBatchStore.getState().applyEvent({
      event: 'material',
      seq: 1,
      material_id: record.material_id,
      scenario_key: record.scenario_key,
      index: record.index,
      verdict: record.verdict,
      audit_rejection: null,
      degraded: false,
      material: record.material,
      blueprint: record.blueprint,
      audit: record.audit,
      cross_check: record.cross_check,
    } as never)
    historyBatches = [historyEntry({ batch_id: BATCH })]
    renderAt(BATCH)

    await waitFor(() => expect(document.querySelectorAll('.mat-card').length).toBe(1))
    await userEvent.click(document.querySelector<HTMLButtonElement>('.select-check')!)
    await userEvent.click(screen.getByRole('button', { name: '提交审核' }))

    await waitFor(() => expect(calls.submit).toEqual([[BATCH, ['m1']]]))
    // 本地队列也照旧写了：队列页显示的每条摘要后端没有，两者记的是同一件事的两个层面。
    expect(useReviewQueue.getState().items.map((i) => i.materialId)).toEqual(['m1'])
  })
})
