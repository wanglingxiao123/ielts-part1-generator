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
  CURRENT_LAYOUTS,
  V1_LEGACY_LAYOUT,
  blueprintVersion,
  coverageAvailable,
  isCurrentLayout,
  isLegacyLayout,
  itemLayout,
  layoutCoverage,
} from './blueprintVersion'
import { layoutLabel } from './types'
import { BASE_BLUEPRINT_V1, buildRecord } from '@/mocks/fixtures'
import type { Blueprint } from '@/contracts'

const v2 = () =>
  structuredClone(
    buildRecord('balanced', { batchId: 'b1', scenarioKey: 'accommodation-rental', index: 0, materialId: 'm' })
      .blueprint,
  ) as Blueprint

/**
 * The REAL archived v1 record (`blueprint_v1_legacy.json`), not a downgrade written here.
 *
 * This used to hand-derive a v1 record from the v2 fixture by deleting the three v2 item fields and
 * nulling the note groups. Everything that test knew to change, it changed — which is the flaw: the
 * result still had `item_form` values inside v2's three-value enum and no empty coverage array, so
 * the v1 read path was never really exercised. `layoutCoverage()` could have dropped
 * `multiple_choice` entirely and every assertion below would still have passed.
 */
const v1 = (): Blueprint => structuredClone(BASE_BLUEPRINT_V1)

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

describe('the real v1 fixture is actually a v1 record', () => {
  /**
   * Guards the fixture, not the code. Every v1 assertion in this file is only worth its runtime if
   * the fixture really carries the three archived shapes; a future `build_fixtures.py` edit that
   * quietly normalised any of them would otherwise leave a file full of tests that pass vacuously.
   */
  it('carries no version field, the v1 coverage name, MC layouts and a null group', () => {
    const bp = v1() as Blueprint & Record<string, unknown>
    expect(bp.blueprint_schema_version).toBeUndefined()
    expect(bp.question_type_coverage).toBeDefined()
    expect(bp.completion_layout_coverage).toBeUndefined()

    const mc = bp.items.filter((i) => i.item_form === V1_LEGACY_LAYOUT)
    expect(mc.length).toBeGreaterThan(0)
    // Measured over every archived blueprint and capture in the repo: all 9 real MC points are
    // ungrouped. The fixture must match that, because the homogeneity check is deliberately NOT
    // relaxed for v1 — a grouped MC point would demand leniency no real record needs.
    expect(mc.every((i) => i.form_group === null)).toBe(true)
    // An empty layout list is real data and exactly what a `.filter()`/`.flat()` bug swallows.
    expect((bp.question_type_coverage as Record<string, number[]>).note).toEqual([])
  })
})

describe('layoutCoverage', () => {
  it('reads the v2 name', () => {
    expect(Object.keys(layoutCoverage(v2())).sort()).toEqual(['form', 'note', 'table'])
  })

  it('reads the v1 name and accounts for all ten points', () => {
    const coverage = layoutCoverage(v1())
    expect(Object.values(coverage).flat().sort((a, b) => a - b)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ])
  })

  /**
   * Field selection is keyed on `blueprintVersion()`, so this is what distinguishes "reads the v1
   * name" from "reads whichever name happens to be there". A `??` chain passes the test above and
   * fails this one.
   */
  it('does not read a v1 record through the v2 name', () => {
    const bp = v1() as Blueprint & Record<string, unknown>
    // The v1 name removed, the v2 name supplied instead. A version-blind reader would happily
    // report these layouts; a version-keyed one reports nothing, because a record with no version
    // field has no business carrying v2's field name.
    delete bp.question_type_coverage
    bp.completion_layout_coverage = { form: [1, 2, 3] }
    expect(layoutCoverage(bp as Blueprint)).toEqual({})
  })

  it('preserves an empty layout array from the real capture', () => {
    // `note: []` and "no note key at all" are different statements about the material, and only one
    // of them is what the record made. Dropping empties would silently turn the first into the
    // second.
    expect(layoutCoverage(v1()).note).toEqual([])
  })

  /**
   * The whole point of the compatibility layer. `multiple_choice` is no longer a legal layout, but
   * two numbers of a real archived capture live under that key — dropping it made those numbers
   * report as missing and the panel told reviewers the record contradicted itself.
   */
  it('keeps a v1 layout key that the UI no longer renders', () => {
    // Straight off the fixture rather than hand-assigned: the point is that the archived record's
    // own MC numbers survive the read, and asserting against a map written in this test would only
    // prove that `Object.entries` works.
    const bp = v1()
    // Presence asserted rather than defaulted with `?? []`: on `Blueprint` the v1 coverage field is
    // optional (a v2 record must not carry it), so `?? []` would let a fixture that lost the field
    // satisfy this test with an empty-equals-empty comparison and prove nothing.
    const declared = bp.question_type_coverage?.[V1_LEGACY_LAYOUT]
    expect(declared).toBeDefined()
    expect(declared!.length).toBeGreaterThan(0)
    expect(layoutCoverage(bp)[V1_LEGACY_LAYOUT]).toEqual(declared)
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

  /**
   * An unknown version must display NOTHING, not "whatever field name it happens to have".
   *
   * The earlier implementation was a `??` chain over the two names, so a version-3 record whose
   * coverage field kept either spelling was read as if this build understood its contract. The
   * failure is silent by construction: the numbers look plausible, and nothing states that they came
   * from a schema nobody here has seen.
   */
  it('reads nothing from an unrecognised version, under either field name', () => {
    for (const field of ['completion_layout_coverage', 'question_type_coverage']) {
      const bp = v2() as unknown as Record<string, unknown>
      bp.blueprint_schema_version = 3
      delete bp.completion_layout_coverage
      bp[field] = { form: [1, 2, 3], table: [4, 5, 6] }
      expect(layoutCoverage(bp as unknown as Blueprint), field).toEqual({})
      expect(coverageAvailable(bp as unknown as Blueprint), field).toBe(false)
    }
  })

  it('distinguishes "declares no layouts" from "cannot be interpreted"', () => {
    // Both produce an empty map, and the difference is the whole reason `coverageAvailable` exists:
    // one is a defect in the material, the other a stale reader.
    const empty = v2() as Blueprint & Record<string, unknown>
    delete empty.completion_layout_coverage
    expect(layoutCoverage(empty)).toEqual({})
    expect(coverageAvailable(empty)).toBe(true)

    expect(coverageAvailable(v1())).toBe(true)
    expect(coverageAvailable(v2())).toBe(true)
  })
})

describe('itemLayout / labels', () => {
  it('reports a v1-only layout as legacy and a current one as not', () => {
    // Both items come from the archived record as-is; nothing is assigned here, so the assertion is
    // about real data rather than about a value this test injected.
    const bp = v1()
    const legacy = bp.items.find((i) => i.item_form === V1_LEGACY_LAYOUT)!
    const current = bp.items.find((i) => i.item_form === 'form')!
    expect(isLegacyLayout(itemLayout(legacy))).toBe(true)
    expect(isLegacyLayout(itemLayout(current))).toBe(false)
    expect(isCurrentLayout(itemLayout(legacy))).toBe(false)
    expect(isCurrentLayout(itemLayout(current))).toBe(true)
  })

  /**
   * `ItemForm` (what may arrive) is deliberately wider than `CURRENT_LAYOUTS` (what we render), and
   * this pins the gap to exactly one value. If a fourth renderable layout is ever added, this fails
   * and the glyph/label maps in `types.ts` have to be extended with it — which is the point: those
   * maps are keyed on `CurrentLayout`, so nothing else would force the question.
   */
  it('separates the layouts we render from the layouts we accept', () => {
    expect([...CURRENT_LAYOUTS]).toEqual(['form', 'table', 'note'])
    expect(isCurrentLayout(V1_LEGACY_LAYOUT)).toBe(false)
    expect(isLegacyLayout(V1_LEGACY_LAYOUT)).toBe(true)
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
