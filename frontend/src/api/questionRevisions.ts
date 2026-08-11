import { getConfig } from '@/config/runtimeConfig'
import type {
  CreateQuestionRevisionRequest,
  MaterialRevisionReason,
  QuestionRevisionEvent,
  QuestionRevisionTerminalEvent,
} from '@/contracts/questionVersions'
import { ApiError, CREDENTIALS, notifyUnauthorized } from './http'

export function decodeRevisionFrame(frame: string): QuestionRevisionEvent | null {
  let eventName = ''
  const data: string[] = []
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith(':')) continue
    const colon = rawLine.indexOf(':')
    const field = colon < 0 ? rawLine : rawLine.slice(0, colon)
    const value = colon < 0 ? '' : rawLine.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') eventName = value
    if (field === 'data') data.push(value)
  }
  if (data.length === 0) return null
  try {
    const payload = JSON.parse(data.join('\n')) as Record<string, unknown>
    const wireType = typeof payload.type === 'string' ? payload.type : eventName
    const requestId = typeof payload.request_id === 'string' ? payload.request_id : ''
    if (wireType === 'question_revision_started') {
      return { event: 'progress', request_id: requestId, stage: 'analysing' }
    }
    if (wireType === 'question_revision_validating') {
      return { event: 'progress', request_id: requestId, stage: 'validating' }
    }
    if (wireType === 'question_revision_auditing') {
      return { event: 'progress', request_id: requestId, stage: 'auditing' }
    }
    if (wireType === 'question_revision_completed' && typeof payload.version_id === 'string') {
      return {
        event: 'revised',
        request_id: requestId,
        version_id: payload.version_id,
      }
    }
    if (wireType === 'question_revision_needs_material' && Array.isArray(payload.reasons)) {
      return {
        event: 'needs_material_revision',
        request_id: requestId,
        reasons: payload.reasons as MaterialRevisionReason[],
      }
    }
    if (wireType === 'question_revision_failed') {
      return {
        event: 'failed',
        request_id: requestId,
        message: typeof payload.message === 'string' ? payload.message : '题目修改没有完成',
      }
    }
    return null
  } catch {
    return null
  }
}

function isTerminal(event: QuestionRevisionEvent): event is QuestionRevisionTerminalEvent {
  return event.event !== 'progress'
}

export async function streamQuestionRevision(
  materialId: string,
  body: CreateQuestionRevisionRequest,
  onEvent: (event: QuestionRevisionEvent) => void,
  signal?: AbortSignal,
): Promise<QuestionRevisionTerminalEvent> {
  const response = await fetch(
    `${getConfig().apiBaseUrl}/material-question-revisions/${encodeURIComponent(materialId)}`,
    {
      method: 'POST',
      credentials: CREDENTIALS,
      headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    },
  )
  if (response.status === 401) {
    notifyUnauthorized()
    throw new ApiError(401, 'UNAUTHENTICATED', '登录状态已失效，请重新登录')
  }
  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as
      | { error?: { code?: string; message?: string } }
      | null
    throw new ApiError(
      response.status,
      error?.error?.code ?? 'REVISION_FAILED',
      error?.error?.message ?? '题目修改请求没有成功开始',
    )
  }
  if (!response.body) {
    throw new ApiError(502, 'REVISION_STREAM_MISSING', '题目修改请求没有返回进度')
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    for (;;) {
      const split = /\r?\n\r?\n/.exec(buffer)
      if (!split || split.index === undefined) break
      const frame = buffer.slice(0, split.index)
      buffer = buffer.slice(split.index + split[0].length)
      const event = decodeRevisionFrame(frame)
      if (!event) continue
      onEvent(event)
      if (isTerminal(event)) return event
    }
  }
  throw new ApiError(502, 'REVISION_STREAM_ENDED', '题目修改连接提前结束，请稍后重试')
}
