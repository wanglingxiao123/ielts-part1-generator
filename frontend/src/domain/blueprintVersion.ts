/**
 * v1/v2 blueprint compatibility — the single place that knows two versions exist (design.md D2).
 *
 * Why this file is hand-written while `contracts/blueprint.ts` is generated: the schema expresses
 * "required in v2, absent in v1" with `if/then`, and `json-schema-to-typescript` IGNORES `if/then`.
 * Measured — the v2-conditional schema and one with the new fields simply marked optional generate
 * byte-identical TypeScript. So the generated contract cannot state the version rules, and reading
 * `blueprint_schema_version` at each call site would spread the compatibility decision across the UI
 * with no single place to test it.
 *
 * The `question_type_coverage` case makes this a type-safety requirement rather than a tidiness one.
 * Because the v1 property has no `additionalProperties: false` (v1 data legitimately carries a
 * `multiple_choice` key), codegen emits it as `{}` — a type that accepts everything except
 * null/undefined. Reading that field directly is barely safer than `any`; `layoutCoverage()` below
 * is the only sanctioned way in.
 */
import type { Blueprint, BlueprintItem, ItemForm } from '@/contracts'

/** Layout keys a coverage map may be keyed by. Wider than `ItemForm`: a map key is not validated. */
export type LayoutKey = ItemForm | string

/**
 * The three layouts v2 permits and the UI renders.
 *
 * `ItemForm` is FOUR values, because the read schema — which generates it — must admit archived
 * records carrying `multiple_choice`. So `ItemForm` answers "what may arrive?" and `CurrentLayout`
 * answers "what do we render?", and the glyph/label maps in `types.ts` are keyed on the latter.
 * Keying them on `ItemForm` would demand a glyph for a layout this product no longer produces;
 * inventing one would put the deleted layout back in front of reviewers as if it were on offer.
 */
export const CURRENT_LAYOUTS = ['form', 'table', 'note'] as const
export type CurrentLayout = (typeof CURRENT_LAYOUTS)[number]

/**
 * The one extra layout v1 records may carry. v1 read path only — never written, never valid in v2.
 *
 * Typed as `ItemForm` rather than as a bare string, so it is a compile error if the read schema ever
 * drops `multiple_choice` from its enum while this constant still claims records carry it.
 */
export const V1_LEGACY_LAYOUT: ItemForm = 'multiple_choice'

export type BlueprintVersion = 1 | 2 | 'unknown'

/**
 * Decided by the version field ALONE — never by whether the v2 fields happen to be present.
 *
 * Inferring "no response_form, so probably v1" would silently downgrade a v2 record that merely
 * forgot the field, and the v2 checks would then never run on it. `'unknown'` is returned rather
 * than falling back to 1 for the same reason: an unrecognised version is a defect to surface, not a
 * value to guess at.
 */
export function blueprintVersion(bp: Blueprint): BlueprintVersion {
  if (!('blueprint_schema_version' in bp) || bp.blueprint_schema_version === undefined) return 1
  return bp.blueprint_schema_version === 2 ? 2 : 'unknown'
}

/**
 * Coverage read from the field THIS VERSION declares, not from whichever field happens to be there.
 *
 * The field is selected by `blueprintVersion()`, so a record cannot be read through the other
 * version's name:
 *
 *   - v2 reads `completion_layout_coverage` only. A v2 record that also carries the v1 name is a
 *     record the validator rejects outright; honouring the v1 name as a fallback here would render
 *     it as if it were fine, which is the opposite of surfacing it.
 *   - v1 reads `question_type_coverage` only, for the same reason in reverse.
 *   - `'unknown'` returns EMPTY, and callers must branch on `coverageAvailable()` before treating an
 *     empty map as data. An unrecognised version rendered through either name would state layout
 *     facts about a record whose contract this build does not know — the earlier `??` chain did
 *     exactly that, silently reading a version-3 record through whichever name it happened to have.
 *
 * Within the selected field, keys are taken as the data declares them: v1's `multiple_choice` is
 * KEPT. Dropping it is the regression this exists to fix — the panel told reviewers that a material
 * generated last week contradicted itself, because two of its item numbers lived under a key the
 * flattening skipped. Whether a value is still a legal layout is the write-side schema's question;
 * what the display layer asks is whether the artefact's two views of itself agree.
 */
export function layoutCoverage(bp: Blueprint): Record<LayoutKey, number[]> {
  const version = blueprintVersion(bp)
  if (version === 'unknown') return {}
  const raw = (version === 2 ? bp.completion_layout_coverage : bp.question_type_coverage) as
    | Record<string, unknown>
    | undefined
  const out: Record<LayoutKey, number[]> = {}
  for (const [key, value] of Object.entries(raw ?? {})) {
    // Empty arrays are preserved: the real capture carries `note: []`, and a declared-but-empty
    // layout is different information from an absent one.
    if (Array.isArray(value)) out[key] = value.filter((n): n is number => typeof n === 'number')
  }
  return out
}

/**
 * Whether layout facts may be stated about this record at all.
 *
 * False for an unrecognised version, where `layoutCoverage()` returns empty because the contract is
 * unknown — NOT because the record declares no layouts. Without this distinction a UI would report
 * "第 1…10 题没有对应信息点" for a v3 record, which reads as a defect in the material rather than as
 * this build being too old to interpret it.
 */
export function coverageAvailable(bp: Blueprint): boolean {
  return blueprintVersion(bp) !== 'unknown'
}

/**
 * An item's layout as a plain `string`, not `ItemForm`.
 *
 * The widened return type is the point: v1 items carry `multiple_choice`, which is no longer in the
 * union, and a function typed `ItemForm` would let callers assume exhaustiveness over three values
 * that real data does not respect.
 */
export function itemLayout(item: BlueprintItem): LayoutKey {
  return item.item_form as LayoutKey
}

/** True for a layout value outside the three the UI renders, i.e. v1-only data. */
export function isLegacyLayout(layout: LayoutKey): boolean {
  return !(CURRENT_LAYOUTS as readonly string[]).includes(layout)
}

/** Narrowing guard for the three renderable layouts — the way into a `CurrentLayout`-keyed map. */
export function isCurrentLayout(layout: LayoutKey): layout is CurrentLayout {
  return (CURRENT_LAYOUTS as readonly string[]).includes(layout)
}
