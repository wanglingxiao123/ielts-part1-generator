import { describe, expect, it } from 'vitest'
import { decodeRevisionFrame } from './questionRevisions'

describe('question revision SSE wire adapter', () => {
  it('normalises Runtime data-only progress frames', () => {
    expect(
      decodeRevisionFrame(
        'data: {"type":"question_revision_validating","request_id":"request-1"}',
      ),
    ).toEqual({
      event: 'progress',
      request_id: 'request-1',
      stage: 'validating',
    })
  })

  it('normalises a completed Runtime frame to the frontend terminal event', () => {
    expect(
      decodeRevisionFrame(
        'data: {"type":"question_revision_completed","request_id":"request-1","version_id":"version-2"}',
      ),
    ).toEqual({
      event: 'revised',
      request_id: 'request-1',
      version_id: 'version-2',
    })
  })

  it('preserves material-revision reasons', () => {
    expect(
      decodeRevisionFrame(
        'data: {"type":"question_revision_needs_material","request_id":"request-1","reasons":[{"comment_id":"c1","question_number":3,"reason":"录音中答案不唯一"}]}',
      ),
    ).toEqual({
      event: 'needs_material_revision',
      request_id: 'request-1',
      reasons: [{ comment_id: 'c1', question_number: 3, reason: '录音中答案不唯一' }],
    })
  })
})
