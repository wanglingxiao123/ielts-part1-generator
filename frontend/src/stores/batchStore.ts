/**
 * Batch + material state (design.md §7.2).
 *
 * Zustand rather than Query: SSE is a long-lived push channel, not a query, and
 * selector subscriptions are a performance requirement here — the 5th material
 * arriving must not re-render the 1st material's reader, because that would
 * recompute the annotation layout and visibly jitter.
 */
import { create } from 'zustand'
import type {
  BatchItemSnapshot,
  BatchSnapshot,
  BatchStatus,
  MaterialRecord,
  MaterialStage,
  SseEvent,
} from '@/contracts/api'
import { advancePhase, phaseOfProgress, type ProgressPhase } from '@/domain/progressStages'
import type { RequestedScenario } from '@/domain/resultSlots'

export type ConnectionState =
  | 'idle'
  | 'creating'
  | 'streaming'
  | 'reconnecting'
  | 'recovered'
  | 'done'
  | 'partial'
  | 'failed'

export interface BatchItemState extends BatchItemSnapshot {
  failure?: { code: string; message: string; attempts: number } | null
  /**
   * The backend's own stage name. A machine token, NOT display copy: it feeds
   * `domain/progressStages.ts` and nothing else. See SseProgressEvent.raw_stage.
   */
  rawStage?: string | null
  /**
   * User-facing progression (生成→校验→修改→复评), advance-only.
   *
   * Held in the store rather than derived per render because monotonicity needs
   * the previous value: a `regenerating` event maps back to 生成, and recomputing
   * from the latest event alone would make the display walk backwards — exactly
   * the "the system failed and started over" reading the client rejected.
   */
  phase?: ProgressPhase | null
}

export interface BatchState {
  batchId: string | null
  status: BatchStatus
  total: number
  /**
   * 用户提交时选的「每场景几套」，顺序即他勾选的顺序。
   *
   * 结果页要在**第一个 material 事件之前**就铺出全部卡位（骨架卡），所以这个形状
   * 必须一起带过来：`items` 虽然也够用，但那是后端回的，而客户要的是「提交后立刻
   * 看到结构」——这份数据在 POST 返回的那一刻就在手上了。
   *
   * 刷新后为空数组：那时页面从快照的 `items` 反推形状（见 domain/resultSlots.ts）。
   */
  requested: RequestedScenario[]
  createdAt: number | null
  seqHigh: number
  seenSeqs: Set<number>
  items: Record<string, BatchItemState>
  itemOrder: string[]
  materials: Record<string, MaterialRecord>
  connection: ConnectionState
  reconnectAttempt: number
  lastError: string | null
  /** True once a disconnect happened — the UI must say it is in recovery. */
  degradedRecovery: boolean
}

interface Actions {
  reset: () => void
  startCreating: () => void
  initBatch: (input: {
    batchId: string
    total: number
    items: BatchItemSnapshot[]
    requested?: RequestedScenario[]
  }) => void
  applySnapshot: (snapshot: BatchSnapshot) => void
  applyEvent: (event: SseEvent) => boolean
  setConnection: (state: ConnectionState, attempt?: number, error?: string | null) => void
  hydrate: (persisted: PersistedBatch, materials: MaterialRecord[]) => void
}

const EMPTY: BatchState = {
  batchId: null,
  status: 'queued',
  total: 0,
  requested: [],
  createdAt: null,
  seqHigh: 0,
  seenSeqs: new Set(),
  items: {},
  itemOrder: [],
  materials: {},
  connection: 'idle',
  reconnectAttempt: 0,
  lastError: null,
  degradedRecovery: false,
}

export interface PersistedBatch {
  batchId: string
  seqHigh: number
  receivedIds: string[]
  createdAt: number
  total: number
}

/** Version prefix so a stale shape is discarded rather than crashing (design §9). */
const LS_PREFIX = 'bcielts.v1.batch.'

export function persistKey(owner: string): string {
  return `${LS_PREFIX}${owner}`
}

export function loadPersisted(owner: string): PersistedBatch | null {
  try {
    const raw = localStorage.getItem(persistKey(owner))
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedBatch
    if (typeof parsed.batchId !== 'string' || typeof parsed.seqHigh !== 'number') return null
    return parsed
  } catch {
    return null
  }
}

export function savePersisted(owner: string, value: PersistedBatch): void {
  try {
    localStorage.setItem(persistKey(owner), JSON.stringify(value))
  } catch {
    /* quota / private mode — persistence is a convenience, not a requirement */
  }
}

export function clearPersisted(owner: string): void {
  try {
    localStorage.removeItem(persistKey(owner))
  } catch {
    /* ignore */
  }
}

function materialFromEvent(
  event: Extract<SseEvent, { event: 'material' }>,
  batchId: string,
): MaterialRecord {
  return {
    material_id: event.material_id,
    batch_id: batchId,
    scenario_key: event.scenario_key,
    index: event.index,
    status: 'done',
    verdict: event.verdict,
    audit_rejection: event.audit_rejection ?? null,
    degraded: event.degraded ?? false,
    material: event.material,
    blueprint: event.blueprint,
    audit: event.audit,
    cross_check: event.cross_check,
    created_at: new Date().toISOString(),
  }
}

export const useBatchStore = create<BatchState & Actions>((set, get) => ({
  ...EMPTY,

  reset: () => set({ ...EMPTY, seenSeqs: new Set() }),

  startCreating: () => set({ connection: 'creating', lastError: null }),

  initBatch: ({ batchId, total, items, requested }) =>
    set({
      ...EMPTY,
      seenSeqs: new Set(),
      batchId,
      total,
      requested: requested ?? [],
      createdAt: Date.now(),
      status: 'running',
      items: Object.fromEntries(items.map((i) => [i.material_id, i])),
      itemOrder: items.map((i) => i.material_id),
      connection: 'streaming',
    }),

  applySnapshot: (snapshot) =>
    set((s) => {
      const items = { ...s.items }
      for (const item of snapshot.items) {
        items[item.material_id] = { ...items[item.material_id], ...item }
      }
      return {
        batchId: snapshot.batch_id,
        status: snapshot.status,
        total: snapshot.total,
        createdAt: s.createdAt ?? Date.parse(snapshot.created_at),
        items,
        itemOrder:
          s.itemOrder.length > 0 ? s.itemOrder : snapshot.items.map((i) => i.material_id),
        // Never lower seqHigh: the local cursor may be ahead of the snapshot.
        seqHigh: Math.max(s.seqHigh, 0),
      }
    }),

  applyEvent: (event) => {
    const state = get()
    if ('seq' in event && state.seenSeqs.has(event.seq)) return false // replay dedupe

    set((s) => {
      const seenSeqs = new Set(s.seenSeqs)
      seenSeqs.add(event.seq)
      const seqHigh = Math.max(s.seqHigh, event.seq)
      const items = { ...s.items }
      const materials = { ...s.materials }
      let status = s.status
      let connection = s.connection
      let itemOrder = s.itemOrder

      switch (event.event) {
        case 'hello':
          status = 'running'
          break
        case 'progress': {
          const prev = items[event.material_id]
          items[event.material_id] = {
            material_id: event.material_id,
            scenario_key: prev?.scenario_key ?? '',
            index: prev?.index ?? 0,
            status: 'running',
            stage: event.stage,
            attempt: event.attempt,
            verdict: prev?.verdict,
            rawStage: event.raw_stage ?? null,
            // Advance-only: see BatchItemState.phase.
            phase: advancePhase(
              prev?.phase ?? null,
              phaseOfProgress({ stage: event.stage, rawStage: event.raw_stage }),
            ),
          }
          if (!itemOrder.includes(event.material_id)) {
            itemOrder = [...itemOrder, event.material_id]
          }
          break
        }
        case 'material': {
          const record = materialFromEvent(event, s.batchId ?? event.material_id)
          materials[event.material_id] = record
          items[event.material_id] = {
            material_id: event.material_id,
            scenario_key: event.scenario_key,
            index: event.index,
            status: 'done',
            stage: 're_auditing',
            attempt: items[event.material_id]?.attempt ?? 1,
            verdict: event.verdict,
            phase: 'reviewing',
          }
          if (!itemOrder.includes(event.material_id)) {
            itemOrder = [...itemOrder, event.material_id]
          }
          break
        }
        case 'material_failed': {
          const prev = items[event.material_id]
          items[event.material_id] = {
            material_id: event.material_id,
            scenario_key: prev?.scenario_key ?? '',
            index: prev?.index ?? 0,
            status: 'failed',
            stage: prev?.stage ?? 'generating',
            attempt: event.attempts,
            error: event.message,
            failure: { code: event.code, message: event.message, attempts: event.attempts },
          }
          break
        }
        case 'batch_done':
          status = event.status
          connection = event.status === 'done' ? 'done' : 'partial'
          break
        case 'ping':
          break
      }

      return { seenSeqs, seqHigh, items, materials, status, connection, itemOrder }
    })
    return true
  },

  setConnection: (connection, attempt = 0, error = null) =>
    set((s) => ({
      connection,
      reconnectAttempt: attempt,
      lastError: error,
      degradedRecovery:
        s.degradedRecovery || connection === 'reconnecting' || connection === 'recovered',
    })),

  hydrate: (persisted, materials) =>
    set(() => ({
      ...EMPTY,
      seenSeqs: new Set(),
      batchId: persisted.batchId,
      seqHigh: persisted.seqHigh,
      total: persisted.total,
      createdAt: persisted.createdAt,
      status: 'running',
      materials: Object.fromEntries(materials.map((m) => [m.material_id, m])),
      itemOrder: persisted.receivedIds,
      items: Object.fromEntries(
        materials.map((m) => [
          m.material_id,
          {
            material_id: m.material_id,
            scenario_key: m.scenario_key,
            index: m.index,
            status: 'done' as const,
            stage: 're_auditing' as MaterialStage,
            attempt: 1,
            verdict: m.verdict,
            phase: 'reviewing' as ProgressPhase,
          },
        ]),
      ),
    })),
}))

/* ── selectors: keep subscriptions narrow ────────────────────────────────── */

export const selectMaterial = (materialId: string) => (s: BatchState) => s.materials[materialId]
export const selectItem = (materialId: string) => (s: BatchState) => s.items[materialId]
export const selectCompleted = (s: BatchState) => Object.keys(s.materials).length
export const selectReadyMaterials = (s: BatchState): MaterialRecord[] =>
  s.itemOrder.map((id) => s.materials[id]).filter((m): m is MaterialRecord => Boolean(m))

/**
 * The furthest phase reached by any material still in flight — the batch-wide
 * "正在校验" caption.
 *
 * Read off the in-flight items only. Including the finished ones would peg the
 * caption at 复评 the moment the first material lands, while five others are
 * still being written.
 */
export function selectActivePhase(s: BatchState): ProgressPhase | null {
  let furthest: ProgressPhase | null = null
  for (const id of s.itemOrder) {
    const item = s.items[id]
    if (!item || item.status === 'done' || item.status === 'failed') continue
    furthest = advancePhase(furthest, item.phase ?? null)
  }
  return furthest
}
