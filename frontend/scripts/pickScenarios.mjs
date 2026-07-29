// Shared scenario-picking step for the Playwright harnesses.
//
// The rebuilt 场景选择 page is chips + one global 每场景生成数量, not a checkbox row
// with its own stepper each. The scripts still want to say "give me these
// scenarios, N sets each" by KEY, so the key→中文名 mapping is read out of
// src/config/scenarios.generated.ts — the same codegen output the app imports.
// Reading it beats hardcoding a second copy that would drift from the catalogue.
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const GENERATED = fileURLToPath(
  new URL('../src/config/scenarios.generated.ts', import.meta.url),
)

let cached = null

/** The catalogue as plain data. */
export async function catalogue() {
  if (cached) return cached
  const source = await readFile(GENERATED, 'utf8')
  // Anchored on the export, not on the first `{`: the file opens with an
  // `import { ScenarioCatalog }` whose brace would otherwise be picked up.
  const marker = 'SCENARIO_CATALOG: ScenarioCatalog = '
  const start = source.indexOf(marker)
  if (start === -1) throw new Error('scenarios.generated.ts: SCENARIO_CATALOG not found')
  const body = source.slice(start + marker.length, source.lastIndexOf('}') + 1)
  cached = JSON.parse(body)
  return cached
}

/** scenario_key → 中文名, i.e. the label actually rendered on a chip. */
export async function titleByKey() {
  const cat = await catalogue()
  return new Map(cat.categories.flatMap((c) => c.scenarios.map((s) => [s.key, s.titleZh])))
}

/**
 * Selects the given scenario keys and sets the per-scenario count.
 *
 * @param page    Playwright page, already signed in and on the scenario page
 * @param keys    scenario_key values
 * @param count   每场景生成数量; omitted leaves the catalogue default
 */
export async function pickScenarios(page, keys, count) {
  await page.waitForSelector('.scn-chip')
  const titles = await titleByKey()
  for (const key of keys) {
    const title = titles.get(key)
    if (!title) throw new Error(`unknown scenario_key ${key}`)
    const chip = page.locator(`.scn-chip:has-text("${title}")`).first()
    // Idempotent: a chip already on is left alone rather than toggled off.
    if ((await chip.getAttribute('aria-pressed')) !== 'true') await chip.click()
  }
  if (count !== undefined) {
    await page.locator('.scn-setting input').fill(String(count))
    await page.locator('.scn-setting input').blur()
  }
}

/** Clicks 提交生成 and waits for the results route. */
export async function submitBatch(page, timeout = 30_000) {
  await page.locator('.summary-bar button.btn-primary').click()
  await page.waitForURL(/\/batches\//, { timeout })
}
