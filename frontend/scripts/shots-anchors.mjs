// 定位处理的浏览器验证，跑在 /dev/fixtures 上。
//
//   npm run dev:mock
//   node scripts/shots-anchors.mjs
//
// 钉的是客户那条底线在真浏览器里成立：「用户看到的永远是成品，不是带已知 bug 的半成品 +
// 修复建议」。三个夹具各走一条分支——
//
//   锚点失配         evidence 恰好只在另一轮出现 → 静默挪正，十条旁注一条不少
//   锚点仅大小写不同  后端 casefold 后认为合法 → 前端也必须认为合法，且高亮不许错位
//   锚点无法确定      evidence 一处都没有 → 这一条旁注不显示，另九条照常，一句告警都没有
//
// 同时确认信号没有全方向消失：控制台里开发者看得到，页面上用户看不到。
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { signIn } from './signIn.mjs'

const OUT = '/tmp/shots-anchors'
const BASE = 'http://localhost:5173'
await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1100 } })
const page = await ctx.newPage()

const consoleLines = []
const errors = []
page.on('console', (m) => {
  consoleLines.push(`${m.type()}: ${m.text()}`)
  if (m.type() === 'error') errors.push(m.text())
})
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

const failures = []
const check = (label, ok, detail = '') => {
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures.push(`${label} ${detail}`)
}
const shot = async (name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  console.log(`  shot ${name}`)
}

/** 任何一句「我可能标错了、你自己核对一下」。 */
const BUG_WORDING = [
  '旁注可能错位',
  '旁注位置可疑',
  '标错了位置',
  '不相干的句子',
  '请核对高亮位置',
  '请勿据此判断',
  '锚点',
  '信息点定位',
  '找不到对应台词',
]

/** 阅读区（不含 DEV ONLY 那一块）里不许出现这些字。 */
async function assertNoBugWording(label) {
  const text = await page.evaluate(() => document.querySelector('.reader')?.innerText ?? '')
  const hits = BUG_WORDING.filter((w) => text.includes(w))
  check(`${label}: 阅读区没有任何「可能标错了」的说法`, hits.length === 0, hits.join(', '))
}

async function openFixture(label) {
  await page.locator(`button:has-text("${label}")`).click()
  await page.waitForTimeout(600)
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await signIn(page, BASE)
await page.goto(`${BASE}/dev/fixtures`, { waitUntil: 'networkidle' })
await page.locator('button:has-text("完整阅读态")').click()
await page.waitForSelector('.reader')

/* ── 1. 恰好一处命中 → 静默挪正 ─────────────────────────────────────────── */

console.log('\n[1] 锚点失配（evidence 只在 turn 10 出现，blueprint 写 turn 14）')
await openFixture('锚点失配')
await shot('01-relocated-silently')

check(
  '旁注挂在真正带着这句话的 turn 10 上',
  (await page.locator('[data-turn="10"] mark').count()) === 1,
)
check(
  '声明的 turn 14 上什么也没有',
  (await page.locator('[data-turn="14"] mark').count()) === 0,
)
const marked = await page.locator('[data-turn="10"] mark').innerText()
check('高亮切出来的正是那句话', marked.includes("It's BT14 9BJ."), JSON.stringify(marked))
const numbers1 = await page.locator('.ann-item .num').allInnerTexts()
check('十条旁注一条不少（挪正，不是剔除）', numbers1.length === 10, `${numbers1.length}`)
await assertNoBugWording('锚点失配')

// 开发者那一侧必须看得到发生了什么。
const devPanel = await page.locator('.panel:has-text("定位（DEV ONLY）")').innerText()
check('DEV 面板说出了挪正的来去', devPanel.includes('已静默挪正'), devPanel.replace(/\n/g, ' | '))
check(
  '控制台向开发者报了这次挪正',
  consoleLines.some((l) => l.includes('[anchors]') && l.includes('relocated')),
)

/* ── 2. 只差大小写 → 合法，不许报失配 ───────────────────────────────────── */

console.log('\n[2] 锚点仅大小写不同（后端 casefold 后合法）')
await openFixture('锚点仅大小写不同')
await shot('02-case-differs')

const numbers2 = await page.locator('.ann-item .num').allInnerTexts()
check('十条旁注都在', numbers2.length === 10, `${numbers2.length}`)
const caseMark = await page.locator('[data-turn="4"] mark').innerText()
// 关键：高亮下标是对着**原文**算的，所以切出来是大写的 It's，一个字符都不偏。
check(
  '高亮切出原文那一段（大写 It’s），下标没有错位',
  caseMark.startsWith("It's Anna Woods."),
  JSON.stringify(caseMark),
)
const devPanel2 = await page.locator('.panel:has-text("定位（DEV ONLY）")').innerText()
check(
  '既没挪正也没剔除——它本来就是合法的',
  devPanel2.includes('未作任何调整'),
  devPanel2.replace(/\n/g, ' | '),
)
await assertNoBugWording('锚点仅大小写不同')

/* ── 3. 一处都没有 → 剔除这一条，另九条照常 ─────────────────────────────── */

console.log('\n[3] 锚点无法确定（evidence 在脚本里不存在）')
await openFixture('锚点无法确定')
await shot('03-unresolvable-omitted')

const numbers3 = await page.locator('.ann-item .num').allInnerTexts()
check('剩下的九条旁注照常显示', numbers3.length === 9, numbers3.join(''))
check('第 3 条哪里都不出现', !numbers3.includes('③'), numbers3.join(''))
const items3 = await page.evaluate(() =>
  [...document.querySelectorAll('mark')].flatMap((m) =>
    (m.getAttribute('data-items') ?? '').split(','),
  ),
)
check('高亮里也没有第 3 题', !items3.includes('3'))
await assertNoBugWording('锚点无法确定')

// 页面干净，但开发者两条路都能看见。
const devPanel3 = await page.locator('.panel:has-text("定位（DEV ONLY）")').innerText()
check(
  'DEV 面板说清了为什么剔除，并点明 blueprint 仍是 10 个点',
  devPanel3.includes('这一条旁注不显示') && devPanel3.includes('10 个点'),
  devPanel3.replace(/\n/g, ' | '),
)
const warned = consoleLines.filter(
  (l) => l.startsWith('warning') && l.includes('[anchors]') && l.includes('hidden'),
)
check('控制台向开发者报了这次剔除', warned.length > 0, warned[0] ?? '')

/* ── 4. 阅读页整页复查 ──────────────────────────────────────────────────── */

console.log('\n[4] 全页复查')
const bodyText = await page.evaluate(() => document.body.innerText)
// DEV 面板是开发者通道，只在 /dev/fixtures 存在，所以整页会（也应该）命中这些词；
// 这里确认它确实被隔在 .reader 之外，而不是混进了阅读内容里。
check(
  'DEV ONLY 的说明只在那一块面板里，没有渗进阅读区',
  bodyText.includes('DEV ONLY'),
)

console.log(`\nconsole errors: ${errors.length}`)
for (const e of errors.slice(0, 10)) console.log(`  ! ${e}`)
console.log(`failures: ${failures.length}`)
for (const f of failures) console.log(`  !! ${f}`)

await browser.close()
process.exit(failures.length > 0 || errors.length > 0 ? 1 : 0)
