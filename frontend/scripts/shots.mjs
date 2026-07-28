// Browser walkthrough + screenshots against the mock backend.
//   VITE_MOCK=1 npm run dev
//   node scripts/shots.mjs
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'

const OUT = '/tmp/shots'
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

const shot = async (name, opts = {}) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, ...opts })
  console.log(`  shot ${name}`)
}

async function ctx2Run() {
  const ctx2 = await browser.newContext({ viewport: { width: 1480, height: 1000 } })
  const p2 = await ctx2.newPage()
  p2.on('console', (m) => {
    if (m.type() === 'error') errors.push(`[audio-ctx] ${m.text()}`)
  })
  p2.on('pageerror', (e) => errors.push(`[audio-ctx] pageerror: ${e.message}`))
  const shot2 = async (name, opts = {}) => {
    await p2.screenshot({ path: `${OUT}/${name}.png`, ...opts })
    console.log(`  shot ${name}`)
  }

  await p2.goto(BASE, { waitUntil: 'networkidle' })
  await p2.locator('.scn-row:has-text("booking-hotel") input[type=checkbox]').first().check()
  await p2.locator('.summary-bar button').click()
  await p2.waitForURL(/\/batches\//)
  await p2.waitForFunction(
    () => document.querySelectorAll('.mat-card .flag-good, .mat-card .flag-bad').length >= 2,
    null,
    { timeout: 40000 },
  )
  await p2.goto(`${BASE}/compare/booking-hotel`, { waitUntil: 'networkidle' })
  await p2.waitForTimeout(1200)

  await p2.locator('button:has-text("→ 合成语音")').first().click()
  await p2.waitForSelector('.dialog')
  await shot2('17-select-dialog')
  console.log(`   confirm disabled before ack = ${await p2.locator('.dialog .btn-danger').isDisabled()}`)
  await p2.locator('.dialog input[type=checkbox]').check()
  await p2.locator('.dialog .btn-danger').click()
  await p2.waitForTimeout(900)
  const discarded = await p2.locator('.banner-good:has-text("已选定")').count()
  console.log(`   selected banner=${discarded}`)
  await shot2('18-selected')

  await p2.locator('a:has-text("打开选定材料")').click()
  await p2.waitForTimeout(2500)
  console.log(`   synthesis progress banner = ${await p2.locator('.banner:has-text("语音合成中")').count()}`)
  await shot2('19-material-synthesizing')

  await p2.waitForSelector('.player', { timeout: 60000 })
  await p2.waitForTimeout(400)
  await shot2('20-material-player')
  const total = await p2.locator('.player .mono').first().innerText()
  console.log(`   player ready, timeline = ${total}`)
  console.log(`   ${(await p2.locator('.player .muted').last().innerText()).replace(/\n/g, ' ')}`)
  const skipFlag = await p2.locator('.player .flag-warn').count()
  console.log(`   url:null skip notice = ${skipFlag}`)

  // Full playback. Turn 0 is the ~60s exam narration, so start from turn 1 to
  // observe the segment-to-segment handoff within a reasonable wall time.
  const t1 = p2.locator('.turn').nth(1)
  await t1.hover()
  await t1.locator('button:has-text("此句")').click()
  const seq = []
  for (let i = 0; i < 9; i++) {
    await p2.waitForTimeout(1700)
    const cur = await p2.evaluate(
      () => document.querySelector('.turn.playing')?.getAttribute('data-turn') ?? null,
    )
    if (seq[seq.length - 1] !== cur) seq.push(cur)
  }
  console.log(`   playback turn sequence: ${seq.join(' → ')}`)
  console.log(`   position now = ${await p2.locator('.player .mono').first().innerText()}`)
  console.log(`   ${(await p2.locator('.player .muted').last().innerText()).replace(/\n/g, ' ')}`)
  await shot2('21-playing')

  // per-turn
  await p2.locator('.player button:has-text("暂停")').click()
  const t = p2.locator('.turn').nth(12)
  await t.hover()
  const btn = t.locator('button:has-text("此句")')
  if (await btn.count()) {
    await btn.click()
    await p2.waitForTimeout(1500)
    console.log(`   per-turn play → playing turn = ${await p2.evaluate(() => document.querySelector('.turn.playing')?.getAttribute('data-turn'))}`)
    await shot2('22-per-turn-play')
  }
  // unplayable turn 30 must be marked
  console.log(`   turn 30 marked unplayable = ${await p2.locator('.turn[data-turn="30"] .flag-bad').count()}`)
  await shot2('23-material-full', { fullPage: true })
  await ctx2.close()
}

// ── 1. scenario selection ────────────────────────────────────────────────────
console.log('1. scenario selection')
await page.goto(BASE, { waitUntil: 'networkidle' })
await page.waitForSelector('.scn-row')
const catCount = await page.locator('.scn-cat').count()
const scnCount = await page.locator('.scn-row input[type=checkbox]').count()
console.log(`   categories=${catCount} scenarios=${scnCount}`)
await shot('01-scenario-empty')

// pick 3 scenarios x2 = 6, at the limit
for (const key of ['booking-hotel', 'booking-car-rental', 'accommodation-rental']) {
  await page.locator(`.scn-row:has-text("${key}") input[type=checkbox]`).first().check()
}
await shot('02-scenario-at-limit')
const submitDisabledAt6 = await page.locator('.summary-bar button').isDisabled()
console.log(`   at 6: submit disabled = ${submitDisabledAt6}`)

// push to 7 → must be blocked BEFORE submit
await page.locator('.scn-row:has-text("booking-hotel") .stepper button:last-child').first().click()
await page.waitForTimeout(150)
const overText = await page.locator('.summary-bar').innerText()
const submitDisabledAt7 = await page.locator('.summary-bar button').isDisabled()
console.log(`   at 7: submit disabled = ${submitDisabledAt7}`)
console.log(`   summary: ${overText.replace(/\n/g, ' | ')}`)
await shot('03-scenario-over-limit')

// back to 6 and add a custom scenario alongside (drop one checkbox first)
await page.locator('.scn-row:has-text("booking-hotel") .stepper button:first-child').first().click()
await page.locator('.scn-row:has-text("accommodation-rental") input[type=checkbox]').first().uncheck()
await page.locator('textarea').fill('A student phones a bike shop about repairing a bicycle.')
await page.waitForTimeout(150)
await shot('04-scenario-custom')

// ── 2. SSE progressive arrival ───────────────────────────────────────────────
console.log('2. SSE batch progress')
await page.locator('.summary-bar button').click()
await page.waitForURL(/\/batches\//)
await page.waitForTimeout(1200)
const early = await page.locator('.mat-card').count()
const doneEarly = await page.locator('.mat-card .flag-good, .mat-card .flag-bad').count()
console.log(`   after ~1.2s: cards=${early} finished=${doneEarly}`)
await shot('05-batch-partial')

await page.waitForTimeout(4000)
const midDone = await page.locator('.mat-card .flag-good, .mat-card .flag-bad').count()
console.log(`   after ~5s: finished=${midDone}`)
await shot('06-batch-more')

await page.waitForFunction(
  () => document.querySelectorAll('.mat-card .flag-good, .mat-card .flag-bad').length >= 6,
  null,
  { timeout: 30000 },
)
const allDone = await page.locator('.mat-card .flag-good, .mat-card .flag-bad').count()
console.log(`   all arrived: finished=${allDone}`)
await shot('07-batch-done')

// refresh mid-batch → must return to the same batch
const batchUrl = page.url()
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
const afterReload = await page.locator('.mat-card').count()
console.log(`   after reload: cards=${afterReload} url=${page.url() === batchUrl}`)
await shot('08-batch-after-reload')

// ── 3. fixture gallery: balanced vs clustered ────────────────────────────────
console.log('3. fixture gallery')
await page.goto(`${BASE}/gallery`, { waitUntil: 'networkidle' })
await page.waitForSelector('.strip-axis')
await shot('09-gallery-strips', { fullPage: true })
await page.locator('.strip').first().screenshot({ path: `${OUT}/10-strip-balanced.png` })
await page.locator('.strip').nth(1).screenshot({ path: `${OUT}/11-strip-clustered.png` })

// metric table values
const rows = await page.locator('.panel:has-text("指标对照") tbody tr').allInnerTexts()
console.log('   metric table:')
for (const r of rows) console.log(`     ${r.replace(/\n/g, ' | ')}`)

// ── 4. full reader, clustered then balanced ──────────────────────────────────
console.log('4. reader (annotation column)')
await page.locator('button:has-text("完整阅读态")').click()
await page.waitForSelector('.ann-card')
await page.waitForTimeout(700)
const clusterCards = await page.locator('.ann-card.cluster').count()
const clusterHead = clusterCards
  ? await page.locator('.ann-card.cluster .ann-cluster-head').first().innerText()
  : '(none)'
console.log(`   clustered fixture: cluster cards=${clusterCards} head="${clusterHead}"`)
await shot('12-reader-clustered', { fullPage: true })

// overlap check in the live DOM
const overlap = await page.evaluate(() => {
  const cards = [...document.querySelectorAll('.ann-card')].map((el) => el.getBoundingClientRect())
  const bad = []
  for (let i = 0; i < cards.length; i++)
    for (let j = i + 1; j < cards.length; j++) {
      const a = cards[i], b = cards[j]
      if (a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5) bad.push([i, j])
    }
  return { count: cards.length, overlaps: bad }
})
console.log(`   live DOM: ${overlap.count} cards, overlaps=${JSON.stringify(overlap.overlaps)}`)

await page.locator('button:has-text("均衡")').first().click()
await page.waitForTimeout(700)
const balClusters = await page.locator('.ann-card.cluster').count()
const balCards = await page.locator('.ann-card').count()
console.log(`   balanced fixture: cards=${balCards} cluster cards=${balClusters}`)
await shot('13-reader-balanced', { fullPage: true })
const overlapBal = await page.evaluate(() => {
  const cards = [...document.querySelectorAll('.ann-card')].map((el) => el.getBoundingClientRect())
  const bad = []
  for (let i = 0; i < cards.length; i++)
    for (let j = i + 1; j < cards.length; j++) {
      const a = cards[i], b = cards[j]
      if (a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5) bad.push([i, j])
    }
  return bad
})
console.log(`   balanced overlaps=${JSON.stringify(overlapBal)}`)

// anchor mismatch fixture
await page.locator('button:has-text("锚点失配")').click()
await page.waitForTimeout(600)
const bannerVisible = await page.locator('.banner-bad:has-text("旁注可能错位")').count()
const redFlag = await page.locator('.ann-card .flag-bad:has-text("锚点失配")').count()
console.log(`   anchorMismatch: banner=${bannerVisible} red flags=${redFlag}`)
await shot('14-reader-anchor-mismatch')

// ── 5. compare view ──────────────────────────────────────────────────────────
console.log('5. compare view')
await page.goto(`${BASE}/compare/booking-hotel`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
const audioControls = await page.locator('.player, audio, button:has-text("播放")').count()
console.log(`   audio controls present in compare = ${audioControls}`)
const summary = await page.locator('.panel:has-text("差异摘要")').count()
if (summary) {
  console.log(`   summary: ${(await page.locator('.panel:has-text("差异摘要") > div').first().innerText()).slice(0, 200)}`)
}
await shot('15-compare', { fullPage: true })

// finding jump
const jumpBtn = page.locator('button:has-text("→ turn")').first()
if (await jumpBtn.count()) {
  const label = await jumpBtn.innerText()
  await jumpBtn.click()
  await page.waitForTimeout(900)
  const flashed = await page.locator('.turn.selected, .turn.flash').count()
  console.log(`   clicked "${label}" → highlighted turns=${flashed}`)
  await shot('16-compare-finding-jump')
}

// ── 6. select + audio ────────────────────────────────────────────────────────
// Fresh context: the walkthrough above already selected/played in this session,
// and select is irreversible by design, so the flow needs a clean world.
console.log('6. select → synthesis → playback (fresh context)')
await ctx2Run()

// ── 7. quarantine ────────────────────────────────────────────────────────────
console.log('7. quarantine')
await page.goto(`${BASE}/quarantine`, { waitUntil: 'networkidle' })
await page.waitForTimeout(900)
const qRows = await page.locator('.q-row').count()
const noMaterial = await page.locator('.flag-warn:has-text("无可选材料")').count()
console.log(`   quarantined rows=${qRows}, "no candidates" notices=${noMaterial}`)
await shot('24-quarantine', { fullPage: true })

console.log('\nconsole errors:', errors.length ? errors : 'none')
await browser.close()
