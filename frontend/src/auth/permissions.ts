/**
 * Action × role matrix (design.md §7.1, prd R1).
 *
 * Both roles currently have identical permissions. The point of the matrix is
 * that every check goes through useCan(), so a future split changes this table
 * only — never a component.
 */
export type Role = 'generator' | 'reviewer'

export type Action =
  | 'batch.create'
  | 'batch.view'
  | 'material.view'
  | 'material.select'
  | 'material.retry'
  | 'quarantine.view'
  | 'audio.play'

const MATRIX: Record<Action, Role[]> = {
  'batch.create': ['generator', 'reviewer'],
  'batch.view': ['generator', 'reviewer'],
  'material.view': ['generator', 'reviewer'],
  'material.select': ['generator', 'reviewer'],
  'material.retry': ['generator', 'reviewer'],
  'quarantine.view': ['generator', 'reviewer'],
  'audio.play': ['generator', 'reviewer'],
}

export function can(roles: Role[], action: Action): boolean {
  const allowed = MATRIX[action]
  return roles.some((r) => allowed.includes(r))
}

/** `cognito:groups` → Role[]. Unknown groups are ignored, not guessed. */
export function rolesFromGroups(groups: unknown): Role[] {
  if (!Array.isArray(groups)) return []
  const out: Role[] = []
  for (const g of groups) {
    if (g === 'generator' || g === 'reviewer') out.push(g)
  }
  return out
}
