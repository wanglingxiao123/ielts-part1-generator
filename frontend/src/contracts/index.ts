/**
 * Convenience aliases over the AUTO-GENERATED contract types.
 *
 * Every alias here is *derived* from the generated shapes (indexed access /
 * re-export), never re-declared. If a schema field changes, the generated file
 * changes and these aliases change with it — there is no second definition of
 * the contract to keep in sync (design.md §10).
 */
import type { IELTSListeningPart1Material, Part, Script, Turn } from './material'
import type { IELTSListeningPart1InformationPointBlueprint, Item } from './blueprint'
import type { IELTSListeningPart1AuditResult } from './audit'

export type Material = IELTSListeningPart1Material
export type MaterialPart = Part
export type MaterialScript = Script
export type MaterialTurn = Turn
export type SpeakerId = Turn['speaker']

export type Blueprint = IELTSListeningPart1InformationPointBlueprint
export type BlueprintItem = Item
export type ItemForm = Item['item_form']
export type ItemType = Item['type']
/**
 * v2's coverage shape. The v1 name (`question_type_coverage`) is deliberately NOT aliased here:
 * codegen emits it as `{}` because v1 data may key layouts outside the union, and `{}` accepts
 * everything but null/undefined. Read coverage through `domain/blueprintVersion.ts`'s
 * `layoutCoverage()`, which handles both names and returns one shape.
 */
export type CompletionLayoutCoverage = Blueprint['completion_layout_coverage']

export type Audit = IELTSListeningPart1AuditResult
export type Verdict = Audit['verdict']
export type AuditFinding = Audit['findings'][number]
export type FindingSeverity = AuditFinding['severity']
export type BlindMapEntry = Audit['blind_information_map'][number]
export type AuditMetrics = Audit['metrics']
export type ScoreDimensions = Audit['score']['dimensions']
export type DimensionKey = keyof ScoreDimensions

export type { IELTSListeningPart1Material, IELTSListeningPart1AuditResult }
export type { IELTSListeningPart1InformationPointBlueprint }
