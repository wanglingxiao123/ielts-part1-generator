// Browser walkthrough + screenshots against the REAL backend container.
//
//   docker run -d --name ielts-be -p 8080:8080 … ielts-backend:dev
//   npm run dev                       # no VITE_MOCK
//   node scripts/shots-real.mjs
//
// A real generation is 150-225s per material and can fail validation three times
// and give up, so the waits here are minutes, not seconds, and a failed material
// is a legitimate observed outcome rather than a script error.
import { mkdir, writeFile } from 'node:fs/promises'
import { chromium } from 'playwright'
import { pickScenarios, submitBatch } from './pickScenarios.mjs'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-real'
const BASE = 'http://localhost:5173'
const SCENARIOS = (process.env.SHOT_SCENARIOS ?? 'daily-driving-lessons,community-environment')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)
const COUNT = Number(process.env.SHOT_COUNT ?? '1')
const BATCH_TIMEOUT_MS = Number(process.env.SHOT_TIMEOUT_MS ?? String(16 * 60_000))

await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1000 } })
const page = await ctx.newPage()
const errors = []
const log = []
page.on('console', (m) => {
  const text = `[${m.type()}] ${m.text()}`
  log.push(text)
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

const shot = async (name, opts = {}) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, ...opts })
  console.log(`  shot ${name}`)
}

const note = (msg) => {
  console.log(msg)
  log.push(`### ${msg}`)
}

note(`scenarios=${SCENARIOS.join(',')} count=${COUNT}`)

await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await shot('01-scenario-select')

// One global 每场景生成数量 now, so COUNT is set once rather than stepped per row.
// Real generation is expensive, hence the low default; a 2-candidate comparison
// needs COUNT=2.
await pickScenarios(page, SCENARIOS, COUNT)
note(`estimate shown = ${(await page.locator('.scn-bar-left').innerText()).replace(/\n/g, ' | ')}`)
await shot('02-scenarios-checked')

await submitBatch(page)
const batchUrl = page.url()
note(`batch page ${batchUrl}`)

// The skeleton grid is up immediately — the results-page structure IS the loading
// state, so this also proves the shape was known before any material arrived.
await page.waitForSelector('.skel-card', { timeout: 30_000 })
note(`skeletons before first material = ${await page.locator('.skel-card').count()}`)
await shot('02b-skeletons')

// First stage event proves the SSE translation is live.
await page
  .waitForFunction(
    () => document.querySelector('.phase-step.active, .phase-step.done') !== null,
    null,
    { timeout: 120_000 },
  )
  .catch(() => note('WARN: no phase progress within 120s'))
await shot('03-batch-generating')

// Progressive arrival: capture as each material resolves.
const expected = SCENARIOS.length * COUNT
let seen = 0
const deadline = Date.now() + BATCH_TIMEOUT_MS
while (seen < expected && Date.now() < deadline) {
  const resolved = await page.evaluate(
    () => document.querySelectorAll('.mat-card:not(.skel-card):not(.err-card)').length,
  )
  if (resolved > seen) {
    seen = resolved
    note(`material resolved: ${seen}/${expected} at ${new Date().toISOString()}`)
    await shot(`04-arrival-${seen}`)
  }
  const done = await page.evaluate(() => document.body.innerText.includes('batch_done'))
  if (done) break
  await page.waitForTimeout(3000)
}
await shot('05-batch-final', { fullPage: true })

const cards = await page.evaluate(() =>
  [...document.querySelectorAll('.mat-card')].map((c) => c.innerText.replace(/\s+/g, ' ')),
)
for (const c of cards) note(`card: ${c}`)

// Open the first readable material via in-app navigation (see the compare note
// below for why goto is avoided): annotations driven by the real blueprint.
const readLink = page.locator('.mat-card a:has-text("阅读")').first()
if ((await readLink.count()) > 0) {
  await readLink.click()
  await page.waitForURL(/\/materials\//, { timeout: 20_000 })
  await page.waitForTimeout(2500)
  await shot('06-material-reader', { fullPage: true })

  // The alignment claim, checked in the DOM rather than asserted in prose:
  // every <mark> in a turn must sit inside the turn the blueprint anchored it to.
  const alignment = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('[data-turn]')]
    const marks = rows.flatMap((row) => {
      const turnIndex = Number(row.getAttribute('data-turn'))
      return [...row.querySelectorAll('mark')].map((m) => ({
        turnIndex,
        text: m.textContent?.trim() ?? '',
      }))
    })
    const banner = document.body.innerText.includes('旁注可能错位')
    return { markCount: marks.length, marks: marks.slice(0, 20), anchorBanner: banner }
  })
  note(`highlight marks: ${alignment.markCount}, anchor-mismatch banner: ${alignment.anchorBanner}`)
  for (const m of alignment.marks) note(`  turn ${m.turnIndex} ⟦${m.text}⟧`)

  await page.locator('.strip, .dist-strip').first().screenshot({ path: `${OUT}/07-distribution.png` })
    .catch(() => note('WARN: distribution strip selector missed'))

  const crossCheck = await page.evaluate(() => {
    const h = [...document.querySelectorAll('h3')].find((x) => x.textContent?.includes('盲测对照'))
    return h?.parentElement?.innerText.replace(/\s+/g, ' ') ?? null
  })
  note(`cross_check panel: ${crossCheck}`)

  const audioNotice = await page.evaluate(() => {
    const b = [...document.querySelectorAll('.banner')].find((x) =>
      x.innerText.includes('音频'),
    )
    return b?.innerText.replace(/\s+/g, ' ') ?? null
  })
  note(`audio notice: ${audioNotice}`)
  await shot('08-audio-notice')
}

// Comparison view. MUST be reached by in-app navigation, never page.goto: the
// batch lives in module scope because the backend has no batch registry, so a
// hard load drops it. That is a real property of this backend, not a script
// detail — a reviewer who pastes a /compare URL gets an empty page.
const compareLink = page.locator('a:has-text("对比本场景")').first()
if ((await compareLink.count()) > 0) {
  await compareLink.click()
  await page.waitForURL(/\/compare\//, { timeout: 20_000 })
  await page.waitForTimeout(2500)
  await shot('09-compare', { fullPage: true })
  const body = await page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').slice(0, 900))
  note(`compare: ${body}`)
} else {
  note('WARN: no 对比本场景 link — fewer than 2 candidates in a single scenario')
}

// Selection: with the synthesis endpoint absent this must fail loudly, not
// silently claim success.
const selectBtn = page.locator('button:has-text("→ 合成语音")').first()
if ((await selectBtn.count()) > 0 && (await selectBtn.isEnabled())) {
  await selectBtn.click()
  await page.waitForSelector('.dialog', { timeout: 5000 })
  await page.locator('.dialog input[type=checkbox]').check()
  await shot('10-select-dialog')
  await page.locator('.dialog .btn-danger').click()
  await page.waitForTimeout(1500)
  await shot('11-after-select')
  const banner = await page.evaluate(() =>
    [...document.querySelectorAll('.banner')].map((b) => b.innerText.replace(/\s+/g, ' ')),
  )
  for (const b of banner) note(`after-select banner: ${b}`)

  // With flags.syntheticAudio on, selection produces local stand-in clips so the
  // player, timeline and turn-highlight sync stay verifiable while the real
  // synthesis endpoint is missing. The audio is NOT Polly output and the UI has
  // to say so — that label is what this block checks.
  const openSelected = page.locator('a:has-text("打开选定材料")')
  if ((await openSelected.count()) > 0) {
    await openSelected.click()
    await page.waitForTimeout(2500)
    await shot('14-synth-progress')
    const player = page.locator('.player')
    await player.waitFor({ timeout: 90_000 }).catch(() => note('WARN: player never appeared'))
    if ((await player.count()) > 0) {
      await shot('15-player')
      note(`synthetic label present: ${await page.locator('.player .flag-bad').count()}`)
      note(`timeline: ${await page.locator('.player .mono').first().innerText()}`)

      // Playback must highlight the turn the manifest's turn_index names. This is
      // the audio side of the same alignment claim the annotations make.
      const t1 = page.locator('.turn').nth(1)
      await t1.hover()
      await t1.locator('button:has-text("此句")').click()
      const seq = []
      for (let i = 0; i < 8; i += 1) {
        await page.waitForTimeout(1600)
        const cur = await page.evaluate(
          () => document.querySelector('.turn.playing')?.getAttribute('data-turn') ?? null,
        )
        if (seq[seq.length - 1] !== cur) seq.push(cur)
      }
      note(`playback turn sequence: ${seq.join(' → ')}`)
      await shot('16-playing')
    }
  }
}

await page.locator('nav a:has-text("隔离区")').click()
await page.waitForTimeout(1500)
await shot('12-quarantine', { fullPage: true })
note(
  `quarantine: ${await page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').slice(0, 400))}`,
)

// Reload behaviour: the batch is bound to the POST, so this must say so.
await page.goto(batchUrl, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
await shot('13-after-reload')
const reloadText = await page.evaluate(() =>
  document.body.innerText.replace(/\s+/g, ' ').slice(0, 500),
)
note(`after reload: ${reloadText}`)

note(`console errors: ${errors.length}`)
for (const e of errors) note(`  ERR ${e}`)

await writeFile(`${OUT}/log.txt`, log.join('\n'), 'utf8')
await browser.close()
console.log(`\nartifacts in ${OUT}`)
if (errors.length > 0) process.exitCode = 1
