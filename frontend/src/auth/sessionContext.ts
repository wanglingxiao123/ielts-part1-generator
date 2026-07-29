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
   * The first account ever registered (web/auth.py). A LABEL, not a permission:
   * the backend gates every `/api/*` path on "has a valid session" and nothing
   * else, so there is no capability this could honestly unlock client-side.
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
