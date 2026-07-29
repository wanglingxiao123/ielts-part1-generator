/**
 * Backend contract — hard requirement on 07-28-agent-backend (design.md §8.1/§8.2).
 *
 * Hand-written on purpose: this file IS the proposal the sibling task must
 * confirm. `material` / `blueprint` / `audit` are inlined verbatim from the
 * generated schema types, so a rename on either side breaks compilation here.
 */
import type { Audit, Blueprint, Material, Verdict } from './index'
import type { AudioManifest } from './manifest'

/* ── shared envelope ─────────────────────────────────────────────────────── */

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    detail?: Record<string, unknown>
  }
}

export type BatchStatus = 'queued' | 'running' | 'partial' | 'done' | 'failed'

/** Backend `progress.stage`. Rendered as 排队/生成/校验/评价/修改/复评 (prd R3). */
export type MaterialStage =
  | 'queued'
  | 'generating'
  | 'validating'
  | 'auditing'
  | 'revising'
  | 're_auditing'

export type MaterialItemStatus = 'pending' | 'running' | 'done' | 'failed'

/**
 * cross_check.py output. Frontend cannot compute it (needs both maps).
 *
 * §8.1 originally proposed `unrecoverable: number[]` / `unintended_target:
 * number[]`. The real script (skills/.../shared/cross_check.py) emits arrays of
 * OBJECTS carrying the reason, evidence and turn_index — strictly more
 * information. Verified against a live batch. The adapter normalises to these
 * row shapes rather than flattening to numbers, because the reason text is what
 * lets a reviewer act on an unrecoverable point instead of merely counting it.
 */
export interface UnrecoverableRow {
  number: number
  type?: string
  target?: string
  turn_index: number
  evidence?: string
  reason?: string
}

export interface UnintendedRow {
  audit_seq: number
  type?: string
  turn_index: number
  evidence?: string
  reason?: string
}

export interface AmbiguousRow {
  number: number
  turn_index: number
  audit_clarity?: string
  reason?: string
}

export interface CrossCheck {
  /** Blueprint points the blind auditor could not recover. Decision signal #1. */
  unrecoverable: UnrecoverableRow[]
  /** Recordable details the auditor found that the blueprint did not plan. */
  unintended_target: UnintendedRow[]
  matched: number
  /** Recoverable but flagged ambiguous by the auditor. Real backend field. */
  ambiguous?: AmbiguousRow[]
  planned?: number
  observed?: number
  ok?: boolean
}

/* ── POST /api/batches ───────────────────────────────────────────────────── */

export interface CreateBatchRequest {
  requests: Array<{
    scenario_key: string
    /** Free text for `scenario_key: 'custom'`. */
    scenario_text?: string
    count: number
  }>
  options: { narration_mode: Blueprint['narration_mode'] }
}

export interface CreateBatchResponse {
  batch_id: string
  total: number
  estimated_seconds: [number, number]
  items: Array<{ material_id: string; scenario_key: string; index: number }>
}

/* ── GET /api/batches/{id} ───────────────────────────────────────────────── */

export interface BatchItemSnapshot {
  material_id: string
  scenario_key: string
  index: number
  status: MaterialItemStatus
  stage: MaterialStage
  attempt: number
  verdict?: Verdict
  error?: string | null
}

export interface BatchSnapshot {
  batch_id: string
  status: BatchStatus
  created_at: string
  elapsed_ms: number
  total: number
  completed: number
  failed: number
  /**
   * Materials the audit rejected. A COUNT ONLY — these materials are delivered
   * and selectable like any other; see `MaterialRecord.audit_rejection`.
   */
  audit_rejected: number
  seq_high: number
  items: BatchItemSnapshot[]
}

export interface BatchListResponse {
  batches: Array<Pick<BatchSnapshot, 'batch_id' | 'status' | 'created_at' | 'total' | 'completed'>>
  next_cursor?: string | null
}

/* ── GET /api/materials/{id} ─────────────────────────────────────────────── */

export interface MaterialRecord {
  material_id: string
  batch_id: string
  scenario_key: string
  index: number
  status: MaterialItemStatus
  verdict: Verdict
  /**
   * Set when the audit rejected this material (FAIL / NOT_ASSESSABLE).
   *
   * It does NOT gate anything in the UI: the material is shown, readable and
   * selectable. This is a shortcoming to state, and the only place `verdict` is
   * allowed to influence user-visible copy — never as a status badge.
   */
  audit_rejection?: { code: string; message: string } | null
  /** Skipped revise+re-audit; must be surfaced, not silently treated as complete. */
  degraded?: boolean
  material: Material
  blueprint: Blueprint
  audit: Audit
  cross_check: CrossCheck
  created_at: string
}

export interface MaterialListResponse {
  materials: MaterialRecord[]
  next_cursor?: string | null
}

/* ── POST /api/materials/{id}/select ─────────────────────────────────────── */

export interface SelectMaterialResponse {
  material_id: string
  audio_job_id: string
  siblings_discarded: string[]
}

/* ── POST /api/materials/{id}/audio ──────────────────────────────────────── */

/**
 * 生成音频（试听），不等于选定。
 *
 * 后端的 `preview_audio` action：只给这一套合成语音，不认领候选组、不丢弃同场景的另一套。所以
 * 它和 `select` 是两个端点而不是一个带开关的端点——一个只想先听听的人，不该因此失去备选。
 * 合成结果与 `select` 共用同一份 clip，之后选定这一套不会再次计费。
 *
 * 幂等：重复 POST 返回同一个 job（`repeat: true`），不会第二次调用 Polly。
 */
export interface PreviewAudioResponse {
  material_id: string
  audio_job_id: string
  /** true = 这套音频已经在合成或已合成好，本次没有新开任务。 */
  repeat: boolean
}

/* ── GET /api/materials/{id}/audio ───────────────────────────────────────── */

export type AudioJobStatus = 'not_requested' | 'queued' | 'synthesizing' | 'ready' | 'failed'

export interface AudioStatusResponse {
  status: AudioJobStatus
  progress: { done: number; total: number }
  error?: string | null
  manifest?: AudioManifest
}

/* ── SSE: GET /api/batches/{id}/stream?since_seq=N ───────────────────────── */

export interface SseHelloEvent {
  event: 'hello'
  seq: number
  batch_id: string
  total: number
  server_time: string
  resumed_from: number
}

export interface SseProgressEvent {
  event: 'progress'
  seq: number
  material_id: string
  stage: MaterialStage
  attempt: number
  /**
   * The backend's own stage name, verbatim, for stages §8 did not model
   * (`regenerating`, `anchors_repaired`, `infra_retry`, `audited`, `refilling`,
   * `refill_abandoned`). Carried because folding it into one of the six §8
   * stages loses information the progress mapping needs — `regenerating` and
   * `generating` are the same §8 stage but not the same event.
   *
   * A MACHINE TOKEN. It must never reach the DOM: these names describe the
   * system retrying itself, and a user told "校验未过，重新生成" is being handed
   * an internal failure they can neither act on nor should see.
   * `domain/progressStages.ts` is the only permitted consumer.
   */
  raw_stage?: string | null
}

export interface SseMaterialEvent {
  event: 'material'
  seq: number
  material_id: string
  scenario_key: string
  index: number
  verdict: Verdict
  audit_rejection?: { code: string; message: string } | null
  degraded?: boolean
  material: Material
  blueprint: Blueprint
  audit: Audit
  cross_check: CrossCheck
}

export interface SseMaterialFailedEvent {
  event: 'material_failed'
  seq: number
  material_id: string
  code: string
  message: string
  attempts: number
}

export interface SseBatchDoneEvent {
  event: 'batch_done'
  seq: number
  status: 'done' | 'partial'
  completed: number
  failed: number
  audit_rejected: number
}

export interface SsePingEvent {
  event: 'ping'
  seq: number
}

export type SseEvent =
  | SseHelloEvent
  | SseProgressEvent
  | SseMaterialEvent
  | SseMaterialFailedEvent
  | SseBatchDoneEvent
  | SsePingEvent
