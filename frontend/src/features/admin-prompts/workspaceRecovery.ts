import type { PromptTrialRequest, PromptValues } from '@/api/adminPrompts'

interface DraftRecovery {
  values: PromptValues
  baseline?: PromptValues
  draft_etag: string | null
  selected: string
  conflicted: boolean
}

// Fallback retains navigation recovery even when browser storage is unavailable.
const memory = new Map<string, string>()
const keyFor = (email: string, kind: string) => `ielts:prompt-workspace:${email}:${kind}`

function read(email: string, kind: string): unknown {
  const key = keyFor(email, kind)
  try {
    const raw = memory.get(key) ?? sessionStorage.getItem(key)
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

export function rememberWorkspace(email: string, kind: 'draft' | 'trial', value: DraftRecovery | PromptTrialRequest | null) {
  const key = keyFor(email, kind)
  if (value === null) memory.delete(key)
  else memory.set(key, JSON.stringify(value))
  try {
    if (value === null) sessionStorage.removeItem(key)
    else sessionStorage.setItem(key, JSON.stringify(value))
  } catch { /* Per-tab memory still preserves SPA navigation. */ }
}

export function recoverDraft(email: string): DraftRecovery | null {
  const row = read(email, 'draft') as Partial<DraftRecovery> | null
  if (!row || !row.values || typeof row.values !== 'object' ||
    !Object.values(row.values).every((value) => typeof value === 'string') ||
    typeof row.selected !== 'string' || typeof row.conflicted !== 'boolean' ||
    !(row.draft_etag === null || typeof row.draft_etag === 'string')) return null
  if (row.baseline && (typeof row.baseline !== 'object' ||
    !Object.values(row.baseline).every((value) => typeof value === 'string'))) delete row.baseline
  return row as DraftRecovery
}

export function recoverTrial(email: string): PromptTrialRequest | null {
  const row = read(email, 'trial') as Partial<PromptTrialRequest> | null
  if (!row || typeof row.request_id !== 'string' || !/^[a-f0-9]{32}$/.test(row.request_id) ||
    typeof row.prompt_id !== 'string' || typeof row.input !== 'string' ||
    typeof row.model_id !== 'string' ||
    !(row.draft_etag === null || typeof row.draft_etag === 'string')) return null
  return row as PromptTrialRequest
}
