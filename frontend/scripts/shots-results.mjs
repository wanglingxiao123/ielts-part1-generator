// Browser walkthrough of the rebuilt 生成结果 page, against the mock backend.
//
//   npm run dev:mock
//   node scripts/shots-results.mjs
//
// Beyond screenshots it asserts the two things the client complained about, in a
// real browser rather than jsdom: that no internal stage wording appears while
// the batch is mid-flight, and that the layout groups by scenario with the
// selection bar wired up.
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-results'
const BASE = 'http://localhost:5173'
await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1100 } })
const page = await ctx.newPage()
const errors = []
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

const shot = async (name, opts = {}) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true, ...opts })
  console.log(`  shot ${name}`)
}

/** Words that must never be on screen. */
const FORBIDDEN = [
  '校验未过',
  '重新生成',
  'regenerating',
  'refilling',
  'infra_retry',
  'anchors_repaired',
  '隔离',
  'NOT_ASSESSABLE',
  'MINOR_EDITS',
]

async function assertClean(label) {
  const body = await page.evaluate(() => document.body.innerText)
  const hits = FORBIDDEN.filter((w) => body.includes(w))
  // Verdict enums are English tokens; checked as whole words so ordinary prose
  // cannot trip them.
  if (/\b(PASS|FAIL)\b/.test(body)) hits.push('PASS/FAIL verdict token')
  // 试听 as an ACTION (a button/link) is what must not exist before selection.
  // The word also appears legitimately inside 「连试听的人都没听出来」, which
  // describes the blind auditor rather than offering a preview.
  const previewControls = await page
    .locator('button:has-text("试听"), a:has-text("试听")')
    .count()
  if (previewControls > 0) hits.push('试听 control')
  console.log(`  ${label}: ${hits.length === 0 ? 'clean' : `LEAK ${hits.join(', ')}`}`)
  return hits
}

const leaks = []

await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await page.waitForSelector('.scn-row')

// Three scenarios × 2 = 6, matching the client's "N 场景 × M 套" caption.
for (const key of ['booking-hotel', 'accommodation-rental', 'employment-vacancy']) {
  await page.locator(`.scn-row:has-text("${key}") input[type=checkbox]`).first().check()
}
await shot('00-scenario-select')
await page.locator('.summary-bar button').click()
await page.waitForURL(/\/batches\//)

// Mid-flight: this is the moment the old page said 校验未过，重新生成.
await page.waitForTimeout(1500)
await shot('01-in-flight')
leaks.push(...(await assertClean('mid-flight')))
console.log(`   phase caption = ${await page.locator('.results-stats').innerText()}`)

// All six delivered.
await page.waitForSelector('.results-bar', { timeout: 60_000 })
await page.waitForFunction(() => document.querySelectorAll('.mat-card').length >= 6, null, {
  timeout: 60_000,
})
await page.waitForTimeout(800)
await shot('02-all-delivered')
leaks.push(...(await assertClean('delivered')))

console.log(`   scenario groups = ${await page.locator('.scn-group').count()}`)
console.log(`   cards = ${await page.locator('.mat-card').count()}`)
console.log(`   badges 待审核 = ${await page.locator('.status-badge:has-text("待审核")').count()}`)
console.log(`   point dots on card 1 = ${await page.locator('.mat-card').first().locator('.point-dot').count()}`)
console.log(`   flagged dots total = ${await page.locator('.point-dot.flagged').count()}`)
console.log(`   cards with a shortcomings list = ${await page.locator('.mat-flaws').count()}`)
console.log(`   progress caption = ${await page.locator('.results-stats').innerText()}`)
console.log(`   submit disabled with 0 selected = ${await page.locator('button:has-text("提交审核")').isDisabled()}`)

// Selection: one scenario only → still blocked by the per-scenario rule.
await page.locator('.scn-group').first().locator('.select-check').first().click()
await page.waitForTimeout(200)
await shot('03-one-selected')
console.log(`   count after 1 pick = ${await page.locator('.results-bar .count').innerText()}`)
console.log(`   submit still disabled = ${await page.locator('button:has-text("提交审核")').isDisabled()}`)
console.log(`   missing hint = ${await page.locator('.bar-left').innerText()}`)

// Cover the other two scenarios.
for (const i of [1, 2]) {
  await page.locator('.scn-group').nth(i).locator('.select-check').first().click()
}
await page.waitForTimeout(200)
await shot('04-all-scenarios-selected')
console.log(`   count after 3 picks = ${await page.locator('.results-bar .count').innerText()}`)
console.log(`   submit enabled = ${!(await page.locator('button:has-text("提交审核")').isDisabled())}`)
console.log(`   selected cards = ${await page.locator('.mat-card.selected').count()}`)

// Compare mode.
await page.locator('button:has-text("对比本场景")').first().click()
await page.waitForSelector('.compare-banner')
await shot('05-compare-mode')
console.log(`   compare banner = ${(await page.locator('.compare-banner').innerText()).replace(/\n/g, ' | ')}`)
await page.locator('.scn-group').first().locator('.select-check').first().click()
await page.waitForTimeout(150)
console.log(`   pick-a cards = ${await page.locator('.mat-card.pick-a').count()}`)
await shot('06-compare-a-picked')
await page.locator('.scn-group').first().locator('.select-check').nth(1).click()
await page.waitForURL(/\/compare\//, { timeout: 10_000 })
console.log(`   navigated to = ${new URL(page.url()).pathname}${new URL(page.url()).search}`)
await page.waitForTimeout(1500)
await shot('07-compare-view')
leaks.push(...(await assertClean('compare view')))

// Back to the results page, submit, land in the review queue.
await page.goBack()
await page.waitForSelector('.results-bar', { timeout: 20_000 })
await page.waitForTimeout(600)
for (let i = 0; i < 3; i += 1) {
  await page.locator('.scn-group').nth(i).locator('.select-check').first().click()
}
await page.waitForTimeout(200)
await page.locator('button:has-text("提交审核")').click()
await page.waitForURL(/review-queue/, { timeout: 10_000 })
await page.waitForTimeout(500)
await shot('08-review-queue')
console.log(`   queue rows = ${await page.locator('.q-row').count()}`)
leaks.push(...(await assertClean('review queue')))

// Nav: three tabs, no 隔离区.
console.log(`   nav tabs = ${(await page.locator('.topbar nav').innerText()).replace(/\n/g, ' / ')}`)

// A flawed material, opened from the queue.
await page.locator('.q-row a:has-text("阅读全文")').first().click()
await page.waitForTimeout(1500)
await shot('09-material-reader')
leaks.push(...(await assertClean('material reader')))

console.log(`\nconsole errors: ${errors.length}`)
for (const e of errors.slice(0, 10)) console.log(`  ! ${e}`)
console.log(`forbidden-wording leaks: ${leaks.length}`)
for (const l of [...new Set(leaks)]) console.log(`  !! ${l}`)

await browser.close()
process.exit(leaks.length > 0 || errors.length > 0 ? 1 : 0)
