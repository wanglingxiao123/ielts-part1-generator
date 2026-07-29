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
  /** Real-backend stage name with no §8 equivalent; shown as a sub-label. */
  subStage?: string | null
}

export interface BatchState {
  batchId: string | null
  status: BatchStatus
  total: number
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
  initBatch: (input: { batchId: string; total: number; items: BatchItemSnapshot[] }) => void
  applySnapshot: (snapshot: BatchSnapshot) => void
  applyEvent: (event: SseEvent) => boolean
  setConnection: (state: ConnectionState, attempt?: number, error?: string | null) => void
  hydrate: (persisted: PersistedBatch, materials: MaterialRecord[]) => void
}

const EMPTY: BatchState = {
  batchId: null,
  status: 'queued',
  total: 0,
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
    quarantined: event.quarantined,
    quarantine_reason: event.quarantine_reason ?? null,
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

  initBatch: ({ batchId, total, items }) =>
    set({
      ...EMPTY,
      seenSeqs: new Set(),
      batchId,
      total,
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
            quarantined: prev?.quarantined,
            subStage: event.sub_stage ?? null,
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
            quarantined: event.quarantined,
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
            quarantined: m.quarantined,
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
