// Generates src/config/scenarios.generated.ts from config/scenarios.yaml.
//
// design.md §8.4 asks for a repo config file with a shared scenario_key space.
// config/scenarios.yaml already is that file and the backend reads the same
// copy, so the frontend generates from it rather than hand-typing a duplicate
// (a duplicate is exactly how scenario_key drift starts). Codegen also removes
// a runtime YAML dependency from the bundle.
//
//   node scripts/gen-scenarios.mjs           # write
//   node scripts/gen-scenarios.mjs --check   # fail if output would change

import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { load } from 'js-yaml'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const srcPath = resolve(repoRoot, 'config/scenarios.yaml')
const outPath = resolve(here, '..', 'src/config/scenarios.generated.ts')

const raw = load(await readFile(srcPath, 'utf8'))

const categories = raw.categories.map((c) => ({
  id: c.id,
  titleZh: c.title_zh,
  scenarios: c.scenarios.map((s) => ({
    key: s.id,
    titleZh: s.title_zh,
    hint: String(s.prompt_hint ?? '').replace(/\s+/g, ' ').trim(),
  })),
}))

const payload = {
  version: raw.version,
  defaultCount: raw.default_count,
  maxBatch: raw.max_batch,
  customScenario: {
    enabled: Boolean(raw.custom_scenario?.enabled),
    maxLength: raw.custom_scenario?.max_length ?? 200,
  },
  categories,
}

const next = `/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: config/scenarios.yaml
 * Regenerate: npm run scenarios:gen
 */
import type { ScenarioCatalog } from './scenarioTypes'

export const SCENARIO_CATALOG: ScenarioCatalog = ${JSON.stringify(payload, null, 2)} as const
`

if (process.argv.includes('--check')) {
  let current = ''
  try {
    current = await readFile(outPath, 'utf8')
  } catch {
    /* missing counts as drift */
  }
  if (current !== next) {
    console.error('scenarios drift: src/config/scenarios.generated.ts')
    console.error('Run `npm run scenarios:gen` and commit the result.')
    process.exit(1)
  }
  console.log('scenarios up to date')
} else {
  await writeFile(outPath, next, 'utf8')
  console.log(
    `wrote src/config/scenarios.generated.ts (${categories.length} categories, ` +
      `${categories.reduce((n, c) => n + c.scenarios.length, 0)} scenarios)`,
  )
}
