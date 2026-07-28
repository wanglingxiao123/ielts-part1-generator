import type { ReactNode } from 'react'
import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { useSession } from './useSession'

/** Route guard. No business page renders without a session (prd R1). */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession()
  const location = useLocation()
  const returnTo = location.pathname + location.search

  useEffect(() => {
    if (!session.isLoading && !session.isAuthenticated && !session.error) {
      session.signIn(returnTo)
    }
  }, [session, returnTo])

  if (session.isLoading) {
    return (
      <div className="page">
        <div className="panel panel-pad">正在校验登录状态…</div>
      </div>
    )
  }

  if (session.error) {
    return (
      <div className="page">
        <div className="banner banner-bad">
          <strong>登录失败</strong>
          <div>
            {session.error}
            <div style={{ marginTop: 8 }}>
              <button type="button" className="btn" onClick={() => session.signIn(returnTo)}>
                重新登录
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (!session.isAuthenticated) {
    return (
      <div className="page">
        <div className="panel panel-pad">
          正在跳转到 Cognito 登录…
          <div className="muted" style={{ marginTop: 6 }}>
            登录后将返回 <span className="mono">{returnTo}</span>
          </div>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
