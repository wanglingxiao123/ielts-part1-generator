/**
 * The v1/v2 narrowing layer (design.md D2).
 *
 * Tested separately from `formGroups` because the compatibility decision has to have one place it
 * can be asserted. Before this file existed, "does a v1 blueprint still display correctly" could
 * only be answered by reading the one real capture through the whole analysis — which is a useful
 * end-to-end check (see `agentcore.test.ts`) but a poor place to pin the rules themselves.
 */
import { describe, expect, it } from 'vitest'
import {
  blueprintVersion,
  isLegacyLayout,
  itemLayout,
  layoutCoverage,
} from './blueprintVersion'
import { layoutLabel } from './types'
import { buildRecord } from '@/mocks/fixtures'
import type { Blueprint } from '@/contracts'

const v2 = () =>
  structuredClone(
    buildRecord('balanced', { batchId: 'b1', scenarioKey: 'accommodation-rental', index: 0, materialId: 'm' })
      .blueprint,
  ) as Blueprint

/** A v1 record, downgraded from the v2 fixture: no version, old coverage name, nullable groups. */
const v1 = (): Blueprint => {
  const bp = v2() as Blueprint & Record<string, unknown>
  delete bp.blueprint_schema_version
  bp.question_type_coverage = bp.completion_layout_coverage
  delete bp.completion_layout_coverage
  for (const item of bp.items) {
    // Double cast: `Item` has no index signature, and asking TS to accept `delete` on a declared
    // optional property of a generated type is not worth loosening the generated type over.
    const loose = item as unknown as Record<string, unknown>
    delete loose.response_form
    delete loose.answer_category
    delete loose.narrator_window_id
    if (item.item_form === 'note') item.form_group = null
  }
  return bp
}

describe('blueprintVersion', () => {
  it('reads v2 from the version field', () => {
    expect(blueprintVersion(v2())).toBe(2)
  })

  it('reads a record with no version field as v1', () => {
    expect(blueprintVersion(v1())).toBe(1)
  })

  /**
   * The mistake this exists to prevent: inferring the version from whether the v2 fields are
   * present. A v2 record that merely forgot `response_form` would then read as v1, the v2 checks
   * would never run on it, and the three added fields would be pure added trust surface.
   */
  it('still reads a v2 record as v2 when the v2 item fields are missing', () => {
    const bp = v2()
    for (const item of bp.items) delete (item as unknown as Record<string, unknown>).response_form
    expect(blueprintVersion(bp)).toBe(2)
  })

  it('does not fall back to v1 on an unrecognised version', () => {
    // Cast through `unknown`: the generated type says `2`, which is exactly why a wrong value has
    // to be forced in here — it can only arrive from data, never from typed code.
    const bp = v2() as unknown as Record<string, unknown>
    bp.blueprint_schema_version = 3
    expect(blueprintVersion(bp as unknown as Blueprint)).toBe('unknown')
  })
})

describe('layoutCoverage', () => {
  it('reads the v2 name', () => {
    expect(Object.keys(layoutCoverage(v2())).sort()).toEqual(['form', 'note', 'table'])
  })

  it('falls back to the v1 name', () => {
    const coverage = layoutCoverage(v1())
    expect(Object.values(coverage).flat().sort((a, b) => a - b)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ])
  })

  /**
   * The whole point of the compatibility layer. `multiple_choice` is no longer a legal layout, but
   * two numbers of a real archived capture live under that key — dropping it made those numbers
   * report as missing and the panel told reviewers the record contradicted itself.
   */
  it('keeps a v1 layout key that is no longer in the union', () => {
    const bp = v1() as Blueprint & Record<string, any>
    bp.question_type_coverage = { form: [1, 3, 4], multiple_choice: [2], table: [5, 6, 7, 8, 9, 10] }
    expect(layoutCoverage(bp).multiple_choice).toEqual([2])
  })

  it('prefers the v2 name when a record wrongly carries both', () => {
    // The write-side schema rejects this outright; the reader still must not merge two
    // contradictory views into one silently-wrong answer.
    const bp = v2() as Blueprint & Record<string, any>
    bp.question_type_coverage = { form: [99] }
    expect(layoutCoverage(bp).form).not.toContain(99)
  })

  it('returns an empty map rather than throwing when neither name is present', () => {
    const bp = v2() as Blueprint & Record<string, unknown>
    delete bp.completion_layout_coverage
    expect(layoutCoverage(bp)).toEqual({})
  })
})

describe('itemLayout / labels', () => {
  it('reports a v1-only layout as legacy and a current one as not', () => {
    const bp = v1()
    bp.items[1]!.item_form = 'multiple_choice' as never
    expect(isLegacyLayout(itemLayout(bp.items[1]!))).toBe(true)
    expect(isLegacyLayout(itemLayout(bp.items[0]!))).toBe(false)
  })

  /**
   * Rendering `undefined：①②` is the failure mode here. Falling back to the raw string rather than
   * to "未知" is deliberate: the reviewer needs to see what the archived record actually declared in
   * order to judge whether the panel is right about it.
   */
  it('falls back to the raw layout string instead of undefined', () => {
    expect(layoutLabel('note')).toBe('填空')
    expect(layoutLabel('multiple_choice')).toBe('multiple_choice')
  })
})
