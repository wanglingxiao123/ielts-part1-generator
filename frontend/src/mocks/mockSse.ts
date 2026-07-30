/**
 * Programmable mock SSE stream (implement.md phase 2).
 *
 * Supports: interval emission, "drop the connection after the Nth material",
 * since_seq replay, and a partial terminal state. Events are stored so a
 * reconnect replays exactly what the contract promises (design.md §8.2).
 */
import type { SseEvent } from '@/contracts/api'
import { buildRecord, type FixtureKind } from './fixtures'

export interface MockBatchPlan {
  batchId: string
  /** One entry per material, in completion order. */
  materials: Array<{
    materialId: string
    scenarioKey: string
    index: number
    kind: FixtureKind
    /**
     * `slot-N`, so the mock can address a card BEFORE its material exists — exactly as production
     * does. Optional only so a plan persisted before this field was added still rehydrates.
     *
     * This is the second half of the production id shape the mock was missing. `progress` events
     * carry `<batchId>::<slot_id>` on the real wire because the backend has not minted a
     * `material_id` yet, and `material` then arrives under a different key. The mock used to send
     * the final `material_id` from the very first frame, so the skeleton→material handover — where
     * 「有 N 套未能生成」 came from — simply did not exist in `dev:mock`.
     */
    slotId?: string
  }>
  /** Drop the stream after this many `material` events have been sent. */
  dropAfterMaterials?: number
  /** Materials never delivered → batch ends `partial`. */
  neverComplete?: number
  /** ms between stage transitions. */
  tickMs: number
}

/**
 * The stage sequence the REAL backend emits, retries included.
 *
 * `regenerating` and `infra_retry` are here on purpose: they are the events that
 * used to leak "校验未过，重新生成" onto a card, so the mock must produce them or
 * the dev server would never exercise the path that has to hide them.
 */
const STAGES = [
  'queued',
  'generating',
  'validating',
  'regenerating',
  'generating',
  'validating',
  'auditing',
  'infra_retry',
  'revising',
  're_auditing',
] as const

/** §8's six-stage space, for the `stage` field. `raw_stage` keeps the real name. */
const STAGE_TO_CONTRACT: Record<(typeof STAGES)[number], string> = {
  queued: 'queued',
  generating: 'generating',
  regenerating: 'generating',
  validating: 'validating',
  auditing: 'auditing',
  infra_retry: 'auditing',
  revising: 'revising',
  re_auditing: 're_auditing',
}

export class MockBatch {
  readonly events: SseEvent[] = []
  private seq = 0
  private timers: number[] = []
  private listeners = new Set<(e: SseEvent) => void>()
  private dropped = false
  private droppedCount = 0
  /** Set once the drop has been "repaired" by a reconnect. */
  private repaired = false

  private readonly plan: MockBatchPlan

  constructor(plan: MockBatchPlan) {
    this.plan = plan
  }

  get batchId(): string {
    return this.plan.batchId
  }

  /**
   * The placeholder key a card carries before its material arrives: `<batchId>::<slot_id>`.
   *
   * Mirrors `agentcore.ts`'s `Slot.placeholderId` exactly. It has to be the same string, not merely
   * the same shape: `progress` addresses it, `material.replaces` names it, and the store drops that
   * row when the real one lands.
   */
  private placeholderFor(index: number): string {
    const slotId = this.plan.materials[index]?.slotId ?? `slot-${index + 1}`
    return `${this.plan.batchId}::${slotId}`
  }

  get total(): number {
    return this.plan.materials.length
  }

  private emit(event: Omit<SseEvent, 'seq'> & { seq?: number }) {
    this.seq += 1
    const full = { ...event, seq: this.seq } as SseEvent
    this.events.push(full)
    // A dropped connection stops DELIVERY but not generation: the job keeps
    // producing events, which is exactly the guarantee design.md §5.1 demands
    // of the backend. Replay after reconnect then fills the hole.
    if (!this.dropped) for (const fn of this.listeners) fn(full)
  }

  private at(ms: number, fn: () => void) {
    this.timers.push(window.setTimeout(fn, ms))
  }

  /**
   * Replays the whole job instantly, as if it had been running server-side
   * while the page was gone. Used after a reload: the batch is a job, not a
   * connection (design.md §5.1), so the events must already exist.
   */
  fastForward() {
    const { materials, neverComplete = 0 } = this.plan
    const deliverable = materials.length - neverComplete
    this.emit({
      event: 'hello',
      batch_id: this.plan.batchId,
      total: materials.length,
      server_time: new Date().toISOString(),
      resumed_from: 0,
    } as never)
    materials.forEach((m, i) => {
      if (i >= deliverable) return
      const rec = buildRecord(m.kind, {
        materialId: m.materialId,
        batchId: this.plan.batchId,
        scenarioKey: m.scenarioKey,
        index: m.index,
      })
      this.emit({
        event: 'material',
        material_id: rec.material_id,
        replaces: this.placeholderFor(i),
        scenario_key: rec.scenario_key,
        index: rec.index,
        verdict: rec.verdict,
        audit_rejection: rec.audit_rejection ?? null,
        degraded: rec.degraded ?? false,
        material: rec.material,
        blueprint: rec.blueprint,
        audit: rec.audit,
        cross_check: rec.cross_check,
      } as never)
    })
    this.emit({
      event: 'batch_done',
      status: neverComplete > 0 ? 'partial' : 'done',
      completed: deliverable,
      failed: 0,
      audit_rejected: materials.filter((m) => m.kind === 'failed').length,
    } as never)
  }

  /** Starts the simulated job. Independent of any subscriber. */
  start() {
    const { materials, tickMs, neverComplete = 0 } = this.plan
    const deliverable = materials.length - neverComplete

    this.emit({
      event: 'hello',
      batch_id: this.plan.batchId,
      total: materials.length,
      server_time: new Date().toISOString(),
      resumed_from: 0,
    } as never)

    materials.forEach((m, i) => {
      // Stagger materials so results arrive one at a time (prd R3).
      const base = i * tickMs * 2.2
      STAGES.forEach((stage, si) => {
        this.at(base + si * tickMs * 0.55, () => {
          this.emit({
            event: 'progress',
            // The PLACEHOLDER, not the final material id: mid-generation the backend has not minted
            // one, so this is the only handle a progress event has. Sending the final id here is
            // what made the mock unable to reproduce the stale-skeleton bug.
            material_id: this.placeholderFor(i),
            stage: STAGE_TO_CONTRACT[stage],
            attempt: stage === 'regenerating' ? 2 : 1,
            raw_stage: stage,
          } as never)
        })
      })
      if (i < deliverable) {
        this.at(base + STAGES.length * tickMs * 0.55, () => {
          const rec = buildRecord(m.kind, {
            materialId: m.materialId,
            batchId: this.plan.batchId,
            scenarioKey: m.scenarioKey,
            index: m.index,
          })
          this.emit({
            event: 'material',
            material_id: rec.material_id,
            replaces: this.placeholderFor(i),
            scenario_key: rec.scenario_key,
            index: rec.index,
            verdict: rec.verdict,
            audit_rejection: rec.audit_rejection ?? null,
            degraded: rec.degraded ?? false,
            material: rec.material,
            blueprint: rec.blueprint,
            audit: rec.audit,
            cross_check: rec.cross_check,
          } as never)

          this.droppedCount += 1
          if (
            this.plan.dropAfterMaterials !== undefined &&
            this.droppedCount === this.plan.dropAfterMaterials &&
            !this.repaired
          ) {
            this.dropped = true
            for (const fn of this.listeners) fn({ event: '__drop__' } as never)
          }
        })
      }
    })

    const endAt = materials.length * tickMs * 2.2 + tickMs * 4
    this.at(endAt, () => {
      const completed = deliverable
      this.emit({
        event: 'batch_done',
        status: neverComplete > 0 ? 'partial' : 'done',
        completed,
        failed: 0,
        audit_rejected: materials.filter((m) => m.kind === 'failed').length,
      } as never)
    })
  }

  /** A subscriber attaching (or re-attaching) with a replay cursor. */
  subscribe(sinceSeq: number, fn: (e: SseEvent) => void): () => void {
    if (this.dropped) {
      this.dropped = false
      this.repaired = true
    }
    this.listeners.add(fn)
    // Replay: seq > sinceSeq, contents identical to first delivery.
    for (const e of this.events) {
      if (e.seq > sinceSeq) fn(e)
    }
    return () => this.listeners.delete(fn)
  }

  snapshotStatus(): 'running' | 'done' | 'partial' {
    const last = this.events[this.events.length - 1]
    if (last?.event === 'batch_done') return last.status
    return 'running'
  }

  dispose() {
    for (const id of this.timers) window.clearTimeout(id)
    this.timers = []
    this.listeners.clear()
  }
}
