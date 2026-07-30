export interface ScenarioEntry {
  /**
   * `scenario_key` sent to the backend. Key space is shared with
   * config/scenarios.yaml; an unknown key must yield 400 UNKNOWN_SCENARIO
   * rather than being silently accepted (design.md §8.4).
   */
  key: string
  titleZh: string
  /** prompt_hint — a generator constraint, not user-facing copy. */
  hint: string
}

export interface ScenarioCategory {
  id: string
  titleZh: string
  scenarios: readonly ScenarioEntry[]
}

/**
 * There is no `maxBatch`, and its absence is the requirement.
 *
 * It used to be "a hard cap from the AgentCore Runtime 15-minute sync limit, never widened", which
 * was true while a whole batch travelled inside one invocation. `web/fanout.py` now sends one
 * invocation per material, so the wall bounds a single ~150-230s material and the cap has no
 * platform basis. The honest signal about a large submission is the time estimate
 * (`domain/batchEstimate.ts`), not a refusal.
 */
export interface ScenarioCatalog {
  version: number
  defaultCount: number
  customScenario: { enabled: boolean; maxLength: number }
  categories: readonly ScenarioCategory[]
}

export const CUSTOM_SCENARIO_KEY = 'custom'
