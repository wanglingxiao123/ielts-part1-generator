/**
 * 没有题目时页面说什么。
 *
 * 这一组要区分的是五种被「暂无题目」一句话盖住的处境。它们对读者的意思完全不同——一种是等着就行，
 * 一种是等这个页面毫无用处，一种是要有人去看日志——所以断言钉的是「这几句话彼此不同」，而不是某个
 * 具体措辞：措辞会调，把 checkpoint 说成「还在生成」不会。
 */
import { describe, expect, it } from 'vitest'
import type { MaterialQuestionsResponse, MaterialQuestionSlot, RequestStatus } from '@/contracts/api'
import { QUESTION_PACKAGE } from '@/mocks/fixtures'
import { explainMissingQuestions } from './questionStatus'

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

function res(
  over: Partial<MaterialQuestionsResponse> = {},
): MaterialQuestionsResponse {
  return { material_id: 'm1', questions: null, slot: null, request_status: null, ...over }
}

describe('explainMissingQuestions', () => {
  it('有题时返回 null——这个函数不该被拿去问一个已经有题的包', () => {
    expect(explainMissingQuestions(res({ questions: QUESTION_PACKAGE }))).toBeNull()
    expect(explainMissingQuestions(null)).toBeNull()
  })

  it('读不到 slot（没带 batch_id）时只给最弱的那句话，不猜原因', () => {
    const note = explainMissingQuestions(res())!
    expect(note.tone).toBe('neutral')
    expect(note.headline).toBe('暂无题目')
    expect(note.willResolveItself).toBe(false)
  })

  it('还在推进时说「正在生成」，并让页面继续等', () => {
    const note = explainMissingQuestions(
      res({ slot: slot(), request_status: 'running' }),
    )!
    expect(note.tone).toBe('info')
    expect(note.willResolveItself).toBe(true)
    // `state` 具体到哪一步：「出题中」和「材料刚做完还没进出题」不是一件事。
    expect(note.detail).toContain('正在生成、审核与修订题目')
    const earlier = explainMissingQuestions(
      res({ slot: slot({ state: 'material_pending' }), request_status: 'running' }),
    )!
    expect(earlier.detail).toContain('材料还在生成')
    const between = explainMissingQuestions(
      res({ slot: slot({ state: 'material_done' }), request_status: 'running' }),
    )!
    expect(between.detail).toContain('出题即将开始')
  })

  it('停在 checkpoint 时说清「材料已存住、下一次运行接着做」，并且不让页面白等', () => {
    const note = explainMissingQuestions(
      res({
        slot: slot({ checkpointed: true, last_failure: { stage: 'questions', reason: 'time_budget' } }),
        request_status: 'incomplete',
      }),
    )!
    expect(note.tone).toBe('warn')
    expect(note.willResolveItself).toBe(false)
    // 这是最容易被误读成「卡住了、要重跑」的一种，所以「不会重新生成材料」必须说出来。
    expect(note.detail).toContain('不会重新生成材料')
    expect(note.detail).toContain('这一次运行的时间用完了')
  })

  it('系统故障优先于别的判断：它与材料质量无关', () => {
    // 同时 checkpoint + system_fault 时，说的必须是故障——反过来会把一次后端异常说成「等下一轮」。
    const note = explainMissingQuestions(
      res({
        slot: slot({ system_fault: true, checkpointed: true, resumable: false }),
        request_status: 'system_failure',
      }),
    )!
    expect(note.tone).toBe('bad')
    expect(note.detail).toContain('不是材料质量问题')
    expect(note.willResolveItself).toBe(false)
    // 只有请求状态报故障（slot 自己没标）时也一样。
    expect(
      explainMissingQuestions(res({ slot: slot(), request_status: 'system_failure' }))!.tone,
    ).toBe('bad')
  })

  it('名额用尽时说到「换过的材料都没能出成题」，需要人工介入', () => {
    const note = explainMissingQuestions(
      res({
        slot: slot({
          state: 'exhausted',
          resumable: false,
          last_failure: { stage: 'questions', reason: 'questions_not_deliverable' },
        }),
        request_status: 'incomplete',
      }),
    )!
    expect(note.tone).toBe('bad')
    expect(note.detail).toContain('出题反复修订后仍不达交付标准')
    expect(note.willResolveItself).toBe(false)
  })

  it('请求已经结束而这一位仍然没题：不会再自己动了', () => {
    for (const status of ['incomplete', 'succeeded'] as RequestStatus[]) {
      const note = explainMissingQuestions(res({ slot: slot(), request_status: status }))!
      expect(note.tone, status).toBe('warn')
      expect(note.willResolveItself, status).toBe(false)
      expect(note.detail, status).toContain('再跑一次')
    }
  })

  it('五种处境的说法互不相同——否则区分它们的全部工作都白做了', () => {
    const notes = [
      explainMissingQuestions(res())!,
      explainMissingQuestions(res({ slot: slot(), request_status: 'running' }))!,
      explainMissingQuestions(res({ slot: slot({ checkpointed: true }) }))!,
      explainMissingQuestions(res({ slot: slot({ state: 'exhausted', resumable: false }) }))!,
      explainMissingQuestions(res({ slot: slot({ system_fault: true }) }))!,
    ]
    expect(new Set(notes.map((n) => n.headline)).size).toBe(5)
  })

  it('认不出的失败原因照原样显示，不悄悄丢掉', () => {
    const note = explainMissingQuestions(
      res({
        slot: slot({
          checkpointed: true,
          last_failure: { stage: 'questions', reason: 'brand_new_reason_nobody_mapped' },
        }),
      }),
    )!
    // 翻不动的原因仍然是原因。丢掉它会让这一条看起来没有原因。
    expect(note.detail).toContain('brand_new_reason_nobody_mapped')
  })

  it('没有 last_failure 时不硬凑一句「原因：」', () => {
    const note = explainMissingQuestions(res({ slot: slot({ checkpointed: true }) }))!
    expect(note.detail).not.toContain('原因：')
  })
})
