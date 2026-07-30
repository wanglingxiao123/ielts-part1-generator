/**
 * The frontend contract against the MERGED wire stream `web/fanout.py` now produces.
 *
 * The claim this change rests on is "the frontend contract does not need to change". That claim is
 * cheap to make and easy to get wrong, because the failure mode is not an exception — a merged
 * stream with colliding slot ids renders *something*, and the last writer wins. So it is verified
 * here rather than assumed, against a stream assembled exactly the way the web tier assembles one:
 *
 *   - ONE `batch_started` carrying the batch total, emitted before any child answers;
 *   - each child's events with its `slot-1` rewritten to the batch-wide slot it was allotted;
 *   - children INTERLEAVED, because six independent invocations do not take turns;
 *   - ONE `batch_completed` with the aggregate counts.
 *
 * Two things are checked: that the adapter turns that into N distinct progressive cards, and that
 * `since_seq` replay still works — the seq numbers are minted by whoever owns the merged stream, so
 * "the sequence space survived the fan-out" is a property with a specific owner
 * (`agentcore.ts`'s `emit`) and a specific test below.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { installAgentCoreAdapter, resetAgentCore } from './agentcore'
import { setTransport } from './http'
import { api } from './endpoints'
import { openBatchStream } from './sseClient'
import type { SseEvent } from '@/contracts/api'
import RAW from './__fixtures__/real-batch.sse.txt?raw'

/* ── a real material, borrowed from the captured batch ────────────────────── */

function capturedCompletion(): Record<string, unknown> {
  for (const frame of RAW.split(/\r?\n\r?\n/)) {
    const line = frame.split(/\r?\n/).find((l) => l.startsWith('data:'))
    if (!line) continue
    const event = JSON.parse(line.slice(5).trim()) as Record<string, unknown>
    if (event.ok === true && event.material) return event
  }
  throw new Error('capture has no completed material')
}

/**
 * One child's events, already renamed to its batch-wide slot — i.e. what `_translate` emits.
 *
 * Note what is NOT here: the child's own `batch_started` and `batch_completed`. The web tier
 * swallows both, and this helper mirrors that so the fixture cannot accidentally test a contract
 * the wire does not have.
 */
function childEvents(slotId: string, scenario: string, ok: boolean) {
  const stages = ['generating', 'validating', 'auditing', 're_auditing'].map((stage) => ({
    type: 'stage',
    slot_id: slotId,
    scenario,
    stage,
    attempt: 1,
    at: 1785228044,
  }))
  const terminal = ok
    ? {
        ...capturedCompletion(),
        type: 'material_completed',
        slot_id: slotId,
        scenario,
        material_id: `20260101-${scenario}-${slotId.replace('slot-', 'aaaaaaa')}`,
        scenario_key: scenario,
        at: 1785228044,
      }
    : {
        type: 'material_failed',
        slot_id: slotId,
        scenario,
        ok: false,
        reason: 'model_error',
        at: 1785228044,
      }
  return [...stages, terminal]
}

/** Interleave the children round-robin: independent invocations do not take turns. */
function interleave<T>(streams: T[][]): T[] {
  const out: T[] = []
  const longest = Math.max(0, ...streams.map((s) => s.length))
  for (let i = 0; i < longest; i += 1) {
    for (const stream of streams) if (i < stream.length) out.push(stream[i]!)
  }
  return out
}

/**
 * The whole merged stream for a batch of `plan.length` materials, in web-tier shape.
 */
function mergedFrames(plan: Array<{ scenario: string; ok: boolean }>): string[] {
  const total = plan.length
  const started = {
    type: 'batch_started',
    total,
    deadline_at: 1785228854,
    config: { fanout: 'per_material_invoke', children: total, web_concurrency: 6 },
    at: 1785228044,
  }
  const children = plan.map((p, index) => childEvents(`slot-${index + 1}`, p.scenario, p.ok))
  const completed = {
    type: 'batch_completed',
    succeeded: plan.filter((p) => p.ok).length,
    failed: plan.filter((p) => !p.ok).length,
    skipped: 0,
    degraded: 0,
    refilled: 0,
    stage_timings: {},
    slots: plan.map((p, index) => ({
      slot_id: `slot-${index + 1}`,
      scenario: p.scenario,
      ok: p.ok,
    })),
    at: 1785228044,
  }
  return [started, ...interleave(children), completed].map(
    (event) => `data: ${JSON.stringify(event)}\n\n`,
  )
}

/* ── the harness ──────────────────────────────────────────────────────────── */

/** Serves `frames` as the /invocations response; other actions answer the catalogue. */
function installFanoutBackend(frames: string[], scenarios: string[]) {
  const original = globalThis.fetch
  globalThis.fetch = ((_url: RequestInfo | URL, init?: RequestInit) => {
    const body = init?.body ? (JSON.parse(String(init.body)) as { action?: string }) : {}
    if (body.action === 'list_scenarios') {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            scenarios: {
              version: 1,
              default_count: 2,
              categories: [
                {
                  id: 'c',
                  title_zh: 'c',
                  scenarios: scenarios.map((id) => ({
                    id,
                    category: 'c',
                    title_zh: id,
                    prompt_hint: 'h',
                    default_count: 1,
                  })),
                },
              ],
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }
    const encoder = new TextEncoder()
    return Promise.resolve(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            for (const f of frames) controller.enqueue(encoder.encode(f))
            controller.close()
          },
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    )
  }) as typeof fetch
  return () => {
    globalThis.fetch = original
  }
}

async function collect(batchId: string, sinceSeq = 0): Promise<SseEvent[]> {
  const events: SseEvent[] = []
  await new Promise<void>((resolve) => {
    openBatchStream({
      batchId,
      sinceSeq: () => sinceSeq,
      onEvent: (e) => events.push(e),
      onClosed: () => resolve(),
      onGiveUp: () => resolve(),
      maxAttempts: 0,
    })
  })
  return events
}

/* ── the tests ────────────────────────────────────────────────────────────── */

describe('the merged fan-out stream', () => {
  let restore: () => void = () => {}

  beforeEach(() => {
    resetAgentCore()
  })

  afterEach(() => {
    restore()
    resetAgentCore()
  })

  const PLAN = [
    { scenario: 'a', ok: true },
    { scenario: 'a', ok: true },
    { scenario: 'b', ok: true },
    { scenario: 'b', ok: false },
    { scenario: 'c', ok: true },
    { scenario: 'c', ok: true },
    { scenario: 'd', ok: true },
    { scenario: 'd', ok: true },
  ]

  async function runBatch(plan = PLAN) {
    restore = installFanoutBackend(
      mergedFrames(plan),
      [...new Set(plan.map((p) => p.scenario))],
    )
    const { transport } = installAgentCoreAdapter()
    setTransport(transport)
    const scenarios = [...new Set(plan.map((p) => p.scenario))]
    const created = await api.createBatch({
      requests: scenarios.map((scenario_key) => ({
        scenario_key,
        count: plan.filter((p) => p.scenario === scenario_key).length,
      })),
      options: { narration_mode: 'full' },
    })
    return created
  }

  it('accepts a batch well past the old ceiling of 6', async () => {
    // The exact submission the client was refused: 8 sets, no BATCH_LIMIT_EXCEEDED anywhere.
    const created = await runBatch()
    expect(created.total).toBe(8)
    expect(created.items).toHaveLength(8)
    expect(new Set(created.items.map((i) => i.material_id)).size).toBe(8)
  })

  it('produces one card per material, not N cards fighting over slot-1', async () => {
    const created = await runBatch()
    const events = await collect(created.batch_id)

    // One `hello`, from the single merged batch_started, carrying the BATCH total.
    const hellos = events.filter((e) => e.event === 'hello')
    expect(hellos).toHaveLength(1)
    expect(hellos[0]).toMatchObject({ total: 8 })

    // One terminal event per material, and eight distinct ids.
    const materials = events.filter((e) => e.event === 'material')
    const failures = events.filter((e) => e.event === 'material_failed')
    expect(materials).toHaveLength(7)
    expect(failures).toHaveLength(1)
    const ids = [...materials, ...failures].map((e) => (e as { material_id: string }).material_id)
    expect(new Set(ids).size).toBe(8)

    // Exactly one batch_done, and it is last.
    const done = events.filter((e) => e.event === 'batch_done')
    expect(done).toHaveLength(1)
    expect(events[events.length - 1]!.event).toBe('batch_done')
    expect(done[0]).toMatchObject({ status: 'partial', completed: 7, failed: 1 })
  })

  it('reports every material as progressing, not just the first', async () => {
    // The collision symptom, stated as a property: if all eight children were merged onto one card,
    // seven of them would never emit a progress event of their own.
    const created = await runBatch()
    const events = await collect(created.batch_id)
    const progressed = new Set(
      events
        .filter((e) => e.event === 'progress')
        .map((e) => (e as { material_id: string }).material_id),
    )
    expect(progressed.size).toBe(8)
  })

  it('groups the per-scenario index correctly across children', async () => {
    // Two materials for the same scenario must be index 0 and 1 of that scenario — that is what
    // makes the compare view possible. Slot order is the web tier's plan order, so this is really a
    // test that `plan_children`'s ordering and `createBatch`'s planning agree.
    const created = await runBatch()
    await collect(created.batch_id)
    const snapshot = await api.getBatch(created.batch_id)
    const byScenario = new Map<string, number[]>()
    for (const item of snapshot.items) {
      byScenario.set(item.scenario_key, [...(byScenario.get(item.scenario_key) ?? []), item.index])
    }
    for (const [scenario, indexes] of byScenario) {
      expect(indexes.sort(), scenario).toEqual([0, 1])
    }
  })

  describe('since_seq replay', () => {
    it('assigns a gapless sequence over the whole merged stream', async () => {
      // The sequence space is owned by whoever owns the merged stream. With eight children it must
      // still be one monotonic run, or a reconnecting client would either re-apply events or skip
      // them — and skipping loses a material permanently, since there is no second delivery.
      const created = await runBatch()
      const events = await collect(created.batch_id)
      const seqs = events.map((e) => e.seq)
      expect(seqs).toEqual(seqs.map((_, i) => i + 1))
    })

    it('replays only what a reconnecting client has not seen', async () => {
      const created = await runBatch()
      const all = await collect(created.batch_id)
      const cut = Math.floor(all.length / 2)

      const replayed = await collect(created.batch_id, all[cut - 1]!.seq)
      expect(replayed.map((e) => e.seq)).toEqual(all.slice(cut).map((e) => e.seq))
      // And the replayed events are the same events, not regenerated ones.
      expect(replayed.map((e) => e.event)).toEqual(all.slice(cut).map((e) => e.event))
    })

    it('replays nothing when the client is already current', async () => {
      const created = await runBatch()
      const all = await collect(created.batch_id)
      expect(await collect(created.batch_id, all[all.length - 1]!.seq)).toEqual([])
    })

    it('serves a snapshot whose seq_high matches the stream', async () => {
      // `reconcile()` fetches this snapshot before resuming, so a seq_high that disagreed with the
      // stream's numbering would make the reconnect either replay or skip.
      const created = await runBatch()
      const all = await collect(created.batch_id)
      const snapshot = await api.getBatch(created.batch_id)
      expect(snapshot.seq_high).toBe(all[all.length - 1]!.seq)
      expect(snapshot.total).toBe(8)
      expect(snapshot.completed).toBe(7)
      expect(snapshot.failed).toBe(1)
    })
  })
})
