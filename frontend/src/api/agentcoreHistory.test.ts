/**
 * The adapter's two seams with batch history.
 *
 * Both exist because the adapter's own store — a module-scope `Map` of page-session batches — cannot
 * answer a question about a batch it never saw, and after this feature a reviewer can ask exactly
 * that. Both are transport-level routing decisions, so they are asserted by driving the transport
 * and observing which URL it reaches for.
 *
 * The second one was found in a real browser, not here: 阅读全文 on an archived batch rendered
 * 「材料不存在」, because `GET /materials/{id}` only ever consulted the session cache. The client's
 * rule for a read-only batch is 「可看材料、可试听」 and both begin on the reader page, so a history
 * fallback is not a nicety.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installAgentCoreAdapter, resetAgentCore } from './agentcore'
import type { Transport } from './http'

/** Every URL the adapter fetched, in order. */
let fetched: string[] = []
/** url -> [status, body]. A miss is a 404, which is what an unknown material really is. */
let responses: Record<string, [number, unknown]> = {}

let transport: Transport

beforeEach(() => {
  fetched = []
  responses = {}
  resetAgentCore()
  vi.stubGlobal('fetch', (url: string) => {
    fetched.push(url)
    const hit = responses[new URL(url, 'http://localhost').pathname]
    const [status, body] = hit ?? [404, { error: { code: 'NOT_FOUND', message: 'no' } }]
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
  transport = installAgentCoreAdapter().transport
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetAgentCore()
})

describe('batch history is passed through, not translated', () => {
  it('GET /batch-history goes to the web tier as plain REST', async () => {
    responses['/api/batch-history'] = [200, { batches: [], next_cursor: null }]
    await transport({ method: 'GET', path: '/batch-history' })
    // The Runtime is invoked once per material and has never heard of a batch, so routing this
    // through /invocations would mean asking it a question it cannot answer.
    expect(fetched).toEqual(['/api/batch-history'])
    expect(fetched.some((u) => u.includes('/invocations'))).toBe(false)
  })

  it('GET /batch-history/{id} likewise', async () => {
    responses['/api/batch-history/web-1-1'] = [200, { batch_id: 'web-1-1', materials: [] }]
    const body = await transport({ method: 'GET', path: '/batch-history/web-1-1' })
    expect(fetched).toEqual(['/api/batch-history/web-1-1'])
    expect(body).toMatchObject({ batch_id: 'web-1-1' })
  })

  it('POST .../submit likewise', async () => {
    responses['/api/batch-history/web-1-1/submit'] = [200, { status: 'submitted' }]
    await transport({
      method: 'POST',
      path: '/batch-history/web-1-1/submit',
      body: { material_ids: ['m1'] },
    })
    expect(fetched).toEqual(['/api/batch-history/web-1-1/submit'])
  })
})

describe('a material not in this page session falls back to history', () => {
  const ID = '20260101-booking-hotel-aaaabbbb'

  it('resolves a historical material through the history route', async () => {
    responses[`/api/batch-history-material/${ID}`] = [
      200,
      { material_id: ID, scenario_key: 'booking-hotel', material: { x: 1 } },
    ]
    const body = await transport({ method: 'GET', path: `/materials/${ID}` })
    expect(fetched).toEqual([`/api/batch-history-material/${ID}`])
    expect(body).toMatchObject({ material_id: ID, scenario_key: 'booking-hotel' })
  })

  it('a material in neither place is a 404 that says so', async () => {
    // Both halves named, because "not in this session" and "not in history" send the reader to very
    // different next steps.
    await expect(transport({ method: 'GET', path: `/materials/${ID}` })).rejects.toMatchObject({
      status: 404,
      code: 'MATERIAL_NOT_FOUND',
    })
    const message = await transport({ method: 'GET', path: `/materials/${ID}` }).catch(
      (e: Error) => e.message,
    )
    expect(message).toContain('本页会话')
    expect(message).toContain('历史记录')
  })
})

describe('补生成 after a reload', () => {
  /**
   * 补生成 used to resolve each pending slot's scenario through the adapter's in-memory `sessions`
   * map. That map is empty after a reload and for any historical batch, so `keys.length === 0` and
   * the call threw 「没有可补生成的场景」 — in exactly the situations a user reaches for it. The page
   * always knows the scenario, so the caller now sends `scenario_keys`.
   *
   * Asserted by its observable consequence: with keys supplied and NO page session, the adapter
   * must get past the resolution step and start a batch (it then needs an SSE stream, which this
   * transport-level harness does not serve — reaching that point is the proof). With no keys it
   * must still refuse, so the guard is not simply gone.
   */
  it('gets past scenario resolution with no page session', async () => {
    const failure = await transport({
      method: 'POST',
      path: '/batches/web-1785395193789-2/retry',
      body: { scenario_keys: ['booking-hotel', 'daily-driving-lessons'] },
    }).catch((e: { code?: string }) => e)
    expect((failure as { code?: string }).code).not.toBe('RETRY_EMPTY')
  })

  it('still refuses when there is nothing to regenerate', async () => {
    await expect(
      transport({
        method: 'POST',
        path: '/batches/web-1785395193789-2/retry',
        body: { scenario_keys: [] },
      }),
    ).rejects.toMatchObject({ code: 'RETRY_EMPTY' })
  })
})

/**
 * 试听历史批次的材料。
 *
 * 用户报的：历史记录里的材料点「生成音频」，报「材料不存在（本页会话内未见此材料）」。
 *
 * 根因和上面那条 `GET /materials/{id}` 是同一个：适配层的 `slots` 只装**本页会话生成的**批次，
 * 历史批次按定义不在里面。`previewAudio` 先查它、查不到就本地抛 404——请求根本没发出去。而后端
 * `preview_audio` 的第一步是 `registry.get(material_id)`，按 id 直接查候选注册表，跟前端会话无关，
 * `store.load` 也不套 TTL（只有 `list_candidates` 套）。这个请求后端本来处理得了。
 *
 * 客户对只读批次的要求是「可看材料、可试听」，所以这不是可选项。
 */
describe('试听不依赖本页会话', () => {
  const HISTORICAL = '20260730-booking-hotel-f7155004'

  it('历史材料的 preview_audio 真的发到后端，而不是本地拒掉', async () => {
    responses['/api/invocations'] = [
      200,
      { material_id: HISTORICAL, audio_job_id: 'job-1', status: 'queued', repeat: false },
    ]
    const body = await transport({
      method: 'POST',
      path: `/materials/${HISTORICAL}/audio`,
      body: {},
    })
    // 这一行是缺陷本身：以前 fetched 是空的——本地 404，一个字节都没发出去。
    expect(fetched).toEqual(['/api/invocations'])
    expect(body).toMatchObject({ material_id: HISTORICAL })
  })

  it('材料真的不存在时，用的是后端的说法', async () => {
    // 后端 `UnknownMaterial` 分得清三种情况（从未提供 / 已丢弃 / 提供已过期），比前端那句
    // 「本页会话内未见此材料」准——那句话描述的是前端自己的记忆，不是材料的状态。
    responses['/api/invocations'] = [
      404,
      { error: { code: 'MATERIAL_NOT_FOUND', message: "no candidate 'x'; it was never offered" } },
    ]
    const failure = await transport({
      method: 'POST',
      path: '/materials/does-not-exist/audio',
      body: {},
    }).catch((e: { message?: string }) => e)
    expect(fetched).toEqual(['/api/invocations'])
    expect((failure as { message?: string }).message).not.toContain('本页会话')
  })

  it('轮询音频状态也不依赖本页会话', async () => {
    responses['/api/invocations'] = [
      200,
      { material_id: HISTORICAL, status: 'not_requested', progress: { done: 0, total: 0 } },
    ]
    const body = await transport({ method: 'GET', path: `/materials/${HISTORICAL}/audio` })
    expect(fetched).toEqual(['/api/invocations'])
    expect(body).toMatchObject({ status: 'not_requested' })
  })
})
