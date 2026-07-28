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

export interface ScenarioCatalog {
  version: number
  defaultCount: number
  /** Hard cap from the AgentCore Runtime 15-minute sync limit. Never widened. */
  maxBatch: number
  customScenario: { enabled: boolean; maxLength: number }
  categories: readonly ScenarioCategory[]
}

export const CUSTOM_SCENARIO_KEY = 'custom'
