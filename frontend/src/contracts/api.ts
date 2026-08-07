/**
 * Backend contract — hard requirement on 07-28-agent-backend (design.md §8.1/§8.2).
 *
 * Hand-written on purpose: this file IS the proposal the sibling task must
 * confirm. `material` / `blueprint` / `audit` are inlined verbatim from the
 * generated schema types, so a rename on either side breaks compilation here.
 */
import type { Audit, Blueprint, Material, QuestionPackage, Verdict } from './index'
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

/* ── GET /api/batches — batch history (web/batch_history.py) ──────────────── */

/**
 * The two statuses the history panel filters on.
 *
 * Not `BatchStatus`. That one is about a run's outcome (queued/running/partial/done/failed) and is
 * per-request; this one is about what a reviewer can still do with a batch, which is a fact about
 * storage. A batch can perfectly well be `done` and `submitted` at once, so collapsing them into one
 * enum would force a choice between two things that are both true.
 *
 * `pending_selection` is the default and `submitted` is RECORDED — it is the transition the backend
 * did not have before this feature. There was a third, `archived`, derived from expired candidates;
 * it was dropped on the client's instruction. `normalizeStatus` in domain/batchHistory.ts still folds
 * it into `submitted`, because a browser can hold a cached response from before the change.
 */
export type BatchHistoryStatus = 'pending_selection' | 'submitted'

export interface BatchHistoryMaterialSummary {
  material_id: string
  scenario_key: string
  index?: number
  slot_id?: string
  verdict?: Verdict | ''
  degraded?: boolean
}

export interface BatchHistoryEntry {
  batch_id: string
  /** Unix seconds, not an ISO string: the backend stores what `time.time()` gave it. */
  created_at: number
  completed_at?: number | null
  status: BatchHistoryStatus
  /**
   * Whether a selection can still be made in this batch. Comes from the BACKEND rather than being
   * re-derived from `status`, and that is deliberate: a submitted batch is read-only because the
   * choice was made, while an unsubmitted batch whose candidates expired is read-only because the
   * choice can no longer be made. The second case has no status of its own, so `status ===
   * 'submitted'` would render it mutable and hand the user a control the backend will refuse.
   */
  read_only: boolean
  /** The web task died mid-batch. The materials listed did arrive; the missing ones never will. */
  interrupted: boolean
  state: 'running' | 'complete' | string
  requested_total: number
  arrived: number
  scenarios: Array<{ scenario_key: string; count: number }>
  counts?: Record<string, number>
  submitted_at?: number | null
  submitted_by?: string | null
  submitted_material_ids?: string[]
  materials: BatchHistoryMaterialSummary[]
}

export interface BatchHistoryResponse {
  batches: BatchHistoryEntry[]
  next_cursor?: string | null
}

/**
 * One batch with every recorded material's full artifacts. `GET /api/batch-history/{id}`.
 *
 * `verdict` is omitted from the summary half of the intersection: the summary allows `''` (the
 * recorder writes it when a material arrived with no audit verdict) while `MaterialRecord` does not,
 * and the artifacts are authoritative wherever both are present.
 */
export interface BatchHistoryDetail extends Omit<BatchHistoryEntry, 'materials'> {
  /** 自定义场景的用户原文；目录场景为空串。 */
  custom_label?: string
  materials: Array<
    Omit<BatchHistoryMaterialSummary, 'verdict'> &
      Partial<Pick<BatchHistoryMaterialSummary, 'verdict'>> &
      Partial<Omit<MaterialRecord, 'material_id' | 'verdict'>>
  >
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
  /**
   * Structural notes the deterministic validator still had about this exact script.
   *
   * Non-empty when three generation attempts all carried validator errors and the Loop delivered
   * the last one anyway — validation is a report, not a gate (backend/orchestration/loop.py).
   *
   * A separate field from `degraded`, which means "delivered without the full pipeline". This
   * material went through the whole pipeline; the validator merely still has notes. The two can
   * co-occur and need different copy, so one boolean cannot answer both.
   *
   * Rendered on the READER page only. The result card is scenario name + timeline + first line +
   * buttons; the client ruled evaluation prose off it, because a note has no meaning without the
   * script beside it.
   */
  validation_findings?: string[]
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

/* ── GET /api/material-questions/{id} ────────────────────────────────────── */

/**
 * 这套材料已交付的题目包，或者 null。
 *
 * `questions: null` 走 200，不走 404：出题在材料之后，一次 invocation 常常在材料做完之后就被时钟
 * 停住，所以「还没有题」是常态而不是错误。404 还有一个致命的含混——它分不清「没有这套材料」和
 * 「这套材料还没出题」，而页面对这两件事要说不同的话。
 *
 * 只有过了全部门槛的题目包才会被写进 `_questions/`（backend/orchestration/slot_store.py），所以
 * 这里拿到的包一定是可交付的；被判掉的那一版存在别的键下，端点不会把它当成题目送出来。
 */
export interface MaterialQuestionsResponse {
  material_id: string
  questions: QuestionPackage | null
  /**
   * 为什么没有题。只在 `questions` 为 null、且请求带上了 `batch_id` 时才有值。
   *
   * 「暂无题目」有好几种完全不同的处境——还在出题、被时钟停在半路（下一次 invocation 会接着做）、
   * 名额用尽、整个请求是系统故障——而页面对它们要说不同的话。刷新之后 SSE 流已经不在了，这两个
   * 字段是那个答案唯一还存在的地方（`web/slot_state.py` 的 `find_slot`）。
   */
  slot: MaterialQuestionSlot | null
  /** 请求文档自己写下的状态，不是前端推的：Runtime 已经判过了，这里不出第二个意见。 */
  request_status: RequestStatus | null
}

/**
 * 一个 slot 在请求文档里的样子（`backend/orchestration/delivery._slot_row` 的投影）。
 *
 * `resumable` / `checkpointed` / `system_fault` 都是后端记下来的判断，不是从 `state` 名字反推的。
 */
export interface MaterialQuestionSlot {
  slot_id: string
  scenario: string
  state: 'material_pending' | 'material_done' | 'questions_pending' | 'complete' | 'exhausted'
  material_id: string | null
  created_at: number
  resumable: boolean
  checkpointed: boolean
  system_fault: boolean
  last_failure: { stage: string; reason: string; detail?: unknown } | null
  attempts: Record<string, number>
  replaces?: string | null
  replaced_by?: string | null
}

/** `backend/orchestration/slot_store.py` 的四个请求状态。 */
export type RequestStatus = 'running' | 'succeeded' | 'incomplete' | 'system_failure'

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
  /**
   * 自定义场景的用户原文（`餐厅点餐`），目录场景为空。
   *
   * 必须走这条事件：生成过程中前端不请求历史接口（那是给已结束批次的），所以在这之前拿不到
   * 这段文本，标题只能退回材料自带的英文句——那是模型扩写的，不是用户输入的。
   */
  custom_label?: string
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
  /**
   * The SKELETON row this material takes over from — `<batchId>::<slot_id>`.
   *
   * A batch is planned before it runs, so every card exists as a row keyed on a placeholder
   * (`agentcore.ts`'s `Slot.placeholderId`) before any material arrives. A material then arrives
   * under the BACKEND's `material_id`, which is a different key — so without this field the store
   * gained a second row and the placeholder row stayed `pending` forever. `store.items` then held
   * 2N rows for an N-material batch, and 「有 N 套未能生成」 was rendered over a page where every
   * material had in fact arrived. That was the client's 「怎么又开始报有未生成的了」.
   *
   * Sent by the adapter, which is the only layer that knows both ids. Optional because a material
   * the plan never had a slot for (a scenario the backend expanded differently) legitimately
   * replaces nothing.
   */
  replaces?: string | null
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
  /** Same role as `SseMaterialEvent.replaces`: the skeleton row this failure resolves. */
  replaces?: string | null
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

  /**
   * 精确 N 套请求（`action: generate_sets`）才有的四个字段，普通批次上**缺席**而不是填零：
   * `generate` 本来就允许少交付，给它一个 `incomplete` 等于把正常结果报成缺口。
   *
   * `incomplete` + 非空 `resumable_slots` 是 checkpoint：这一次运行时间用完了，进度存在 S3 里，
   * 下一次运行接着做。它不是失败，页面不能画成失败。
   *
   * 类型里排掉 `running`：这是**终态**事件，而 `RequestStatus` 的 `running` 描述的是还在跑的请求。
   * 用完整的 `RequestStatus` 会让「批次已结束」和「请求仍在进行」在类型上同时成立。
   */
  request_status?: Exclude<RequestStatus, 'running'>
  requested?: number
  delivered?: number
  resumable_slots?: string[]
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
