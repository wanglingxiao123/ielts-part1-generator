// SSE degradation walkthrough: mid-stream disconnect at the 4th material, and
// a partial terminal state with retry. Uses the mock's programmable options.
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots'
const BASE = 'http://localhost:5173'
await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
const errors = []

async function run(label, mockOptions, steps) {
  const ctx = await browser.newContext({ viewport: { width: 1480, height: 1000 } })
  const page = await ctx.newPage()
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`[${label}] ${m.text()}`)
  })
  page.on('pageerror', (e) => errors.push(`[${label}] pageerror: ${e.message}`))
  // Configure the mock before the app boots.
  await page.addInitScript((opts) => {
    window.__MOCK_OPTIONS__ = opts
  }, mockOptions)
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await signIn(page, BASE)
  for (const key of ['booking-hotel', 'booking-car-rental', 'accommodation-rental']) {
    await page.locator(`.scn-row:has-text("${key}") input[type=checkbox]`).first().check()
  }
  await page.locator('.summary-bar button').click()
  await page.waitForURL(/\/batches\//)
  await steps(page, async (name, opts = {}) => {
    await page.screenshot({ path: `${OUT}/${name}.png`, ...opts })
    console.log(`  shot ${name}`)
  })
  await ctx.close()
}

// ── disconnect at the 4th material, then recover ─────────────────────────────
console.log('A. disconnect after 4th material')
await run('drop', { tickMs: 700, dropAfterMaterials: 4 }, async (page, shot) => {
  await page.waitForSelector('.banner-warn:has-text("连接中断")', { timeout: 40000 })
  const kept = await page.locator('.mat-card .flag-good, .mat-card .flag-bad').count()
  const bannerText = (await page.locator('.banner-warn').first().innerText()).replace(/\n/g, ' ')
  console.log(`   disconnected: kept ${kept} finished materials`)
  console.log(`   banner: ${bannerText}`)
  await shot('25-sse-reconnecting')

  // Readable while disconnected?
  const readable = await page.locator('.mat-card a:has-text("阅读")').count()
  console.log(`   readable材料 links still present = ${readable}`)

  await page.waitForSelector('.banner-good:has-text("连接已恢复")', { timeout: 40000 })
  console.log('   recovered banner shown')
  await shot('26-sse-recovered')

  await page.waitForFunction(
    () => document.querySelectorAll('.mat-card .flag-good, .mat-card .flag-bad').length >= 6,
    null,
    { timeout: 60000 },
  )
  const final = await page.locator('.mat-card .flag-good, .mat-card .flag-bad').count()
  console.log(`   after recovery: finished=${final} (5th and 6th arrived)`)
  await shot('27-sse-recovered-complete')
  // The green "recovered" bar owns the banner slot for 3s; the persistent
  // degraded-recovery notice takes over after it clears.
  await page.waitForSelector('.banner-info:has-text("降级恢复")', { timeout: 10000 })
  console.log('   degraded-recovery notice shown after the green bar clears')
  await shot('30-sse-degraded-notice')
})

// ── partial terminal state ───────────────────────────────────────────────────
console.log('B. partial batch + retry')
await run('partial', { tickMs: 500, neverComplete: 2 }, async (page, shot) => {
  await page.waitForSelector('.banner-warn:has-text("partial")', { timeout: 60000 })
  const done = await page.locator('.mat-card .flag-good, .mat-card .flag-bad').count()
  const text = (await page.locator('.banner-warn').first().innerText()).replace(/\n/g, ' ')
  console.log(`   partial: ${done} finished`)
  console.log(`   banner: ${text.slice(0, 160)}`)
  await shot('28-sse-partial')

  const btn = page.locator('button:has-text("补生成")')
  if (await btn.count()) {
    const before = page.url()
    await btn.click()
    await page.waitForTimeout(2500)
    console.log(`   retry navigated to a new batch = ${page.url() !== before}`)
    await shot('29-sse-retry-batch')
  }
})

console.log('\nconsole errors:', errors.length ? errors.slice(0, 5) : 'none')
await browser.close()
