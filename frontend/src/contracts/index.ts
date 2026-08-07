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
import type { IELTSListeningPart1QuestionPackage } from './questions'

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

/**
 * 题目包。三块的分离是 schema 的核心约定，所以别名保留三块各自的名字，不合成一个「题目」类型：
 * 一个把答案和题面揉在一起的类型，会让「关掉答案开关时不能泄露 answer_key」变成一句约定而不是
 * 一件类型上做得到的事。`QuestionFace` 是考生可见的全部，也是关掉开关后唯一允许渲染的东西。
 */
export type QuestionPackage = IELTSListeningPart1QuestionPackage
export type QuestionFace = QuestionPackage['question_face']
export type QuestionFaceItem = QuestionFace['questions'][number]
export type QuestionGroup = QuestionFace['groups'][number]
export type QuestionInstruction = QuestionFace['instructions'][number]
export type QuestionLayout = QuestionGroup['layout']
export type AnswerKeyRow = QuestionPackage['answer_key'][number]
export type EvidenceRow = QuestionPackage['evidence'][number]
export type AnswerCategory = QuestionFaceItem['answer_category']
export type ResponseForm = QuestionFaceItem['response_form']

export type { IELTSListeningPart1Material, IELTSListeningPart1AuditResult }
export type { IELTSListeningPart1InformationPointBlueprint }
export type { IELTSListeningPart1QuestionPackage }
