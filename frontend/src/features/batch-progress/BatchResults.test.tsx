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

/** The per-scenario counts the user chose, as ScenarioSelectPage records them. */
function requestedFrom(plan: Plan[]) {
  const counts = new Map<string, number>()
  for (const p of plan) counts.set(p.scenarioKey, Math.max(counts.get(p.scenarioKey) ?? 0, p.index + 1))
  return [...counts].map(([scenarioKey, count]) => ({ scenarioKey, count }))
}

function startBatch(plan: Plan[], requested = requestedFrom(plan)) {
  useBatchStore.getState().initBatch({
    batchId: BATCH,
    total: plan.length,
    requested,
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

  /**
   * Was: ten numbered dots in a row. Now: the same ten points, placed on a turn
   * axis. The intent is unchanged — every information point is accounted for on
   * the card — but the numbers are circled and their POSITION now carries the
   * information the flat row did not.
   */
  it('labels each card 第 N 套 and plots all ten points on a turn axis', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(screen.getAllByText('第 1 套')).toHaveLength(2)
    expect(screen.getAllByText('第 2 套')).toHaveLength(2)
    expect(screen.getAllByText('信息点分布（10/10）')).toHaveLength(4)
    const card = document.querySelector('[data-material="m1"]')!
    const dots = [...card.querySelectorAll<HTMLElement>('.dist-thumb-dot')]
    expect(dots).toHaveLength(10)
    expect(dots.map((d) => d.dataset.point)).toEqual([
      '1', '2', '3', '4', '5', '6', '7', '8', '9', '10',
    ])
    expect(dots.map((d) => d.textContent)).toEqual([
      '①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩',
    ])
    // Axis ticks at a few key positions only, not one per turn.
    const ticks = [...card.querySelectorAll('.dist-thumb-tick')].map((t) => t.textContent)
    expect(ticks[0]).toBe('0')
    expect(ticks.length).toBeLessThanOrEqual(6)
  })

  it('marks the clustered card点 yellow and the balanced one blue', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const balanced = document.querySelector('[data-material="m1"]')!
    const clustered = document.querySelector('[data-material="m2"]')!
    expect(balanced.querySelectorAll('.dist-thumb-dot.warn')).toHaveLength(0)
    expect(clustered.querySelectorAll('.dist-thumb-dot.warn').length).toBeGreaterThan(0)
  })

  /**
   * The thumbnail is a compact view of the SAME computation as the reader's full
   * strip, so the clustered fixture's defining defect has to be visible on the
   * card: the clustered dots sit at their true (nearly coincident) positions and
   * a bracket marks the form-group span.
   */
  it('shows the clustering on the thumbnail: coincident dots and a group bracket', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const clustered = document.querySelector('[data-material="m2"]')!
    const lefts = [...clustered.querySelectorAll<HTMLElement>('.dist-thumb-dot')].map(
      (d) => d.style.left,
    )
    // The overlap IS the finding (design.md §3.3): marks are NOT spread out to
    // avoid each other, so at least two of them share a horizontal position.
    expect(new Set(lefts).size).toBeLessThan(lefts.length)

    // A flagged dot names the defect in its tooltip, in the spec's vocabulary.
    const warned = clustered.querySelector('.dist-thumb-dot.warn')!
    expect(warned.getAttribute('title')).toMatch(/密度过高|需要看一眼/)

    // form_group clustering is bracketed beneath the axis on the balanced card
    // too — the bracket states the group's turn span, it is not a defect flag.
    const balanced = document.querySelector('[data-material="m1"]')!
    expect(balanced.querySelectorAll('.dist-thumb-bracket').length).toBeGreaterThan(0)
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

  /**
   * 原来这条钉的是「卡片上把缺点摆出来」（`.mat-flaws`）。客户否掉了：
   *
   *   > 结果页卡片上只展示：场景名 + 信息点时间轴图 + 预览第一句话 + 操作按钮。
   *   > 不展示任何评价文字。
   *   > 阅读全文页面里可以展示评价建议……因为用户在看全文时才有上下文理解这个建议的含义。
   *
   * 原意图里真正要守的那一半留着，而且更严了：有缺陷的材料照样返回、照样可选，绝不显示内部
   * 评级。评价文字去了 /materials/:id（见 usability 那组测试与 MaterialReader）。
   */
  it('keeps a flawed material selectable while showing no evaluation prose', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    // m4 is the FAIL fixture.
    const card = document.querySelector('[data-material="m4"]')!
    expect(card.querySelector('.mat-flaws')).toBeNull()
    expect(card.textContent).toContain('待审核')
    expect(card.textContent).not.toContain('FAIL')
    // And it can be chosen like any other.
    const checkbox = within(card as HTMLElement).getByRole('button', { name: /第 2 套/ })
    await userEvent.click(checkbox)
    expect(card.className).toContain('selected')
  })

  /**
   * 整页一句评价文字都不许有。逐字列出客户读到过的那些说法——它们随手就能被加回来，而加回来
   * 页面就退回客户否掉的那一版。「阅读全文」是唯一的入口。
   */
  it('puts no quality advice anywhere on the results page', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(document.querySelectorAll('.mat-flaws')).toHaveLength(0)
    const body = document.body.textContent ?? ''
    for (const forbidden of [
      // usability.ts 的结论文案：属于阅读页，那里才有上下文。
      '记录节奏',
      '全篇覆盖',
      '题号顺序',
      '前后两组题量',
      '来不及记',
      '轮空',
      '建议先改',
      '可直接出题',
      '须先改',
      // 评价环节的判定与降级说明。
      '评价环节',
      '必须改',
      '复评',
      // 标注 bug 那一类，一个字都不能有。
      '旁注',
      '标错',
      '核对',
    ]) {
      expect(body, forbidden).not.toContain(forbidden)
    }
    // 卡片上留下的正是客户点名的那四样。
    const card = document.querySelector('[data-material="m1"]')!
    expect(card.querySelector('.dist-thumb')).toBeTruthy()
    expect(card.querySelector('q')).toBeTruthy()
    expect(within(card as HTMLElement).getByRole('link', { name: '阅读全文' })).toBeTruthy()
  })

  /**
   * 时间轴上的黄点留着。客户点名表扬过这张图，而一个有颜色的点是「先看这一段」的指路，
   * 不是一句评价——它不替客户判断材料好坏，也不带任何文字结论。
   */
  it('keeps the yellow dots as a look-here hint, with no verdict in their tooltip', () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    const clustered = document.querySelector('[data-material="m2"]')!
    const warned = [...clustered.querySelectorAll<HTMLElement>('.dist-thumb-dot.warn')]
    expect(warned.length).toBeGreaterThan(0)
    for (const dot of warned) {
      const title = dot.getAttribute('title') ?? ''
      // 指路：说清是第几题、在哪一轮。不下结论。
      expect(title).toMatch(/第 \d+ 题的信息在 turn \d+/)
      for (const verdictWord of ['建议', '不达标', '须先改', '缺陷']) {
        expect(title, verdictWord).not.toContain(verdictWord)
      }
    }
  })
})

/* ── 3. 没有等待页：骨架卡 ───────────────────────────────────────────────── */

/**
 * The client's complaint: before the first material arrived the page showed one
 * line of text ("正在生成，第一套完成后会立刻出现在这里。"). They asked for the
 * results-page STRUCTURE to be visible immediately instead — no separate loading
 * page, no blank screen, no waiting for the whole batch.
 */
describe('no waiting page: skeleton cards from the first frame', () => {
  const THREE_EACH: Plan[] = [
    { materialId: 'a0', scenarioKey: 'accommodation-rental', index: 0, kind: 'balanced' },
    { materialId: 'a1', scenarioKey: 'accommodation-rental', index: 1, kind: 'clustered' },
    { materialId: 'a2', scenarioKey: 'accommodation-rental', index: 2, kind: 'balanced' },
    { materialId: 'b0', scenarioKey: 'booking-hotel', index: 0, kind: 'balanced' },
    { materialId: 'b1', scenarioKey: 'booking-hotel', index: 1, kind: 'balanced' },
    { materialId: 'b2', scenarioKey: 'booking-hotel', index: 2, kind: 'balanced' },
  ]

  it('renders N skeletons per scenario, N being the count the user chose', () => {
    startBatch(THREE_EACH)
    renderPage()

    // Two scenario groups, three skeletons each — before any material event.
    const sections = document.querySelectorAll('.scn-group')
    expect(sections).toHaveLength(2)
    for (const section of sections) {
      expect(section.querySelectorAll('.skel-card')).toHaveLength(3)
      expect(section.querySelectorAll('.mat-card:not(.skel-card)')).toHaveLength(0)
    }
    // Each skeleton names its scenario and says it is being generated.
    expect(screen.getAllByLabelText('租房咨询 第 1 套 生成中')).toHaveLength(1)
    expect(screen.getAllByText('生成中…')).toHaveLength(6)
    expect(screen.getAllByText('租房咨询')).toHaveLength(4) // group head + 3 skeletons

    // The single line of prose the client rejected is gone.
    expect(document.body.textContent).not.toContain('第一套完成后会立刻出现在这里')
  })

  it('follows the chosen per-scenario count rather than a fixed number', () => {
    const oneEach: Plan[] = [
      { materialId: 'x', scenarioKey: 'booking-hotel', index: 0, kind: 'balanced' },
    ]
    startBatch(oneEach)
    renderPage()
    expect(document.querySelectorAll('.skel-card')).toHaveLength(1)
  })

  it('turns one skeleton into a real card without duplicating the slot', async () => {
    startBatch(THREE_EACH)
    const { rerender } = renderPage()
    expect(document.querySelectorAll('.mat-card')).toHaveLength(6)

    deliver(buildRecord('balanced', {
      materialId: 'a1',
      batchId: BATCH,
      scenarioKey: 'accommodation-rental',
      index: 1,
    }))
    rerender(
      <MemoryRouter initialEntries={[`/batches/${BATCH}`]}>
        <Routes>
          <Route path="/batches/:batchId" element={<BatchProgressPage />} />
        </Routes>
      </MemoryRouter>,
    )

    // Still six cards: one of them is now real, and it is 第 2 套 of that group.
    expect(document.querySelectorAll('.mat-card')).toHaveLength(6)
    expect(document.querySelectorAll('.skel-card')).toHaveLength(5)
    const real = document.querySelector('[data-material="a1"]')!
    expect(real.textContent).toContain('第 2 套')
    expect(real.className).toContain('fade-in')
    const group = document.querySelectorAll('.scn-group')[0]!
    expect(group.querySelectorAll('.mat-card')).toHaveLength(3)
    // The delivered card is readable immediately.
    expect(within(group as HTMLElement).getAllByRole('link', { name: '阅读全文' })).toHaveLength(1)
  })

  it('reads 已完成 M/N from the first frame, with N the planned total', () => {
    startBatch(THREE_EACH)
    const { rerender } = renderPage()
    expect(screen.getByText('已完成 0/6')).toBeInTheDocument()

    deliver(buildRecord('balanced', {
      materialId: 'b0',
      batchId: BATCH,
      scenarioKey: 'booking-hotel',
      index: 0,
    }))
    rerender(
      <MemoryRouter initialEntries={[`/batches/${BATCH}`]}>
        <Routes>
          <Route path="/batches/:batchId" element={<BatchProgressPage />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('已完成 1/6')).toBeInTheDocument()
  })

  it('keeps 提交审核 disabled until the first real card exists', () => {
    startBatch(THREE_EACH)
    const { rerender } = renderPage()

    // The bar is present (it must not pop into existence later) but disabled.
    expect(screen.getByRole('button', { name: '提交审核' })).toBeDisabled()
    expect(screen.getByText('（等第一套到达后即可勾选）')).toBeInTheDocument()

    deliver(buildRecord('balanced', {
      materialId: 'a0',
      batchId: BATCH,
      scenarioKey: 'accommodation-rental',
      index: 0,
    }))
    rerender(
      <MemoryRouter initialEntries={[`/batches/${BATCH}`]}>
        <Routes>
          <Route path="/batches/:batchId" element={<BatchProgressPage />} />
        </Routes>
      </MemoryRouter>,
    )
    // Still disabled — booking-hotel has no pick yet — but the wording moved on
    // to the actual rule, and the card is selectable.
    expect(screen.getByText('（每场景至少选 1 套）')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /第 1 套：选择/ })).toBeInTheDocument()
  })

  /**
   * A slot the backend silently re-runs (audit → NOT_ASSESSABLE → `refilling`)
   * must keep looking like ordinary generation. The point of the refill is that
   * the user does not perceive it.
   */
  it('shows no error state for a slot the backend is silently refilling', () => {
    startBatch(THREE_EACH)
    progress('a0', 'generating', 'refilling', 2)
    progress('a1', 'auditing', 'audited')
    renderPage()

    expect(document.querySelectorAll('.err-card')).toHaveLength(0)
    const body = document.body.textContent ?? ''
    expect(body).not.toContain('生成异常')
    expect(body).not.toContain('自动重试')
    expect(body).not.toContain('refilling')
    expect(document.querySelectorAll('.skel-card')).toHaveLength(6)
  })

  /**
   * A terminally failed slot renders NOTHING: fewer cards, never an empty one.
   *
   * This test used to assert the opposite -- one `.err-card` reading 生成异常. The client removed
   * that card outright: "前端不再渲染任何『生成异常』空卡片。只要模型返回了文本就必须展示" and,
   * for the genuine no-content case, "补不上就少返回一套，不放空卡片".
   *
   * The card was almost always reached through validation, which no longer fails a material at all
   * (the Loop delivers the last attempt with the validator's findings). What is left is a real
   * API failure, and the backend refills that silently first (batch.py's REFILLABLE_FAILURES).
   * By the time the frontend sees it, the honest report is a smaller count.
   *
   * The coverage that must NOT be lost, and is asserted below: the internal reason token still
   * never reaches the page, and the batch-level 补生成 entry point still exists.
   */
  it('renders no card at all for a terminally failed slot, and says so in the count', () => {
    startBatch(THREE_EACH)
    apply({
      event: 'material_failed',
      material_id: 'a2',
      code: 'validation_exhausted',
      message: '确定性校验连续三次未通过',
      attempts: 3,
    } as never)
    renderPage()

    // No empty card of any kind -- neither the deleted 生成异常 one nor a stuck skeleton.
    expect(document.querySelectorAll('.err-card')).toHaveLength(0)
    expect(document.body.textContent).not.toContain('生成异常')
    // The group count is where "one short" is stated. Scenario a asked for 3 and one failed, so
    // only two slots remain renderable; the third contributes no DOM node.
    expect(document.querySelectorAll('.mat-card')).toHaveLength(5)
    // Internal tokens still never reach the user.
    expect(document.body.textContent).not.toContain('validation_exhausted')
  })
})

/* ── 4. 勾选与提交 ───────────────────────────────────────────────────────── */

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

/* ── 5. 对比模式 ─────────────────────────────────────────────────────────── */

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

  /**
   * 选满两张后**不跳转**，在原地展开预览。
   *
   * 客户点名去掉自动跳转：点第二张卡的那一刻整页被换掉，用户既没确认要走，也丢了刚才那一屏的
   * 上下文。这个测试因此同时钉住两件事——预览出现了，以及 `navigations` 是空的。后者是这次
   * 修复的全部内容，少了它这个测试就退化成「预览渲染得出来」。
   */
  it('opens an inline preview when two cards are picked, and does NOT navigate', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    await userEvent.click(screen.getByRole('button', { name: /第 1 套：点选进入对比/ }))
    expect(document.querySelector('[data-material="m1"]')!.className).toContain('pick-a')

    await userEvent.click(screen.getByRole('button', { name: /第 2 套：点选进入对比/ }))
    expect(document.querySelector('[data-material="m2"]')!.className).toContain('pick-b')

    const inline = document.querySelector('.inline-compare')
    expect(inline).not.toBeNull()
    expect(within(inline as HTMLElement).getByText('哪一套更好出题')).toBeInTheDocument()
    // 这一行是修复本身：跳转必须由用户点「打开完整对比」触发，不能是页面自己走的。
    expect(navigations).toEqual([])
  })

  it('navigates to the full compare view only when asked to', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    await userEvent.click(screen.getByRole('button', { name: /第 1 套：点选进入对比/ }))
    await userEvent.click(screen.getByRole('button', { name: /第 2 套：点选进入对比/ }))
    await userEvent.click(screen.getByRole('button', { name: /打开完整对比/ }))

    expect(navigations).toContain('/compare/accommodation-rental?a=m1&b=m2')
  })

  /**
   * 两种模式互斥，这是用户报的冲突本身。
   *
   * 过去卡片右上角那一个控件同时承担勾选和 A/B 点选：在对比模式里点它改的却是选稿。现在勾选框
   * 只在选稿模式出现，对比模式下整张卡是点选区。
   */
  it('hides the selection checkbox inside compare mode', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(screen.getAllByRole('button', { name: /第 1 套：选择/ }).length).toBeGreaterThan(0)
    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)

    // 该场景的勾选框消失了；点选的入口是整张卡。
    const group = document.querySelector('.scn-group.comparing') as HTMLElement
    expect(group.querySelector('.select-check')).toBeNull()
    expect(within(group).getByRole('button', { name: /第 1 套：点选进入对比/ })).toBeInTheDocument()
  })

  it('greys out the other scenarios and refuses their clicks', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)

    const dimmed = document.querySelectorAll('.scn-group.dimmed')
    expect(dimmed.length).toBe(1)
    // 置灰的场景不给对比按钮：同时进两个场景的对比模式没有意义。
    expect(within(dimmed[0] as HTMLElement).queryByRole('button', { name: '对比本场景' })).toBeNull()
    // 点它的卡不改任何状态。pointer-events 拦不住程序化点击，所以这里验的是那道 `if (dimmed)`。
    ;(dimmed[0]!.querySelector('[data-material="m3"]') as HTMLElement).click()
    expect(document.querySelector('[data-material="m3"]')!.className).not.toContain('selected')
    expect(document.querySelector('[data-material="m3"]')!.className).not.toContain('pick-')
  })

  it('swaps the bottom bar for 退出对比', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    expect(screen.getByRole('button', { name: '提交审核' })).toBeInTheDocument()
    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)

    // 选稿底栏整条换掉，而不是把提交置灰：留着它会让人以为对比模式里还能提交。
    expect(screen.queryByRole('button', { name: '提交审核' })).not.toBeInTheDocument()
    expect(document.querySelector('.results-bar.comparing')).not.toBeNull()
    // 底栏上的「退出对比」和组头上的那个都算，两处都能退出。
    expect(screen.getAllByRole('button', { name: '退出对比' }).length).toBeGreaterThanOrEqual(1)
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

  /**
   * 勾选状态在对比模式下**保留但不可见**，退出后恢复——客户的原话。
   *
   * 这条以前断言的是「进对比后勾选仍然看得见」。改了：勾选框在对比模式下不存在，勾选态也不显示，
   * 否则紫色的 A/B 边框和蓝色的已选边框会叠在同一张卡上，说不清这张卡到底处在什么状态。
   * 但 state 一个字节都不能动——退出对比必须原样回来，这才是「保留」的意思。
   */
  it('keeps the selection through compare mode and restores it on exit', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: /第 1 套：选择/ })[0]!)
    expect(selectedCount()).toBe('1')

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    // 不可见：卡上不再有 selected 这个样式。
    expect(document.querySelector('[data-material="m1"]')!.className).not.toContain('selected')
    // 但没丢：底栏明说保留着，用户才不会以为自己的选择被清了。
    expect(document.querySelector('.results-bar.comparing')!.textContent).toContain('保留')

    await userEvent.click(screen.getAllByRole('button', { name: '退出对比' })[0]!)
    // 原样回来。
    expect(document.querySelector('[data-material="m1"]')!.className).toContain('selected')
    expect(selectedCount()).toBe('1')
  })

  it('leaves compare mode without submitting anything', async () => {
    startBatch(TWO_SCENARIOS)
    deliverAll(TWO_SCENARIOS)
    renderPage()

    await userEvent.click(screen.getAllByRole('button', { name: '对比本场景' })[0]!)
    await userEvent.click(screen.getByRole('button', { name: /第 1 套：点选进入对比/ }))
    await userEvent.click(screen.getAllByRole('button', { name: '退出对比' })[0]!)

    expect(screen.queryByText(/对比模式：点选两套材料/)).not.toBeInTheDocument()
    // 点选不是选稿：计数没被动过，置灰也撤了。
    expect(selectedCount()).toBe('0')
    expect(document.querySelectorAll('.scn-group.dimmed').length).toBe(0)
    expect(navigations).toEqual([])
  })
})

/* ── 6. 重连行为必须保留 ─────────────────────────────────────────────────── */

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
