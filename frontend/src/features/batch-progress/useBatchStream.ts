/**
 * Thin hook over the module-level stream manager. The manager owns the
 * connection lifecycle so navigating between the scenario page and the batch
 * page does not tear the stream down (see batchStreamManager.ts).
 */
import { useCallback } from 'react'
import { useSession } from '@/auth/useSession'
import { loadPersisted, type PersistedBatch } from '@/stores/batchStore'
import {
  connectBatchStream,
  disconnectBatchStream,
  isStreamActive,
  retryBatchStream,
} from './batchStreamManager'

export interface BatchStreamApi {
  connect: (batchId: string) => void
  disconnect: () => void
  retryNow: () => void
  isActive: (batchId: string) => boolean
  resumePersisted: () => PersistedBatch | null
}

export function useBatchStream(): BatchStreamApi {
  const { sub } = useSession()

  const connect = useCallback((batchId: string) => connectBatchStream(batchId, sub), [sub])
  const resumePersisted = useCallback(() => loadPersisted(sub), [sub])

  return {
    connect,
    disconnect: disconnectBatchStream,
    retryNow: retryBatchStream,
    isActive: isStreamActive,
    resumePersisted,
  }
}
