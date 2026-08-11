import type { QuestionPackage } from './index'

export type QuestionVersionStatus = 'original' | 'ready'

export interface QuestionPackageVersion {
  id: string
  created_at: string
  based_on_version_id: string | null
  source_comment_ids: string[]
  status: QuestionVersionStatus
  package: QuestionPackage
  is_active: boolean
  /** Display-only V-number assigned by the server after sorting immutable versions. */
  ordinal: number
}

export interface MaterialQuestionVersionsResponse {
  material_id: string
  active_version_id: string | null
  versions: QuestionPackageVersion[]
  running_request?: {
    request_id: string
    status: 'running'
    base_version_id: string
    created_at: string
  } | null
}

export interface CreateQuestionRevisionRequest {
  base_version_id: string
  comment_ids?: string[]
}

export interface AdoptQuestionVersionResponse {
  material_id: string
  active_version_id: string | null
}

export type QuestionRevisionStage =
  | 'queued'
  | 'analysing'
  | 'revising'
  | 'validating'
  | 'auditing'

export interface QuestionRevisionProgressEvent {
  event: 'progress'
  request_id: string
  stage: QuestionRevisionStage
  message?: string
}

export interface QuestionRevisionRevisedEvent {
  event: 'revised'
  request_id: string
  version_id: string
}

export interface MaterialRevisionReason {
  comment_id: string
  question_number: number
  reason: string
}

export interface QuestionRevisionNeedsMaterialEvent {
  event: 'needs_material_revision'
  request_id: string
  reasons: MaterialRevisionReason[]
}

export interface QuestionRevisionFailedEvent {
  event: 'failed'
  request_id?: string
  message: string
}

export type QuestionRevisionEvent =
  | QuestionRevisionProgressEvent
  | QuestionRevisionRevisedEvent
  | QuestionRevisionNeedsMaterialEvent
  | QuestionRevisionFailedEvent

export type QuestionRevisionTerminalEvent =
  | QuestionRevisionRevisedEvent
  | QuestionRevisionNeedsMaterialEvent
  | QuestionRevisionFailedEvent
