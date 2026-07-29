/**
 * Session state over the web tier's cookie API (web/app.py `/api/auth/*`).
 *
 * Replaces an oidc-client-ts / Cognito Hosted UI provider. Cognito was never
 * deployed — its hosted login refuses HTTP callbacks for any host but localhost,
 * and this deployment is plain HTTP on a Fargate task IP — so the web tier owns
 * accounts itself. The old provider's `devBypass` flag papered over that with a
 * fake session, which meant the only two configurations were "fake login" and
 * "redirect to a Hosted UI that does not exist".
 *
 * The cookie is HttpOnly, so mounting cannot read a session out of storage: the
 * single source of truth is `GET /api/auth/me`, called once on mount. That probe
 * is also why `isLoading` exists — rendering the login form before it answers
 * would flash a form at an already-signed-in user on every reload.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import * as authApi from './authApi'
import type { AuthUser } from './authApi'
import { SessionContext, type Session } from './sessionContext'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const found = await authApi.me()
        if (!cancelled) {
          setUser(found)
          setError(null)
        }
      } catch (err) {
        // Only a transport/5xx failure lands here; `me()` maps 401 → null.
        if (!cancelled) setError(err instanceof Error ? err.message : '无法确认登录状态')
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    const next = await authApi.login(email, password)
    setUser(next)
    setError(null)
  }, [])

  const signUp = useCallback(async (email: string, password: string) => {
    const next = await authApi.register(email, password)
    setUser(next)
    setError(null)
  }, [])

  /**
   * Never rejects. Sign-out has no failure a user could act on, and the top bar
   * calls it as `void signOut()` — a rejecting promise there is an unhandled
   * rejection, not an error message.
   *
   * The local session is dropped even when the request failed, so the UI matches
   * what the user asked for. That is best-effort by design at both ends:
   * web/auth.py's token is stateless, so logout only ever clears the cookie and a
   * token that escaped stays valid until it expires.
   */
  const signOut = useCallback(async () => {
    try {
      await authApi.logout()
    } catch (err) {
      console.warn('[auth] logout request failed; clearing the local session anyway:', err)
    }
    setUser(null)
    setError(null)
  }, [])

  const session = useMemo<Session>(
    () => ({
      isAuthenticated: user !== null,
      isLoading,
      user,
      email: user?.email ?? '',
      isAdmin: user?.is_admin ?? false,
      error,
      signIn,
      signUp,
      signOut,
    }),
    [user, isLoading, error, signIn, signUp, signOut],
  )

  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
}
