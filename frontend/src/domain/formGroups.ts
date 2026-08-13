/**
 * form_group aggregation + layout-coverage consistency (design.md §3.6).
 *
 * item_form and the coverage map are two views of the same information
 * (the contract is redundant on purpose). The UI must report WHETHER THEY AGREE
 * rather than picking one: a disagreement means the artefact contradicts
 * itself, which is precisely what the reviewer needs to know.
 *
 * Reads coverage through `layoutCoverage()` so v1 records (which use the old
 * `question_type_coverage` name and may key layouts this union no longer has)
 * and v2 records both arrive in one shape.
 */
import type { ItemForm } from '@/contracts'
import type { Thresholds } from '@/config/runtimeConfig'
import { displayTurns } from './joinArtifacts'
import { CURRENT_LAYOUTS, coverageAvailable, itemLayout, layoutCoverage } from './blueprintVersion'
import type { CurrentLayout } from './blueprintVersion'
import { ITEM_FORM_LABEL } from './types'
import type { ViewMaterial } from './types'

export interface FormGroupSummary {
  /** null → ungrouped items of this item_form (form_group === null). */
  name: string | null
  /**
   * `ItemForm` (four values), unlike `CoverageRow.itemForm`. A group is grouped-up REAL DATA, so a
   * v1 record's `multiple_choice` points form a group like any other; narrowing here would mean
   * dropping them from the grouping and under-reporting what an archived material contains.
   */
  itemForm: ItemForm
  /**
   * True when `form_group` is null, i.e. these points were never declared to
   * belong together.
   */
  ungrouped: boolean
  numbers: number[]
  turnStart: number
  turnEnd: number
  /** Turn-index span, shown as descriptive information rather than a quality judgement. */
  turnSpan: number
  /** A coherent Form, Note, or Table completion group needs >= 3 homogeneous points. */
  canFormQuestion: boolean
}

export interface CoverageRow {
  /**
   * `CurrentLayout`, not `ItemForm`: the table has exactly one row per layout the UI renders.
   *
   * A v1 record's `multiple_choice` points get no row — deliberately, and the narrower type is what
   * says so. They are still counted in `consistency` below, which asks whether the artefact's two
   * views of ITSELF agree and must therefore see every layout the data declares.
   */
  itemForm: CurrentLayout
  label: string
  /** Numbers as declared by the coverage map (either version's name). */
  coverageNumbers: number[]
  /** Numbers as declared per-item by item_form. */
  itemFormNumbers: number[]
  agrees: boolean
}

export interface CoverageConsistency {
  /** Flattened coverage equals exactly 1..10 with no repeats. */
  coversAllTen: boolean
  duplicateNumbers: number[]
  missingNumbers: number[]
  extraNumbers: number[]
  /** Item numbers where item_form disagrees with the coverage map. */
  disagreeingNumbers: number[]
  consistent: boolean
  /**
   * False when the record declares a version this build does not know, in which case every field
   * above is meaningless rather than merely negative and the UI must say so instead of rendering it.
   *
   * Without this flag an unknown-version record produced `missingNumbers: [1..10]` — the panel then
   * told the reviewer that all ten points were absent from a record that in fact declares them all,
   * which is a defect report against the material for what is really a stale reader.
   */
  known: boolean
}

export interface FormGroupAnalysis {
  groups: FormGroupSummary[]
  rows: CoverageRow[]
  consistency: CoverageConsistency
  /** True when at least one group of >= 3 homogeneous completion-layout points exists. */
  hasViableQuestionGroup: boolean
}

const FORMS: readonly CurrentLayout[] = CURRENT_LAYOUTS

export function analyseFormGroups(
  view: ViewMaterial,
  _thresholds: Thresholds,
): FormGroupAnalysis {
  const items = view.blueprint.items

  /**
   * 组跨度按**显示位置**算，不按 blueprint 声明的 turn_index。
   *
   * 一个被挪正过的点，声明位置和真实位置可能差很远；按声明算出来的括号会画在分布图上那些
   * 点并不在的地方，读图的人会以为这一组横跨了半篇。解不出来的点没有显示位置，只能退回它
   * 声明的坐标——它在图上本来也不画，所以只影响 `numbers` 里的计数，不会让括号落错。
   */
  const shown = displayTurns(view)
  const turnOf = (item: (typeof items)[number]) => shown.get(item.number) ?? item.turn_index

  // Composite key (item_form, form_group) — skill-contract design §7 D2: counting
  // labels alone lets a group mixing several layouts masquerade as one printable task.
  //
  // The two separators are written as `\u0000` / `\u0001` escapes rather than as the literal
  // bytes. Identical string at runtime, but a literal NUL in the source makes git classify the
  // whole file as binary: every change to it then arrives for review as `Bin 6366 -> 7447 bytes`
  // with no diff at all, which is how it looked when this stage touched the flattening below.
  // They stay control characters because any printable separator could legitimately occur inside
  // a form_group label, and `\u0001null` keeps a null group distinct from one named "null".
  const buckets = new Map<string, typeof items[number][]>()
  for (const item of items) {
    const key = `${item.item_form}\u0000${item.form_group ?? '\u0001null'}`
    const list = buckets.get(key) ?? []
    list.push(item)
    buckets.set(key, list)
  }

  const groups: FormGroupSummary[] = []
  for (const list of buckets.values()) {
    const first = list[0]!
    const ungrouped = first.form_group === null
    const turns = list.map(turnOf)
    const turnStart = Math.min(...turns)
    const turnEnd = Math.max(...turns)
    const turnSpan = turnEnd - turnStart
    const homogeneous = list.every((i) => i.item_form === first.item_form)
    groups.push({
      name: first.form_group,
      itemForm: first.item_form,
      ungrouped,
      numbers: list.map((i) => i.number).sort((a, b) => a - b),
      turnStart,
      turnEnd,
      turnSpan,
      // Likewise "可成题" is a claim about a declared group.
      canFormQuestion:
        !ungrouped &&
        list.length >= 3 &&
        homogeneous &&
        CURRENT_LAYOUTS.some((layout) => layout === first.item_form),
    })
  }
  groups.sort((a, b) => a.turnStart - b.turnStart)

  // Version-keyed: `layoutCoverage()` reads the field THIS version declares, and returns empty for a
  // version this build does not know. `known` is what separates "declares no layouts" from "cannot
  // be interpreted" — see the guard on the consistency result below.
  const known = coverageAvailable(view.blueprint)
  const coverage = layoutCoverage(view.blueprint)
  const rows: CoverageRow[] = FORMS.map((form) => {
    const coverageNumbers = [...(coverage[form] ?? [])].sort((a, b) => a - b)
    const itemFormNumbers = items
      .filter((i) => i.item_form === form)
      .map((i) => i.number)
      .sort((a, b) => a - b)
    return {
      itemForm: form,
      label: ITEM_FORM_LABEL[form],
      coverageNumbers,
      itemFormNumbers,
      agrees: coverageNumbers.join(',') === itemFormNumbers.join(','),
    }
  })

  /**
   * Flattened over the keys the DATA declares, not over the three-value `FORMS`.
   *
   * `FORMS.flatMap(...)` was the bug: a v1 record's `multiple_choice: [2, 6]` fell
   * out of the flattening entirely, so the panel reported numbers 2 and 6 as
   * missing and told the reviewer that a material generated last week
   * contradicted itself. Whether `multiple_choice` is still a legal layout is the
   * write-side schema's question; consistency asks only whether the artefact's
   * two views of itself agree.
   *
   * `FORMS` stays for `rows` above — the coverage table lists exactly the three
   * layouts the UI renders, and v1's MC points do not get a new row.
   */
  const flat = Object.values(coverage).flat()
  const seen = new Set<number>()
  const duplicateNumbers: number[] = []
  for (const n of flat) {
    if (seen.has(n)) duplicateNumbers.push(n)
    seen.add(n)
  }
  const declared = new Set(items.map((i) => i.number))
  const missingNumbers = [...declared].filter((n) => !seen.has(n)).sort((a, b) => a - b)
  const extraNumbers = [...seen].filter((n) => !declared.has(n)).sort((a, b) => a - b)
  // Looked up via `itemLayout()` rather than `i.item_form` so the widened key type is explicit: a
  // v1 MC point must find `coverage['multiple_choice']`, which the union does not contain.
  const disagreeingNumbers = items
    .filter((i) => !(coverage[itemLayout(i)] ?? []).includes(i.number))
    .map((i) => i.number)
    .sort((a, b) => a - b)

  const coversAllTen =
    duplicateNumbers.length === 0 && missingNumbers.length === 0 && extraNumbers.length === 0

  return {
    groups,
    rows,
    /**
     * An unknown version reports NOTHING rather than everything-is-missing.
     *
     * The numbers above are all computed against an empty coverage map in that case, so they would
     * read as "all ten points absent, all ten self-contradictory" — a maximally alarming defect
     * report about a record this build simply cannot interpret. `consistent: false` is kept (nothing
     * was verified, so nothing may be claimed as consistent) while the specific accusations are
     * withheld, and `known: false` tells the panel to explain itself instead.
     */
    consistency: known
      ? {
          coversAllTen,
          duplicateNumbers,
          missingNumbers,
          extraNumbers,
          disagreeingNumbers,
          consistent: coversAllTen && disagreeingNumbers.length === 0,
          known: true,
        }
      : {
          coversAllTen: false,
          duplicateNumbers: [],
          missingNumbers: [],
          extraNumbers: [],
          disagreeingNumbers: [],
          consistent: false,
          known: false,
        },
    hasViableQuestionGroup: groups.some((g) => g.canFormQuestion),
  }
}
