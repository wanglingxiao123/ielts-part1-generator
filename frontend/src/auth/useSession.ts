import { useContext } from 'react'
import { SessionContext, type Session } from './sessionContext'

export function useSession(): Session {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('useSession must be used inside <AuthProvider>')
  return ctx
}
