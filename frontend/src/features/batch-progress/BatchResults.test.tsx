/**
 * The results page as rendered.
 *
 * The store is driven with real §8 SSE events rather than by poking state, so
 * these tests exercise the same path the stream does — including the retry
 * stages (`regenerating`, `infra_retry`, `refilling`) whose wording is the whole
 * point of the first describe block.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { MaterialRecord, SseEvent } from '@/contracts/api'
import { buildRecord, type FixtureKind } from '@/mocks/fixtures'
import { useBatchStore } from '@/stores/batchStore'
import { useReviewQueue } from '@/stores/reviewQueueStore'
import { BatchProgressPage } from './BatchProgressPage'

/* ── harness ─────────────────────────────────────────────────────────────── */

const BATCH = 'batch-test'
let seq = 0
const next = () => (seq += 1)

/** The stream manager is module-scoped; a no-op keeps these tests offline. */
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
  return {
    ...actual,
    useNavigate: () => (to: string) => navigations.push(to),
  }
})

function apply(event: Omit<SseEvent, 'seq'>) {
  useBatchStore.getState().applyEvent({ ...event, seq: next() } as SseEvent)
}

function progress(materialId: string, stage: string, rawStage: string, attempt = 1) {
  apply({
    event: 'progress',
    material_id: materialId,
    stage,
    attempt,
    raw_stage: rawStage,
  } as never)
}

function deliver(record: MaterialRecord) {
  apply({
    event: 'material',
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

interface Plan {
  materialId: string
  scenarioKey: string
  index: number
  kind: FixtureKind
}

function startBatch(plan: Plan[]) {
  useBatchStore.getState().initBatch({
    batchId: BATCH,
    total: plan.length,
    items: plan.map((p) => ({
      material_id: p.materialId,
      scenario_key: p.scenarioKey,
      index: p.index,
      status: 'pending' as const,
      stage: 'queued' as const,
      attempt: 0,
    })),
  })
  apply({ event: 'hello', batch_id: BATCH, total: plan.length, server_time: '', resumed_from: 0 } as never)
}

function deliverAll(plan: Plan[]) {
  for (const p of plan) {
    deliver(buildRecord(p.kind, {
      materialId: p.materialId,
      batchId: BATCH,
      scenarioKey: p.scenarioKey,
      index: p.index,
    }))
  }
  apply({
    event: 'batch_done',
    status: 'done',
    completed: plan.length,
    failed: 0,
    audit_rejected: 0,
  } as never)
}

const TWO_SCENARIOS: Plan[] = [
  { materialId: 'm1', scenarioKey: 'accommodation-rental', index: 0, kind: 'balanced' },
  { materialId: 'm2', scenarioKey: 'accommodation-rental', index: 1, kind: 'clustered' },
  { materialId: 'm3', scenarioKey: 'booking-hotel', index: 0, kind: 'balanced' },
  { materialId: 'm4', scenarioKey: 'booking-hotel', index: 1, kind: 'failed' },
]

/** The bottom bar's count. Not `getByText`: a point dot is also labelled "1". */
function selectedCount(): string {
  return document.querySelector('.results-bar .count')!.textContent!
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/batches/${BATCH}`]}>
      <Routes>
        <Route path="/batches/:batchId" element={<BatchProgressPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  seq = 0
  navigations.length = 0
  useBatchStore.getState().reset()
  useReviewQueue.getState().clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

/* ── 1. 内部环节名不得到达 DOM ───────────────────────────────────────────── */

describe('internal stage wording never reaches the user', () => {
  /**
   * The complaint this replaces, verbatim from the client: the page showed
   * `booking-hotel · 第 1 套生成中 / 生成中 / 校验未过，重新生成`. "校验未过" is
   * the system reporting its own validation failure — a user should not be told
   * that, and cannot act on it.
   */
  it('shows no retry, attempt or validation-failure wording mid-batch', () => {
    startBatch(TWO_SCENARIOS)
    // The exact sequence a real slot emits when the validator rejects it twice.
    progress('m1', 'generating', 'generating')
    progress('m1', 'validating', 'validating')
    progress('m1', 'generating', 'regenerating', 2)
    progress('m1', 'validating', 'validating', 2)
    progress('m1', 'auditing', 'infra_retry', 2)
    progress('m2', 'generating', 'refilling', 3)
    renderPage()

    const body = document.body.textContent ?? ''
    for (const forbidden of [
      '校验未过',
      '重新生成',
      'regenerating',
      'refilling',
      'refill_abandoned',
      'infra_retry',
      'anchors_repaired',
      '隔离',
      '第 2 次尝试',
      '第 3 次尝试',
    ]) {
      expect(body, forbidden).not.toContain(forbidden)
    }
  })

  it('renders the four user-facing phases instead', () => {
    startBatch(TWO_SCENARIOS)
    progress('m1', 'validating', 'validating')
    renderPage()

    const track = screen.getByLabelText('生成进度')
    for (const label of ['生成', '校验', '修改', '复评']) {
      expect(within(track).getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText(/正在校验/)).toBeInTheDocument()
  })

  it('keeps a retry looking like continued progress, not a step backwards', () => {
    startBatch(TWO_SCENARIOS)
    progress('m1', 'validating', 'validating')
    const { rerender } = renderPage()
    expect(screen.getByText(/正在校验/)).toBeInTheDocument()

    // The validator rejects it; the slot goes back to generating.
    progress('m1', 'generating', 'regenerating', 2)
    rerender(
      <MemoryRouter initialEntries={[`/batches/${BATCH}`]}>
        <Routes>
          <Route path="/batches/:batchId" element={<BatchProgressPage />} />
        </Routes>
      </MemoryRouter>,
    )
    // Still 校验 — the caption did not regress to 生成.
    expect(screen.getByText(/正在校验/)).toBeInTheDocument()
  })

  it('exposes no verdict badge and no 隔离 concept once materials arrive', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const body = document.body.textContent ?? ''
    for (const forbidden of ['PASS', 'MINOR_EDITS', 'FAIL', 'NOT_ASSESSABLE', '隔离']) {
      expect(body, forbidden).not.toContain(forbidden)
    }
    // Every card carries the same uniform badge.
    expect(screen.getAllByText('待审核')).toHaveLength(4)
  })

  it('reports a partial batch as a count, not as a per-material stage log', () => {
    startBatch(TWO_SCENARIOS)
    deliver(buildRecord('balanced', {
      materialId: 'm1',
      batchId: BATCH,
      scenarioKey: 'accommodation-rental',
      index: 0,
    }))
    apply({
      event: 'material_failed',
      material_id: 'm2',
      code: 'validation_exhausted',
      message: '确定性校验连续三次未通过',
      attempts: 3,
    } as never)
    apply({
      event: 'batch_done',
      status: 'partial',
      completed: 1,
      failed: 1,
      audit_rejected: 0,
    } as never)
    renderPage()

    expect(screen.getByText(/有 3 套未能生成/)).toBeInTheDocument()
    // The backend's own failure token and its Chinese rendering both stay out.
    const body = document.body.textContent ?? ''
    expect(body).not.toContain('validation_exhausted')
    expect(body).not.toContain('确定性校验')
  })
})

/* ── 2. 版式 ─────────────────────────────────────────────────────────────── */

describe('layout', () => {
  it('groups cards by scenario with the Chinese title and category tag', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    // Both from config/scenarios.yaml via codegen.
    expect(screen.getByText('租房咨询')).toBeInTheDocument()
    expect(screen.getByText('住宿')).toBeInTheDocument()
    expect(screen.getByText('酒店预订')).toBeInTheDocument()
    expect(screen.getByText('预订/咨询服务')).toBeInTheDocument()
    // Raw scenario keys are internal ids and do not belong on the page.
    expect(document.body.textContent).not.toContain('accommodation-rental')
  })

  it('labels each card 第 N 套 and gives it ten numbered points', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(screen.getAllByText('第 1 套')).toHaveLength(2)
    expect(screen.getAllByText('第 2 套')).toHaveLength(2)
    expect(screen.getAllByText('信息点分布（10/10）')).toHaveLength(4)
    const card = document.querySelector('[data-material="m1"]')!
    expect(card.querySelectorAll('.point-dot')).toHaveLength(10)
    expect([...card.querySelectorAll('.point-dot')].map((d) => d.textContent)).toEqual([
      '1', '2', '3', '4', '5', '6', '7', '8', '9', '10',
    ])
  })

  it('marks the clustered card点 yellow and the balanced one blue', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const balanced = document.querySelector('[data-material="m1"]')!
    const clustered = document.querySelector('[data-material="m2"]')!
    expect(balanced.querySelectorAll('.point-dot.flagged')).toHaveLength(0)
    expect(clustered.querySelectorAll('.point-dot.flagged').length).toBeGreaterThan(0)
  })

  it('previews the first line of dialogue and a one-line summary', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const card = document.querySelector('[data-material="m1"]')!
    const quote = card.querySelector('q')!
    expect(quote.textContent).toBeTruthy()
    expect(card.querySelector('.mat-preview')!.textContent).toContain('租房咨询')
  })

  it('offers 阅读全文 and NO 试听: audio does not exist before selection', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(screen.getAllByRole('link', { name: '阅读全文' })).toHaveLength(4)
    expect(screen.queryByText('试听')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain('试听')
  })

  it('states shortcomings on a flawed material while keeping it selectable', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    // m4 is the FAIL fixture.
    const card = document.querySelector('[data-material="m4"]')!
    expect(card.querySelector('.mat-flaws')).toBeTruthy()
    expect(card.textContent).toContain('待审核')
    expect(card.textContent).not.toContain('FAIL')
    // And it can be chosen like any other.
    const checkbox = within(card as HTMLElement).getByRole('button', { name: /第 2 套/ })
    await userEvent.click(checkbox)
    expect(card.className).toContain('selected')
  })
})

/* ── 3. 勾选与提交 ───────────────────────────────────────────────────────── */

describe('selection and submission', () => {
  it('selects a card on checkbox click and reflects the count', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(selectedCount()).toBe('0')
    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    expect(selectedCount()).toBe('1')
    expect(document.querySelector('[data-material="m1"]')!.className).toContain('selected')
  })

  it('deselects on a second click', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const box = screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!
    await userEvent.click(box)
    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：已选择/ })[0]!)
    expect(selectedCount()).toBe('0')
    expect(document.querySelector('[data-material="m1"]')!.className).not.toContain('selected')
  })

  it('enforces at least one per scenario before 提交审核 is enabled', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const submit = screen.getByRole('button', { name: '提交审核' })
    expect(submit).toBeDisabled()
    expect(screen.getByText('（每场景至少选 1 套）')).toBeInTheDocument()

    // One scenario covered — still not enough.
    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    expect(submit).toBeDisabled()
    expect(screen.getByText(/还差：酒店预订/)).toBeInTheDocument()

    // Both covered.
    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    expect(submit).toBeEnabled()
  })

  it('submits the selection into the review queue and lands there', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    await userEvent.click(screen.getByRole('button', { name: '提交审核' }))

    const queued = useReviewQueue.getState().items
    expect(queued.map((q) => q.materialId).sort()).toEqual(['m1', 'm3'])
    expect(queued[0]!.summary).toBeTruthy()
    expect(navigations).toContain('/review-queue')
    // The page clears its own selection after handing it over.
    expect(selectedCount()).toBe('0')
  })

  it('clears the selection on 取消选择', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    await userEvent.click(screen.getByRole('button', { name: '取消选择' }))
    expect(selectedCount()).toBe('0')
  })
})

/* ── 4. 对比模式 ─────────────────────────────────────────────────────────── */

describe('compare mode', () => {
  it('shows the purple banner with an A/B legend when entered', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(screen.queryByText(/对比模式/)).not.toBeInTheDocument()
    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    expect(screen.getByText('对比模式：点选两套材料进行并排对比')).toBeInTheDocument()
    expect(screen.getByText('材料 A')).toBeInTheDocument()
    expect(screen.getByText('材料 B')).toBeInTheDocument()
  })

  it('marks the two picked cards A and B, then navigates to the compare view', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    await userEvent.click(screen.getByRole('button', { name: /第 1 套：点选进入对比/ }))
    expect(document.querySelector('[data-material="m1"]')!.className).toContain('pick-a')

    await userEvent.click(screen.getByRole('button', { name: /第 2 套：点选进入对比/ }))
    // A/B borders applied, and the existing side-by-side view is wired up.
    expect(navigations).toContain('/compare/accommodation-rental?a=m1&b=m2')
  })

  it('offers no compare button for a scenario with a single material', () => {
    const single: Plan[] = [
      { materialId: 'only', scenarioKey: 'booking-hotel', index: 0, kind: 'balanced' },
    ]
    startBatch(single)
    deliverAll(single)
    renderPage()

    expect(screen.queryByRole('button', { name: '对比本场景' })).not.toBeInTheDocument()
  })

  it('keeps an existing selection visible inside compare mode', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)

    // Entering compare mode must not read as "my selection was thrown away" —
    // the bottom bar still counts it, so the card has to agree.
    expect(document.querySelector('[data-material="m1"]')!.className).toContain('selected')
    expect(selectedCount()).toBe('1')
  })

  it('leaves compare mode without submitting anything', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    await userEvent.click(screen.getByRole('button', { name: /第 1 套：点选进入对比/ }))
    await userEvent.click(screen.getByRole('button', { name: '退出对比' }))

    expect(screen.queryByText(/对比模式/)).not.toBeInTheDocument()
    // Compare picking is not selection: the count is untouched.
    expect(selectedCount()).toBe('0')
    expect(navigations).toEqual([])
  })
})

/* ── 5. 重连行为必须保留 ─────────────────────────────────────────────────── */

describe('reconnect behaviour is preserved', () => {
  it('says the delivered materials are unaffected while reconnecting', () => {
    startBatch(TWO_SCENARIOS)
    deliver(buildRecord('balanced', {
      materialId: 'm1',
      batchId: BATCH,
      scenarioKey: 'accommodation-rental',
      index: 0,
    }))
    useBatchStore.getState().setConnection('reconnecting', 3)
    renderPage()

    expect(screen.getByText(/连接中断，正在重连（第 3\/8 次）/)).toBeInTheDocument()
    expect(screen.getByText(/已到达的 1 套完全不受影响/)).toBeInTheDocument()
    // The card is still there and still selectable.
    expect(document.querySelector('[data-material="m1"]')).toBeTruthy()
  })

  it('offers a manual retry after giving up, without discarding results', () => {
    startBatch(TWO_SCENARIOS)
    deliver(buildRecord('balanced', {
      materialId: 'm1',
      batchId: BATCH,
      scenarioKey: 'accommodation-rental',
      index: 0,
    }))
    useBatchStore.getState().setConnection('failed', 0, 'network error')
    renderPage()

    expect(screen.getByText(/重连 8 次均失败/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '手动重新连接' })).toBeInTheDocument()
    expect(screen.getByText(/已完成的 1 套不会丢失/)).toBeInTheDocument()
    expect(document.querySelector('[data-material="m1"]')).toBeTruthy()
  })

  it('notes the degraded recovery once the stream is back', () => {
    startBatch(TWO_SCENARIOS)
    useBatchStore.getState().setConnection('reconnecting', 1)
    useBatchStore.getState().setConnection('streaming')
    renderPage()

    expect(screen.getByText('本批次曾发生连接中断')).toBeInTheDocument()
  })
})
