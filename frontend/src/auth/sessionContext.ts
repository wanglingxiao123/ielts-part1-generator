import { createContext } from 'react'
import type { Role } from './permissions'

export interface Session {
  isAuthenticated: boolean
  isLoading: boolean
  /** null while loading, unauthenticated, or under the dev bypass. */
  idToken: string | null
  username: string
  sub: string
  roles: Role[]
  /** Real Cognito session vs the dev bypass — surfaced in the UI. */
  mode: 'cognito' | 'dev-bypass'
  error: string | null
  signIn: (returnTo?: string) => void
  signOut: () => void
}

export const SessionContext = createContext<Session | null>(null)

export const RETURN_TO_KEY = 'bcielts.v1.returnTo'
