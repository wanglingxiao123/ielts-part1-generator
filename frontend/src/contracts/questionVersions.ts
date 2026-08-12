import type { Blueprint, QuestionPackage } from './index'

export type QuestionVersionStatus = 'original' | 'ready'

export interface QuestionPackageVersion {
  id: string
  created_at: string
  based_on_version_id: string | null
  source_comment_ids: string[]
  status: QuestionVersionStatus
  package: QuestionPackage
  /** Present on replanned versions; old versions fall back to the material record blueprint. */
  blueprint?: Blueprint
  is_active: boolean
  /** Display-only V-number assigned by the server after sorting immutable versions. */
  ordinal: number
  field_changes?: QuestionVersionFieldChange[]
}

export interface QuestionVersionFieldChange {
  question_number: number
  section: 'question' | 'answer_key' | 'evidence' | 'group' | 'instruction'
  field: string
  before: unknown
  after: unknown
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
  revision_request?: QuestionRevisionRecord | null
}

export interface QuestionRevisionRecord {
  request_id: string
  status:
    | 'running'
    | 'completed'
    | 'no_change'
    | 'replan_questions'
    | 'needs_material_revision'
    | 'failed'
  stage?: QuestionRevisionStage
  operation?: 'revise_questions' | 'replan_questions'
  source_request_id?: string
  base_version_id: string
  comment_count?: number
  created_at?: string
  completed_at?: string
  version_id?: string
  message?: string
  reasons?: MaterialRevisionReason[]
  blockers?: string[]
  baseline_advisories?: string[]
}

export interface CreateQuestionRevisionRequest {
  base_version_id: string
  comment_ids?: string[]
}

export interface CreateQuestionReplanRequest {
  source_request_id: string
}

export interface AdoptQuestionVersionResponse {
  material_id: string
  active_version_id: string | null
}

export type QuestionRevisionStage =
  | 'queued'
  | 'analysing'
  | 'planning'
  | 'feasibility'
  | 'generating'
  | 'revising'
  | 'validating'
  | 'auditing'
  | 'storing'

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
  baseline_advisories?: string[]
}

export interface MaterialRevisionReason {
  comment_id: string
  question_number: number
  reason: string
  replan_scope?: 'layout_only' | 'retarget'
  references?: string[]
}

export interface QuestionRevisionNoChangeEvent {
  event: 'no_change'
  request_id: string
  reasons: MaterialRevisionReason[]
}

export interface QuestionRevisionNeedsReplanEvent {
  event: 'needs_replan'
  request_id: string
  reasons: MaterialRevisionReason[]
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
  | QuestionRevisionNoChangeEvent
  | QuestionRevisionNeedsReplanEvent
  | QuestionRevisionNeedsMaterialEvent
  | QuestionRevisionFailedEvent

export type QuestionRevisionTerminalEvent =
  | QuestionRevisionRevisedEvent
  | QuestionRevisionNoChangeEvent
  | QuestionRevisionNeedsReplanEvent
  | QuestionRevisionNeedsMaterialEvent
  | QuestionRevisionFailedEvent
