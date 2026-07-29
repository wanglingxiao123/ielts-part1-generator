/**
 * Mock backend (VITE_MOCK=1). Intercepts every design.md §8.1 endpoint and the
 * §8.2 stream. Installed by replacing http.ts's transport and sseClient's fetch
 * — the single seam. Response payloads match the contract exactly, so switching
 * to the real backend is a config change.
 */
import type {
  AudioStatusResponse,
  BatchListResponse,
  BatchSnapshot,
  CreateBatchRequest,
  CreateBatchResponse,
  MaterialListResponse,
  MaterialRecord,
  SelectMaterialResponse,
  SseEvent,
} from '@/contracts/api'
import { ApiError, setTransport, type RequestSpec } from '@/api/http'
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
const audioJobs = new Map<string, { startedAt: number; total: number }>()
const selected = new Set<string>()

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
 */
function seedStandalone() {
  if (standaloneMaterials.size > 0) return
  const seeds: Array<{ kind: FixtureKind; scenarioKey: string }> = [
    { kind: 'failed', scenarioKey: 'booking-car-rental' },
    { kind: 'failed', scenarioKey: 'booking-car-rental' },
    { kind: 'failed', scenarioKey: 'employment-vacancy' },
  ]
  seeds.forEach((s, i) => {
    const id = `seed-rejected-${i + 1}`
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

  if (spec.method === 'POST' && resource === 'batches' && !id) {
    const body = spec.body as CreateBatchRequest
    const total = body.requests.reduce((n, r) => n + r.count, 0)
    if (total > SCENARIO_CATALOG.maxBatch) {
      throw new ApiError(400, 'BATCH_LIMIT_EXCEEDED', '单批总数超过上限', {
        limit: SCENARIO_CATALOG.maxBatch,
        requested: total,
      })
    }
    for (const r of body.requests) {
      if (r.scenario_key !== CUSTOM_SCENARIO_KEY && !knownKeys.has(r.scenario_key)) {
        throw new ApiError(400, 'UNKNOWN_SCENARIO', `未知场景 ${r.scenario_key}`)
      }
    }

    const batchId = nextId('batch')
    const materials = body.requests.flatMap((r) =>
      Array.from({ length: r.count }, (_, i) => ({
        materialId: nextId('mat'),
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
    const response: CreateBatchResponse = {
      batch_id: batchId,
      total,
      estimated_seconds: [total * 100, total * 160],
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
        materialId: nextId('mat'),
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
    const m = findMaterial(id)
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
    const total = m.material.listening_material_parts[0].script.turns.length
    audioJobs.set(id!, { startedAt: Date.now(), total })
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
}
