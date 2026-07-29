/**
 * HTTP client. The mock seam is here and nowhere else: swapping mock for real
 * HTTP is `setTransport()` / VITE_MOCK, no call site changes.
 *
 * `realTransport` below is the plain §8 REST client and is what a §8-shaped
 * backend would use. The backend that exists today is an AgentCore Runtime with a
 * single `POST /invocations`, so main.tsx installs `api/agentcore.ts`'s transport
 * instead. Keeping both means the day the REST surface lands, deleting one line
 * in main.tsx is the whole migration.
 */
import { getConfig } from '@/config/runtimeConfig'
import type { ApiErrorBody } from '@/contracts/api'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail?: Record<string, unknown>

  constructor(
    status: number,
    code: string,
    message: string,
    detail?: Record<string, unknown>,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export interface RequestSpec {
  method: 'GET' | 'POST'
  /** Path relative to apiBaseUrl, e.g. `/batches/abc`. */
  path: string
  body?: unknown
  signal?: AbortSignal
}

export type Transport = (spec: RequestSpec) => Promise<unknown>

/** Called on a 401, i.e. the session cookie is missing, expired or tampered. */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

/**
 * Announce a 401 seen outside `realTransport`.
 *
 * The AgentCore adapter issues its own fetches (SigV4 shape, streaming), so the
 * 401 branch below never runs in the deployed configuration. Without this the
 * session-expiry path existed only in code no environment executes.
 */
export function notifyUnauthorized() {
  onUnauthorized?.()
}

/**
 * Credentials for every API call: the web tier's HttpOnly session cookie.
 *
 * There is no Authorization header anywhere in the frontend, deliberately. The
 * SigV4-signed call to the AgentCore Runtime happens server-side in web/app.py,
 * so the browser never holds an AWS credential; and the session cookie is
 * HttpOnly, so JS could not read it to build a header even if it wanted to.
 */
export const CREDENTIALS: RequestCredentials = 'same-origin'

export const realTransport: Transport = async (spec) => {
  const cfg = getConfig()
  const res = await fetch(`${cfg.apiBaseUrl}${spec.path}`, {
    method: spec.method,
    credentials: CREDENTIALS,
    headers: spec.body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: spec.body === undefined ? undefined : JSON.stringify(spec.body),
    signal: spec.signal,
  })

  const contractVersion = res.headers.get('X-Contract-Version')
  if (contractVersion && contractVersion !== cfg.contractVersion) {
    console.warn(
      `[api] contract version mismatch: server ${contractVersion}, client ${cfg.contractVersion}`,
    )
  }

  if (res.status === 401) {
    onUnauthorized?.()
    throw new ApiError(401, 'UNAUTHENTICATED', '登录状态已失效，请重新登录')
  }
  if (!res.ok) {
    let body: ApiErrorBody | null = null
    try {
      body = (await res.json()) as ApiErrorBody
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(
      res.status,
      body?.error.code ?? 'HTTP_ERROR',
      body?.error.message ?? `请求失败 (${res.status})`,
      body?.error.detail,
    )
  }
  if (res.status === 204) return null
  return res.json()
}

let transport: Transport = realTransport

export function setTransport(next: Transport) {
  transport = next
}

export function useRealTransport() {
  transport = realTransport
}

export async function request<T>(spec: RequestSpec): Promise<T> {
  return (await transport(spec)) as T
}
