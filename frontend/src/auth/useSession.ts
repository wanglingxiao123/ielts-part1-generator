import { useContext } from 'react'
import { can, type Action } from './permissions'
import { SessionContext, type Session } from './sessionContext'

export function useSession(): Session {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('useSession must be used inside <AuthProvider>')
  return ctx
}

/** The single entry point for every permission check (prd R1). */
export function useCan(action: Action): boolean {
  const { roles } = useSession()
  return can(roles, action)
}
