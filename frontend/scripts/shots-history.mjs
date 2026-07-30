// Browser walkthrough of the 历史批次 side panel, against the mock backend.
//
//   npm run dev:mock
//   node scripts/shots-history.mjs
//
// Beyond screenshots it asserts, in a real browser rather than jsdom, the four things a component
// test cannot honestly claim:
//
//   1. **A generated batch survives a page RELOAD.** This is the whole feature. jsdom can assert
//      that the panel renders what the API returned; only a real reload proves the batch is not
//      living in a module-scope `Map` that the reload discards — which is exactly where it lived
//      before (`frontend/src/api/agentcore.ts`).
//   2. **Switching batches** actually re-renders the card area, rather than changing the URL while
//      the old cards stay put.
//   3. **A read-only batch's selection controls are genuinely disabled** — checked by clicking them
//      and observing that nothing became selected, not by reading an attribute.
//   4. **Search and the status chips** filter the list.
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-history'
const BASE = 'http://localhost:5173'
await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

const failures = []
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures.push(label)
}
const shot = async (name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  console.log(`  shot ${name}`)
}

const rowIds = () =>
  page.evaluate(() => [...document.querySelectorAll('.hist-row')].map((el) => el.dataset.batch))

const cardIds = () =>
  page.evaluate(() =>
    [...document.querySelectorAll('.mat-card:not(.skel-card)')].map((el) => el.dataset.material),
  )

/* ── 1. generate a batch ──────────────────────────────────────────────────── */

console.log('\n== generate a batch ==')
await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await page.waitForSelector('.scn-chip')

for (const title of ['酒店预订', '租房咨询']) {
  await page.locator(`.scn-chip:has-text("${title}")`).first().click()
}
await page.locator('.summary-bar button.btn-primary').click()
await page.waitForURL(/\/batches\//)
const liveBatchId = new URL(page.url()).pathname.split('/').pop()
console.log(`  batch = ${liveBatchId}`)

// Wait for it to finish, so its record carries the final set count.
await page.waitForSelector('.results-bar', { timeout: 60_000 })
await page.waitForFunction(
  () => document.querySelectorAll('.mat-card:not(.skel-card)').length >= 4,
  null,
  { timeout: 60_000 },
)
await page.waitForTimeout(700)
await shot('01-live-batch-with-panel')

check('the panel is on screen', (await page.locator('.hist-panel').count()) === 1)
check(
  'the live batch has a row',
  (await rowIds()).includes(liveBatchId),
  `rows = ${(await rowIds()).join(', ')}`,
)
check(
  'the live batch row is marked as selected',
  (await page.locator(`.hist-row[data-batch="${liveBatchId}"].active`).count()) === 1,
)
check(
  'the panel says how many batches there are',
  /\d+ 批/.test(await page.locator('.hist-total').innerText()),
  await page.locator('.hist-total').innerText(),
)

/* ── 2. THE reload ────────────────────────────────────────────────────────── */

console.log('\n== reload the page ==')
await page.reload({ waitUntil: 'networkidle' })
await page.waitForSelector('.hist-row', { timeout: 15_000 })
await page.waitForTimeout(700)
await shot('02-after-reload')

const afterReload = await rowIds()
check(
  'the batch is still in the panel after a reload',
  afterReload.includes(liveBatchId),
  `rows = ${afterReload.join(', ')}`,
)
// The reload also drops the live SSE session, so this batch is now reached through the HISTORY
// path. Its cards must therefore still render — that is the difference between "the row survived"
// and "the batch survived".
await page.waitForSelector('.mat-card:not(.skel-card)', { timeout: 15_000 })
check(
  'its materials are still readable after a reload',
  (await cardIds()).length >= 4,
  `${(await cardIds()).length} cards`,
)

/* ── 3. switch between batches ────────────────────────────────────────────── */

console.log('\n== switch batches ==')
const before = await cardIds()
// The mock seeds two historical batches so the submitted / archived chips are not empty.
await page.locator('.hist-row[data-batch="web-seed-submitted"]').click()
await page.waitForSelector('.results-bar.readonly', { timeout: 15_000 })
await page.waitForTimeout(500)
await shot('03-switched-to-submitted')

const after = await cardIds()
check(
  'switching re-renders the card area',
  after.length > 0 && after.join() !== before.join(),
  `${before.length} cards -> ${after.length} cards`,
)
check(
  'the newly selected row is highlighted and the old one is not',
  (await page.locator('.hist-row[data-batch="web-seed-submitted"].active').count()) === 1 &&
    (await page.locator(`.hist-row[data-batch="${liveBatchId}"].active`).count()) === 0,
)

/* ── 4. read-only, asserted by clicking ───────────────────────────────────── */

console.log('\n== read-only (已提交) ==')
const checks = page.locator('.mat-card .select-check')
const total = await checks.count()
const disabled = await page.evaluate(
  () => [...document.querySelectorAll('.mat-card .select-check')].filter((b) => b.disabled).length,
)
check('every selection control is disabled', total > 0 && disabled === total, `${disabled}/${total}`)

// Click them anyway. `force` because Playwright refuses to click a disabled control, and the point
// is to prove that even a click that lands changes nothing.
for (let i = 0; i < total; i += 1) {
  await checks.nth(i).click({ force: true }).catch(() => {})
}
await page.waitForTimeout(300)
check(
  'clicking them selects nothing',
  (await page.locator('.mat-card.selected').count()) === 0,
)
check(
  'there is no 提交审核 button on a read-only batch',
  (await page.locator('button:has-text("提交审核")').count()) === 0,
)
check(
  'the read-only bar explains why',
  (await page.locator('.results-bar.readonly').innerText()).includes('不能修改选稿'),
)
// 可看材料、可试听: the reader link is what leads to 生成音频, so it must still be there.
check(
  'the 阅读全文 links are still there',
  (await page.locator('.mat-actions a').count()) === total,
)

console.log('\n== read-only (已归档) ==')
await page.locator('.hist-row[data-batch="web-seed-archived"]').click()
await page.waitForSelector('.results-bar.readonly', { timeout: 15_000 })
await page.waitForTimeout(500)
await shot('04-switched-to-archived')
const archivedBanner = await page.locator('.banner-info').innerText()
check('the archived batch says why it is read-only', archivedBanner.includes('已归档'), archivedBanner.replace(/\n/g, ' | '))

/* ── 5. 试听 on a >24h-old batch ──────────────────────────────────────────── */

console.log('\n== 试听 on the archived batch ==')
// The archived seed is deliberately older than the 24h candidate window, which is the case the
// backend had to be checked for: `REGISTRY.get` reads the candidate object directly and applies no
// TTL, so playback still resolves. Here we only confirm the UI offers the button rather than
// hiding it or showing one that fails.
const firstReader = await page.locator('.mat-actions a').first().getAttribute('href')
await page.goto(`${BASE}${firstReader}`, { waitUntil: 'networkidle' })
await page.waitForSelector('.reader, .audio-cta', { timeout: 15_000 })
await page.waitForTimeout(400)
await shot('05-archived-material-reader')
const generate = page.locator('button:has-text("生成音频")')
check('生成音频 is offered on an archived batch material', (await generate.count()) > 0)
if ((await generate.count()) > 0) {
  await generate.first().click()
  await page.waitForSelector('.audio-player, .audio-cta.busy', { timeout: 30_000 })
  await page.waitForTimeout(1500)
  await shot('06-archived-material-audio')
  const audioText = await page.locator('.audio-player, .audio-cta').first().innerText()
  check('audio synthesis starts and does not error', !audioText.includes('失败'), audioText.replace(/\n/g, ' | '))
}

/* ── 6. search and chips ──────────────────────────────────────────────────── */

console.log('\n== search and status chips ==')
await page.goto(`${BASE}/batches/${liveBatchId}`, { waitUntil: 'networkidle' })
await page.waitForSelector('.hist-row', { timeout: 15_000 })
const allRows = await rowIds()

await page.locator('.hist-search').fill('酒店')
await page.waitForTimeout(400)
await shot('07-search-hotel')
const searched = await rowIds()
check(
  'searching 酒店 narrows the list',
  searched.length > 0 && searched.length < allRows.length,
  `${allRows.length} -> ${searched.length}`,
)
check(
  'the hotel batch is among the results',
  searched.includes(liveBatchId),
  searched.join(', '),
)

await page.locator('.hist-search').fill('')
await page.waitForTimeout(300)
check('clearing the search restores the list', (await rowIds()).length === allRows.length)

const chip = (label) => page.locator(`.hist-chips button:has-text("${label}")`)
await chip('已归档').click()
await page.waitForTimeout(400)
await shot('08-chip-archived')
const archivedOnly = await rowIds()
check(
  'the 已归档 chip shows only archived batches',
  archivedOnly.length > 0 && archivedOnly.every((id) => id.includes('archived')),
  archivedOnly.join(', '),
)

await chip('待选稿').click()
await page.waitForTimeout(400)
const pendingOnly = await rowIds()
check(
  'the 待选稿 chip shows the fresh batch',
  pendingOnly.includes(liveBatchId),
  pendingOnly.join(', '),
)

await chip('全部').click()
await page.waitForTimeout(300)
check('全部 restores every batch', (await rowIds()).length === allRows.length)

/* ── 7. collapse ──────────────────────────────────────────────────────────── */

console.log('\n== collapse ==')
await page.locator('[aria-label="收起历史批次面板"]').click()
await page.waitForSelector('.hist-rail', { timeout: 5_000 })
await shot('09-collapsed')
check('collapsing leaves a narrow icon rail', (await page.locator('.hist-panel').count()) === 0)

await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(600)
check('the collapsed state survives a reload', (await page.locator('.hist-rail').count()) === 1)

await page.locator('[aria-label="展开历史批次面板"]').click()
await page.waitForSelector('.hist-panel', { timeout: 5_000 })
check('it expands again', (await page.locator('.hist-panel').count()) === 1)

/* ── done ─────────────────────────────────────────────────────────────────── */

console.log(`\nconsole errors: ${errors.length}`)
for (const e of errors.slice(0, 10)) console.log(`  ${e}`)

await browser.close()

if (failures.length > 0 || errors.length > 0) {
  console.log(`\nFAILED: ${failures.length} assertion(s), ${errors.length} console error(s)`)
  process.exit(1)
}
console.log(`\nall assertions passed. shots in ${OUT}`)
