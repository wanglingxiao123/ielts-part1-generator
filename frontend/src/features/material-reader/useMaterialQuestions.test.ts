/**
 * 轮询条件。
 *
 * 这一条单独测，因为它是这个页签最容易写错、而且错了看不出来的地方：把「现在没有题」当成继续问的
 * 理由，页面就会对一个被时钟停在断点的 slot 每 15 秒问一次，问到有人关掉标签页——题目要等**下一次**
 * invocation，可能是几十分钟以后。所以判据必须是「后端说这件事还会变」。
 */
import { describe, expect, it } from 'vitest'
import type { MaterialQuestionsResponse, MaterialQuestionSlot } from '@/contracts/api'
import { QUESTION_PACKAGE } from '@/mocks/fixtures'
import { isInFlight } from './useMaterialQuestions'

function slot(over: Partial<MaterialQuestionSlot> = {}): MaterialQuestionSlot {
  return {
    slot_id: 'slot-1',
    scenario: 'accommodation-rental',
    state: 'questions_pending',
    material_id: 'm1',
    created_at: 1_770_000_000,
    resumable: true,
    checkpointed: false,
    system_fault: false,
    last_failure: null,
    attempts: { questions: 1 },
    ...over,
  }
}

const res = (over: Partial<MaterialQuestionsResponse> = {}): MaterialQuestionsResponse => ({
  material_id: 'm1',
  questions: null,
  slot: null,
  request_status: null,
  ...over,
})

describe('isInFlight', () => {
  it('三个推进中的 state 都算还在路上', () => {
    for (const state of ['material_pending', 'material_done', 'questions_pending'] as const) {
      expect(isInFlight(res({ slot: slot({ state }), request_status: 'running' })), state).toBe(true)
    }
  })

  it('已经有题就停：交付过的包不会变', () => {
    expect(isInFlight(res({ questions: QUESTION_PACKAGE, slot: slot() }))).toBe(false)
  })

  it('checkpoint 停：这一次已经停了，等的是下一次 invocation', () => {
    expect(isInFlight(res({ slot: slot({ checkpointed: true }) }))).toBe(false)
  })

  it('系统故障停、不可续做停', () => {
    expect(isInFlight(res({ slot: slot({ system_fault: true }) }))).toBe(false)
    expect(isInFlight(res({ slot: slot({ resumable: false }) }))).toBe(false)
  })

  it('请求已经结束就停，哪怕 slot 的 state 看起来还在推进', () => {
    // 这一条是关键：`state` 是最后写下的那一笔，请求结束之后不会再变。只看 state 会永久轮询。
    for (const status of ['succeeded', 'incomplete', 'system_failure'] as const) {
      expect(isInFlight(res({ slot: slot(), request_status: status })), status).toBe(false)
    }
  })

  it('读不到 slot 时不轮询：没有依据说它还会变', () => {
    expect(isInFlight(res())).toBe(false)
    expect(isInFlight(null)).toBe(false)
  })

  it('终态 state 停', () => {
    for (const state of ['complete', 'exhausted'] as const) {
      expect(isInFlight(res({ slot: slot({ state }) })), state).toBe(false)
    }
  })
})
