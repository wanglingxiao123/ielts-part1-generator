import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useSession } from './useSession'

/**
 * Route guard. No business page renders without a session (prd R1).
 *
 * Redirects to the in-SPA /login rather than firing a `signIn()` side effect, and
 * carries the attempted path in router state so LoginPage can return the user
 * there. State rather than a query string or sessionStorage: it is scoped to this
 * navigation, so a stale entry cannot redirect a later login somewhere unrelated.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession()
  const location = useLocation()
  const from = location.pathname + location.search

  if (session.isLoading) {
    return (
      <div className="page">
        <div className="panel panel-pad">正在校验登录状态…</div>
      </div>
    )
  }

  // Only a failed probe (offline, 5xx) reaches here — a 401 is "anonymous",
  // handled below. Sending someone to a login form they cannot submit would
  // misattribute an outage to their credentials.
  if (session.error && !session.isAuthenticated) {
    return (
      <div className="page">
        <div className="banner banner-bad">
          <strong>无法确认登录状态</strong>
          <div>
            {session.error}
            <div style={{ marginTop: 8 }}>
              <button type="button" className="btn" onClick={() => window.location.reload()}>
                重试
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (!session.isAuthenticated) {
    return <Navigate to="/login" replace state={{ from }} />
  }

  return <>{children}</>
}
