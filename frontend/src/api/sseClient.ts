/**
 * SSE over fetch + ReadableStream (design.md §5.2).
 *
 * Not EventSource: it cannot set an Authorization header, so the token would
 * have to travel in the query string and land in ALB / CloudWatch access logs.
 * Reconnection with since_seq has to be hand-written either way — the native
 * retry semantics are not sufficient here.
 */
import { getConfig } from '@/config/runtimeConfig'
import type { SseEvent } from '@/contracts/api'
import { authHeader } from './http'

export interface SseHandlers {
  onEvent: (event: SseEvent) => void
  onOpen?: () => void
  /** Fired per failed attempt; `attempt` is 1-based. */
  onReconnecting?: (attempt: number, delayMs: number) => void
  onGiveUp?: (lastError: string) => void
  onClosed?: () => void
}

export interface SseStreamOptions extends SseHandlers {
  batchId: string
  /** Highest seq already applied. The server replays seq > this. */
  sinceSeq: () => number
  /** Snapshot reconciliation before resuming (design.md §5.3). */
  reconcile?: () => Promise<void>
  maxAttempts?: number
}

/**
 * A terminal event means the stream SHOULD end; the server closing after it is
 * not a disconnect.
 *
 * Without this the client showed 「连接中断，正在重连（第 1/8 次）」 over an
 * already-finished batch, because `done` on the reader looks the same whether
 * the batch finished or the connection dropped. Observed against the real
 * backend, where the response body always closes when generation ends, but the
 * §8 contract has the same property — a compliant server also closes after
 * `batch_done`.
 */
function isTerminal(event: SseEvent): boolean {
  return event.event === 'batch_done'
}

const BACKOFF_MS = [1000, 2000, 4000, 8000, 15_000]
const MAX_ATTEMPTS = 8

function jitter(ms: number): number {
  return Math.round(ms * (0.8 + Math.random() * 0.4))
}

export type SseFetch = (
  url: string,
  init: { headers: Record<string, string>; signal: AbortSignal },
) => Promise<Response>

let sseFetch: SseFetch = (url, init) => fetch(url, init)

/** Mock seam for the stream, matching http.ts's setTransport. */
export function setSseFetch(fn: SseFetch) {
  sseFetch = fn
}

/** Parses SSE frames out of a raw text chunk stream. */
export function parseFrames(buffer: string): { events: string[]; rest: string } {
  const events: string[] = []
  let rest = buffer
  for (;;) {
    const idx = rest.search(/\r?\n\r?\n/)
    if (idx < 0) break
    const match = /\r?\n\r?\n/.exec(rest.slice(idx))!
    events.push(rest.slice(0, idx))
    rest = rest.slice(idx + match[0].length)
  }
  return { events, rest }
}

/** One frame → SseEvent, or null for comments/keepalives/unparseable data. */
export function decodeFrame(frame: string): SseEvent | null {
  let eventName = 'message'
  const dataLines: string[] = []
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith(':')) continue // comment / keepalive
    const colon = rawLine.indexOf(':')
    const field = colon < 0 ? rawLine : rawLine.slice(0, colon)
    const value = colon < 0 ? '' : rawLine.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') eventName = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return null
  try {
    const payload = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
    // The `event:` line is authoritative; `data.event` is only a convenience.
    return { ...payload, event: eventName } as SseEvent
  } catch {
    console.warn('[sse] undecodable frame', frame)
    return null
  }
}

export interface SseController {
  close: () => void
  /** Manual retry after give-up (design.md §5.3: no infinite retry). */
  retryNow: () => void
}

export function openBatchStream(options: SseStreamOptions): SseController {
  const maxAttempts = options.maxAttempts ?? MAX_ATTEMPTS
  let closed = false
  let attempt = 0
  let abort: AbortController | null = null
  let timer: number | null = null

  const clearTimer = () => {
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  }

  const scheduleRetry = (reason: string) => {
    if (closed) return
    attempt += 1
    if (attempt > maxAttempts) {
      options.onGiveUp?.(reason)
      return
    }
    const base = BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]!
    const delay = jitter(base)
    options.onReconnecting?.(attempt, delay)
    clearTimer()
    timer = window.setTimeout(() => void connect(), delay)
  }

  async function connect(): Promise<void> {
    if (closed) return
    abort = new AbortController()
    try {
      // Snapshot first, then resume: reconciling against the authoritative
      // snapshot corrects local drift that pure replay would preserve.
      if (attempt > 0 && options.reconcile) await options.reconcile()
      if (closed) return

      const cfg = getConfig()
      const since = options.sinceSeq()
      const url = `${cfg.apiBaseUrl}/batches/${options.batchId}/stream?since_seq=${since}`
      const res = await sseFetch(url, {
        headers: { ...authHeader(), Accept: 'text/event-stream' },
        signal: abort.signal,
      })
      if (!res.ok || !res.body) throw new Error(`stream ${res.status}`)

      attempt = 0
      options.onOpen?.()

      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
      let buffer = ''
      let terminal = false
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += value
        const { events, rest } = parseFrames(buffer)
        buffer = rest
        for (const frame of events) {
          const event = decodeFrame(frame)
          if (!event) continue
          if (isTerminal(event)) terminal = true
          options.onEvent(event)
        }
      }
      if (terminal) {
        closed = true
        options.onClosed?.()
      } else if (!closed) scheduleRetry('stream ended')
      else options.onClosed?.()
    } catch (err) {
      if (closed || (err instanceof Error && err.name === 'AbortError')) {
        options.onClosed?.()
        return
      }
      scheduleRetry(err instanceof Error ? err.message : String(err))
    }
  }

  void connect()

  return {
    close: () => {
      closed = true
      clearTimer()
      abort?.abort()
    },
    retryNow: () => {
      attempt = 0
      clearTimer()
      void connect()
    },
  }
}
