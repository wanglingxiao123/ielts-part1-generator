/**
 * Cognito auth (design.md §7.1): oidc-client-ts + react-oidc-context,
 * Hosted UI, Authorization Code + PKCE, no client secret.
 *
 * DEV BYPASS: `auth.devBypass` in config.json short-circuits to a fake session.
 * The AWS credentials on this machine are expired, so the User Pool cannot be
 * reached; the bypass keeps the app reviewable without weakening the real path —
 * the guard, the role plumbing and the 401 interceptor are the same code either
 * way, and a deployed config.json must set devBypass:false.
 */
import { useMemo, type ReactNode } from 'react'
import { AuthProvider as OidcProvider, useAuth as useOidcAuth } from 'react-oidc-context'
import { WebStorageStateStore } from 'oidc-client-ts'
import { getConfig } from '@/config/runtimeConfig'
import { rolesFromGroups } from './permissions'
import { RETURN_TO_KEY, SessionContext, type Session } from './sessionContext'

const DEV_SESSION: Omit<Session, 'signIn' | 'signOut'> = {
  isAuthenticated: true,
  isLoading: false,
  idToken: null,
  username: 'dev@local',
  sub: 'dev-local-sub',
  roles: ['generator', 'reviewer'],
  mode: 'dev-bypass',
  error: null,
}

function CognitoSession({ children }: { children: ReactNode }) {
  const auth = useOidcAuth()
  const session = useMemo<Session>(() => {
    const profile = auth.user?.profile as Record<string, unknown> | undefined
    return {
      isAuthenticated: auth.isAuthenticated,
      isLoading: auth.isLoading,
      idToken: auth.user?.id_token ?? null,
      username:
        (profile?.email as string | undefined) ??
        (profile?.['cognito:username'] as string | undefined) ??
        '',
      sub: (profile?.sub as string | undefined) ?? '',
      roles: rolesFromGroups(profile?.['cognito:groups']),
      mode: 'cognito',
      error: auth.error?.message ?? null,
      signIn: (returnTo) => {
        if (returnTo) sessionStorage.setItem(RETURN_TO_KEY, returnTo)
        void auth.signinRedirect()
      },
      signOut: () => void auth.signoutRedirect(),
    }
  }, [auth])
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const cfg = getConfig()

  if (cfg.auth.devBypass) {
    const session: Session = { ...DEV_SESSION, signIn: () => {}, signOut: () => {} }
    return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
  }

  return (
    <OidcProvider
      authority={cfg.auth.authority}
      client_id={cfg.auth.clientId}
      redirect_uri={`${window.location.origin}/auth/callback`}
      post_logout_redirect_uri={window.location.origin}
      response_type="code"
      scope={cfg.auth.scope}
      // access/id tokens stay in memory; only the refresh state is persisted.
      // A pure SPA cannot use httpOnly cookies without a BFF (prd Open Q4).
      userStore={new WebStorageStateStore({ store: window.sessionStorage })}
      automaticSilentRenew
      onSigninCallback={() => {
        const returnTo = sessionStorage.getItem(RETURN_TO_KEY)
        sessionStorage.removeItem(RETURN_TO_KEY)
        window.history.replaceState({}, '', returnTo || '/')
      }}
    >
      <CognitoSession>{children}</CognitoSession>
    </OidcProvider>
  )
}
