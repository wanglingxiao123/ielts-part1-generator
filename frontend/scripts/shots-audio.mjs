// Focused audio verification against the REAL backend, with
// flags.syntheticAudio=true in public/config.json.
//
// Split out from shots-real.mjs because a real generation is 150-225s per
// material and can exhaust validation, so bundling the player checks behind a
// full batch means a stochastic generation failure costs the audio evidence too.
// This script generates ONE material and keeps retrying scenarios until one
// passes, then exercises the player.
import { mkdir, writeFile } from 'node:fs/promises'
import { chromium } from 'playwright'
import { pickScenarios, submitBatch } from './pickScenarios.mjs'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-audio'
const BASE = 'http://localhost:5173'
const CANDIDATES = (
  process.env.SHOT_SCENARIOS ?? 'daily-driving-lessons,community-environment,booking-hotel'
)
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)

await mkdir(OUT, { recursive: true })
const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1000 } })
const page = await ctx.newPage()
const errors = []
const log = []
page.on('console', (m) => {
  log.push(`[${m.type()}] ${m.text()}`)
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

const shot = async (name, opts = {}) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, ...opts })
  console.log(`  shot ${name}`)
}
const note = (m) => {
  console.log(m)
  log.push(`### ${m}`)
}

let readable = false
for (const key of CANDIDATES) {
  note(`attempting ${key}`)
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await signIn(page, BASE)
  // One set only: this script just needs a material to synthesise, and real
  // generation is expensive.
  await pickScenarios(page, [key], 1)
  await submitBatch(page)
  await page
    .waitForFunction(
      () => document.querySelectorAll('.mat-card:not(.skel-card):not(.err-card)').length >= 1,
      null,
      { timeout: 10 * 60_000 },
    )
    .catch(() => note('WARN: material never resolved'))
  const cards = await page.evaluate(() =>
    [...document.querySelectorAll('.mat-card')].map((c) => c.innerText.replace(/\s+/g, ' ')),
  )
  for (const c of cards) note(`card: ${c}`)
  if ((await page.locator('.mat-card a:has-text("阅读")').count()) > 0) {
    readable = true
    break
  }
  note(`${key} produced nothing readable; trying next scenario`)
}

if (!readable) {
  note('FAIL: no scenario produced a readable material')
  await writeFile(`${OUT}/log.txt`, log.join('\n'), 'utf8')
  await browser.close()
  process.exit(1)
}

// One candidate → compare view offers the explicit single-candidate selection.
await page.locator('.mat-card a:has-text("阅读")').first().click()
await page.waitForURL(/\/materials\//, { timeout: 20_000 })
await page.waitForTimeout(2000)
await page.locator('a:has-text("对比本场景")').click()
await page.waitForURL(/\/compare\//, { timeout: 20_000 })
await page.waitForTimeout(2000)
await shot('01-compare-single')

await page.locator('button:has-text("→ 合成语音")').first().click()
await page.waitForSelector('.dialog', { timeout: 5000 })
await page.locator('.dialog input[type=checkbox]').check()
await page.locator('.dialog .btn-danger').click()
await page.waitForTimeout(1200)
await shot('02-selected')
note(
  `banners: ${(
    await page.evaluate(() =>
      [...document.querySelectorAll('.banner')].map((b) => b.innerText.replace(/\s+/g, ' ')),
    )
  ).join(' || ')}`,
)

const open = page.locator('a:has-text("打开选定材料")')
if ((await open.count()) === 0) {
  note('FAIL: selection did not produce an audio job (is flags.syntheticAudio true?)')
  await writeFile(`${OUT}/log.txt`, log.join('\n'), 'utf8')
  await browser.close()
  process.exit(1)
}
await open.click()
await page.waitForTimeout(2500)
await shot('03-synthesizing')
note(
  `progress banner: ${await page.evaluate(() => {
    const b = [...document.querySelectorAll('.banner')].find((x) => x.innerText.includes('合成'))
    return b?.innerText.replace(/\s+/g, ' ') ?? 'none'
  })}`,
)

await page.waitForSelector('.player', { timeout: 90_000 })
await page.waitForTimeout(600)
await shot('04-player')
note(`synthetic warning label count: ${await page.locator('.player .flag-bad').count()}`)
note(`player line: ${(await page.locator('.player').innerText()).replace(/\s+/g, ' ')}`)

// The audio-side alignment claim: the highlighted turn must be the one the
// manifest's turn_index names, for the same material the annotations use.
const t1 = page.locator('.turn').nth(1)
await t1.hover()
await t1.locator('button:has-text("此句")').click()
const seq = []
for (let i = 0; i < 10; i += 1) {
  await page.waitForTimeout(1500)
  const cur = await page.evaluate(
    () => document.querySelector('.turn.playing')?.getAttribute('data-turn') ?? null,
  )
  if (seq[seq.length - 1] !== cur) seq.push(cur)
}
note(`playback turn sequence: ${seq.join(' → ')}`)
await shot('05-playing')

// Per-turn play from a mid-script turn: turnToEntry must resolve by turn_index.
const t12 = page.locator('.turn').nth(12)
await t12.hover()
await t12.locator('button:has-text("此句")').click()
await page.waitForTimeout(1400)
note(
  `after clicking turn 12: playing=${await page.evaluate(
    () => document.querySelector('.turn.playing')?.getAttribute('data-turn') ?? null,
  )}`,
)
await shot('06-play-turn-12')

note(`console errors: ${errors.length}`)
for (const e of errors) note(`  ERR ${e}`)
await writeFile(`${OUT}/log.txt`, log.join('\n'), 'utf8')
await browser.close()
console.log(`\nartifacts in ${OUT}`)
if (errors.length > 0) process.exitCode = 1
