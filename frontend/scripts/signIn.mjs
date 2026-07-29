// Shared sign-in step for the screenshot harnesses.
//
// Every page in the app is behind RequireAuth, so a bare `goto(BASE)` now lands
// on /login rather than the scenario page. This registers-or-logs-in once per
// browser context and leaves the page on the app, so each script's first
// `waitForSelector('.scn-chip')` still means what it did before.
//
// Idempotent: an existing session short-circuits, which is what makes it safe to
// call before every goto in the scripts that navigate repeatedly.

export const DEMO_EMAIL = process.env.SHOTS_EMAIL ?? 'demo@amazon.com'
export const DEMO_PASSWORD = process.env.SHOTS_PASSWORD ?? 'hunter2hunter2'

/**
 * @param page          a Playwright page, already `goto`-ed to the app
 * @param base          origin, e.g. http://localhost:5173
 */
export async function signIn(page, base) {
  if (!page.url().startsWith(base)) {
    await page.goto(base, { waitUntil: 'networkidle' })
  }
  // Already signed in: the guard let us through, so there is nothing to do.
  if ((await page.locator('.auth-card').count()) === 0) return

  const fill = async () => {
    await page.locator('.auth-field:has-text("邮箱") input').fill(DEMO_EMAIL)
    await page.locator('.auth-field:has-text("密码") input').first().fill(DEMO_PASSWORD)
  }

  // Try login first; a fresh store has no account, so fall back to register.
  await fill()
  await page.locator('button[type=submit]').click()
  await page.waitForTimeout(600)

  if ((await page.locator('.auth-card').count()) > 0) {
    await page.locator('button:has-text("没有账号？注册")').click()
    await fill()
    await page.locator('.auth-field:has-text("确认密码") input').fill(DEMO_PASSWORD)
    await page.locator('button[type=submit]').click()
    // The success panel is deliberate UX, not a redirect — acknowledge it.
    await page.waitForSelector('button:has-text("进入系统")', { timeout: 10_000 })
    await page.locator('button:has-text("进入系统")').click()
  }

  await page.waitForSelector('button:has-text("退出")', { timeout: 10_000 })
}
