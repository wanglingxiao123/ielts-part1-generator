import type { PromptValues } from '@/api/adminPrompts'

// Keep the original baseline for unresolved modules: refreshing again must not
// turn an unresolved conflict into an intentional overwrite.
export function mergeDraft(base: PromptValues, local: PromptValues, remote: PromptValues) {
  const values: PromptValues = {}
  const baseline: PromptValues = {}
  const conflicts: string[] = []
  for (const id of Object.keys(remote)) {
    const mine = local[id] ?? remote[id]!
    const theirs = remote[id]!
    if (mine === theirs || mine === base[id]) values[id] = theirs
    else if (theirs === base[id]) values[id] = mine
    else {
      values[id] = mine
      conflicts.push(id)
    }
    if (conflicts.includes(id)) {
      if (base[id] !== undefined) baseline[id] = base[id]!
    } else baseline[id] = theirs
  }
  return { values, baseline, conflicts }
}
