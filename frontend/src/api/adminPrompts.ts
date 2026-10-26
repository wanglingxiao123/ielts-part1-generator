import { realTransport } from './http'

export type PromptValues = Record<string, string>

export interface PromptState {
  catalogue: Array<{
    id: string
    name: string
    purpose: string
    default: string
    source: string
    protected: unknown
  }>
  active: { release_id: string; values: PromptValues }
  bundled?: { release_id: string; values: PromptValues }
  active_etag: string | null
  draft: { values: PromptValues } | null
  draft_etag: string | null
  history: Array<{ release_id: string; actor: string; created_at: string | number }>
  skills: Array<{ path: string; text: string }>
}

export interface PromptTrial {
  test_id?: string
  prompt_id?: string
  created_at?: number
  actor?: string
  input?: string
  baseline_prompt?: string
  draft_prompt?: string
  baseline_snapshot_release?: string
  status: 'running' | 'completed' | 'failed'
  baseline?: string
  draft?: string
  error?: string
  model_id?: string
  baseline_release?: string
  draft_release?: string
  baseline_digest?: string
  draft_digest?: string
  draft_etag?: string
}

export interface TrialHistory {
  items: Array<PromptTrial & { test_id: string; has_input: boolean }>
  next_offset: number | null
}

// These endpoints belong to the authenticated web tier, never the generation adapter.
async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return await realTransport({ method: 'GET', path, signal }) as T
}

async function post<T>(path: string, body: unknown): Promise<T> {
  return await realTransport({ method: 'POST', path, body }) as T
}

const root = '/admin/prompts'
export const adminPrompts = {
  get: (signal?: AbortSignal) => get<PromptState>(root, signal),
  save: (values: PromptValues, expected_etag: string | null) =>
    post<PromptState>(`${root}/draft`, { values, expected_etag }),
  publish: (expected_etag: string | null, draft_etag: string | null) =>
    post<PromptState>(`${root}/publish`, { expected_etag, draft_etag }),
  rollback: (release_id: string, expected_etag: string | null) =>
    post<PromptState>(`${root}/rollback`, { release_id, expected_etag }),
  restoreDefaults: (release_id: string, expected_etag: string | null) =>
    post<PromptState>(`${root}/restore-defaults`, { release_id, expected_etag }),
  history: (offset = 0, signal?: AbortSignal) =>
    get<TrialHistory>(`${root}/tests?offset=${offset}`, signal),
  test: (body: { prompt_id: string; input: string; model_id: string; draft_etag: string | null }) =>
    post<{ test_id: string }>(`${root}/test`, body),
  result: (id: string, signal?: AbortSignal) =>
    get<PromptTrial>(`${root}/test/${encodeURIComponent(id)}`, signal),
}
