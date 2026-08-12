/**
 * AgentCore protocol adapter: the real backend's shape → design.md §8 contract.
 *
 * §8 proposed a REST surface (POST /api/batches, GET /api/batches/{id},
 * GET /api/batches/{id}/stream, GET /api/materials/{id}, …). The backend that
 * actually exists is `07-28-agent-backend`'s AgentCore Runtime entrypoint, which
 * is ONE endpoint:
 *
 *   POST /invocations  {"action":"list_scenarios"}                 → JSON
 *   POST /invocations  {"action":"generate","scenarios":[id,…],…}  → SSE
 *
 * The gap is structural, not cosmetic, and this module is where it is absorbed:
 *
 * | §8 assumed                        | reality                                  |
 * |-----------------------------------|------------------------------------------|
 * | batch is a server-side job        | batch lives for the duration of the POST |
 * | `id: <seq>` per event, replayable  | `data:`-only frames, no seq, no replay   |
 * | `event: material` names the type  | type is a field INSIDE data              |
 * | GET /batches/{id} snapshot        | no such route                            |
 * | GET /materials/{id}               | no such route                            |
 *
 * Two decisions follow, and both are deliberate rather than convenient:
 *
 * 1. **The backend's `material_id` is authoritative.** This used to read "ids are
 *    minted client-side as `<batchId>::<slot_id>`, the backend has no material
 *    identity that outlives the request, so there is nothing to adopt". That was
 *    true when it was written and has not been for some time: `_run_slot` now
 *    mints a real id through `new_material_id` (`YYYYMMDD-<scenario>-<hash8>`),
 *    registers a candidate under it in shared storage, and puts it in
 *    `material_completed`. The synthetic id kept being sent anyway, so every
 *    call that resolves a candidate server-side — `preview_audio`, `select`,
 *    `audio_status`, `presign_audio` — was handed a key the registry had never
 *    seen and answered:
 *
 *      no candidate 'batch-ms61jp3r-1::slot-2'; it was never offered, was
 *      discarded, or the offer expired
 *
 *    Clicking 试听 on a material the page had just rendered therefore failed
 *    100% of the time. The rule that closes it is the client's: 前端展示了的材料，
 *    后端必须保留其可操作状态 — so the id the UI holds must be the id the backend
 *    knows, which means adopting theirs rather than inventing ours.
 *
 *    A slot key is still needed to place a card before its material arrives, and
 *    it is still `<batchId>::<slot_id>` — but it now lives in `Slot.placeholderId`
 *    and is replaced by the real `material_id` the moment `material_completed`
 *    lands. A placeholder never reaches an endpoint, because a placeholder card
 *    is a skeleton and a skeleton has no buttons.
 * 2. **Materials are cached in this module** so that GET-material / GET-materials /
 *    compare keep working while a batch is streaming. That cache still does not
 *    survive a reload, and `sessionResumable` still says so.
 *
 *    What HAS changed is that losing it is no longer losing the batch. The web
 *    tier now records every batch to S3 as its materials arrive
 *    (`web/batch_history.py`), and `/api/batch-history` serves them back — which
 *    is what the 历史批次 panel reads. So a reload loses the live STREAM and keeps
 *    the RESULTS. The two stores answer different questions and are deliberately
 *    not merged: this one is the in-flight batch with its stage events, that one
 *    is every batch ever generated. `agentCoreTransport` therefore passes
 *    `/batch-history` straight through to `realTransport` rather than translating
 *    it — the Runtime is invoked once per material and has never heard of a batch.
 *
 * Everything above `request()` is untouched: call sites still speak §8.
 */
import type {
  AudioStatusResponse,
  BatchItemSnapshot,
  BatchListResponse,
  BatchSnapshot,
  CreateBatchRequest,
  CreateBatchResponse,
  CrossCheck,
  MaterialListResponse,
  MaterialRecord,
  MaterialStage,
  PreviewAudioResponse,
  SelectMaterialResponse,
  SseEvent,
} from '@/contracts/api'
import type { Audit, Blueprint, Material, Verdict } from '@/contracts'
import { getConfig } from '@/config/runtimeConfig'
import { CUSTOM_SCENARIO_KEY } from '@/config/scenarioTypes'
import { estimateBatchSeconds } from '@/domain/batchEstimate'
import {
  ApiError,
  CREDENTIALS,
  notifyUnauthorized,
  realTransport,
  type RequestSpec,
  type Transport,
} from './http'
import { setSseFetch } from './sseClient'

/* ── backend wire types (what /invocations really emits) ──────────────────── */

interface WireBatchStarted {
  type: 'batch_started'
  /** 自定义场景的用户原文，web 层从 custom_scenario.prompt_hint 取。 */
  custom_label?: string
  /**
   * The batch id the WEB TIER minted (`web-<ms>-<counter>`), and the only one that exists.
   *
   * Adopted, never re-derived. This module used to mint `batch-<ms36>-<n>` here because the frame
   * carried no id — so the URL, the store and every local key spoke an id space the backend had
   * never issued, and `/api/batch-history/<that id>` answered 「没有找到批次 … 的历史记录」 for a
   * batch sitting in S3 under `web-…`. Same class of bug as the old `placeholderId`: the frontend
   * inventing an identifier only it knows.
   *
   * Optional in the type only so a truncated frame is a caught error rather than a type lie; a
   * missing id is refused in `startBatch` instead of being papered over with a local one.
   */
  batch_id?: string
  total: number
  deadline_at: number
  config?: Record<string, unknown>
  at: number
}

interface WireStage {
  type: 'stage'
  slot_id: string
  scenario: string
  stage: string
  attempt?: number
  detail?: Record<string, unknown>
  at: number
}

interface WireMaterialCompleted {
  type: 'material_completed'
  slot_id: string
  scenario: string
  ok: true
  /**
   * The registry key. `YYYYMMDD-<scenario_key>-<8 hex>`, from `new_material_id`.
   *
   * Optional in the type, not because the backend omits it — `MaterialResult.as_dict`
   * always emits the field — but because it is `null` when candidate registration
   * failed. `_run_slot` deliberately assigns `result.material_id` only after
   * `REGISTRY.register` succeeds, so a null here means "this material exists but no
   * candidate backs it", and offering it for 试听 would reproduce the very error this
   * field is here to prevent.
   */
  material_id?: string | null
  scenario_key?: string | null
  group_key?: string | null
  /**
   * Structural notes the validator still had about the delivered script.
   *
   * Non-empty when three generation attempts all carried validator errors and the
   * Loop delivered the last one anyway (validation is a report, not a gate). Rendered
   * on the reader page only — never on the result card.
   */
  validation_findings?: string[]
  material: Material
  blueprint: Blueprint
  audit: Audit
  cross_check: CrossCheck
  selected_version: string
  route: 'pending' | 'quarantine'
  note?: string | null
  degraded: boolean
  degraded_reason?: string | null
  anchor_repairs?: unknown[]
  warnings?: string[]
  timings?: Record<string, number>
  at: number
}

interface WireMaterialFailed {
  type: 'material_failed'
  slot_id: string
  scenario: string
  ok: false
  reason: string
  detail?: unknown
  skipped?: boolean
  timings?: Record<string, number>
  at: number
}

interface WireBatchCompleted {
  type: 'batch_completed'
  succeeded: number
  failed: number
  skipped: number
  degraded: number
  stage_timings?: Record<string, unknown>
  slots?: unknown[]
  at: number

  /**
   * Present only for an `action: generate_sets` batch, and absent rather than zeroed on a plain
   * `generate` one — see `FanOut.request_status` in web/fanout.py: `generate` may legitimately
   * deliver fewer materials than asked, so `incomplete` on one of its batches would report a normal
   * outcome as a shortfall.
   *
   * `incomplete` with a non-empty `resumable_slots` is the checkpoint case: the invocation ran out
   * of clock, the work is saved in S3, and the NEXT invocation continues it. That is not a failure
   * and must not be drawn as one.
   */
  request_status?: 'succeeded' | 'incomplete' | 'system_failure'
  requested?: number
  delivered?: number
  resumable_slots?: string[]
  system_faults?: unknown[]
  request_ids?: string[]
}

interface WireBatchFailed {
  type: 'batch_failed'
  reason: string
  detail?: string
}

type WireEvent =
  | WireBatchStarted
  | WireStage
  | WireMaterialCompleted
  | WireMaterialFailed
  | WireBatchCompleted
  | WireBatchFailed

interface WireScenario {
  id: string
  category: string
  title_zh: string
  prompt_hint: string
  default_count: number
}

interface WireCatalogue {
  scenarios: {
    version: number
    default_count: number
    /** No `max_batch`: the field was removed from the backend catalogue along with the concept. */
    categories: Array<{ id: string; title_zh: string; scenarios: WireScenario[] }>
  }
}

/* ── audio-storage's manifest (audio_storage/manifest.py) ─────────────────── */

/**
 * The real manifest is NOT design.md §8.3's shape. Confirmed by reading
 * `audio_storage/manifest.py`'s `build_manifest`:
 *
 * | §8.3 asked for          | audio_storage emits                     |
 * |-------------------------|-----------------------------------------|
 * | `segments[]`            | `clips[]`                               |
 * | `segments[].url`        | `clips[].key` (an S3 key, not a URL)    |
 * | `gap_after_ms`          | `trailing_silence_ms` (+ `prep_pause_ms`) |
 * | `total_duration_ms`     | `totals.total_duration_ms`              |
 * | `voice_map` at top      | `synthesis.voice_map`                   |
 * | `sample_rate_hz` number | `synthesis.sample_rate` string          |
 * | `url_expires_at`        | absent; TTL comes from `presign_audio`  |
 *
 * The one thing §8.3 called non-negotiable — `turn_index` in the same index
 * space as `material...script.turns` — IS honoured, which is what actually
 * matters. Presigned URLs arrive separately from `action: presign_audio` as
 * `{turn_index: url}`; the manifest deliberately carries keys so a state
 * transition cannot invalidate a stored link.
 */
interface WireClip {
  turn_index: number
  speaker: string
  role?: string
  voice_id?: string
  key: string
  duration_ms: number
  trailing_silence_ms: number
  prep_pause_ms?: number
  text_sha256?: string
}

interface WireManifest {
  manifest_version?: number
  material_id: string
  scenario_key?: string
  synthesis?: {
    engine?: string
    output_format?: string
    sample_rate?: string
    voice_map?: Record<string, string>
    synthesized_at?: string
  }
  clips: WireClip[]
  totals?: { total_duration_ms?: number; clip_count?: number }
  degraded?: boolean
}

interface WireAudioStatus {
  audio_job_id: string
  material_id: string
  status: string
  progress: { done: number; total: number }
  state?: string
  siblings_discarded?: string[]
  error?: string
  manifest?: WireManifest
  polly_calls?: number
  reused_clips?: number
  cost_usd?: number
  repeat?: boolean
}

interface WirePresign {
  material_id: string
  urls: Record<string, string>
  ttl_seconds: number
}

interface WireErrorBody {
  error: { code: string; message: string; detail?: Record<string, unknown> }
}

/* ── stage mapping ───────────────────────────────────────────────────────── */

/**
 * The backend emits more stages than §8's six, because the Loop has retry and
 * repair steps §8 did not model. Unknown names must NOT collapse to `queued`:
 * that would render a material as "排队" while it is actually mid-generation.
 */
const STAGE_MAP: Record<string, MaterialStage> = {
  queued: 'queued',
  generating: 'generating',
  regenerating: 'generating',
  // A NOT_ASSESSABLE slot being silently re-run to fill the requested count. It
  // starts over, so it is a `generating` stage in §8 terms.
  refilling: 'generating',
  material_started: 'generating',
  validating: 'validating',
  anchors_repaired: 'validating',
  auditing: 'auditing',
  audited: 'auditing',
  revising: 'revising',
  re_auditing: 're_auditing',

  // ── the question stage (`action: generate_sets`) ──
  //
  // §8's six `MaterialStage` values describe producing a MATERIAL, and the question stage is not one
  // of them: it runs after `material_done`, on a material that is already finished. Rather than widen
  // that union — which would make every existing consumer handle values it has no rendering for —
  // each question step maps onto the material stage whose *kind of work* it repeats: writing a set is
  // `generating`, checking it is `validating`, auditing it is `auditing`, revising it is `revising`.
  //
  // The precise names still reach the UI: `progress.raw_stage` carries them verbatim, and
  // `domain/progressStages.ts` maps them to the user-facing 出题/审核/修订 phases. This map exists so a
  // card is not frozen on its last material stage for the whole question phase — before this, every
  // name below fell through `?? previous` and the grid sat motionless for minutes.
  material_done: 'validating',
  questions_started: 'generating',
  question_generation_started: 'generating',
  // A question set the loop refused to deliver; the slot re-enters the stage on the same material.
  questions_restarting: 'generating',
  question_validated: 'validating',
  question_cross_check: 'auditing',
  question_revision_started: 'revising',
  question_revision_skipped: 'revising',
  question_set_clean: 're_auditing',
  question_set_blocked: 're_auditing',
  questions_rejected: 'generating',
  set_complete: 're_auditing',
  // infra_retry / refill_abandoned keep whatever stage they interrupted;
  // handled at the call site.
}

export function mapStage(name: string, previous: MaterialStage): MaterialStage {
  if (name === 'infra_retry' || name === 'refill_abandoned') return previous
  return STAGE_MAP[name] ?? previous
}

/* ── verdict derivation ──────────────────────────────────────────────────── */

/**
 * A material the audit rejected is still offered to the user.
 *
 * `route: 'quarantine'` used to hide the material behind a separate 隔离区 page
 * and strip it of audio. That concept is gone: the client's rule is that a
 * flawed material is returned, its shortcomings are stated, and the user
 * decides. So the route is recorded on the record — audio synthesis still keys
 * off it server-side — but it withholds nothing from the reviewer, and the
 * reason is phrased as a shortcoming rather than as a sentence.
 */
function auditRejection(audit: Audit, route: string): MaterialRecord['audit_rejection'] {
  if (route !== 'quarantine') return null
  if (audit.verdict === 'NOT_ASSESSABLE') {
    return {
      code: 'NOT_ASSESSABLE',
      message: '评价环节未能给出结论，本套的质量没有经过复核',
    }
  }
  const critical = audit.findings.filter((f) => f.severity === 'critical')
  return {
    code: 'VERDICT_FAIL',
    message:
      critical.length > 0
        ? `评价环节判为不达标：${critical[0]!.rule}`
        : '评价环节判为不达标',
  }
}

/** Failure reasons → a code + human sentence. The backend sends a bare token. */
const FAILURE_TEXT: Record<string, string> = {
  validation_exhausted: '确定性校验连续三次未通过，已放弃本套（非模型可用性问题）',
  model_error: '模型调用失败，已用尽基础设施重试',
  validator_unavailable: '校验脚本不可用',
  audit_failed: '评价环节失败',
  // 措辞跟着语义改了：这条现在只可能出现在「本套自己的重试把 15 分钟用完了」的情况下。
  // 旧文案「时间预算不足，本套未开始（15 分钟同步硬限）」暗示的是「别人占用了时间」——那在
  // 一整批共用一次 invoke 时是对的，现在每套一次独立 invoke，把责任推给同批的其他套是错的。
  skipped_time_budget: '本套在允许的时间内没能完成，已放弃（不影响同批其他材料）',
  unhandled_error: '后端出现未预期的异常，本套未能生成',
  bad_request: '请求不被后端接受',
}

function failureMessage(reason: string, detail: unknown): string {
  const base = FAILURE_TEXT[reason] ?? reason
  if (detail && typeof detail === 'object' && 'errors' in detail) {
    const errors = (detail as { errors?: unknown }).errors
    if (Array.isArray(errors) && errors.length > 0) return `${base}：${errors.join('；')}`
  }
  if (typeof detail === 'string' && detail.length > 0) return `${base}：${detail}`
  return base
}

/* ── the session store ───────────────────────────────────────────────────── */

interface Slot {
  /**
   * `<batchId>::<slot_id>`. A React key and a snapshot row id — nothing more.
   *
   * It must never be sent to an endpoint that resolves a candidate: it is not a
   * `material_id` and the backend registry has never heard of it. That is why the
   * field is not called `materialId` any more; the old name is what made passing it
   * to `previewAudio` look correct.
   */
  placeholderId: string
  slotId: string
  scenarioKey: string
  index: number
  stage: MaterialStage
  attempt: number
  status: BatchItemSnapshot['status']
  /**
   * The backend's registry key, present once `material_completed` has arrived AND
   * carried one. Every operable action keys off THIS.
   */
  materialId: string | null
  record?: MaterialRecord
  failure?: { code: string; message: string; attempts: number }
}

interface Session {
  batchId: string
  createdAt: number
  total: number
  /** Slot order is the backend's slot-1..slot-N; index within a scenario is derived. */
  slots: Map<string, Slot>
  slotOrder: string[]
  status: BatchSnapshot['status']
  /** Wire events, translated, in arrival order. Enables late-attach within a session. */
  emitted: SseEvent[]
  seq: number
  done: boolean
  listeners: Set<(e: SseEvent) => void>
  /** Resolved once the POST /invocations stream ends. */
  finished: Promise<void>
}

const sessions = new Map<string, Session>()
/** Selection + audio state. Frontend-side until the backend endpoint exists. */
const selections = new Map<string, { at: number; audioJobId: string }>()

/*
 * There is deliberately no batch-id counter here any more.
 *
 * It used to mint `batch-<ms36>-<n>` in `createBatch`, and that id existed nowhere but this module:
 * the web tier records the batch under `web-<ms>-<n>` (`web/batch_history.py`), so the URL, the
 * store and S3 disagreed and the history panel could never find a batch it had just generated. The
 * id now arrives in `batch_started` and is adopted verbatim.
 */

type SlotHit = { session: Session; slot: Slot }

/**
 * Resolve a slot by the BACKEND's material_id only.
 *
 * Deliberately no fallback to `placeholderId`: a lookup that accepted a slot key would let
 * `previewAudio` pass its local guard and then fail server-side with "no candidate", which is
 * exactly the failure this module now prevents. A local 404 naming the material is a far better
 * report than a backend error naming an id nobody can look up.
 */
function findSlotByMaterial(materialId: string): SlotHit | null {
  for (const session of sessions.values()) {
    for (const slot of session.slots.values()) {
      if (slot.materialId !== null && slot.materialId === materialId) return { session, slot }
    }
  }
  return null
}

/** True when a batch id belongs to this page session. Nothing survives a reload. */
export function sessionResumable(batchId: string): boolean {
  return sessions.has(batchId)
}

export function knownMaterials(): MaterialRecord[] {
  const out: MaterialRecord[] = []
  for (const session of sessions.values()) {
    for (const slotId of session.slotOrder) {
      const slot = session.slots.get(slotId)
      if (slot?.record) out.push(slot.record)
    }
  }
  return out
}

/* ── SSE frame parsing (the backend's data-only dialect) ─────────────────── */

export function decodeWireFrame(frame: string): WireEvent | null {
  const dataLines: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(':')) continue
    if (!line.startsWith('data:')) continue
    dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (dataLines.length === 0) return null
  try {
    return JSON.parse(dataLines.join('\n')) as WireEvent
  } catch {
    console.warn('[agentcore] undecodable frame', frame.slice(0, 200))
    return null
  }
}

/* ── translation: wire event → §8 SseEvent ───────────────────────────────── */

function emit(session: Session, build: (seq: number) => SseEvent | null): void {
  session.seq += 1
  const event = build(session.seq)
  if (!event) {
    session.seq -= 1
    return
  }
  session.emitted.push(event)
  for (const fn of session.listeners) fn(event)
}

function toRecord(
  session: Session,
  slot: Slot,
  wire: WireMaterialCompleted,
  materialId: string,
): MaterialRecord {
  return {
    material_id: materialId,
    batch_id: session.batchId,
    scenario_key: slot.scenarioKey,
    index: slot.index,
    status: 'done',
    verdict: wire.audit.verdict as Verdict,
    audit_rejection: auditRejection(wire.audit, wire.route),
    degraded: wire.degraded,
    // Passed through verbatim. The card must not read these (the client ruled evaluation prose off
    // the card); the reader page states them as reference for a question-writer.
    validation_findings: wire.validation_findings ?? [],
    material: wire.material,
    blueprint: wire.blueprint,
    audit: wire.audit,
    cross_check: wire.cross_check,
    created_at: new Date(wire.at * 1000).toISOString(),
  }
}

function applyWire(session: Session, wire: WireEvent): void {
  switch (wire.type) {
    case 'batch_started': {
      session.total = wire.total
      session.status = 'running'
      emit(session, (seq) => ({
        event: 'hello',
        seq,
        batch_id: session.batchId,
        total: wire.total,
        server_time: new Date(wire.at * 1000).toISOString(),
        resumed_from: 0,
        custom_label: wire.custom_label,
      }))
      break
    }
    case 'stage': {
      const slot: Slot = ensureSlot(session, wire.slot_id, wire.scenario)
      slot.stage = mapStage(wire.stage, slot.stage)
      slot.attempt = wire.attempt ?? slot.attempt
      slot.status = 'running'
      emit(session, (seq) => ({
        event: 'progress',
        seq,
        // A progress event addresses a SLOT, and before its material arrives there is no
        // material_id to address it by. The placeholder is right here and only here: the store
        // keys skeleton cards on it, and nothing operable hangs off a skeleton.
        material_id: slot.materialId ?? slot.placeholderId,
        stage: slot.stage,
        attempt: slot.attempt,
        // Verbatim, untranslated: the consumer is the progress mapping, not a label.
        raw_stage: wire.stage,
      }))
      break
    }
    case 'material_completed': {
      const slot = ensureSlot(session, wire.slot_id, wire.scenario)
      slot.status = 'done'
      slot.stage = 're_auditing'
      if (wire.scenario_key) slot.scenarioKey = wire.scenario_key
      // Adopt the backend's id. When it is absent the material exists but no candidate backs it
      // (registration failed server-side), so `select` and 试听 genuinely cannot work on it. It is
      // still delivered and still readable — the client's rule is that a material the model
      // produced is never withheld — and the reader page reports the audio button as unavailable
      // rather than offering one that fails. `materialId` stays null, which is what makes that
      // difference visible instead of turning it into a runtime error.
      const materialId = wire.material_id ?? null
      slot.materialId = materialId
      const record = toRecord(session, slot, wire, materialId ?? slot.placeholderId)
      slot.record = record
      emit(session, (seq) => ({
        event: 'material',
        seq,
        material_id: record.material_id,
        // The skeleton row this card takes over from. Only this module knows both keys: the store
        // planned its rows on `placeholderId` and the backend delivers under `material_id`, so
        // without saying which is which the store keeps BOTH — leaving an N-material batch with N
        // rows stuck at `pending` and the page reporting them as 未生成. See SseMaterialEvent.
        replaces: slot.placeholderId,
        scenario_key: record.scenario_key,
        index: record.index,
        verdict: record.verdict,
        audit_rejection: record.audit_rejection ?? null,
        degraded: record.degraded ?? false,
        material: record.material,
        blueprint: record.blueprint,
        audit: record.audit,
        cross_check: record.cross_check,
      }))
      break
    }
    case 'material_failed': {
      const slot = ensureSlot(session, wire.slot_id, wire.scenario)
      slot.status = 'failed'
      const message = failureMessage(wire.reason, wire.detail)
      slot.failure = { code: wire.reason, message, attempts: slot.attempt }
      emit(session, (seq) => ({
        event: 'material_failed',
        seq,
        // A failed slot has no material and therefore no backend id; the placeholder is the only
        // handle the store can key the failed card on.
        material_id: slot.materialId ?? slot.placeholderId,
        // Named even when it equals `material_id` (the usual case for a failure), so the store
        // resolves the skeleton row rather than leaving one behind next to the failed one.
        replaces: slot.placeholderId,
        code: wire.reason,
        message,
        attempts: slot.attempt,
      }))
      break
    }
    case 'batch_completed': {
      // `skipped` are materials the time budget never started and `failed` are
      // ones that gave up: reporting either as `done` would hide them, which is
      // precisely what the backend's own events.py refuses to do. Observed live
      // — a batch where all materials exhausted validation is a normal outcome,
      // and a green "done" over an empty grid reads as a frontend bug.
      const status: 'done' | 'partial' =
        wire.skipped > 0 || wire.failed > 0 ? 'partial' : 'done'
      session.status = status
      session.done = true
      // The exact-count fields, passed through and NOT re-derived. `request_status` is the web tier's
      // fold of what the children reported about their own requests (web/fanout.py), and it knows
      // things this frontend cannot see: a storage refusal or an absent validator makes a request
      // `system_failure` while every slot state still looks merely unfinished. Recomputing it from
      // `succeeded`/`failed` here would be a second, worse copy of that rule.
      emit(session, (seq) => ({
        event: 'batch_done',
        seq,
        status,
        completed: wire.succeeded,
        failed: wire.failed,
        audit_rejected: [...session.slots.values()].filter((s) => s.record?.audit_rejection)
          .length,
        ...(wire.request_status ? { request_status: wire.request_status } : {}),
        ...(wire.requested !== undefined ? { requested: wire.requested } : {}),
        ...(wire.delivered !== undefined ? { delivered: wire.delivered } : {}),
        ...(wire.resumable_slots ? { resumable_slots: wire.resumable_slots } : {}),
      }))
      break
    }
    case 'batch_failed': {
      session.status = 'failed'
      session.done = true
      emit(session, (seq) => ({
        event: 'material_failed',
        seq,
        material_id: `${session.batchId}::batch`,
        code: wire.reason,
        message: failureMessage(wire.reason, wire.detail),
        attempts: 1,
      }))
      emit(session, (seq) => ({
        event: 'batch_done',
        seq,
        status: 'partial',
        completed: 0,
        failed: session.total || 1,
        audit_rejected: 0,
      }))
      break
    }
  }
}

/**
 * Slots appear only when their first event arrives, because the backend does not
 * announce the slot→scenario mapping up front. The planned slots created at
 * POST time are matched by scenario, in order, so the two views agree.
 */
function ensureSlot(session: Session, slotId: string, scenario: string): Slot {
  const existing = session.slots.get(slotId)
  if (existing) {
    // The plan guessed a scenario per slot; the backend is authoritative.
    if (existing.scenarioKey !== scenario) existing.scenarioKey = scenario
    return existing
  }
  const sameScenario = [...session.slots.values()].filter((s) => s.scenarioKey === scenario)
  const slot: Slot = {
    placeholderId: `${session.batchId}::${slotId}`,
    slotId,
    scenarioKey: scenario,
    index: sameScenario.length,
    stage: 'queued',
    attempt: 1,
    status: 'pending',
    materialId: null,
  }
  session.slots.set(slotId, slot)
  session.slotOrder.push(slotId)
  return slot
}

/* ── the generate call ───────────────────────────────────────────────────── */

function invocationsUrl(): string {
  return `${getConfig().apiBaseUrl}/invocations`
}

async function invoke<T>(payload: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(invocationsUrl(), {
    method: 'POST',
    credentials: CREDENTIALS,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (res.status === 401) {
    // The web tier's /api gate, not the Runtime: the session cookie expired.
    notifyUnauthorized()
    throw new ApiError(401, 'UNAUTHENTICATED', '登录状态已失效，请重新登录')
  }
  if (!res.ok) {
    throw new ApiError(res.status, 'BACKEND_ERROR', `后端返回 ${res.status}`)
  }
  const body = (await res.json()) as T & { error?: string | WireErrorBody['error'] }
  // Two error shapes reach us: `generate` and `list_scenarios` return a bare string, while the
  // selection/audio actions return the §8 object with a code. Preserving the code matters --
  // ALREADY_SELECTED must be distinguishable from a real failure, since re-selecting is a
  // harmless no-op the UI should report calmly rather than as an error.
  if (body && typeof body === 'object' && body.error) {
    if (typeof body.error === 'string') {
      throw new ApiError(400, 'BAD_REQUEST', body.error)
    }
    const { code, message, detail } = body.error
    throw new ApiError(code === 'ALREADY_SELECTED' ? 409 : 400, code, message, detail)
  }
  return body
}

/**
 * Read one SSE response body as a sequence of decoded wire events.
 *
 * Factored out of `startBatch` because `createBatch` now has to consume the FIRST frame before a
 * session exists at all — `batch_started` carries the batch id, and the id has to be known before
 * anything can be keyed on it. One frame splitter rather than two, so the two consumers cannot
 * disagree about where a frame ends.
 */
async function* readWireFrames(body: NonNullable<Response['body']>): AsyncGenerator<WireEvent> {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    for (;;) {
      const idx = buffer.search(/\r?\n\r?\n/)
      if (idx < 0) break
      const match = /\r?\n\r?\n/.exec(buffer.slice(idx))!
      const frame = buffer.slice(0, idx)
      buffer = buffer.slice(idx + match[0].length)
      const wire = decodeWireFrame(frame)
      if (wire) yield wire
    }
  }
}

/**
 * POST `generate` and wait for `batch_started` — no further.
 *
 * That frame is the handshake: `web/fanout.py` yields it before invoking any child, so it costs one
 * round trip and no model time, and it carries the `batch_id` the web tier minted. Waiting for it is
 * what makes the backend's id authoritative: `createBatch` cannot return an id, put it in the URL, or
 * key a session on it before knowing which id the batch will be RECORDED under
 * (`web/batch_history.py`). The alternative — mint locally, adopt later — is what the client hit:
 * 历史记录 keyed on `web-…`, URL keyed on `batch-…`, and 「没有找到批次 … 的历史记录」 after a reload.
 *
 * An error here is thrown rather than folded into a session, because there is no session yet. The
 * scenario page already renders a thrown error as 「无法提交」, which is the honest place for
 * "the batch never started" — better than navigating to a results page whose only content is a
 * broken-connection banner.
 */
async function openGenerateStream(
  payload: unknown,
): Promise<{ batchId: string; started: WireBatchStarted; frames: AsyncGenerator<WireEvent> }> {
  const res = await fetch(invocationsUrl(), {
    method: 'POST',
    credentials: CREDENTIALS,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
  })
  if (res.status === 401) {
    notifyUnauthorized()
    throw new ApiError(401, 'UNAUTHENTICATED', '登录状态已失效，请重新登录')
  }
  if (!res.ok) {
    // The web tier's own refusals arrive here as JSON in the frontend's error shape
    // (RUNTIME_NOT_CONFIGURED is the one that actually happens). Reading it gives the user the
    // reason instead of a bare status code.
    const body = (await res.json().catch(() => null)) as { error?: { code: string; message: string } } | null
    if (body?.error) throw new ApiError(res.status, body.error.code, body.error.message)
    throw new ApiError(res.status, 'BACKEND_ERROR', `后端返回 ${res.status}，批次没有开始`)
  }
  if (!res.body) throw new ApiError(502, 'BACKEND_ERROR', '后端没有返回事件流，批次没有开始')

  const frames = readWireFrames(res.body)
  for (;;) {
    const next = await frames.next()
    if (next.done) {
      throw new ApiError(502, 'BATCH_NOT_STARTED', '后端的事件流在批次开始前就结束了，请重试')
    }
    if (next.value.type !== 'batch_started') continue
    const started = next.value
    const batchId = typeof started.batch_id === 'string' ? started.batch_id.trim() : ''
    if (!batchId) {
      // A `batch_started` with no id means the web tier is older than this frontend. Refused rather
      // than falling back to a locally minted id: that fallback IS the bug, and a batch whose
      // results silently cannot be found later is worse than one that never started.
      throw new ApiError(
        502,
        'BATCH_ID_MISSING',
        '后端没有下发批次编号（web 层版本过旧），这一批的结果将无法在历史里找回，已中止',
      )
    }
    return { batchId, started, frames }
  }
}

/**
 * Drives the rest of the SSE response into the session.
 *
 * The POST is issued in `createBatch` rather than when the UI opens the stream: the batch cannot
 * exist without an in-flight request, so if generation only started when the SSE view mounted, a
 * user who navigated away mid-batch would kill it. This way the request lives in module scope,
 * exactly as batchStreamManager keeps the stream out of component scope.
 */
function startBatch(session: Session, frames: AsyncGenerator<WireEvent>): Promise<void> {
  return (async () => {
    try {
      for await (const wire of frames) applyWire(session, wire)
      if (!session.done) {
        // Stream ended without batch_completed: the connection died mid-batch and
        // there is no job to reconnect to. Say so instead of spinning forever.
        session.status = 'partial'
        session.done = true
        emit(session, (seq) => ({
          event: 'batch_done',
          seq,
          status: 'partial',
          completed: [...session.slots.values()].filter((s) => s.record).length,
          failed: 0,
          audit_rejected: 0,
        }))
      }
    } catch (err) {
      session.status = 'failed'
      session.done = true
      const message = err instanceof Error ? err.message : String(err)
      emit(session, (seq) => ({
        event: 'material_failed',
        seq,
        material_id: `${session.batchId}::batch`,
        code: 'STREAM_LOST',
        message: `与后端的生成连接中断：${message}。批次绑定在这次请求上，无法续接。`,
        attempts: 1,
      }))
      emit(session, (seq) => ({
        event: 'batch_done',
        seq,
        status: 'partial',
        completed: [...session.slots.values()].filter((s) => s.record).length,
        failed: 0,
        audit_rejected: 0,
      }))
    }
  })()
}

/* ── catalogue check ─────────────────────────────────────────────────────── */

let catalogueCache: WireCatalogue['scenarios'] | null = null

export async function fetchCatalogue(): Promise<WireCatalogue['scenarios']> {
  if (catalogueCache) return catalogueCache
  const body = await invoke<WireCatalogue>({ action: 'list_scenarios' })
  catalogueCache = body.scenarios
  return catalogueCache
}

/* ── §8 endpoints implemented on top of the above ────────────────────────── */

async function createBatch(body: CreateBatchRequest): Promise<CreateBatchResponse> {
  const catalogue = await fetchCatalogue()
  const known = new Set(
    catalogue.categories.flatMap((c) => c.scenarios.map((s) => s.id)),
  )
  const total = body.requests.reduce((n, r) => n + r.count, 0)
  // No total check. `max_batch` is gone from the catalogue, from `backend/request.py` and from
  // `config/scenarios.yaml`: the web tier sends one invocation per material, so the 15-minute wall
  // bounds one material and there is nothing left for a ceiling to protect.
  //
  // Checked here so a drifted scenarios.generated.ts fails loudly rather than spending a whole
  // batch's worth of invocations on a key the backend will reject (design §8.4). Cheaper to catch
  // than it used to be — one bad key now fails one child, not the batch — and still worth catching
  // before the user waits at all.
  for (const r of body.requests) {
    if (r.scenario_key !== CUSTOM_SCENARIO_KEY && !known.has(r.scenario_key)) {
      throw new ApiError(400, 'UNKNOWN_SCENARIO', `后端不认识场景 ${r.scenario_key}`, {
        scenario_key: r.scenario_key,
      })
    }
  }

  // The payload shape is the real one: `scenarios` is a list of id STRINGS and
  // per-scenario counts go in a separate `counts` map. §8's
  // `requests:[{scenario_key,count}]` has no counterpart on the wire.
  const scenarios: string[] = []
  const counts: Record<string, number> = {}
  let custom: { prompt_hint: string; count: number } | undefined
  for (const r of body.requests) {
    if (r.scenario_key === CUSTOM_SCENARIO_KEY) {
      custom = { prompt_hint: r.scenario_text ?? '', count: r.count }
      continue
    }
    scenarios.push(r.scenario_key)
    counts[r.scenario_key] = r.count
  }

  // `generate_sets`, not `generate`. The two are separate actions with different promises
  // (backend/app.py §8.2): `generate` delivers MATERIALS and may deliver fewer than asked;
  // `generate_sets` delivers complete material+question sets, exactly N of them, and is resumable
  // across invocations because it persists slot state under `_slots/`. Questions only exist on this
  // path — asking for `generate` and then opening 题目预览 finds `_questions/` empty forever, which
  // is exactly what it did before this line changed.
  //
  // No `batch_id` here even though the action requires one. It must be unique PER CHILD, since each
  // child's slot record is keyed on it and every child calls its own slot `slot-1`; the web tier
  // therefore mints `{batch}-{slot}` per child plus a shared `group_id` (web/fanout.py's
  // `plan_children`). A single id chosen here would have N children overwriting one record.
  const payload: Record<string, unknown> = { action: 'generate_sets', scenarios, counts }
  if (custom) payload.custom_scenario = custom

  // The batch id comes from the BACKEND, and getting it is why this awaits before building
  // anything. `batch_started` is emitted before the first child is invoked, so this costs one round
  // trip and no model time — and it means the id in the URL, in the store, in `placeholderId` and
  // in S3 are all one id. Nothing is minted client-side any more; see `openGenerateStream`.
  const { batchId, started, frames } = await openGenerateStream(payload)

  // Planned slots, so the progress grid has cards before the first stage event.
  const slots = new Map<string, Slot>()
  const slotOrder: string[] = []
  let n = 0
  const plan: Array<{ scenarioKey: string; index: number }> = []
  for (const r of body.requests) {
    for (let i = 0; i < r.count; i += 1) plan.push({ scenarioKey: r.scenario_key, index: i })
  }
  for (const p of plan) {
    n += 1
    const slotId = `slot-${n}`
    slots.set(slotId, {
      placeholderId: `${batchId}::${slotId}`,
      slotId,
      scenarioKey: p.scenarioKey,
      index: p.index,
      stage: 'queued',
      attempt: 0,
      status: 'pending',
      // No id yet: the backend mints it when the material completes. Planned slots exist to lay
      // out skeleton cards, and a skeleton has no operable action.
      materialId: null,
    })
    slotOrder.push(slotId)
  }

  const session: Session = {
    batchId,
    createdAt: Date.now(),
    total,
    slots,
    slotOrder,
    status: 'running',
    emitted: [],
    seq: 0,
    done: false,
    listeners: new Set(),
    finished: Promise.resolve(),
  }
  sessions.set(batchId, session)

  // The handshake frame is applied like any other, so `hello` is emitted and `total` is taken from
  // the backend rather than from the local sum. Then the remainder of the stream is drained in
  // module scope, exactly as before.
  applyWire(session, started)
  session.finished = startBatch(session, frames)

  return {
    batch_id: batchId,
    total,
    // Concurrency-aware: the backend runs up to MAX_CONCURRENCY slots at once, so
    // wall clock follows the WAVE count, not the set count. See domain/batchEstimate.ts.
    estimated_seconds: estimateBatchSeconds(total),
    items: slotOrder.map((slotId) => {
      const slot = slots.get(slotId)!
      return {
        // Placeholder: no material exists yet, and the store uses this only to key the skeleton.
        material_id: slot.placeholderId,
        scenario_key: slot.scenarioKey,
        index: slot.index,
      }
    }),
  }
}

function snapshot(session: Session): BatchSnapshot {
  const items: BatchItemSnapshot[] = session.slotOrder.map((slotId) => {
    const slot = session.slots.get(slotId)!
    return {
      // The real id once the material has arrived; the placeholder while it is still a skeleton.
      material_id: slot.materialId ?? slot.placeholderId,
      scenario_key: slot.scenarioKey,
      index: slot.index,
      status: slot.status,
      stage: slot.stage,
      attempt: slot.attempt,
      verdict: slot.record?.verdict,
      error: slot.failure?.message ?? null,
    }
  })
  return {
    batch_id: session.batchId,
    status: session.status,
    created_at: new Date(session.createdAt).toISOString(),
    elapsed_ms: Date.now() - session.createdAt,
    total: session.total,
    completed: items.filter((i) => i.status === 'done').length,
    failed: items.filter((i) => i.status === 'failed').length,
    audit_rejected: session.slotOrder.filter(
      (id) => session.slots.get(id)?.record?.audit_rejection,
    ).length,
    seq_high: session.seq,
    items,
  }
}

/**
 * Selection, preview, and synthesis. All three call the real backend.
 *
 * `action: select` claims the candidate group and starts synthesis;
 * `action: preview_audio` synthesises ONE candidate without claiming anything, so a reviewer can
 * listen before deciding and keeps the alternative either way; `action: audio_status` polls both.
 * Real Polly clips are fetched through `action: presign_audio`, so the frontend never sees an S3
 * key. Verified end to end against real Polly.
 *
 * There is no synthetic-audio fallback here any more. It existed because /invocations accepted
 * only `generate` and `list_scenarios`, so there was no endpoint to call and the player had to be
 * demonstrated against locally generated tones. Every endpoint it stood in for now exists, and a
 * scaffold that silently substitutes fake audio for real is worse than no scaffold — a reviewer
 * could approve a material on the strength of a tone.
 */
async function selectMaterial(materialId: string): Promise<SelectMaterialResponse> {
  /* 和 `previewAudio` 同一个问题：这里原来查不到 slot 就本地抛 404，而 `slots` 只装本页会话生成的
   * 批次。后端 `select` 也是按 id 查注册表的，所以那道门只会拦住历史材料。
   *
   * `hit` 现在只用于兜底算 siblings，查不到就不兜——后端自己会回 `siblings_discarded`，那份才是
   * 权威的（它知道哪些真被丢弃了）。 */
  const hit = findSlotByMaterial(materialId)

  const body = await invoke<{
    material_id?: string
    audio_job_id?: string
    siblings_discarded?: string[]
    route?: string
  }>({ action: 'select', material_id: materialId, actor: 'reviewer' })

  // Record it locally so audioStatus can poll and the compare view can grey out siblings.
  selections.set(materialId, { at: Date.now(), audioJobId: body.audio_job_id ?? materialId })
  return {
    material_id: body.material_id ?? materialId,
    audio_job_id: body.audio_job_id ?? materialId,
    siblings_discarded:
      body.siblings_discarded ?? (hit ? siblingsOf(hit, materialId) : []),
  }
}

/**
 * 生成音频以便试听。`action: preview_audio`。
 *
 * 与 select 的区别全在后端，也正是这个端点存在的理由：preview 不认领候选组、不丢弃同场景的另一
 * 套。前端这里唯一要跟着做的事是**不要**把它记进 `selections`——那份记录的语义是「这一套被选定
 * 了」，对比视图据此把同组的另一套置灰。试听记成选定会让页面谎报一个还没发生的决定。
 */
async function previewAudio(
  materialId: string,
  versionId?: string,
): Promise<PreviewAudioResponse> {
  /* 这里原来先查 `findSlotByMaterial`，查不到就本地抛 404「材料不存在（本页会话内未见此材料）」。
   *
   * 那道门是多余的，而且它挡掉的正是它该放过的请求：`slots` 只装**本页会话生成的**批次，历史批次
   * 按定义不在里面，所以在历史材料的阅读页点「生成音频」，请求根本没发出去就被自己拒了。而后端
   * `preview_audio` 的第一步是 `registry.get(material_id)`——按 id 直接查候选注册表，跟前端会话
   * 无关，`store.load` 也不套 TTL（只有 `list_candidates` 套）。也就是说这个请求后端本来处理得了。
   *
   * 客户对只读批次的要求是「可看材料、可试听」，而试听的入口就在阅读页。材料不存在的情形交给后端
   * 回答：它的 `UnknownMaterial` 说得比这里准（「从未被提供、已被丢弃、或提供已过期」三种情况）。 */
  const body = await invoke<WireAudioStatus>({
    action: 'preview_audio',
    material_id: materialId,
    ...(versionId ? { version_id: versionId } : {}),
    actor: 'reviewer',
  })
  return {
    material_id: body.material_id ?? materialId,
    audio_job_id: body.audio_job_id ?? materialId,
    repeat: body.repeat ?? false,
  }
}

/** Other candidates for the same scenario in this batch — the ones selection discards. */
function siblingsOf(hit: SlotHit, materialId: string): string[] {
  return [...hit.session.slots.values()]
    .filter(
      (s) =>
        s.record &&
        s.materialId !== null &&
        s.scenarioKey === hit.slot.scenarioKey &&
        s.materialId !== materialId,
    )
    .map((s) => s.materialId!)
}

async function audioStatus(
  materialId: string,
  versionId?: string,
): Promise<AudioStatusResponse> {
  const wire = await invoke<WireAudioStatus>({
    action: 'audio_status',
    material_id: materialId,
    ...(versionId ? { version_id: versionId } : {}),
  })
  if (wire.status !== 'ready' || !wire.manifest) {
    const status = wire.status === 'needs_synthesis' ? 'not_requested' : wire.status
    return {
      status: (status as AudioStatusResponse['status']) ?? 'not_requested',
      progress: wire.progress ?? { done: 0, total: 0 },
      ...(wire.error ? { error: wire.error } : {}),
    }
  }
  // Presigned URLs are fetched separately and keyed by turn_index: the manifest stores S3
  // keys on purpose, so that a state transition cannot invalidate a link already handed out.
  const signed = await invoke<WirePresign>({
    action: 'presign_audio',
    material_id: materialId,
    ...(versionId ? { version_id: versionId } : {}),
    ttl_seconds: 3600,
  })
  return {
    status: 'ready',
    progress: wire.progress ?? { done: wire.manifest.clips.length, total: wire.manifest.clips.length },
    manifest: normaliseManifest(wire.manifest, signed),
  }
}

/**
 * audio_storage's manifest → §8.3's shape.
 *
 * The field renames are mechanical (see the WireManifest table above). The one substantive
 * decision is the gap: audio_storage bakes `trailing_silence_ms` INTO each clip — verified,
 * Polly returned 792ms for an 800ms `<break>` — so the player must NOT insert it again, or
 * every pause plays twice. `gap_after_ms` is therefore 0 for real audio; the silence is
 * already in the bytes the player receives.
 *
 * A clip with no presigned URL gets `url: null` rather than being dropped, so the player can
 * mark that turn unplayable and skip it while keeping turn_index alignment intact.
 */
function asSpeaker(value: string, turnIndex: number): 'speaker1' | 'speaker2' | 'speaker3' {
  if (value === 'speaker1' || value === 'speaker2' || value === 'speaker3') return value
  throw new ApiError(
    502,
    'MANIFEST_SPEAKER_UNKNOWN',
    `manifest 的 turn ${turnIndex} 报了未知说话人 ${value}，与脚本不一致`,
  )
}

function normaliseManifest(
  wire: WireManifest,
  signed: WirePresign,
): NonNullable<AudioStatusResponse['manifest']> {
  const urls = signed.urls ?? {}
  const segments = wire.clips
    .slice()
    .sort((a, b) => a.turn_index - b.turn_index)
    .map((clip) => ({
      turn_index: clip.turn_index,
      // The manifest is built from the same script the UI renders, so a speaker outside the
      // three known ids means the two have diverged. Fail loudly rather than widening the type.
      speaker: asSpeaker(clip.speaker, clip.turn_index),
      url: urls[String(clip.turn_index)] ?? null,
      duration_ms: clip.duration_ms,
      gap_after_ms: 0,
      bytes: 0,
      error: urls[String(clip.turn_index)] ? null : 'no presigned URL for this clip',
    }))
  const sampleRate = Number(wire.synthesis?.sample_rate ?? 24000)
  return {
    material_id: wire.material_id,
    generated_at: wire.synthesis?.synthesized_at ?? new Date().toISOString(),
    engine: wire.synthesis?.engine ?? 'neural',
    format: wire.synthesis?.output_format ?? 'mp3',
    sample_rate_hz: Number.isFinite(sampleRate) ? sampleRate : 24000,
    voice_map: wire.synthesis?.voice_map ?? {},
    total_duration_ms:
      wire.totals?.total_duration_ms ??
      segments.reduce((sum, s) => sum + s.duration_ms, 0),
    url_expires_at: new Date(Date.now() + (signed.ttl_seconds ?? 3600) * 1000).toISOString(),
    segments,
  }
}

/* ── transport + stream wiring ───────────────────────────────────────────── */

const agentCoreTransport: Transport = async (spec: RequestSpec): Promise<unknown> => {
  const path = spec.path.split('?')[0]!
  const query = new URLSearchParams(spec.path.split('?')[1] ?? '')
  const [, resource, id, sub] = path.split('/')

  /**
   * Batch history is served by the WEB TIER, over plain REST, so it is the one family of calls this
   * adapter does not translate — it hands them to `realTransport` unchanged.
   *
   * That is the point of the whole feature: `/api/batch-history` reads S3 (`web/batch_history.py`),
   * while everything else in this module reads the `sessions` Map below, which a reload discards.
   * Routing history through `/invocations` would mean asking the Runtime about a grouping the
   * Runtime has never heard of — it is invoked once per material and never sees a batch.
   */
  if (
    resource === 'batch-history' ||
    resource === 'batch-history-material' ||
    resource === 'material-comments' ||
    resource === 'material-question-versions' ||
    resource === 'material-question-revisions' ||
    // Same reason, same tier: the delivered question set lives in S3 under `_questions/`
    // (`web/slot_state.py`), which the Runtime writes and only the web tier reads back. Asking
    // `/invocations` for it would be asking the process that generated it to remember it.
    resource === 'material-questions'
  ) {
    return realTransport(spec)
  }

  if (spec.method === 'POST' && resource === 'batches' && !id) {
    return createBatch(spec.body as CreateBatchRequest)
  }

  if (spec.method === 'GET' && resource === 'batches' && id && !sub) {
    const session = sessions.get(id)
    if (!session) {
      throw new ApiError(
        404,
        'BATCH_NOT_FOUND',
        '批次不存在。后端批次绑定在生成请求上，不是可持久化的 job，' +
          '因此刷新页面后无法找回（design.md §5.1 的要求尚未由后端满足）。',
      )
    }
    return snapshot(session)
  }

  if (spec.method === 'GET' && resource === 'batches' && !id) {
    const response: BatchListResponse = {
      batches: [...sessions.values()].map((s) => {
        const snap = snapshot(s)
        return {
          batch_id: snap.batch_id,
          status: snap.status,
          created_at: snap.created_at,
          total: snap.total,
          completed: snap.completed,
        }
      }),
      next_cursor: null,
    }
    return response
  }

  if (spec.method === 'POST' && resource === 'batches' && sub === 'retry') {
    const body = spec.body as { material_ids?: string[]; scenario_keys?: string[] }
    const source = sessions.get(id!)
    // Resolve each id back to its scenario. Both shapes reach here: a failed slot is keyed on its
    // placeholder (`<batchId>::slot-N`), a delivered one on the backend's material_id. Matching
    // against BOTH fields rather than parsing the string is what keeps this working now that the
    // two id spaces are no longer interchangeable.
    const keys =
      body.scenario_keys ??
      (body.material_ids ?? [])
        .map(
          (mid) =>
            [...(source?.slots.values() ?? [])].find(
              (s) => s.materialId === mid || s.placeholderId === mid,
            )?.scenarioKey,
        )
        .filter((k): k is string => Boolean(k))
    if (keys.length === 0) {
      throw new ApiError(400, 'RETRY_EMPTY', '没有可补生成的场景')
    }
    const counts: Record<string, number> = {}
    for (const k of keys) counts[k] = (counts[k] ?? 0) + 1
    const created = await createBatch({
      requests: Object.entries(counts).map(([scenario_key, count]) => ({ scenario_key, count })),
      options: { narration_mode: 'full' },
    })
    return { batch_id: created.batch_id }
  }

  if (spec.method === 'GET' && resource === 'materials' && id && !sub) {
    const hit = findSlotByMaterial(id)
    if (hit?.slot.record) return hit.slot.record
    /**
     * Not in this page session — so try the batch history before giving up.
     *
     * This fallback is what makes 阅读全文 work on a historical batch, and without it the history
     * panel would lead straight to "材料不存在": the session cache only holds materials this page
     * generated, and a batch from last week is by definition not one of them. The client's rule for
     * a read-only batch is 「可看材料、可试听」, and both of those start on the reader page.
     *
     * The session cache is still tried FIRST: an in-flight batch's material is already in memory,
     * and a network round trip to fetch what is sitting in a local Map would be slower and could
     * disagree with the cards on screen.
     */
    return realTransport({ ...spec, path: `/batch-history-material/${id}` }).catch(() => {
      throw new ApiError(
        404,
        'MATERIAL_NOT_FOUND',
        '材料不存在：本页会话里没有它，历史记录里也没有。',
      )
    })
  }

  if (spec.method === 'GET' && resource === 'materials' && !id) {
    const status = query.get('status')
    const scenarioKey = query.get('scenario_key')
    let all = knownMaterials()
    // Every material routes to `pending` now; `submitted` is the review queue,
    // which lives in the frontend session until the backend records selections.
    if (status === 'discarded') all = []
    if (scenarioKey) all = all.filter((m) => m.scenario_key === scenarioKey)
    const response: MaterialListResponse = { materials: all, next_cursor: null }
    return response
  }

  if (spec.method === 'POST' && resource === 'materials' && sub === 'select') {
    return selectMaterial(id!)
  }

  if (spec.method === 'POST' && resource === 'materials' && sub === 'audio') {
    return previewAudio(id!, query.get('version_id') ?? undefined)
  }

  if (spec.method === 'GET' && resource === 'materials' && sub === 'audio') {
    return audioStatus(id!, query.get('version_id') ?? undefined)
  }

  throw new ApiError(404, 'NOT_FOUND', `适配层未实现：${spec.method} ${spec.path}`)
}

/**
 * The §8 stream endpoint, served from the session.
 *
 * `since_seq` replays what THIS page session has already translated. It is not
 * the contract's replay guarantee — the backend has no event log, so a reload
 * genuinely loses the batch. Within a session (component remount, navigation,
 * the manager's reconnect path) the replay is exact.
 */
function agentCoreSseFetch(
  url: string,
  init: { signal: AbortSignal },
): Promise<Response> {
  const parsed = new URL(url, window.location.origin)
  const batchId = parsed.pathname.split('/').at(-2)!
  const sinceSeq = Number(parsed.searchParams.get('since_seq') ?? '0')
  const session = sessions.get(batchId)
  if (!session) return Promise.resolve(new Response('no such batch', { status: 404 }))

  const encoder = new TextEncoder()
  let unsubscribe: (() => void) | null = null

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const push = (event: SseEvent) => {
        const frame = `event: ${event.event}\nid: ${event.seq}\ndata: ${JSON.stringify(event)}\n\n`
        try {
          controller.enqueue(encoder.encode(frame))
        } catch {
          /* closed */
        }
      }
      for (const e of session.emitted) if (e.seq > sinceSeq) push(e)
      if (session.done) {
        try {
          controller.close()
        } catch {
          /* already closed */
        }
        return
      }
      session.listeners.add(push)
      unsubscribe = () => session.listeners.delete(push)
      void session.finished.then(() => {
        try {
          controller.close()
        } catch {
          /* already closed */
        }
      })
      init.signal.addEventListener('abort', () => {
        unsubscribe?.()
        try {
          controller.close()
        } catch {
          /* already closed */
        }
      })
    },
    cancel() {
      unsubscribe?.()
    },
  })

  return Promise.resolve(
    new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
  )
}

/** Test/demo helper. */
export function resetAgentCore() {
  sessions.clear()
  selections.clear()
  catalogueCache = null
}

export function installAgentCoreAdapter(): { transport: Transport } {
  setSseFetch((url, init) => agentCoreSseFetch(url, init))
  console.info('[agentcore] adapter installed →', invocationsUrl())
  return { transport: agentCoreTransport }
}
