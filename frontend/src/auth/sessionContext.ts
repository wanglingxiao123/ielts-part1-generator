import { createContext } from 'react'
import type { AuthUser } from './authApi'

export interface Session {
  isAuthenticated: boolean
  /** True only during the initial `/api/auth/me` probe. */
  isLoading: boolean
  user: AuthUser | null
  /** '' when anonymous. Also the per-user key for locally persisted batch state. */
  email: string
  /**
   * Persisted administrator flag. Controls admin navigation and page mounting;
   * admin API endpoints must independently enforce this permission server-side.
   */
  isAdmin: boolean
  /**
   * Set only when the probe itself failed (offline, 5xx). A 401 is not an error,
   * it is the answer "anonymous" — that leads to the login page, not a banner.
   */
  error: string | null
  /** Both reject with `AuthError`; the form renders `.message`. */
  signIn: (email: string, password: string) => Promise<void>
  signUp: (email: string, password: string) => Promise<void>
  /** Never rejects — see AuthProvider. Safe to call as `void signOut()`. */
  signOut: () => Promise<void>
}

export const SessionContext = createContext<Session | null>(null)
