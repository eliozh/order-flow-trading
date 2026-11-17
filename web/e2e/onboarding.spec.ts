import fs from 'node:fs'
import path from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

/**
 * Slice 44: the Paper-only onboarding wizard at `/onboarding` and
 * `/onboarding/1..5`.
 *
 * Preconditions (provided by `e2e/run-onboarding-e2e.sh`):
 * - one real daemon on a temporary `OFT_DATA` dir serves the built `web/dist`;
 * - `OFT_E2E_ONBOARDING_BOOTSTRAP_FILE` mints the browser session;
 * - `OFT_MASTER_KEY` enables the encrypted-file secret fallback so the LLM key
 *   step works headlessly (the journey never touches a real provider).
 *
 * The journey never enables Live, never enters a Binance/Telegram credential,
 * and never reaches an exchange or an LLM provider.
 */

const BOOTSTRAP_FILE = process.env.OFT_E2E_ONBOARDING_BOOTSTRAP_FILE
const BASE_URL = process.env.OFT_E2E_ONBOARDING_BASE_URL
const ROOT = path.resolve(process.cwd(), '..')
const SCREENS_DIR =
  process.env.OFT_E2E_ONBOARDING_SCREENS_DIR ??
  path.resolve(ROOT, '.claude', 'ecc-workflow', 'screens', 'slice-44-e2e')

const HEADING = { level: 1 } as const
const READ_ONLY_NOTE = '移动端只读：请在桌面端（宽度 ≥ 769px）打开本页完成引导。'
const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

interface BrowserDiagnostics {
  consoleErrors: string[]
  pageErrors: string[]
}

function collectDiagnostics(page: Page): BrowserDiagnostics {
  const diagnostics: BrowserDiagnostics = { consoleErrors: [], pageErrors: [] }
  page.on('console', (message) => {
    if (message.type() === 'error') {
      diagnostics.consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => diagnostics.pageErrors.push(error.message))
  return diagnostics
}

function expectClean(diagnostics: BrowserDiagnostics, allowed: readonly string[] = []): void {
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(
    diagnostics.consoleErrors.filter(
      (message) => !allowed.some((marker) => message.includes(marker)),
    ),
    'unexpected console errors',
  ).toEqual([])
}

async function expectNoAxeViolations(page: Page, label: string): Promise<void> {
  // `options()` replaces the builder options, so the tag filter and the
  // explicitly enabled WCAG 2.2 target-size rule (disabled by default in
  // axe-core) are supplied together rather than chaining `.withTags`.
  const results = await new AxeBuilder({ page })
    .options({
      runOnly: { type: 'tag', values: AXE_TAGS },
      rules: { 'target-size': { enabled: true } },
    })
    .analyze()
  const summary = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  )
  expect(results.violations, `${label} axe violations:\n${summary.join('\n')}`).toEqual([])
  // Prove the explicitly enabled WCAG 2.2 rule was evaluated, not silently
  // dropped by the tag filter.
  const targetSizeRan = [...results.passes, ...results.incomplete, ...results.violations].some(
    (result) => result.id === 'target-size',
  )
  expect(targetSizeRan, `${label}: target-size rule did not run`).toBe(true)
}

/** The largest transition/animation duration in milliseconds (``s`` or ``ms``). */
function largestDurationMs(value: string): number {
  const parts = value
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
  if (parts.length === 0) {
    return Number.NaN
  }
  return Math.max(
    ...parts.map((part) => {
      const match = /^(-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(ms|s)$/i.exec(part)
      if (match === null) {
        return Number.NaN
      }
      const amount = Number.parseFloat(match[1] as string)
      return (match[2] as string).toLowerCase() === 's' ? amount * 1000 : amount
    }),
  )
}

/** Mint a browser session from the daemon's one-time bootstrap file. */
async function authenticate(page: Page): Promise<void> {
  if (!BOOTSTRAP_FILE) {
    throw new Error('OFT_E2E_ONBOARDING_BOOTSTRAP_FILE is required for the onboarding journey')
  }
  const secret = fs.readFileSync(BOOTSTRAP_FILE, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

test.beforeAll(async ({ request }) => {
  expect(BOOTSTRAP_FILE, 'run-onboarding-e2e.sh must export the bootstrap file').toBeTruthy()
  expect(BASE_URL, 'run-onboarding-e2e.sh must export the base URL').toBeTruthy()
  expect(fs.existsSync(BOOTSTRAP_FILE as string), 'the bootstrap file must exist').toBe(true)
  const response = await request.get('/health')
  expect(response.status()).toBe(200)
})

test.describe.configure({ mode: 'serial' })

test('fresh install renders step 1 with Paper selected, real checks, and heading focus', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/onboarding/1')

  const heading = page.getByRole('heading', HEADING)
  await expect(heading).toBeVisible()
  await expect(heading).toContainText('环境与本地绑定')
  // The step heading receives focus on load.
  await expect(heading).toBeFocused()

  const environment = page.getByRole('group', { name: '交易环境' })
  await expect(environment.getByRole('radio', { name: /^Paper/ })).toBeChecked()
  await expect(environment.getByRole('radio', { name: /^Paper/ })).toBeDisabled()
  await expect(environment.getByRole('radio', { name: /^Live/ })).toBeDisabled()
  await expect(environment).toContainText('Live 不在引导中启用')

  // Named checks are shown with their real detail; the daemon is ready.
  await expect(page.getByTestId('check-config')).toContainText('PASS')
  await expect(page.getByTestId('check-storage.sqlite')).toContainText('PASS')
  await expect(page.getByTestId('check-storage.parquet')).toContainText('PASS')
  await expect(page.getByTestId('check-clock')).toContainText('PASS')
  await expect(page.getByTestId('check-market_data')).toContainText('PASS')

  const api = await page.request.get('/api/v1/settings/onboarding', {
    headers: { Accept: 'application/json' },
  })
  expect(api.status()).toBe(200)
  const body = (await api.json()) as {
    data: { completed_steps: number[]; resumed_step: number; environment: string }
  }
  expect(body.data.completed_steps).toEqual([])
  expect(body.data.resumed_step).toBe(1)
  expect(body.data.environment).toBe('paper')

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'onboarding-step-1-1440.png'),
    fullPage: true,
  })

  expectClean(diagnostics)
})

test('completing step 1 persists and resumes at step 2', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/onboarding/1')
  await expect(page.getByRole('heading', HEADING)).toContainText('环境与本地绑定')

  await page.getByRole('button', { name: '保存并继续' }).click()
  await page.waitForURL('**/onboarding/2')
  await expect(page.getByRole('heading', HEADING)).toContainText('LLM providers')

  const api = await page.request.get('/api/v1/settings/onboarding', {
    headers: { Accept: 'application/json' },
  })
  const body = (await api.json()) as {
    data: { completed_steps: number[]; resumed_step: number; steps: { completed: boolean }[] }
  }
  expect(body.data.completed_steps).toEqual([1])
  expect(body.data.resumed_step).toBe(2)

  // The static step list names step 1 as completed in text and marks step 2 as
  // the current step; the list itself is not a set of tab stops.
  await expect(page.getByTestId('onboarding-nav-1')).toContainText('✓')
  await expect(page.getByTestId('onboarding-nav-1')).toContainText('已完成')
  await expect(page.getByTestId('onboarding-nav-2')).toHaveAttribute('aria-current', 'step')
  const stepNav = page.getByRole('navigation', { name: '首次配置步骤' })
  await expect(stepNav.locator('a')).toHaveCount(0)

  // Keyboard users move between steps with the explicit links.
  await expect(page.getByTestId('onboarding-prev')).toHaveAttribute('href', '/onboarding/1')
  await expect(page.getByTestId('onboarding-next')).toHaveAttribute('href', '/onboarding/3')

  // A bare `/onboarding?resume` lands on the first incomplete step and keeps
  // the completed step listed.
  await page.goto('/onboarding?resume')
  await page.waitForURL('**/onboarding/2')
  await expect(page.getByRole('heading', HEADING)).toContainText('LLM providers')
  await expect(page.getByTestId('onboarding-nav-1')).toContainText('已完成')

  // Reloading the explicit step keeps the same step and completed state.
  await page.reload()
  await expect(page.getByRole('heading', HEADING)).toContainText('LLM providers')
  await expect(page.getByTestId('onboarding-nav-1')).toContainText('已完成')

  expectClean(diagnostics)
})

test('LLM key save shows only the server mask and never the typed value', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/onboarding/2')
  await expect(page.getByRole('heading', HEADING)).toContainText('LLM providers')

  // With no provider configured, finishing is blocked with the documented
  // consequence as a polite status (not a persistent assertive alert).
  const missingProvider = page.getByTestId('llm-no-provider')
  const finishStepTwo = page.getByRole('button', { name: '保存并继续' })
  await expect(missingProvider).toBeVisible()
  await expect(missingProvider).toHaveAttribute('role', 'status')
  await expect(finishStepTwo).toHaveAttribute('aria-disabled', 'true')
  await expect(finishStepTwo).toHaveAttribute('aria-describedby', 'llm-no-provider')
  await expect(page.getByTestId('llm-key-save-anthropic')).toBeEnabled()

  const sentinel = 'sk-e2e-SENTINEL-44-DO-NOT-RENDER-4f2a'
  await expect(page.getByTestId('llm-key-mask-anthropic')).toHaveText('未配置')
  await page.getByTestId('llm-key-input-anthropic').fill(sentinel)
  await page.getByTestId('llm-key-save-anthropic').click()

  await expect(page.getByTestId('llm-key-mask-anthropic')).toHaveText('****4f2a')
  await expect(page.getByTestId('llm-key-input-anthropic')).toHaveValue('')
  const html = await page.content()
  expect(html).not.toContain(sentinel)
  expect(html).not.toContain(sentinel.slice(0, 16))
  // No password field retains the plaintext and no storage holds it.
  const stored = await page.evaluate(() => ({
    passwords: Array.from(
      document.querySelectorAll<HTMLInputElement>('input[type="password"]'),
    ).map((input) => input.value),
    session: Object.values(sessionStorage),
    local: Object.values(localStorage),
  }))
  expect(stored.passwords.every((value) => value === '')).toBe(true)
  expect(JSON.stringify(stored.session)).not.toContain(sentinel)
  expect(JSON.stringify(stored.local)).not.toContain(sentinel)

  // No reveal control exists anywhere on the step.
  await expect(page.getByRole('button', { name: /显示原值|查看密钥|reveal/i })).toHaveCount(0)

  // The saved provider clears the consequence and enables finishing.
  await expect(missingProvider).toHaveCount(0)
  await expect(finishStepTwo).not.toHaveAttribute('aria-disabled', 'true')

  // The configured provider unblocks finishing step 2; deferred steps are
  // skipped and the resumed step is 5.
  await finishStepTwo.click()
  await page.waitForURL('**/onboarding/5')
  await expect(page.getByRole('heading', HEADING)).toContainText('Watchlist')

  expectClean(diagnostics)
})

test('Binance and Telegram are deferred to Milestones 5 and 6 without inputs', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  await page.goto('/onboarding/3')
  await expect(page.getByRole('heading', HEADING)).toContainText('Binance')
  await expect(page.getByTestId('deferred-step-3')).toContainText('Milestone 5')
  await expect(page.getByTestId('deferred-step-3')).toContainText('不会保存任何内容')
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /保存/ })).toHaveCount(0)

  await page.goto('/onboarding/4')
  await expect(page.getByRole('heading', HEADING)).toContainText('Telegram')
  await expect(page.getByTestId('deferred-step-4')).toContainText('Milestone 6')
  await expect(page.getByTestId('deferred-step-4')).toContainText('不会保存任何内容')
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /保存/ })).toHaveCount(0)

  expectClean(diagnostics)
})

test('rejects out-of-range and non-numeric onboarding step routes', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  // Only `/onboarding` and `/onboarding/1..5` exist; a fabricated step must
  // fall through to not-found rather than render a wizard step.
  for (const path of ['/onboarding/0', '/onboarding/6', '/onboarding/foo', '/onboarding/1/extra']) {
    await page.goto(path)
    await expect(page.getByRole('heading', { name: 'Not found', level: 1 })).toBeVisible()
    await expect(page.locator('section.onboarding-page')).toHaveCount(0)
  }

  expectClean(diagnostics)
})

test('step 5 stays within a 320px viewport and the star control is not glyph-sized', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 320, height: 640 })
  await authenticate(page)
  await page.goto('/onboarding/5')
  await expect(page.getByRole('heading', HEADING)).toContainText('Watchlist')
  await expect(page.getByTestId('scan-source')).toBeVisible()
  await expect(page.locator('section.onboarding-page')).toHaveAttribute('data-readonly', 'false')

  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.document, 'no horizontal overflow at 320 on step 5').toBeLessThanOrEqual(
    widths.viewport,
  )

  // The labelled star action keeps a text-width hit box and the 44px touch
  // height; it must not inherit the glyph-only `.star-toggle` square.
  const star = page.getByRole('button', { name: /^星标 / }).first()
  await expect(star).toBeVisible()
  const box = await star.boundingBox()
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(44)
  expect(box?.width ?? 0).toBeGreaterThan(44)

  expectClean(diagnostics)
})

test('step 5 shows every failing field link and runs the WCAG 2.2 target-size rule', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/onboarding/5')
  await expect(page.getByRole('heading', HEADING)).toContainText('Watchlist')
  await expect(page.getByTestId('scan-source')).toBeVisible()

  // Two independent failures at once: nothing is starred and the daily budget
  // is empty, so the summary renders two field links (the SC 2.5.8 state).
  await page.locator('#onboarding-budget-daily').fill('')
  await page.getByRole('button', { name: '完成引导并查看首次扫描' }).click()

  const finishError = page.getByTestId('onboarding-finish-error')
  await expect(finishError).toBeVisible()
  await expect(finishError.getByRole('link', { name: /首批标的/ })).toBeVisible()
  await expect(finishError.getByRole('link', { name: /日预算/ })).toBeVisible()
  await expectNoAxeViolations(page, 'step 5 multi-issue summary')

  expectClean(diagnostics)
})

test('finish review shows effective settings, focuses the result, and links to /scans', async ({
  page,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/onboarding/5')
  await expect(page.getByRole('heading', HEADING)).toContainText('Watchlist')

  await expect(page.getByTestId('scan-source')).toContainText('source:')
  await expect(page.getByTestId('watchlist-source')).toContainText('registry')
  await expect(page.getByTestId('watchlist-source')).toContainText('data_quality')
  await expect(page.getByTestId('watchlist-validation')).toContainText('至少 1 个')

  // The editable step and its form controls pass axe.
  await expectNoAxeViolations(page, 'step 5 editable')

  // Finishing with no starred symbol is blocked by the documented client
  // validation, and the focused error lists the offending field.
  await page.getByRole('button', { name: '完成引导并查看首次扫描' }).click()
  const finishError = page.getByTestId('onboarding-finish-error')
  await expect(finishError).toBeVisible()
  await expect(finishError).toBeFocused()
  await expect(finishError).toContainText('至少星标 1 个标的')
  await expect(finishError.getByRole('link', { name: /首批标的/ })).toBeVisible()
  await expectNoAxeViolations(page, 'step 5 failed submit')

  // Star one fixture symbol through the shared watchlist service.
  const star = page.getByRole('button', { name: /^星标 / }).first()
  await star.click()
  await expect(page.getByTestId('watchlist-validation')).toContainText('已星标 1 个')

  // Pressing Enter in a budget field submits the same finish action.
  const mutationPaths: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'PUT') {
      mutationPaths.push(new URL(request.url()).pathname)
    }
  })
  await page.getByRole('radio', { name: '8h' }).check()
  await page.locator('#onboarding-budget-daily').fill('12.50')
  await page.locator('#onboarding-budget-daily').press('Enter')

  const review = page.getByTestId('onboarding-review')
  await expect(review).toBeVisible()
  await expect(review).toContainText('Paper')
  await expect(review).toContainText('Live 未启用')
  await expect(review).toContainText('扫描周期')
  await expect(review).toContainText('预算阈值')
  await expect(review.getByRole('link', { name: /打开 \/scans/ })).toHaveAttribute('href', '/scans')
  await expect(review).toContainText('oft scan run')

  // The review reflects the values just persisted through the real services,
  // and both mutations actually hit the settings/watchlist daemon.
  await expect(review).toContainText('8h · source: user')
  await expect(review).toContainText('12.50')
  expect(mutationPaths).toContain('/api/v1/settings/scan')
  expect(mutationPaths).toContain('/api/v1/budget')

  // Finishing the wizard moves focus to the result it produced.
  await expect(page.getByRole('heading', { name: /引导完成/, level: 2 })).toBeFocused()
  await expectNoAxeViolations(page, 'post-finish review')

  const scanResponse = await page.request.get('/api/v1/settings/scan', {
    headers: { Accept: 'application/json' },
  })
  expect(scanResponse.status()).toBe(200)
  const scanBody = (await scanResponse.json()) as {
    ok: boolean
    data: { value: string; source: string }
  }
  expect(scanBody.ok).toBe(true)
  expect(scanBody.data.value).toBe('8h')
  expect(scanBody.data.source).toBe('user')

  const budgetResponse = await page.request.get('/api/v1/budget', {
    headers: { Accept: 'application/json' },
  })
  expect(budgetResponse.status()).toBe(200)
  const budgetBody = (await budgetResponse.json()) as {
    ok: boolean
    data: { thresholds: { daily_usd: string } }
  }
  expect(budgetBody.ok).toBe(true)
  expect(budgetBody.data.thresholds.daily_usd).toBe('12.50')

  // Live is never offered on the onboarding screens.
  await expect(page.getByRole('radio', { name: /^Live/ })).toHaveCount(0)

  const api = await page.request.get('/api/v1/settings/onboarding', {
    headers: { Accept: 'application/json' },
  })
  const body = (await api.json()) as {
    data: { completed_steps: number[]; all_complete: boolean; environment: string }
  }
  expect(body.data.completed_steps).toContain(5)
  expect(body.data.all_complete).toBe(true)
  expect(body.data.environment).toBe('paper')

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'onboarding-review-1440.png'),
    fullPage: true,
  })

  // The /scans CTA marks the destination for focus; RouteFocus moves focus to
  // the destination h1 after the full-page load.
  await review.getByRole('link', { name: /打开 \/scans/ }).click()
  await page.waitForURL('**/scans')
  await expect(page.getByRole('heading', HEADING)).toBeFocused()

  expectClean(diagnostics)
})

test('step 1 is accessible at 1440/768/375 fine pointers and at 320px reflow; 375 coarse is read-only', async ({
  page,
  browser,
}) => {
  test.setTimeout(180_000)
  const diagnostics = collectDiagnostics(page)
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await authenticate(page)

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 768, height: 1024 },
    { width: 375, height: 812 },
    { width: 320, height: 640 },
  ]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await page.goto('/onboarding/1')
    const heading = page.getByRole('heading', HEADING)
    await expect(heading).toContainText('环境与本地绑定')
    await expect(heading).toBeFocused()
    // A fine pointer stays editable even at 320 CSS px (zoom reflow).
    await expect(page.locator('section.onboarding-page')).toHaveAttribute('data-readonly', 'false')
    await expect(page.getByRole('button', { name: '保存并继续' })).toBeEnabled()
    await expect(page.locator('section.onboarding-page').getByRole('note')).toHaveCount(0)

    const widths = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(widths.document, `no horizontal overflow at ${viewport.width}`).toBeLessThanOrEqual(
      widths.viewport,
    )

    await expectNoAxeViolations(page, `step 1 at ${viewport.width}`)
    await page.screenshot({
      path: path.join(SCREENS_DIR, `onboarding-step-1-${viewport.width}.png`),
      fullPage: true,
    })
  }

  // Step 2 passes axe with the write-only key fields.
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/onboarding/2')
  await expect(page.getByRole('heading', HEADING)).toContainText('LLM providers')
  await expectNoAxeViolations(page, 'step 2')

  expectClean(diagnostics)

  // A real coarse-pointer phone context (touch, 375 CSS px) is read-only.
  expect(BASE_URL).toBeTruthy()
  const context = await browser.newContext({
    baseURL: BASE_URL as string,
    hasTouch: true,
    isMobile: true,
    viewport: { width: 375, height: 812 },
  })
  const mobile = await context.newPage()
  const mobileDiagnostics = collectDiagnostics(mobile)
  try {
    await authenticate(mobile)
    await mobile.goto('/onboarding/1')
    await expect(mobile.getByRole('heading', HEADING)).toContainText('环境与本地绑定')
    await expect(mobile.locator('section.onboarding-page')).toHaveAttribute('data-readonly', 'true')
    await expect(mobile.locator('section.onboarding-page').getByRole('note')).toContainText(
      READ_ONLY_NOTE,
    )
    await expect(mobile.getByRole('button', { name: '保存并继续' })).toBeDisabled()
    // DESIGN A3: the explicit step-navigation links meet the 44px touch target.
    const nextBox = await mobile.getByTestId('onboarding-next').boundingBox()
    expect(nextBox?.height ?? 0).toBeGreaterThanOrEqual(44)
    const widths = await mobile.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(widths.document, 'no horizontal overflow at 375 coarse').toBeLessThanOrEqual(
      widths.viewport,
    )
    await expectNoAxeViolations(mobile, 'step 1 at 375 coarse')
    await mobile.screenshot({
      path: path.join(SCREENS_DIR, 'onboarding-step-1-375-coarse.png'),
      fullPage: true,
    })

    // Steps 2 and 5 are read-only at 375 coarse too.
    await mobile.goto('/onboarding/2')
    await expect(mobile.getByTestId('llm-key-input-openai')).toBeDisabled()
    await expect(mobile.getByRole('button', { name: '保存并继续' })).toBeDisabled()
    await mobile.goto('/onboarding/5')
    await expect(mobile.getByTestId('scan-source')).toBeVisible()
    await expect(mobile.getByRole('button', { name: /^星标 / }).first()).toBeDisabled()
    await expect(mobile.getByRole('spinbutton', { name: '日预算（USD）' })).toBeDisabled()
    await expectNoAxeViolations(mobile, 'step 5 at 375 coarse')

    expectClean(mobileDiagnostics)
  } finally {
    await context.close()
  }
})

test('reduced motion clamps the step-1 controls to the 0.01ms floor and the wizard adds no ambient motion', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto('/onboarding/1')
  await expect(page.getByRole('heading', HEADING)).toContainText('环境与本地绑定')

  // Only the step-1 controls are inspected; this URL renders no result surface.
  const targets = [
    page.getByTestId('onboarding-nav-1'),
    page.getByTestId('onboarding-next'),
    page.locator('section.onboarding-page'),
  ]
  const read = () =>
    Promise.all(
      targets.map((target) =>
        target.evaluate((element) => {
          const style = getComputedStyle(element)
          return { transition: style.transitionDuration, animation: style.animationDuration }
        }),
      ),
    )

  // Without the preference the wizard introduces no motion at all.
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const normal = await read()
  for (const [index, value] of normal.entries()) {
    for (const [kind, raw] of Object.entries(value)) {
      const duration = largestDurationMs(raw as string)
      expect(Number.isFinite(duration), `target ${index} ${kind} is parseable`).toBe(true)
      expect(duration, `target ${index} normal ${kind}`).toBe(0)
    }
  }

  // Under reduce the defensive CSS floor applies: > 0 proves the rule matched,
  // and <= 0.01 keeps the A12 #20 imperceptible-duration requirement.
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const reduced = await read()
  for (const [index, value] of reduced.entries()) {
    for (const [kind, raw] of Object.entries(value)) {
      const duration = largestDurationMs(raw as string)
      expect(Number.isFinite(duration), `target ${index} ${kind} is parseable`).toBe(true)
      expect(duration, `target ${index} reduced ${kind}`).toBeGreaterThan(0)
      expect(duration, `target ${index} reduced ${kind}`).toBeLessThanOrEqual(0.01)
    }
  }

  expectClean(diagnostics)
})
