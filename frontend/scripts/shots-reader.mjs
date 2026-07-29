// Reader-page verification against the mock backend.
//   VITE_MOCK=1 npm run dev
//   node scripts/shots-reader.mjs
//
// Covers the four things this round changed on the reader: the speaker labels, the 生成音频 button
// (and that it does NOT discard the sibling), the exam-point summary, and the results-page progress
// strip + 阅读全文 button.
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { pickScenarios, submitBatch } from './pickScenarios.mjs'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-reader'
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
  await page.screenshot({ path: `${OUT}/${name}.png`, ...opts })
  console.log(`  shot ${name}`)
}
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) errors.push(`CHECK FAILED: ${label} ${detail}`)
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await pickScenarios(page, ['booking-hotel'], 2)
await submitBatch(page)

// Progress strip while the batch is still running: the point is that M/N appears ONCE.
await page.waitForSelector('.results-progress')
const strip = await page.locator('.results-stats').innerText()
console.log(`  progress strip: ${JSON.stringify(strip)}`)
const mnCount = (strip.match(/\d+\s*\/\s*\d+/g) ?? []).length
check('progress strip states M/N exactly once', mnCount === 1, `found ${mnCount}`)
await shot('01-progress-strip')

await page.waitForFunction(
  () => document.querySelectorAll('.mat-card:not(.skel-card):not(.err-card)').length >= 2,
  null,
  { timeout: 60_000 },
)
await shot('02-results', { fullPage: true })

const readBtn = page.locator('.mat-actions .btn-card').first()
check('阅读全文 uses the prototype card-action style', (await readBtn.count()) === 1)
console.log(`  read button text: ${JSON.stringify(await readBtn.innerText())}`)

const ids = await page.evaluate(() =>
  [...document.querySelectorAll('.mat-card[data-material]')].map((c) => c.dataset.material),
)
console.log(`  materials: ${ids.join(', ')}`)

await readBtn.click()
await page.waitForURL(/\/materials\//)
await page.waitForSelector('.exam-points')

/* ── speaker labels ────────────────────────────────────────────────────────── */
const roles = await page.evaluate(() =>
  [...document.querySelectorAll('.turn')].slice(0, 6).map((t) => ({
    turn: t.dataset.turn,
    role: t.querySelector('.role')?.innerText.replace(/\n/g, ' '),
  })),
)
console.log(`  roles: ${JSON.stringify(roles)}`)
check(
  'narrator turn is labelled speaker1 + 旁白',
  roles[0].role.includes('speaker1') && roles[0].role.includes('旁白'),
  roles[0].role,
)
check(
  'dialogue turns are labelled speaker2/speaker3',
  roles.slice(1).every((r) => /speaker[23]/.test(r.role)),
)
const body = await page.locator('body').innerText()
for (const invented of ['信息持有方', '需求方']) {
  check(`invented role name ${invented} is gone`, !body.includes(invented))
}

/* ── the caption ───────────────────────────────────────────────────────────── */
const caption = await page.locator('.strip-title').innerText()
console.log(`  caption: ${JSON.stringify(caption)}`)
check('caption is rewritten', caption.includes('点位不作避让，重叠即原文中相邻'))
check('colloquial caption is gone', !caption.includes('就是原文里真的'))

/* ── removed panels ───────────────────────────────────────────────────────── */
for (const removed of [
  '评价指出的问题',
  '无缺陷记录',
  '提示（不影响采用）',
  'dialogue words outside',
  '盲读复核',
  '尚未就绪',
  'list_scenarios',
]) {
  check(`removed: ${removed}`, !body.includes(removed))
}
check('篇幅 metrics kept', body.includes('篇幅'))

/* ── exam-point summary ───────────────────────────────────────────────────── */
const ep = await page.locator('.exam-points').innerText()
console.log(`  exam points: ${JSON.stringify(ep)}`)
for (const label of ['考点小结', '拼读', '先说后改', '同义替换', '有复述确认', '信息点类型']) {
  check(`exam point block: ${label}`, ep.includes(label))
}
await shot('03-reader-top')

/* ── 生成音频 ─────────────────────────────────────────────────────────────── */
const gen = page.locator('button:has-text("生成音频")')
check('生成音频 button is present before any audio exists', (await gen.count()) === 1)
await shot('04-audio-button')
await gen.click()
await page.waitForSelector('.audio-cta.busy')
console.log(`  busy panel: ${JSON.stringify(await page.locator('.audio-cta').innerText())}`)
await page.waitForTimeout(1500)
await shot('05-synthesizing')

await page.waitForSelector('.player', { timeout: 60_000 })
console.log(`  player: ${JSON.stringify((await page.locator('.player').innerText()).slice(0, 160))}`)
check('player replaces the button once ready', true)
check(
  'no synthetic-audio warning on the player',
  !(await page.locator('.player').innerText()).includes('非真实语音'),
)
await shot('06-player')

/* ── the sibling survived the preview ─────────────────────────────────────── */
// The whole reason preview_audio is not `select`: listening to one candidate must not delete the
// other. A raw fetch would not work here — the mock replaces the transport inside api/http.ts, not
// window.fetch — so this goes back to the results grid, which renders one card per live candidate.
await page.goBack({ waitUntil: 'networkidle' })
await page.waitForSelector('.mat-card[data-material]')
const stillThere = await page.evaluate(() =>
  [...document.querySelectorAll('.mat-card[data-material]')].map((c) => c.dataset.material),
)
console.log(`  materials after preview: ${stillThere.join(', ')}`)
check(
  'both candidates survive a preview (preview does not discard the sibling)',
  stillThere.length === ids.length,
  `${stillThere.length} vs ${ids.length}`,
)
await shot('07-siblings-intact', { fullPage: true })

/* ── selecting the previewed material is honest about not re-billing ─────── */
await page.goto(`${BASE}/compare/booking-hotel`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
await page.locator('button:has-text("选定 候选 A")').first().click()
await page.waitForSelector('.dialog')
// 确认框先说「正在确认」再改口，所以等它查完——费用那一句是这个框唯一会影响决定的信息。
await page.waitForFunction(
  () => !document.querySelector('.dialog').innerText.includes('正在确认'),
  null,
  { timeout: 10_000 },
)
const dialog = await page.locator('.dialog').innerText()
console.log(`  select dialog: ${JSON.stringify(dialog)}`)
check('dialog says the existing audio is reused, not re-synthesised', dialog.includes('不会重新合成'))
check('dialog no longer threatens a charge for a previewed material', !dialog.includes('产生费用'))
await shot('08-select-dialog-previewed')

console.log(errors.length === 0 ? '\nALL CHECKS PASSED' : `\nPROBLEMS:\n${errors.join('\n')}`)
await browser.close()
process.exit(errors.length === 0 ? 0 : 1)
