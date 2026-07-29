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
  // The email is the account identity now that there is no Cognito `sub`. It is
  // only a namespace for locally persisted batch state, and the web tier
  // lower-cases addresses before minting a session, so it is stable per account.
  const { email } = useSession()

  const connect = useCallback((batchId: string) => connectBatchStream(batchId, email), [email])
  const resumePersisted = useCallback(() => loadPersisted(email), [email])

  return {
    connect,
    disconnect: disconnectBatchStream,
    retryNow: retryBatchStream,
    isActive: isStreamActive,
    resumePersisted,
  }
}
