/**
 * Module-level stream owner (design.md §5.3).
 *
 * Deliberately NOT per-component: the scenario page opens the stream and then
 * navigates away, so a component-scoped controller would be torn down by its own
 * unmount cleanup and the batch page would silently observe nothing. The stream
 * outlives any single route, exactly like the backend job outlives any single
 * connection.
 */
import { api } from '@/api/endpoints'
import { openBatchStream, type SseController } from '@/api/sseClient'
import { savePersisted, useBatchStore } from '@/stores/batchStore'

let controller: SseController | null = null
let activeBatchId: string | null = null
let activeOwner = ''

function persist() {
  const s = useBatchStore.getState()
  if (!s.batchId) return
  savePersisted(activeOwner, {
    batchId: s.batchId,
    seqHigh: s.seqHigh,
    receivedIds: Object.keys(s.materials),
    createdAt: s.createdAt ?? Date.now(),
    total: s.total,
  })
}

export function connectBatchStream(batchId: string, owner: string): void {
  if (activeBatchId === batchId && controller) return // idempotent
  disconnectBatchStream()
  activeBatchId = batchId
  activeOwner = owner

  controller = openBatchStream({
    batchId,
    sinceSeq: () => useBatchStore.getState().seqHigh,
    reconcile: async () => {
      try {
        const snapshot = await api.getBatch(batchId)
        useBatchStore.getState().applySnapshot(snapshot)
      } catch (err) {
        console.warn('[sse] snapshot reconciliation failed', err)
      }
    },
    onOpen: () => {
      const prev = useBatchStore.getState().connection
      useBatchStore
        .getState()
        .setConnection(prev === 'reconnecting' ? 'recovered' : 'streaming', 0, null)
      if (prev === 'reconnecting') {
        // Green "recovered" bar for 3s, then back to plain streaming.
        window.setTimeout(() => {
          if (useBatchStore.getState().connection === 'recovered') {
            useBatchStore.getState().setConnection('streaming')
          }
        }, 3000)
      }
    },
    onEvent: (event) => {
      if (useBatchStore.getState().applyEvent(event)) persist()
    },
    onReconnecting: (attempt) => {
      useBatchStore.getState().setConnection('reconnecting', attempt, null)
    },
    onGiveUp: (lastError) => {
      useBatchStore.getState().setConnection('failed', 0, lastError)
    },
  })
}

export function disconnectBatchStream(): void {
  controller?.close()
  controller = null
  activeBatchId = null
}

export function retryBatchStream(): void {
  useBatchStore.getState().setConnection('reconnecting', 1, null)
  controller?.retryNow()
}

export function isStreamActive(batchId: string): boolean {
  return activeBatchId === batchId && controller !== null
}
