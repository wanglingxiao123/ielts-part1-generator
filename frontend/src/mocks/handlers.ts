/**
 * Mock backend (VITE_MOCK=1). Intercepts every design.md §8.1 endpoint and the
 * §8.2 stream. Installed by replacing http.ts's transport and sseClient's fetch
 * — the single seam. Response payloads match the contract exactly, so switching
 * to the real backend is a config change.
 */
import type {
  AudioStatusResponse,
  BatchHistoryDetail,
  BatchHistoryEntry,
  BatchHistoryResponse,
  BatchListResponse,
  BatchSnapshot,
  CreateBatchRequest,
  CreateBatchResponse,
  MaterialListResponse,
  MaterialRecord,
  PreviewAudioResponse,
  SelectMaterialResponse,
  SseEvent,
} from '@/contracts/api'
import { ApiError, setTransport, type RequestSpec } from '@/api/http'
import { estimateBatchSeconds } from '@/domain/batchEstimate'
import { setSseFetch } from '@/api/sseClient'
import { setAuthFetch } from '@/auth/authApi'
import { buildRecord, mockManifest, type FixtureKind } from './fixtures'
import { MockBatch } from './mockSse'
import { syntheticClipUrl } from './silentAudio'
import { SCENARIO_CATALOG } from '@/config/scenarios.generated'
import { CUSTOM_SCENARIO_KEY } from '@/config/scenarioTypes'

export interface MockOptions {
  /** Simulate a mid-stream disconnect after the Nth material. */
  dropAfterMaterials?: number
  /** Materials the job never finishes → partial terminal state. */
  neverComplete?: number
  tickMs: number
  /** Fixture rotation for generated materials. */
  kinds: FixtureKind[]
}

export const DEFAULT_MOCK_OPTIONS: MockOptions = {
  tickMs: 900,
  kinds: ['balanced', 'clustered', 'balanced', 'failed', 'balanced', 'anchorMismatch'],
}

let options: MockOptions = { ...DEFAULT_MOCK_OPTIONS }

export function setMockOptions(next: Partial<MockOptions>) {
  options = { ...options, ...next }
}

export function getMockOptions(): MockOptions {
  return options
}

const batches = new Map<string, MockBatch>()
const standaloneMaterials = new Map<string, MaterialRecord>()

/**
 * 音频任务与选定记录。都存在 sessionStorage 里，理由和 batch plan 一样（见下方 PLAN_KEY）：
 * 它们在真后端都是服务端状态（S3 里的 job 与 group claim），刷新一次不会消失。
 *
 * 这一条对「生成音频」尤其重要：客户要的性质是「后续如果选择这个材料音频也一直跟随，不用重新
 * 生成」。如果 mock 一刷新就忘掉 job，页面在 mock 下会退回「音频尚未合成」——那是假后端的失忆，
 * 却看起来像这个性质没实现。
 */
const AUDIO_KEY = 'bcielts.v1.mock.audio'
const SELECTED_KEY = 'bcielts.v1.mock.selected'

function loadSession<T>(key: string, fallback: T): T {
  try {
    const raw = sessionStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

function saveSession(key: string, value: unknown) {
  try {
    sessionStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* private mode */
  }
}

const audioJobs = new Map<string, { startedAt: number; total: number }>(
  loadSession<Array<[string, { startedAt: number; total: number }]>>(AUDIO_KEY, []),
)
const selected = new Set<string>(loadSession<string[]>(SELECTED_KEY, []))

function persistAudio() {
  saveSession(AUDIO_KEY, [...audioJobs.entries()])
}

function persistSelected() {
  saveSession(SELECTED_KEY, [...selected])
}

// Persisted so ids minted after a reload cannot collide with pre-reload ones.
const COUNTER_KEY = 'bcielts.v1.mock.counter'
let counter = Number(sessionStorage.getItem(COUNTER_KEY) ?? '0')
const nextId = (prefix: string) => {
  counter += 1
  try {
    sessionStorage.setItem(COUNTER_KEY, String(counter))
  } catch {
    /* private mode */
  }
  return `${prefix}-${counter.toString().padStart(3, '0')}`
}

/**
 * A material id in the SHAPE the backend actually mints: `YYYYMMDD-<scenario_key>-<8 hex>`.
 * See `audio_storage/state_store.py`'s `new_material_id`.
 *
 * The mock used to hand out `mat-001`, and the AgentCore adapter used to hand out
 * `<batchId>::<slot_id>`. Neither matched production, and that is why the mock could not reproduce
 * the 试听 failure: the backend rejected the adapter's id as an unknown candidate, while the mock
 * happily resolved its own invented one. A mock that cannot reproduce a production bug is worse
 * than no mock — it certifies the broken path as working. Matching the real shape here means a
 * future id-space mismatch fails in `npm run dev:mock` rather than only against real AWS.
 */
const nextMaterialId = (scenarioKey: string) => {
  const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
  counter += 1
  try {
    sessionStorage.setItem(COUNTER_KEY, String(counter))
  } catch {
    /* private mode */
  }
  // Deterministic rather than random: a fixture set that changes id on every reload makes a
  // screenshot diff unreadable, and uniqueness within a session is all the registry needs.
  const hex = (counter * 0x9e3779b1).toString(16).slice(-8).padStart(8, '0')
  return `${stamp}-${scenarioKey}-${hex}`
}

/**
 * Batch plans survive a page reload via sessionStorage.
 *
 * Without this the mock would fail the "refresh and return to the in-flight
 * batch" acceptance item for the wrong reason — not because the frontend lost
 * the batch, but because the fake server forgot it existed. A real batch is a
 * server-side job (design.md §5.1), so the mock has to behave like one.
 */
const PLAN_KEY = 'bcielts.v1.mock.plans'

function loadPlans(): Array<ConstructorParameters<typeof MockBatch>[0]> {
  try {
    return JSON.parse(sessionStorage.getItem(PLAN_KEY) ?? '[]')
  } catch {
    return []
  }
}

function savePlan(plan: ConstructorParameters<typeof MockBatch>[0]) {
  try {
    sessionStorage.setItem(PLAN_KEY, JSON.stringify([...loadPlans(), plan]))
  } catch {
    /* private mode */
  }
}

/* ── batch history (web/batch_history.py) ─────────────────────────────────── */

/**
 * 历史批次记录。**localStorage**，不是 sessionStorage——这一点是刻意的。
 *
 * 别的 mock 状态（batch plan、audio job）用 sessionStorage 就够了，因为它们对应的真后端状态只需
 * 要撑过一次刷新。批次记录不是：它在真后端是 S3 里的对象，会活过整个部署、活到「已归档」。用
 * sessionStorage 会让面板在关掉标签页之后空掉，那是假后端的失忆看起来像功能没实现——而这个功能
 * 存在的全部理由就是「刷新之后还在」。
 *
 * 记录的形状与后端一字不差（`web/batch_history.py` 的 `derive`），包括 `created_at` 是 unix
 * **秒**而不是毫秒。形状对不上的 mock 会把一个真 bug 认证成通过。
 */
const HISTORY_KEY = 'bcielts.v1.mock.batchHistory'

function loadHistory(): BatchHistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as BatchHistoryEntry[]
    return Array.isArray(parsed) ? parsed.filter((b) => typeof b?.batch_id === 'string') : []
  } catch {
    return []
  }
}

function saveHistory(items: BatchHistoryEntry[]) {
  try {
    // 时间倒序，和后端的 `_newest_first` 一致。排序放在写入侧，读取侧就不必再排一遍。
    localStorage.setItem(
      HISTORY_KEY,
      JSON.stringify([...items].sort((a, b) => b.created_at - a.created_at)),
    )
  } catch {
    /* private mode */
  }
}

/** 后端 `derive` 的状态推导，逐条照搬。 */
const MOCK_CANDIDATE_TTL_SECONDS = 24 * 3600
const MOCK_STALE_RUNNING_SECONDS = 2 * 3600

function deriveHistory(entry: BatchHistoryEntry, nowSeconds: number): BatchHistoryEntry {
  const age = nowSeconds - entry.created_at
  const status: BatchHistoryEntry['status'] = entry.submitted_at
    ? 'submitted'
    : age >= MOCK_CANDIDATE_TTL_SECONDS
      ? 'archived'
      : 'pending_selection'
  return {
    ...entry,
    status,
    read_only: status !== 'pending_selection',
    interrupted: entry.state === 'running' && age >= MOCK_STALE_RUNNING_SECONDS,
  }
}

function upsertHistory(entry: BatchHistoryEntry) {
  const items = loadHistory().filter((b) => b.batch_id !== entry.batch_id)
  items.push(entry)
  saveHistory(items)
}

/**
 * 请求的场景形状，从**已持久化的 plan** 反推，而不是记在一个模块级 Map 里。
 *
 * 因为刷新之后 Map 就空了，而这个功能要验证的恰恰是刷新之后面板还在。plan 存在 sessionStorage 里，
 * 所以它是刷新后唯一还说得出「这批要了几套」的东西。顺序即 plan 里材料的顺序，也就是用户勾选的顺序。
 */
function requestedShape(batchId: string): Array<{ scenario_key: string; count: number }> {
  const plan = loadPlans().find((p) => p.batchId === batchId)
  if (!plan) return []
  const order: string[] = []
  const counts = new Map<string, number>()
  for (const m of plan.materials) {
    if (!counts.has(m.scenarioKey)) order.push(m.scenarioKey)
    counts.set(m.scenarioKey, (counts.get(m.scenarioKey) ?? 0) + 1)
  }
  return order.map((scenario_key) => ({ scenario_key, count: counts.get(scenario_key)! }))
}

/**
 * 把一个 mock 批次的当前状态写进历史。
 *
 * 在创建时调一次（记下「这批被要求过」，对应后端在 batch start 的那次写），随后每次读历史时对在跑
 * 的批次再调一次。**不是**只在批次结束时写一次：后端刻意是增量写的，因为 web 任务可能中途被换掉，
 * 而只在结束时写会让一个跑了五套的批次什么都不留下。mock 照着同样的时序走，`dev:mock` 里刷新一个
 * 正在生成的批次才会看到它已经在面板里——那是真后端的行为。
 */
function recordHistory(batch: MockBatch) {
  const snap = snapshotOf(batch)
  const existing = loadHistory().find((b) => b.batch_id === batch.batchId)
  const fromPlan = requestedShape(batch.batchId)
  const requested = fromPlan.length > 0 ? fromPlan : (existing?.scenarios ?? [])
  const materials = snap.items
    .filter((i) => i.status === 'done')
    .map((i) => ({
      material_id: i.material_id,
      scenario_key: i.scenario_key,
      index: i.index,
      verdict: i.verdict,
      degraded: false,
    }))
  const finished = snap.status === 'done' || snap.status === 'partial'
  upsertHistory({
    batch_id: batch.batchId,
    created_at: existing?.created_at ?? Date.now() / 1000,
    completed_at: finished ? (existing?.completed_at ?? Date.now() / 1000) : null,
    // 两个都由 `deriveHistory` 在读取时覆盖；这里写的是占位值，不是第二个判据。
    status: 'pending_selection',
    read_only: false,
    interrupted: false,
    state: finished ? 'complete' : 'running',
    requested_total: batch.total,
    arrived: materials.length,
    scenarios: requested,
    counts: { succeeded: materials.length, failed: 0, skipped: 0, degraded: 0 },
    submitted_at: existing?.submitted_at ?? null,
    submitted_by: existing?.submitted_by ?? null,
    submitted_material_ids: existing?.submitted_material_ids ?? [],
    materials,
  })
}

/** 把所有还在跑的 mock 批次的记录刷新一遍。读历史前调，等价于后端的增量写已经发生过。 */
function refreshRunningHistory() {
  for (const plan of loadPlans()) {
    const batch = getBatch(plan.batchId)
    if (batch) recordHistory(batch)
  }
}

/**
 * 预置两条历史，一条已提交、一条已归档。
 *
 * 没有它们，`npm run dev:mock` 里三个状态 chip 有两个永远是空的——而只读视图（客户明确要求的
 * 「已提交/已归档为只读」）就完全没有办法在浏览器里看到。归档那条的 `created_at` 放在候选过期
 * 之外，也就是让 mock 真的走一遍那个边界，而不是直接写死一个 `status: 'archived'`。
 */
const SEEDED_KEY = 'bcielts.v1.mock.batchHistory.seeded'

function seedHistory() {
  // 用一个独立的标记，**不是**「历史为空就种」。后者看着等价，实际不成立：用户先生成一批再打开面板
  // 时历史已经非空，种子就永远种不进去——于是三个 chip 有两个是空的，只读视图在浏览器里根本看不到。
  // 实际就是这么坏的。
  try {
    if (localStorage.getItem(SEEDED_KEY) === '1') return
    localStorage.setItem(SEEDED_KEY, '1')
  } catch {
    // private mode：种一次总比一次都不种好，重复 upsert 是幂等的（按 batch_id 覆盖）。
  }
  const nowSeconds = Date.now() / 1000
  const seeds: Array<{
    batchId: string
    ageSeconds: number
    scenarios: Array<{ scenario_key: string; count: number }>
    submitted: boolean
  }> = [
    {
      batchId: 'web-seed-submitted',
      ageSeconds: 3 * 3600,
      scenarios: [{ scenario_key: 'booking-hotel', count: 2 }],
      submitted: true,
    },
    {
      batchId: 'web-seed-archived',
      // 过了候选窗口，所以状态是**推导**出来的 archived，不是写死的。
      ageSeconds: MOCK_CANDIDATE_TTL_SECONDS + 5 * 3600,
      scenarios: [
        { scenario_key: 'employment-vacancy', count: 1 },
        { scenario_key: 'accommodation-rental', count: 1 },
      ],
      submitted: false,
    },
  ]
  for (const seed of seeds) {
    const createdAt = nowSeconds - seed.ageSeconds
    const materials = seed.scenarios.flatMap((s) =>
      Array.from({ length: s.count }, (_, i) => ({
        material_id: `20260101-${s.scenario_key}-hist000${i + 1}`,
        scenario_key: s.scenario_key,
        index: i,
        verdict: 'PASS' as const,
        degraded: false,
      })),
    )
    upsertHistory({
      batch_id: seed.batchId,
      created_at: createdAt,
      completed_at: createdAt + 300,
      status: 'pending_selection',
      read_only: false,
      interrupted: false,
      state: 'complete',
      requested_total: materials.length,
      arrived: materials.length,
      scenarios: seed.scenarios,
      counts: { succeeded: materials.length, failed: 0, skipped: 0, degraded: 0 },
      submitted_at: seed.submitted ? createdAt + 600 : null,
      submitted_by: seed.submitted ? 'a@amazon.com' : null,
      submitted_material_ids: seed.submitted ? [materials[0]!.material_id] : [],
      materials,
    })
  }
}

/**
 * 一个历史批次里的材料，按 id 找。
 *
 * 阅读页的 URL 是 `/materials/{id}`，不带 batch id，所以这条路径必须能只靠 id 找到材料——否则历史
 * 面板上的「阅读全文」会直接跳到「材料不存在」，而客户对只读批次的要求正是「可看材料、可试听」。
 * 真后端对应的是 `GET /api/batch-history-material/{id}`（web 层在 `_batches/` 前缀上做一次后缀
 * 匹配）。
 */
function historyMaterial(materialId: string): MaterialRecord | undefined {
  for (const batch of loadHistory()) {
    const summary = batch.materials.find((m) => m.material_id === materialId)
    if (!summary) continue
    const existing = standaloneMaterials.get(materialId)
    if (existing) return existing
    const record = buildRecord('balanced', {
      materialId,
      batchId: batch.batch_id,
      scenarioKey: summary.scenario_key,
      index: summary.index ?? 0,
    })
    standaloneMaterials.set(materialId, record)
    return record
  }
  return undefined
}

/** Rehydrates a batch created before a reload, already finished. */
function rehydrate(batchId: string): MockBatch | undefined {
  const plan = loadPlans().find((p) => p.batchId === batchId)
  if (!plan) return undefined
  const batch = new MockBatch(plan)
  batches.set(batchId, batch)
  batch.fastForward()
  return batch
}

function getBatch(batchId: string): MockBatch | undefined {
  return batches.get(batchId) ?? rehydrate(batchId)
}

const knownKeys = new Set(
  SCENARIO_CATALOG.categories.flatMap((c) => c.scenarios.map((s) => s.key)),
)

/**
 * Seeds a couple of audit-rejected materials so the "flawed but selectable" card
 * is reachable without waiting for a batch to produce one.
 *
 * The fourth seed is the one that matters for the validation-as-a-report change: a material the
 * validator still has notes about, which the backend now DELIVERS instead of swallowing. Without a
 * mock for it, the reader page's 结构校验意见 panel could only be seen against real AWS after three
 * consecutive validation failures — i.e. in practice, never.
 */
function seedStandalone() {
  if (standaloneMaterials.size > 0) return
  const seeds: Array<{ kind: FixtureKind; scenarioKey: string }> = [
    { kind: 'failed', scenarioKey: 'booking-car-rental' },
    { kind: 'failed', scenarioKey: 'booking-car-rental' },
    { kind: 'failed', scenarioKey: 'employment-vacancy' },
    { kind: 'clustered', scenarioKey: 'accommodation-rental' },
  ]
  seeds.forEach((s, i) => {
    // Same production shape as every other mock id. A `seed-rejected-1` would be a second id
    // format the frontend has to tolerate and production never emits.
    const id = `20260101-${s.scenarioKey}-seed000${i + 1}`
    const rec = buildRecord(s.kind, {
      materialId: id,
      batchId: 'seed-batch',
      scenarioKey: s.scenarioKey,
      index: i % 2,
    })
    if (i === 2) {
      rec.verdict = 'NOT_ASSESSABLE'
      rec.audit_rejection = {
        code: 'NOT_ASSESSABLE',
        message: '评价环节未能给出结论，本套的质量没有经过复核',
      }
    }
    if (i === 3) {
      // Verbatim validator output, which is the point: the reader page has to translate whatever
      // the script actually emits, not a pre-tidied version of it.
      rec.validation_findings = [
        'blueprint.items[4].turn_index 20 does not carry its evidence (found at turn 21)',
        'dialogue words outside 450-750: 812 (over the 600-650 target by 187 words)',
        'blueprint must mark at least 3 confirmed items; found 1',
      ]
    }
    standaloneMaterials.set(id, rec)
  })
}

function findMaterial(materialId: string): MaterialRecord | undefined {
  seedStandalone()
  const direct = standaloneMaterials.get(materialId)
  if (direct) return direct
  // Rehydrate any persisted batch that owns this material (post-reload deep link).
  for (const plan of loadPlans()) {
    if (plan.materials.some((m) => m.materialId === materialId)) getBatch(plan.batchId)
  }
  for (const batch of batches.values()) {
    for (const e of batch.events) {
      if (e.event === 'material' && e.material_id === materialId) {
        return {
          material_id: e.material_id,
          batch_id: batch.batchId,
          scenario_key: e.scenario_key,
          index: e.index,
          status: 'done',
          verdict: e.verdict,
          audit_rejection: e.audit_rejection ?? null,
          degraded: e.degraded ?? false,
          material: e.material,
          blueprint: e.blueprint,
          audit: e.audit,
          cross_check: e.cross_check,
          created_at: new Date().toISOString(),
        }
      }
    }
  }
  return undefined
}

function snapshotOf(batch: MockBatch): BatchSnapshot {
  const items = new Map<string, BatchSnapshot['items'][number]>()
  for (const e of batch.events) {
    if (e.event === 'progress') {
      const prev = items.get(e.material_id)
      items.set(e.material_id, {
        material_id: e.material_id,
        scenario_key: prev?.scenario_key ?? '',
        index: prev?.index ?? 0,
        status: 'running',
        stage: e.stage,
        attempt: e.attempt,
      })
    } else if (e.event === 'material') {
      items.set(e.material_id, {
        material_id: e.material_id,
        scenario_key: e.scenario_key,
        index: e.index,
        status: 'done',
        stage: 're_auditing',
        attempt: 1,
        verdict: e.verdict,
      })
    }
  }
  const list = [...items.values()]
  const status = batch.snapshotStatus()
  return {
    batch_id: batch.batchId,
    status: status === 'running' ? 'running' : status,
    created_at: new Date().toISOString(),
    elapsed_ms: 0,
    total: batch.total,
    completed: list.filter((i) => i.status === 'done').length,
    failed: 0,
    audit_rejected: list.filter((i) => findMaterial(i.material_id)?.audit_rejection).length,
    seq_high: batch.events[batch.events.length - 1]?.seq ?? 0,
    items: list,
  }
}

const mockTransport = async (spec: RequestSpec): Promise<unknown> => {
  await new Promise((r) => setTimeout(r, 120))
  const [, resource, id, sub] = spec.path.split('?')[0]!.split('/')

  /* ── batch history ─────────────────────────────────────────────────────── */

  if (spec.method === 'GET' && resource === 'batch-history' && !id) {
    seedHistory()
    refreshRunningHistory()
    // 每次读都重新推导状态，因为状态是时间的函数：一条 23 小时前的批次在一小时后必须自己变成
    // 已归档，而不需要任何人来写它。后端的 `derive` 也是在读取时算的，理由相同。
    const nowSeconds = Date.now() / 1000
    const response: BatchHistoryResponse = {
      batches: loadHistory().map((b) => deriveHistory(b, nowSeconds)),
      next_cursor: null,
    }
    return response
  }

  if (spec.method === 'GET' && resource === 'batch-history' && id && !sub) {
    seedHistory()
    refreshRunningHistory()
    const entry = loadHistory().find((b) => b.batch_id === id)
    if (!entry) throw new ApiError(404, 'BATCH_NOT_FOUND', `没有找到批次 ${id} 的历史记录`)
    const view = deriveHistory(entry, Date.now() / 1000)
    // 详情带完整构件，列表不带——和后端的分法一致（见 web/batch_store.py）。
    const detail: BatchHistoryDetail = {
      ...view,
      // 真后端读的是 S3 里的 sidecar；mock 从跑过的批次里找，找不到（种子批次）就按场景造一份。
      // 与 `/materials/{id}` 走同一个 `historyMaterial`，所以两条入口不可能给出不同的材料。
      materials: view.materials.map((summary) => {
        const full = findMaterial(summary.material_id) ?? historyMaterial(summary.material_id)
        return full ? { ...summary, ...full } : summary
      }),
    }
    return detail
  }

  if (spec.method === 'POST' && resource === 'batch-history' && sub === 'submit') {
    const entry = loadHistory().find((b) => b.batch_id === id)
    if (!entry) throw new ApiError(404, 'BATCH_NOT_FOUND', `没有找到批次 ${id} 的历史记录`)
    const body = spec.body as { material_ids?: string[] }
    upsertHistory({
      ...entry,
      // 保留**第一次**提交的时间，和后端一致：那是批次不再等待决定的时刻，改一次选择不会把它推后。
      submitted_at: entry.submitted_at ?? Date.now() / 1000,
      submitted_by: 'a@amazon.com',
      submitted_material_ids: [...new Set(body.material_ids ?? [])],
    })
    return deriveHistory(
      loadHistory().find((b) => b.batch_id === id)!,
      Date.now() / 1000,
    )
  }

  if (spec.method === 'POST' && resource === 'batches' && !id) {
    const body = spec.body as CreateBatchRequest
    const total = body.requests.reduce((n, r) => n + r.count, 0)
    // 没有 BATCH_LIMIT_EXCEEDED。真后端不再有单批上限（web 层每套一次 invoke），mock 保留一个
    // 只有它才会抛的错误，会让 `dev:mock` 拒绝一个部署后能通的请求——那比没有 mock 更糟。
    for (const r of body.requests) {
      if (r.scenario_key !== CUSTOM_SCENARIO_KEY && !knownKeys.has(r.scenario_key)) {
        throw new ApiError(400, 'UNKNOWN_SCENARIO', `未知场景 ${r.scenario_key}`)
      }
    }

    const batchId = nextId('batch')
    const materials = body.requests.flatMap((r) =>
      Array.from({ length: r.count }, (_, i) => ({
        materialId: nextMaterialId(r.scenario_key),
        scenarioKey: r.scenario_key,
        index: i,
        kind: options.kinds[(i + body.requests.indexOf(r) * 2) % options.kinds.length]!,
      })),
    )
    const plan = {
      batchId,
      materials,
      tickMs: options.tickMs,
      dropAfterMaterials: options.dropAfterMaterials,
      neverComplete: options.neverComplete,
    }
    savePlan(plan)
    const batch = new MockBatch(plan)
    batches.set(batchId, batch)
    batch.start()
    // Recorded at batch START, exactly as the web tier does it: a batch that produces nothing still
    // leaves evidence that it was asked for.
    recordHistory(batch)
    const response: CreateBatchResponse = {
      batch_id: batchId,
      total,
      // Concurrency-aware, same model as the pre-submit estimate the user saw.
      estimated_seconds: estimateBatchSeconds(total),
      items: materials.map((m) => ({
        material_id: m.materialId,
        scenario_key: m.scenarioKey,
        index: m.index,
      })),
    }
    return response
  }

  if (spec.method === 'GET' && resource === 'batches' && !id) {
    const response: BatchListResponse = {
      batches: [...batches.values()].map((b) => {
        const s = snapshotOf(b)
        return {
          batch_id: s.batch_id,
          status: s.status,
          created_at: s.created_at,
          total: s.total,
          completed: s.completed,
        }
      }),
      next_cursor: null,
    }
    return response
  }

  if (spec.method === 'GET' && resource === 'batches' && id) {
    const batch = getBatch(id)
    if (!batch) throw new ApiError(404, 'BATCH_NOT_FOUND', '批次不存在或已过期')
    return snapshotOf(batch)
  }

  if (spec.method === 'POST' && resource === 'batches' && sub === 'retry') {
    const source = getBatch(id!)
    if (!source) throw new ApiError(404, 'BATCH_NOT_FOUND', '批次不存在')
    const body = spec.body as { scenario_keys?: string[]; material_ids?: string[] }
    const count = Math.max(1, body.scenario_keys?.length ?? body.material_ids?.length ?? 1)
    const batchId = nextId('batch')
    const plan = {
      batchId,
      materials: Array.from({ length: count }, (_, i) => ({
        materialId: nextMaterialId(body.scenario_keys?.[i] ?? 'booking-hotel'),
        scenarioKey: body.scenario_keys?.[i] ?? 'booking-hotel',
        index: i,
        kind: 'balanced' as FixtureKind,
      })),
      tickMs: options.tickMs,
    }
    savePlan(plan)
    const batch = new MockBatch(plan)
    batches.set(batchId, batch)
    batch.start()
    recordHistory(batch)
    return { batch_id: batchId }
  }

  if (spec.method === 'GET' && resource === 'materials' && !id) {
    seedStandalone()
    const query = new URLSearchParams(spec.path.split('?')[1] ?? '')
    const status = query.get('status')
    for (const plan of loadPlans()) getBatch(plan.batchId)
    const all = [...standaloneMaterials.values()]
    for (const b of batches.values()) {
      const snap = snapshotOf(b)
      for (const item of snap.items) {
        const m = findMaterial(item.material_id)
        if (m) all.push(m)
      }
    }
    // No status partitions the materials any more: every material routes to
    // pending and is selectable. An unrecognised filter yields nothing rather
    // than silently returning everything.
    const filtered = status ? [] : all
    const response: MaterialListResponse = { materials: filtered, next_cursor: null }
    return response
  }

  if (spec.method === 'GET' && resource === 'materials' && id && !sub) {
    const m = findMaterial(id) ?? historyMaterial(id)
    if (!m) throw new ApiError(404, 'MATERIAL_NOT_FOUND', '材料不存在')
    return m
  }

  if (spec.method === 'POST' && resource === 'materials' && sub === 'select') {
    const m = findMaterial(id!)
    if (!m) throw new ApiError(404, 'MATERIAL_NOT_FOUND', '材料不存在')
    if (selected.has(id!)) {
      // Idempotent by contract, but a second explicit select is a 409 so the UI
      // never implies a second (billable) synthesis was started.
      throw new ApiError(409, 'ALREADY_SELECTED', '该材料已选定，语音合成不会重复计费')
    }
    selected.add(id!)
    persistSelected()
    // A previewed material already has its job (and, on the real backend, its clips). Restarting
    // the timer here would make the progress bar jump back to 0 — the visible signature of a
    // second synthesis, which is exactly what the shared-clip design prevents.
    if (!audioJobs.has(id!)) {
      const total = m.material.listening_material_parts[0].script.turns.length
      audioJobs.set(id!, { startedAt: Date.now(), total })
      persistAudio()
    }
    const siblings = [...batches.values()]
      .flatMap((b) => snapshotOf(b).items)
      .filter((i) => i.scenario_key === m.scenario_key && i.material_id !== id)
      .map((i) => i.material_id)
    const response: SelectMaterialResponse = {
      material_id: id!,
      audio_job_id: nextId('audio'),
      siblings_discarded: siblings,
    }
    return response
  }

  /**
   * 生成音频（试听），后端的 `preview_audio`。
   *
   * 与 select 的关键区别照抄后端语义：**不动 `selected`**，同场景的另一套照样在。真后端的
   * `preview_audio` 不认领候选组，mock 要是顺手把它记成选定，页面在 mock 下就会表现出一个只有
   * mock 才有的行为，而这条正是这个端点存在的理由。
   *
   * 幂等：已经有 job 就返回 `repeat: true`，不重开计时（否则进度条会退回 0，看起来像重新计费）。
   */
  if (spec.method === 'POST' && resource === 'materials' && sub === 'audio') {
    const m = findMaterial(id!)
    if (!m) throw new ApiError(404, 'MATERIAL_NOT_FOUND', '材料不存在')
    const existing = audioJobs.get(id!)
    if (!existing) {
      audioJobs.set(id!, {
        startedAt: Date.now(),
        total: m.material.listening_material_parts[0].script.turns.length,
      })
      persistAudio()
    }
    const response: PreviewAudioResponse = {
      material_id: id!,
      audio_job_id: `audio-${id}`,
      repeat: Boolean(existing),
    }
    return response
  }

  if (spec.method === 'GET' && resource === 'materials' && sub === 'audio') {
    const job = audioJobs.get(id!)
    if (!job) {
      const response: AudioStatusResponse = {
        status: 'not_requested',
        progress: { done: 0, total: 0 },
      }
      return response
    }
    // ~4 segments/second so the progress UI is observable but not tedious.
    const done = Math.min(job.total, Math.floor((Date.now() - job.startedAt) / 250))
    if (done < job.total) {
      const response: AudioStatusResponse = {
        status: done === 0 ? 'queued' : 'synthesizing',
        progress: { done, total: job.total },
      }
      return response
    }
    const m = findMaterial(id!)!
    const turns = m.material.listening_material_parts[0].script.turns
    const manifest = mockManifest(id!, (turnIndex) => {
      const turn = turns[turnIndex]
      if (!turn) return null
      const words = turn.text.trim().split(/\s+/).length
      const durationMs = Math.max(900, Math.round((words / 160) * 60_000))
      return syntheticClipUrl(turn.speaker, durationMs)
    })
    const response: AudioStatusResponse = {
      status: 'ready',
      progress: { done: job.total, total: job.total },
      manifest,
    }
    return response
  }

  throw new ApiError(404, 'NOT_FOUND', `mock 未实现的端点：${spec.method} ${spec.path}`)
}

/* ── cookie auth (web/app.py /api/auth/*) ────────────────────────────────── */

/**
 * A fake accounts store for VITE_MOCK=1.
 *
 * `sessionStorage`, not a module variable, so a reload keeps you signed in —
 * exactly as the real HttpOnly cookie does. A mock that logged you out on every
 * F5 would make the dev server disagree with the deployment about the one
 * property the login flow is built around.
 *
 * The domain allowlist mirrors what a deployed ALLOWED_EMAIL_DOMAINS produces, so
 * the rejected-domain path is reachable without a backend.
 */
const MOCK_SESSION_KEY = 'bcielts.v1.mock.session'
const MOCK_USERS_KEY = 'bcielts.v1.mock.users'
const MOCK_ALLOWED_DOMAINS = ['amazon.com', 'example.com', 'local']
const MOCK_MIN_PASSWORD = 8

interface MockUser {
  email: string
  password: string
  is_admin: boolean
  created_at: number
}

function mockUsers(): Record<string, MockUser> {
  try {
    return JSON.parse(sessionStorage.getItem(MOCK_USERS_KEY) ?? '{}') as Record<string, MockUser>
  } catch {
    return {}
  }
}

function putMockUser(user: MockUser) {
  const users = mockUsers()
  users[user.email] = user
  sessionStorage.setItem(MOCK_USERS_KEY, JSON.stringify(users))
}

function authJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function authFail(status: number, code: string, message: string): Response {
  return authJson(status, { error: { code, message } })
}

function publicUser(user: MockUser) {
  return { email: user.email, is_admin: user.is_admin, created_at: user.created_at }
}

async function mockAuthFetch(path: string, init: RequestInit): Promise<Response> {
  await new Promise((r) => setTimeout(r, 120))
  const body = init.body ? (JSON.parse(String(init.body)) as Record<string, string>) : {}
  const email = (body.email ?? '').trim().toLowerCase()
  const password = body.password ?? ''
  const users = mockUsers()

  if (path === '/auth/me') {
    const current = sessionStorage.getItem(MOCK_SESSION_KEY)
    const user = current ? users[current] : undefined
    if (!user) return authFail(401, 'UNAUTHENTICATED', 'no session cookie')
    return authJson(200, { user: publicUser(user) })
  }

  if (path === '/auth/logout') {
    sessionStorage.removeItem(MOCK_SESSION_KEY)
    return authJson(200, { ok: true })
  }

  if (path === '/auth/register') {
    const domain = email.split('@')[1] ?? ''
    if (!email.includes('@') || !MOCK_ALLOWED_DOMAINS.includes(domain)) {
      return authFail(
        403,
        'EMAIL_DOMAIN_NOT_ALLOWED',
        `邮箱域名不在允许列表内（当前允许：${MOCK_ALLOWED_DOMAINS.join(', ')}）`,
      )
    }
    if (password.length < MOCK_MIN_PASSWORD) {
      return authFail(400, 'WEAK_PASSWORD', `密码至少 ${MOCK_MIN_PASSWORD} 位`)
    }
    if (users[email]) return authFail(409, 'USER_EXISTS', '该邮箱已注册')
    // First account is the admin, matching web/auth.py.
    const user: MockUser = {
      email,
      password,
      is_admin: Object.keys(users).length === 0,
      created_at: Math.floor(Date.now() / 1000),
    }
    putMockUser(user)
    sessionStorage.setItem(MOCK_SESSION_KEY, email)
    return authJson(200, { user: publicUser(user) })
  }

  if (path === '/auth/login') {
    const user = users[email]
    if (!user || user.password !== password) {
      return authFail(401, 'INVALID_CREDENTIALS', '邮箱或密码不正确')
    }
    sessionStorage.setItem(MOCK_SESSION_KEY, email)
    return authJson(200, { user: publicUser(user) })
  }

  return authFail(404, 'NOT_FOUND', `mock 未实现的端点：${path}`)
}

/** Bridges MockBatch into a ReadableStream of real SSE wire frames. */
function mockSseFetch(url: string, init: { signal: AbortSignal }): Promise<Response> {
  const parsed = new URL(url, window.location.origin)
  const batchId = parsed.pathname.split('/').at(-2)!
  const sinceSeq = Number(parsed.searchParams.get('since_seq') ?? '0')
  const batch = getBatch(batchId)
  if (!batch) {
    return Promise.resolve(new Response('missing', { status: 404 }))
  }

  const encoder = new TextEncoder()
  let unsubscribe: (() => void) | null = null
  let keepalive: number | null = null

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const push = (event: SseEvent) => {
        if ((event as { event: string }).event === '__drop__') {
          try {
            controller.error(new Error('mock: connection dropped'))
          } catch {
            /* already closed */
          }
          return
        }
        const frame = `event: ${event.event}\nid: ${event.seq}\ndata: ${JSON.stringify(event)}\n\n`
        try {
          controller.enqueue(encoder.encode(frame))
        } catch {
          /* closed */
        }
      }
      unsubscribe = batch.subscribe(sinceSeq, push)
      // The 15s keepalive comment frame design.md §5.3 requires of the server.
      keepalive = window.setInterval(() => {
        try {
          controller.enqueue(encoder.encode(': ping\n\n'))
        } catch {
          /* closed */
        }
      }, 15_000)
      init.signal.addEventListener('abort', () => {
        unsubscribe?.()
        if (keepalive !== null) window.clearInterval(keepalive)
        try {
          controller.close()
        } catch {
          /* already closed */
        }
      })
    },
    cancel() {
      unsubscribe?.()
      if (keepalive !== null) window.clearInterval(keepalive)
    },
  })

  return Promise.resolve(
    new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Contract-Version': '1' },
    }),
  )
}

export function installMocks() {
  // Lets a dev/demo/e2e session choose a degradation scenario without a rebuild:
  //   window.__MOCK_OPTIONS__ = { dropAfterMaterials: 4 }   // mid-stream drop
  //   window.__MOCK_OPTIONS__ = { neverComplete: 2 }        // partial terminal
  const injected = (window as { __MOCK_OPTIONS__?: Partial<MockOptions> }).__MOCK_OPTIONS__
  if (injected) {
    setMockOptions(injected)
    console.info('[mock] options overridden', injected)
  }
  setTransport(mockTransport)
  setSseFetch((url, init) => mockSseFetch(url, init))
  setAuthFetch(mockAuthFetch)
  console.info('[mock] API + SSE + auth mocked (VITE_MOCK=1)')
}

/** Test/demo helper: force a fresh mock world. */
export function resetMocks() {
  for (const b of batches.values()) b.dispose()
  batches.clear()
  standaloneMaterials.clear()
  audioJobs.clear()
  selected.clear()
  persistAudio()
  persistSelected()
}
