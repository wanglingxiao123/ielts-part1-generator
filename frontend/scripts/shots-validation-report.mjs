// Browser verification of this round's three fixes, against the mock backend.
//
//   npm run dev:mock
//   node scripts/shots-validation-report.mjs
//
// jsdom already covers the component logic. What only a real browser can show is
// whether the PAGE reads correctly to the client, so this script asserts the
// three user-visible outcomes and screenshots each:
//
//   ① no 生成异常 empty card anywhere; a material with validator findings is
//      delivered, and the findings appear on the READER page, never on the card.
//   ② 生成音频 (the 试听 path) is called with a production-shaped material_id and
//      does not fail — a material the UI shows stays operable.
//   ③ the exam-point summary's 听不出来 block reflects the fixed cross-check.
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-validation-report'
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

const failures = []
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures.push(label)
}
const shot = async (name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  console.log(`  shot ${name}`)
}

/** Every request the frontend makes to a candidate-resolving endpoint. */
const invocations = []
await page.route('**/invocations', async (route) => {
  try {
    invocations.push(JSON.parse(route.request().postData() ?? '{}'))
  } catch {
    /* not JSON */
  }
  await route.continue()
})

await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await page.waitForSelector('.scn-chip')

for (const title of ['酒店预订', '租房咨询']) {
  await page.locator(`.scn-chip:has-text("${title}")`).first().click()
}
await page.locator('.summary-bar button.btn-primary').click()
await page.waitForURL(/\/batches\//)

console.log('\n① 结果页：没有「生成异常」空卡片')
await page.waitForSelector('.mat-card:not(.skel-card)', { timeout: 60_000 })
await page.waitForSelector('.results-bar', { timeout: 60_000 })
await page.waitForFunction(() => document.querySelectorAll('.skel-card').length === 0, null, {
  timeout: 60_000,
})
await page.waitForTimeout(600)
await shot('01-results-no-error-card')

const body = await page.evaluate(() => document.body.innerText)
check('no 生成异常 card', !body.includes('生成异常'))
check('no .err-card node', (await page.locator('.err-card').count()) === 0)
// The card layout the client specified: no evaluation prose on it.
const cardText = await page.locator('.mat-card').first().innerText()
for (const prose of ['校验', '缺陷', '评价环节', '听不出来']) {
  check(`card carries no 「${prose}」`, !cardText.includes(prose))
}
const realCards = await page.locator('.mat-card:not(.skel-card)').count()
check('every requested material rendered as a real card', realCards === 4, `${realCards}/4`)

console.log('\n② 阅读页：试听（生成音频）用的是真 material_id')
const materialId = await page.locator('.mat-card').first().getAttribute('data-material')
check(
  'material_id has the backend shape YYYYMMDD-<scenario>-<hash>',
  /^\d{8}-[a-z0-9-]+-[0-9a-z]{8}$/.test(materialId ?? ''),
  materialId ?? '(none)',
)
check('material_id is not a slot key', !(materialId ?? '::').includes('::'))

await page.locator('.mat-card').first().locator('a:has-text("阅读全文")').click()
await page.waitForSelector('.exam-points', { timeout: 30_000 })
await page.waitForTimeout(400)
await shot('02-reader-before-audio')

const audioButton = page.locator('button:has-text("生成音频")')
check('reader page offers 生成音频', (await audioButton.count()) > 0)
await audioButton.first().click()
// The failure mode being verified: "no candidate '<id>'; it was never offered…".
await page.waitForTimeout(1500)
await shot('03-reader-audio-requested')
const afterClick = await page.evaluate(() => document.body.innerText)
check('no "no candidate" error', !afterClick.includes('no candidate'))
check('no MATERIAL_NOT_FOUND', !afterClick.includes('材料不存在'))
const previews = invocations.filter((i) => i.action === 'preview_audio')
check(
  'preview_audio was sent a production-shaped id',
  previews.length === 0 ||
    previews.every((p) => /^\d{8}-/.test(String(p.material_id)) && !String(p.material_id).includes('::')),
  JSON.stringify(previews.map((p) => p.material_id)),
)
check(
  'audio progressed instead of erroring',
  afterClick.includes('正在生成音频') || afterClick.includes('播放') || afterClick.includes('音频'),
)

console.log('\n③ 阅读页：考点小结的「听不出来」')
const panel = await page.locator('.exam-points').innerText()
check('考点小结 panel is present', panel.includes('考点小结'))
for (const label of ['拼读', '先说后改', '同义替换']) {
  check(`考点 「${label}」 is named`, panel.includes(label))
}
// The reader page is where evaluation prose belongs — the layering rule's other half.
check(
  'evaluation prose IS allowed here',
  panel.includes('听不出来') || panel.includes('有复述确认'),
)

console.log('\n① (b) 带校验意见的材料：意见在阅读页，卡片上没有')
// The seeded standalone materials exercise the flawed-but-selectable path.
await page.goto(`${BASE}/materials/20260101-booking-car-rental-seed0001`, {
  waitUntil: 'networkidle',
})
await page.waitForSelector('.exam-points', { timeout: 30_000 })
await shot('04-reader-flawed-material')
const flawed = await page.evaluate(() => document.body.innerText)
check('a flawed material is still fully readable', flawed.includes('考点小结'))
check('and still operable', (await page.locator('button:has-text("生成音频")').count()) > 0)

console.log('\n① (c) 结构校验意见面板（校验从门卫改成质检报告之后的那一类材料）')
await page.goto(`${BASE}/materials/20260101-accommodation-rental-seed0004`, {
  waitUntil: 'networkidle',
})
await page.waitForSelector('.vn-list', { timeout: 30_000 })
await shot('05-reader-validation-notes')
const notes = await page.locator('.vn-list').innerText()
check('校验意见面板 renders', notes.length > 0)
check('三条意见都在', (await page.locator('.vn-list li').count()) === 3)
// The validator's English threshold prose must not reach the page — that is why the previous
// round deleted 「提示（不影响采用）」.
for (const raw of ['blueprint.items', 'turn_index', 'dialogue words outside', 'confirmed items']) {
  check(`raw validator text 「${raw}」 is translated away`, !notes.includes(raw))
}
// Framing: "look here", not "this is broken, you fix it".
check(
  'framed as 待核对 rather than as a verdict',
  (await page.evaluate(() => document.body.innerText)).includes('材料本身完整可用'),
)
for (const blaming of ['缺陷', '不合格', '请修改']) {
  check(`no blaming word 「${blaming}」`, !notes.includes(blaming))
}
check('point numbers are jumpable', (await page.locator('.vn-list .ep-num').count()) > 0)
check(
  'and it is still operable',
  (await page.locator('button:has-text("生成音频")').count()) > 0,
)

console.log(`\nconsole errors: ${errors.length}`)
for (const e of errors.slice(0, 8)) console.log(`  ${e}`)

await browser.close()
console.log(`\n${failures.length === 0 ? 'ALL BROWSER CHECKS PASSED' : `${failures.length} FAILED`}`)
for (const f of failures) console.log(`  - ${f}`)
process.exit(failures.length === 0 && errors.length === 0 ? 0 : 1)
