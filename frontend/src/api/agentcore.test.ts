/**
 * The adapter's translation layer, exercised against a REAL captured batch.
 *
 * `__fixtures__/real-batch.sse.txt` is the verbatim response body of a live
 * `POST /invocations` against the container (2 slots: one PASS, one
 * validation_exhausted). Asserting against invented wire data would only prove
 * the adapter agrees with my guess about the backend; asserting against the
 * capture proves it agrees with the backend.
 *
 * These tests do not need a running backend — that is the point of the capture.
 */
import { describe, expect, it } from 'vitest'
// ?raw rather than node:fs: tsconfig.app deliberately excludes node types so app
// code cannot reach for them, and vitest resolves ?raw the same way vite does.
import RAW from './__fixtures__/real-batch.sse.txt?raw'
import { decodeWireFrame, mapStage } from './agentcore'
import { advancePhase, PHASE_LABEL, phaseOfStage } from '@/domain/progressStages'
import { analyseFormGroups } from '@/domain/formGroups'
import { FALLBACK_CONFIG } from '@/config/runtimeConfig'
import { joinArtifacts } from '@/domain/joinArtifacts'
import type { Audit, Blueprint, Material } from '@/contracts'
import type { CrossCheck, MaterialStage } from '@/contracts/api'

type Wire = NonNullable<ReturnType<typeof decodeWireFrame>>

function wireEvents(): Wire[] {
  return RAW.split(/\r?\n\r?\n/)
    .filter((frame: string) => frame.trim().length > 0)
    .map((frame: string) => decodeWireFrame(frame))
    .filter((e): e is Wire => e !== null)
}

interface CompletedWire {
  material: Material
  blueprint: Blueprint
  audit: Audit
  cross_check: CrossCheck
  scenario: string
}

function completedMaterial(): CompletedWire {
  const hit = wireEvents().find((e) => e.type === 'material_completed')
  if (!hit) throw new Error('capture has no material_completed event')
  return hit as unknown as CompletedWire
}

describe('wire frame decoding', () => {
  it('parses the data-only dialect the backend actually emits', () => {
    const events = wireEvents()
    // No `event:` or `id:` lines exist on the wire; the type is a data field.
    expect(RAW).not.toMatch(/^event:/m)
    expect(RAW).not.toMatch(/^id:/m)
    expect(events.length).toBeGreaterThan(10)
    expect(events[0]!.type).toBe('batch_started')
    expect(events[events.length - 1]!.type).toBe('batch_completed')
  })

  it('ignores keepalive comment frames', () => {
    expect(decodeWireFrame(': ping')).toBeNull()
  })

  it('returns null rather than throwing on a truncated frame', () => {
    expect(decodeWireFrame('data: {"type": "stage"')).toBeNull()
  })
})

describe('stage mapping', () => {
  it('maps every stage name the real batch emitted', () => {
    const names = new Set(
      wireEvents()
        .filter((e) => e.type === 'stage')
        .map((e) => e.stage as string),
    )
    // Regenerating / anchors_repaired are real and absent from design.md §8.
    expect(names).toContain('regenerating')
    expect(names).toContain('anchors_repaired')
    for (const name of names) {
      expect(mapStage(name, 'queued'), name).not.toBe('queued')
    }
  })

  it('never collapses an unknown stage to 排队', () => {
    // A material mid-generation shown as "排队" is a lie the reviewer acts on.
    expect(mapStage('some_future_stage', 'auditing')).toBe('auditing')
    expect(mapStage('infra_retry', 'revising')).toBe('revising')
  })

  it('folds regenerating onto generating for the §8 stage field', () => {
    const stages: MaterialStage[] = ['generating', 'validating', 'auditing', 'revising']
    for (const s of stages) expect(mapStage(s, 'queued')).toBe(s)
    expect(mapStage('regenerating', 'validating')).toBe('generating')
  })

  /**
   * This replaces the old `stageDetailLabel` assertions, which required each
   * retry stage to carry a user-visible label ("校验未过，重新生成"). The intent
   * is preserved and inverted: a retry must still be HANDLED distinctly from a
   * first attempt — that is what `raw_stage` is for — but it must not be
   * rendered as a failure. The mapping is what enforces it now.
   */
  it('places every stage the real batch emitted on the user-facing progression', () => {
    const names = new Set(
      wireEvents()
        .filter((e) => e.type === 'stage')
        .map((e) => e.stage as string),
    )
    // Retry / repair stages are real and must be classified, not guessed at.
    expect(names).toContain('regenerating')
    expect(names).toContain('anchors_repaired')
    for (const name of names) {
      const phase = phaseOfStage(name)
      // `infra_retry` is deliberately null — it re-runs whatever it interrupted.
      if (name === 'infra_retry') expect(phase).toBeNull()
      else expect(phase, name).not.toBeNull()
    }
  })

  it('never shows a retry as a step backwards', () => {
    // 校验 → 重新生成 → 校验 must read as "still checking", not "back to writing":
    // the user cannot act on the system retrying itself.
    const checking = phaseOfStage('validating')
    const regenerating = phaseOfStage('regenerating')
    expect(regenerating).not.toBe(checking)
    expect(advancePhase(checking, regenerating)).toBe(checking)
    // …while genuine forward motion still moves.
    expect(advancePhase(checking, phaseOfStage('re_auditing'))).toBe('reviewing')
  })

  it('exposes no internal stage name as user-facing copy', () => {
    const labels = Object.values(PHASE_LABEL)
    expect(labels).toEqual(['生成', '校验', '修改', '复评'])
    for (const label of labels) {
      expect(label).not.toMatch(/未过|重试|失败|regenerat|retry|refill/i)
    }
  })
})

describe('real model output joins by turn_index', () => {
  const completed = completedMaterial()

  it('anchors every real blueprint item to the turn that carries its evidence', () => {
    const view = joinArtifacts({
      materialId: 'real-1',
      scenarioKey: completed.scenario,
      index: 0,
      material: completed.material,
      blueprint: completed.blueprint,
      audit: completed.audit,
      crossCheck: completed.cross_check,
    })
    // The whole point of the annotation UI: real turn_index values must line up
    // with the highlighted spans. Fixtures were hand-checked; model output is not.
    // Real output needs neither a relocation nor a dropped annotation.
    expect(view.anchorRepairs).toEqual([])
    expect(view.anchorOmissions).toEqual([])
    expect(view.blueprint.items.length).toBe(10)
    for (const item of view.blueprint.items) {
      const turn = view.turns[item.turn_index]
      expect(turn, `item ${item.number} turn ${item.turn_index}`).toBeDefined()
      const covering = turn!.highlights.find((h) => h.itemNumbers.includes(item.number))
      expect(covering, `item ${item.number} highlight`).toBeDefined()
      // Case-insensitive: this is the same rule `anchor_ok` / `_carries` apply.
      expect(turn!.text.slice(covering!.start, covering!.end).toLowerCase()).toContain(
        item.evidence.toLowerCase(),
      )
    }
  })

  it('keeps the blind information map in the same index space', () => {
    const view = joinArtifacts({
      materialId: 'real-1',
      scenarioKey: completed.scenario,
      index: 0,
      material: completed.material,
      blueprint: completed.blueprint,
      audit: completed.audit,
      crossCheck: completed.cross_check,
    })
    for (const entry of view.audit.blind_information_map) {
      expect(view.turns[entry.turn_index], `blind seq ${entry.seq}`).toBeDefined()
    }
  })
})

describe('form groups on real output', () => {
  const completed = completedMaterial()
  const view = joinArtifacts({
    materialId: 'real-1',
    scenarioKey: completed.scenario,
    index: 0,
    material: completed.material,
    blueprint: completed.blueprint,
    audit: completed.audit,
    crossCheck: completed.cross_check,
  })

  it('leaves standalone points ungrouped, unlike every fixture', () => {
    // This is the shape that exposed the false 跨度过大 warning: the real model
    // sets form_group only on genuine table groups and null everywhere else.
    const nulls = view.blueprint.items.filter((i) => i.form_group === null)
    expect(nulls.length).toBeGreaterThan(2)
    const byForm = new Map<string, number>()
    for (const i of nulls) byForm.set(i.item_form, (byForm.get(i.item_form) ?? 0) + 1)
    expect(Math.max(...byForm.values())).toBeGreaterThan(1)
  })

  it('raises no span or viability flag against undeclared groups', () => {
    const analysis = analyseFormGroups(view, FALLBACK_CONFIG.thresholds)
    for (const g of analysis.groups) {
      if (!g.ungrouped) continue
      expect(g.spanWarn, `${g.itemForm} null bucket`).toBe(false)
      expect(g.canFormQuestion).toBe(false)
    }
  })

  it('agrees with the real question_type_coverage', () => {
    const analysis = analyseFormGroups(view, FALLBACK_CONFIG.thresholds)
    expect(analysis.consistency.consistent).toBe(true)
    expect(analysis.consistency.coversAllTen).toBe(true)
  })
})

describe('cross_check shape', () => {
  it('is arrays of rows, not arrays of numbers as §8 proposed', () => {
    const cc = completedMaterial().cross_check as unknown as Record<string, unknown>
    expect(Array.isArray(cc.unrecoverable)).toBe(true)
    expect(Array.isArray(cc.unintended_target)).toBe(true)
    // Fields §8 did not know about, relied on by the reader's jump buttons.
    expect(cc).toHaveProperty('planned')
    expect(cc).toHaveProperty('observed')
    expect(cc).toHaveProperty('ambiguous')
  })
})

describe('failure reporting', () => {
  it('carries the validator errors, not just the reason token', () => {
    const hit = wireEvents().find((e) => e.type === 'material_failed')
    expect(hit).toBeDefined()
    const failed = hit as unknown as { reason: string; detail: { errors: string[] } }
    expect(failed.reason).toBe('validation_exhausted')
    // These strings are the only actionable content in a failure; dropping them
    // leaves a card that says "失败" and nothing else.
    expect(failed.detail.errors.length).toBeGreaterThan(0)
  })
})
