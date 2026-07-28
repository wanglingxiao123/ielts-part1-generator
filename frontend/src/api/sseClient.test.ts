/**
 * SSE client framing + terminal handling.
 *
 * The reconnect behaviour is the part worth testing: `done` on the reader looks
 * identical whether the batch finished or the connection dropped, and getting it
 * wrong shows a scary 「连接中断，正在重连」 banner over a completed batch.
 */
import { describe, expect, it, vi } from 'vitest'
import type { SseEvent } from '@/contracts/api'
import { decodeFrame, openBatchStream, parseFrames, setSseFetch } from './sseClient'

describe('parseFrames', () => {
  it('splits on a blank line and keeps the incomplete tail', () => {
    const { events, rest } = parseFrames('event: a\ndata: 1\n\nevent: b\ndata: 2')
    expect(events).toEqual(['event: a\ndata: 1'])
    expect(rest).toBe('event: b\ndata: 2')
  })

  it('handles CRLF as well as LF', () => {
    const { events } = parseFrames('data: {"seq":1}\r\n\r\n')
    expect(events).toEqual(['data: {"seq":1}'])
  })
})

describe('decodeFrame', () => {
  it('treats the event: line as authoritative over any data.event', () => {
    const e = decodeFrame('event: material\ndata: {"seq":3,"event":"progress"}')
    expect(e?.event).toBe('material')
  })

  it('ignores comment keepalives', () => {
    expect(decodeFrame(': ping')).toBeNull()
  })

  it('joins multi-line data payloads', () => {
    const e = decodeFrame('event: hello\ndata: {"seq":1,\ndata: "batch_id":"b"}')
    expect(e).toMatchObject({ event: 'hello', seq: 1 })
  })
})

function streamOf(frames: string[]): Response {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const f of frames) controller.enqueue(encoder.encode(f))
        controller.close()
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
}

const frame = (event: string, data: Record<string, unknown>) =>
  `event: ${event}\nid: ${data.seq}\ndata: ${JSON.stringify({ ...data, event })}\n\n`

describe('terminal handling', () => {
  it('does not schedule a reconnect after batch_done', async () => {
    vi.useFakeTimers()
    try {
      let calls = 0
      setSseFetch(() => {
        calls += 1
        return Promise.resolve(
          streamOf([
            frame('hello', { seq: 1, batch_id: 'b', total: 1 }),
            frame('batch_done', { seq: 2, status: 'done', completed: 1, failed: 0 }),
          ]),
        )
      })
      const events: SseEvent[] = []
      const onReconnecting = vi.fn()
      const onClosed = vi.fn()
      openBatchStream({
        batchId: 'b',
        sinceSeq: () => 0,
        onEvent: (e) => events.push(e),
        onReconnecting,
        onClosed,
      })
      await vi.waitFor(() => expect(onClosed).toHaveBeenCalled())
      // A finished batch must not look like a dropped connection.
      expect(onReconnecting).not.toHaveBeenCalled()
      expect(events.map((e) => e.event)).toEqual(['hello', 'batch_done'])
      await vi.advanceTimersByTimeAsync(30_000)
      expect(calls).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does reconnect when the stream ends without a terminal event', async () => {
    vi.useFakeTimers()
    try {
      let calls = 0
      setSseFetch(() => {
        calls += 1
        return Promise.resolve(streamOf([frame('hello', { seq: 1, batch_id: 'b', total: 2 })]))
      })
      const onReconnecting = vi.fn()
      openBatchStream({
        batchId: 'b',
        sinceSeq: () => 0,
        onEvent: () => {},
        onReconnecting,
      })
      await vi.waitFor(() => expect(onReconnecting).toHaveBeenCalled())
      expect(calls).toBe(1)
      await vi.advanceTimersByTimeAsync(3000)
      expect(calls).toBeGreaterThan(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
