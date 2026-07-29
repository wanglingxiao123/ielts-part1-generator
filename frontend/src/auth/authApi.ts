/**
 * The four calls of the web tier's cookie-session API (web/app.py).
 *
 * Everything here is same-origin and cookie-based. The session cookie is
 * HttpOnly, so JS cannot read it and there is no token to attach to requests:
 * `credentials: 'same-origin'` is the whole auth mechanism, and "am I logged in"
 * is answerable only by asking the server (`me()`).
 *
 * There is deliberately no Authorization header anywhere in the frontend. The
 * SigV4-signed call to the AgentCore Runtime is made by the web tier, so the
 * browser never holds an AWS credential of any kind.
 */
import { getConfig } from '@/config/runtimeConfig'
import type { ApiErrorBody } from '@/contracts/api'

export interface AuthUser {
  email: string
  /** True for the first account ever registered (web/auth.py). */
  is_admin: boolean
  /** Unix seconds. */
  created_at: number | null
}

/** A 4xx from `/api/auth/*`, carrying the server's error code. */
export class AuthError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'AuthError'
    this.status = status
    this.code = code
  }
}

/**
 * Chinese text per error code from web/auth.py.
 *
 * Used as a *fallback*, not an override: the server's own messages are already
 * Chinese for the cases a user can cause, and `EMAIL_DOMAIN_NOT_ALLOWED` names
 * the actual allowlist, which we cannot reproduce client-side. But the same code
 * is also raised with an English message for a malformed address, and session
 * errors are English throughout — so `messageFor` prefers the server's text only
 * when it is Chinese, and otherwise substitutes ours. A raw code or an English
 * sentence must never reach the user (prd: UI language is Chinese).
 */
const MESSAGES: Record<string, string> = {
  EMAIL_DOMAIN_NOT_ALLOWED: '邮箱地址不符合注册要求，请使用被允许的企业邮箱',
  USER_EXISTS: '该邮箱已注册，请直接登录',
  INVALID_CREDENTIALS: '邮箱或密码不正确',
  WEAK_PASSWORD: '密码至少 8 位',
  UNAUTHENTICATED: '登录状态已失效，请重新登录',
  INVALID_SESSION: '登录状态已失效，请重新登录',
}

const HAS_CHINESE = /[一-鿿]/

export function messageFor(code: string, serverMessage: string, status: number): string {
  if (HAS_CHINESE.test(serverMessage)) return serverMessage
  return MESSAGES[code] ?? `请求失败（${code || status}）`
}

export type AuthFetch = (path: string, init: RequestInit) => Promise<Response>

let authFetch: AuthFetch = (path, init) => fetch(`${getConfig().apiBaseUrl}${path}`, init)

/**
 * Mock seam, matching http.ts's `setTransport` and sseClient's `setSseFetch`.
 *
 * These calls do not go through the `Transport` abstraction: `/api/auth/*` is the
 * web tier's own REST surface, while `Transport` is aimed at the AgentCore
 * adapter's single `POST /invocations`. Routing auth through it would mean
 * teaching the adapter about endpoints it does not have.
 */
export function setAuthFetch(fn: AuthFetch) {
  authFetch = fn
}

async function post(path: string, body?: unknown): Promise<unknown> {
  const res = await authFetch(path, {
    method: 'POST',
    // Explicit rather than relying on the fetch default: this is the auth
    // mechanism, so it should be visible at the call site.
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const payload: unknown = await res.json().catch(() => null)
  if (!res.ok) {
    const err = (payload as ApiErrorBody | null)?.error
    const code = err?.code ?? 'HTTP_ERROR'
    throw new AuthError(res.status, code, messageFor(code, err?.message ?? '', res.status))
  }
  return payload
}

function userFrom(payload: unknown): AuthUser {
  const user = (payload as { user?: Partial<AuthUser> } | null)?.user
  if (!user || typeof user.email !== 'string') {
    throw new AuthError(500, 'BAD_RESPONSE', '服务器返回的登录信息无法识别')
  }
  return {
    email: user.email,
    is_admin: Boolean(user.is_admin),
    created_at: typeof user.created_at === 'number' ? user.created_at : null,
  }
}

/**
 * The current user, or null when nobody is signed in.
 *
 * A 401 here is a normal answer ("anonymous"), not a failure — conflating the
 * two is what would put an error banner in front of a first-time visitor
 * instead of the login form. Only a transport/5xx failure throws.
 */
export async function me(): Promise<AuthUser | null> {
  const res = await authFetch('/auth/me', {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
  })
  if (res.status === 401) return null
  if (!res.ok) {
    throw new AuthError(res.status, 'ME_FAILED', `无法确认登录状态（HTTP ${res.status}）`)
  }
  return userFrom(await res.json())
}

export async function login(email: string, password: string): Promise<AuthUser> {
  return userFrom(await post('/auth/login', { email, password }))
}

/** Registering also signs you in — the web tier sets the cookie on 200. */
export async function register(email: string, password: string): Promise<AuthUser> {
  return userFrom(await post('/auth/register', { email, password }))
}

export async function logout(): Promise<void> {
  await post('/auth/logout')
}
