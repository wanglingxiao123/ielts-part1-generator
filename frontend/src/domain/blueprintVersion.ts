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

/** Layout keys a v1 record may carry beyond the current three-value union. */
export type LayoutKey = ItemForm | string

/**
 * Decided by the version field ALONE — never by whether the v2 fields happen to be present.
 *
 * Inferring "no response_form, so probably v1" would silently downgrade a v2 record that merely
 * forgot the field, and the v2 checks would then never run on it. `'unknown'` is returned rather
 * than falling back to 1 for the same reason: an unrecognised version is a defect to surface, not a
 * value to guess at.
 */
export function blueprintVersion(bp: Blueprint): 1 | 2 | 'unknown' {
  if (!('blueprint_schema_version' in bp) || bp.blueprint_schema_version === undefined) return 1
  return bp.blueprint_schema_version === 2 ? 2 : 'unknown'
}

/**
 * The compatibility layer's only exit: coverage keyed by whatever layouts the data itself declares.
 *
 * v1 keys that fell out of the union (`multiple_choice`) are KEPT. Dropping them is precisely the
 * regression this exists to fix — the panel told reviewers that a material generated last week
 * contradicted itself, because two of its item numbers lived under a key the flattening skipped.
 * Whether a value is still a legal layout is the write-side schema's question; what the display
 * layer asks is whether the artefact's two views of itself agree.
 */
export function layoutCoverage(bp: Blueprint): Record<LayoutKey, number[]> {
  const raw = (bp.completion_layout_coverage ?? bp.question_type_coverage ?? {}) as Record<
    string,
    unknown
  >
  const out: Record<LayoutKey, number[]> = {}
  for (const [key, value] of Object.entries(raw)) {
    if (Array.isArray(value)) out[key] = value.filter((n): n is number => typeof n === 'number')
  }
  return out
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

/** True for a layout value outside the current three-value union, i.e. v1-only data. */
export function isLegacyLayout(layout: LayoutKey): boolean {
  return !(['form', 'table', 'note'] as string[]).includes(layout)
}
