// The change's user-visible claim, checked in a real browser: a batch past the old ceiling of 6 is
// ACCEPTED and its materials arrive progressively.
//
// This is the exact submission the client was refused — 3 scenarios × 5 sets = 15, answered with
// 「超过单批上限 6（后端 15 分钟同步硬限）」. So the assertions are stated as the refusal's absence:
// the submit button is enabled, no over-limit flag renders, and the batch page fills in over time
// rather than all at once at the end.
//
//   npm run dev:mock          # in another shell
//   node scripts/shots-large-batch.mjs
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { pickScenarios, submitBatch } from './pickScenarios.mjs'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots'
const BASE = 'http://localhost:5173'
const SCENARIOS = ['booking-hotel', 'booking-car-rental', 'accommodation-rental']
const PER_SCENARIO = 5
const TOTAL = SCENARIOS.length * PER_SCENARIO

await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1100 } })
const page = await ctx.newPage()
const errors = []
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

// A brisk tick so 15 materials do not take five minutes to demonstrate.
await page.addInitScript(() => {
  window.__MOCK_OPTIONS__ = { tickMs: 350 }
})

const fail = (message) => {
  console.error(`FAIL: ${message}`)
  process.exitCode = 1
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await pickScenarios(page, SCENARIOS, PER_SCENARIO)

// ── before submitting: the refusal must be gone ───────────────────────────────
const bar = (await page.locator('.summary-bar').innerText()).replace(/\n/g, ' ')
console.log(`summary bar: ${bar}`)
if (!bar.includes(`${TOTAL} 套`)) fail(`summary bar does not report ${TOTAL} 套`)
if (await page.locator('.flag-bad').count()) {
  fail(`an over-limit flag is still rendered: ${await page.locator('.flag-bad').innerText()}`)
}
if (!bar.includes('本批较大')) fail('the neutral large-batch notice is missing')

const submit = page.locator('.summary-bar button.btn-primary')
if (await submit.isDisabled()) fail(`submit is disabled for ${TOTAL} sets`)
console.log(`submit enabled for ${TOTAL} sets = ${!(await submit.isDisabled())}`)
await page.screenshot({ path: `${OUT}/40-large-batch-accepted.png` })
console.log('  shot 40-large-batch-accepted')

// ── submit, and watch it fill in ─────────────────────────────────────────────
await submitBatch(page)

const finished = () =>
  page.locator('.mat-card:not(.skel-card):not(.err-card)').count()

// Skeletons for all 15 before anything arrives: proof the grid is laid out from the plan.
await page.waitForSelector('.mat-card', { timeout: 20_000 })
const skeletons = await page.locator('.skel-card').count()
console.log(`skeleton cards on arrival: ${skeletons}`)
await page.screenshot({ path: `${OUT}/41-large-batch-skeletons.png`, fullPage: true })
console.log('  shot 41-large-batch-skeletons')

// Progressive arrival: at least one material rendered while others are still pending.
await page.waitForFunction(
  () => document.querySelectorAll('.mat-card:not(.skel-card):not(.err-card)').length >= 1,
  null,
  { timeout: 60_000 },
)
const partway = await finished()
const stillPending = await page.locator('.skel-card').count()
console.log(`partway: ${partway} arrived, ${stillPending} still pending`)
if (stillPending === 0) {
  fail('no material was still pending when the first one arrived; delivery looks buffered')
}
await page.screenshot({ path: `${OUT}/42-large-batch-progressive.png`, fullPage: true })
console.log('  shot 42-large-batch-progressive')

// Watch the count strictly increase, which is what "一套完成推一套" means observably.
const series = [partway]
for (let i = 0; i < 12 && series[series.length - 1] < TOTAL; i += 1) {
  await page.waitForTimeout(700)
  series.push(await finished())
}
console.log(`arrival series: ${series.join(' → ')}`)
if (new Set(series).size < 3) {
  fail(`materials did not arrive incrementally (series ${series.join(',')})`)
}

// ── all 15 land ──────────────────────────────────────────────────────────────
await page.waitForFunction(
  (total) =>
    document.querySelectorAll('.mat-card:not(.skel-card)').length >= total,
  TOTAL,
  { timeout: 180_000 },
)
const total = await page.locator('.mat-card:not(.skel-card)').count()
console.log(`final: ${total} cards for ${TOTAL} requested`)
if (total < TOTAL) fail(`only ${total} of ${TOTAL} materials rendered`)
await page.screenshot({ path: `${OUT}/43-large-batch-complete.png`, fullPage: true })
console.log('  shot 43-large-batch-complete')

console.log('\nconsole errors:', errors.length ? errors.slice(0, 5) : 'none')
if (errors.length) fail(`${errors.length} console errors`)
await browser.close()
console.log(process.exitCode ? '\nFAILED' : '\nOK')
