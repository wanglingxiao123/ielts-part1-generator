import { realTransport } from './http'
import type { Audit, Blueprint, QuestionPackage } from '@/contracts'
import type { CrossCheck } from '@/contracts/api'

export interface ScenarioRunRequest {
  request_id: string
  scenario_id: string
  model_id: string
  baseline: string
  candidate: string
  active_etag: string | null
  draft_etag: string | null
}
export interface ScenarioSide {
  status: string
  attempt?: number
  retry_action?: 'resume' | 'regenerate' | null
  previous_attempts?: Array<{ attempt: number; status: string; error?: string; elapsed_seconds?: number }>
  stage?: string
  error?: string
  elapsed_seconds?: number
  started_at?: number
  counts?: Record<string, number>
  events?: Array<{ stage?: string; type?: string; at?: number; detail?: unknown; reason?: string }>
  material_result?: {
    material_id?: string
    scenario_key?: string
    material: { listening_material_parts?: Array<{ script?: { turns?: Array<{ speaker: string; text: string }> } }> }
    blueprint?: Blueprint
    audit?: Audit
    cross_check?: CrossCheck
    validation_findings?: unknown
  }
  summary?: {
    sets?: Array<{ questions: { package?: QuestionPackage; review?: unknown; rounds?: number } }>
    slots?: Array<{ attempts?: Record<string, number>; last_failure?: unknown }>
    system_faults?: unknown
  }
}
export interface ScenarioRun {
  run_id: string
  scenario: { id: string; title: string }
  model_id: string
  created_at: number
  actor: string
  status: string
  versions: Record<'baseline' | 'candidate', { source_release: string; release_id: string; digest: string }>
  sides: Record<'baseline' | 'candidate', ScenarioSide>
  prompt_changes: Array<{ id: string; name: string; baseline: string; candidate: string }>
}
export interface ScenarioHistory {
  items: Array<Pick<ScenarioRun, 'run_id' | 'scenario' | 'model_id' | 'created_at' | 'actor' | 'status'>>
  next_offset: number | null
}
const root = '/admin/prompt-scenarios'
export const promptScenarios = {
  retry: (id: string, side: 'baseline' | 'candidate', attempt: number) =>
    realTransport({ method: 'POST', path: `${root}/${encodeURIComponent(id)}/${side}/retry`, body: { attempt } }) as Promise<{ run_id: string; reused: boolean }>,
  start: (body: ScenarioRunRequest) =>
    realTransport({ method: 'POST', path: root, body }) as Promise<{ run_id: string; reused: boolean }>,
  history: (offset: number, signal?: AbortSignal) =>
    realTransport({ method: 'GET', path: `${root}?offset=${offset}`, signal }) as Promise<ScenarioHistory>,
  detail: (id: string, signal?: AbortSignal) =>
    realTransport({ method: 'GET', path: `${root}/${encodeURIComponent(id)}`, signal }) as Promise<ScenarioRun>,
}
