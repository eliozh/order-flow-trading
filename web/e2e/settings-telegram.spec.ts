import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

import { runAxeAudit } from './a11y'

/**
 * Slice 53: `/settings/telegram` (F10f) against one real daemon.
 *
 * Preconditions: a daemon with a temporary OFT_DATA dir serving `web/dist`, and
 * a browser bootstrap file. The token sentinel is a well-formed fake; the test
 * environment uses the deterministic Telegram fake, so no network is touched and
 * no real credential is involved.
 */

const BOOTSTRAP_FILE =
  process.env.OFT_E2E_TELEGRAM_BOOTSTRAP_FILE ?? process.env.OFT_E2E_SETTINGS_BOOTSTRAP_FILE
const BASE_URL =
  process.env.OFT_E2E_TELEGRAM_BASE_URL ??
  process.env.OFT_E2E_SETTINGS_BASE_URL ??
  'http://127.0.0.1:7652'
const ROOT = path.resolve(process.cwd(), '..')
const SCREENS_DIR =
  process.env.OFT_E2E_TELEGRAM_SCREENS_DIR ??
  path.resolve(ROOT, '.claude', 'ecc-workflow', 'screens', 'slice-53-e2e')

const HEADING = { name: 'Telegram 设置', level: 1 } as const
const SENTINEL = '123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv'
const ROTATED = '987654321:BBRotatedSentinelToken_abcdefghijklmnopq'
const READ_ONLY_NOTE = /移动端只读.*Telegram 设置只能在桌面端或 CLI 修改/

interface Diagnostics {
  consoleErrors: string[]
  pageErrors: string[]
}

async function activeTestId(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.getAttribute('data-testid') ?? null)
}

/** Move the keyboard focus to one test id using Tab only. */
async function tabTo(page: Page, testId: string, maxTabs = 80): Promise<void> {
  for (let index = 0; index < maxTabs; index += 1) {
    if ((await activeTestId(page)) === testId) {
      return
    }
    await page.keyboard.press('Tab')
  }
  expect(await activeTestId(page), `Tab never reached ${testId}`).toBe(testId)
}

/** Assert the focused control carries a visible focus indicator. */
async function expectVisibleFocus(page: Page, testId: string): Promise<void> {
  const locator = page.getByTestId(testId)
  await expect(locator).toBeFocused()
  const focus = await locator.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      focusVisible: element.matches(':focus-visible'),
      outline: `${style.outlineStyle} ${style.outlineWidth}`,
      shadow: style.boxShadow,
    }
  })
  expect(focus.focusVisible).toBe(true)
  expect(focus.outline !== 'none 0px' || focus.shadow !== 'none').toBe(true)
}

/** Assert no page-level horizontal scroll at the current viewport. */
async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
  expect(widths.body).toBeLessThanOrEqual(widths.viewport)
}

async function expectTokenAbsentEverywhere(page: Page): Promise<void> {
  expect(await page.content()).not.toContain(SENTINEL)
  const storage = await page.evaluate(() =>
    Object.values(sessionStorage).concat(Object.values(localStorage)),
  )
  expect(JSON.stringify(storage)).not.toContain(SENTINEL)
}

function collect(page: Page): Diagnostics {
  const diagnostics: Diagnostics = { consoleErrors: [], pageErrors: [] }
  page.on('console', (message) => {
    if (message.type() === 'error') {
      diagnostics.consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => diagnostics.pageErrors.push(error.message))
  return diagnostics
}

function expectClean(diagnostics: Diagnostics, allowed: readonly string[] = []): void {
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(
    diagnostics.consoleErrors.filter(
      (message) => !allowed.some((marker) => message.includes(marker)),
    ),
    'unexpected console errors',
  ).toEqual([])
}

async function authenticate(page: Page): Promise<void> {
  if (!BOOTSTRAP_FILE) {
    throw new Error('a browser bootstrap file is required for the Telegram journey')
  }
  const secret = fs.readFileSync(BOOTSTRAP_FILE, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect([200, 302]).toContain(response.status())
}

test.beforeAll(async ({ request }) => {
  expect(BOOTSTRAP_FILE, 'export the daemon bootstrap file').toBeTruthy()
  expect(fs.existsSync(BOOTSTRAP_FILE as string)).toBe(true)
  expect((await request.get('/health')).status()).toBe(200)
})

test.describe.configure({ mode: 'serial' })

test('fresh install shows 未配置, the push consequence, and a disabled test', async ({ page }) => {
  const diagnostics = collect(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/telegram')

  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  await expect(page.getByTestId('telegram-source')).toHaveText('source: default')
  await expect(page.getByTestId('telegram-config-status')).toHaveText('未配置')
  const empty = page.getByTestId('telegram-empty')
  await expect(empty).toContainText('未配置')
  await expect(empty).toContainText('不会推送 Telegram')
  await expect(page.getByTestId('telegram-test')).toBeDisabled()
  await expect(page.getByTestId('telegram-test-disabled-reason')).toContainText('未配置 token')

  const api = await page.request.get('/api/v1/settings/telegram', {
    headers: { Accept: 'application/json' },
  })
  expect(api.status()).toBe(200)
  const body = (await api.json()) as {
    data: { token: { configured: boolean; masked: string | null }; source: string }
  }
  expect(body.data.token.configured).toBe(false)
  expect(body.data.token.masked).toBeNull()
  expect(body.data.source).toBe('default')

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-telegram-empty-1440.png'),
    fullPage: true,
  })
  await runAxeAudit(page, '/settings/telegram empty @ 1440')
  expectClean(diagnostics)
})

test('token, chat, toggles, and test send stay write-only and masked', async ({ page }) => {
  test.setTimeout(90_000)
  const diagnostics = collect(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/telegram')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()

  // The token is entered with the keyboard and submitted with Enter; the field
  // is cleared and only the mask remains.
  const tokenInput = page.getByTestId('telegram-token-input')
  await expect(tokenInput).toHaveAttribute('type', 'password')
  await tokenInput.fill(SENTINEL)
  await tokenInput.press('Enter')
  await expect(page.getByTestId('telegram-token-mask')).toHaveText('****stuv')
  await expect(tokenInput).toHaveValue('')
  const afterSave = await page.content()
  expect(afterSave).not.toContain(SENTINEL)
  expect(afterSave).not.toContain(SENTINEL.slice(0, 14))
  expect(afterSave).not.toContain(SENTINEL.slice(-8))
  // No reveal control or stored plaintext exists.
  await expect(page.getByRole('button', { name: /显示原值|查看密钥|reveal/i })).toHaveCount(0)
  const stored = await page.evaluate(() =>
    Object.values(sessionStorage).concat(Object.values(localStorage)),
  )
  expect(JSON.stringify(stored)).not.toContain(SENTINEL)

  // Add a chat id, a user id, and disable one toggle, then save.
  await page.getByTestId('telegram-chat-input').fill('1001')
  await page.getByTestId('telegram-chat-add').click()
  await page.getByTestId('telegram-user-input').fill('2002')
  await page.getByTestId('telegram-user-add').click()
  await page.getByTestId('telegram-toggle-scan_report').uncheck()
  await page.getByTestId('telegram-save').click()
  await expect(page.getByTestId('telegram-save-status')).toContainText('已保存')
  await expect(page.getByTestId('telegram-source')).toHaveText('source: user')

  const api = await page.request.get('/api/v1/settings/telegram', {
    headers: { Accept: 'application/json' },
  })
  const text = await api.text()
  expect(text).not.toContain(SENTINEL)
  const body = JSON.parse(text) as {
    data: {
      token: { configured: boolean; masked: string | null }
      chat_ids: number[]
      allowed_user_ids: number[]
      toggles: Record<string, boolean>
      source: string
    }
  }
  expect(body.data.token.configured).toBe(true)
  expect(body.data.token.masked).toBe('****stuv')
  expect(body.data.chat_ids).toEqual([1001])
  expect(body.data.allowed_user_ids).toEqual([2002])
  expect(body.data.toggles.scan_report).toBe(false)
  expect(body.data.source).toBe('user')

  // Test send is a separate explicit action and returns a receipt.
  await page.getByTestId('telegram-test').click()
  await expect(page.getByTestId('telegram-test-receipt')).toBeVisible()
  await expect(page.getByTestId('telegram-test-receipt')).toContainText('排队中')
  await expect(page.getByTestId('telegram-receipts')).toBeVisible()

  // Rotate through the same write-only field.
  await page.getByTestId('telegram-token-input').fill(ROTATED)
  await page.getByTestId('telegram-token-save').click()
  await expect(page.getByTestId('telegram-token-mask')).toHaveText('****nopq')
  expect(await page.content()).not.toContain(ROTATED)

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-telegram-configured-1440.png'),
    fullPage: true,
  })
  await runAxeAudit(page, '/settings/telegram configured @ 1440')
  expectClean(diagnostics)
})

test('a reload renders the mask and the saved toggles', async ({ page }) => {
  const diagnostics = collect(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/telegram')

  await expect(page.getByTestId('telegram-token-mask')).toHaveText('****nopq')
  await expect(page.getByTestId('telegram-toggle-scan_report')).not.toBeChecked()
  await expect(page.getByTestId('telegram-toggle-risk')).toBeChecked()
  await expect(page.getByTestId('telegram-source')).toHaveText('source: user')
  expect(await page.content()).not.toContain(SENTINEL)
  expectClean(diagnostics)
})

test('is read-only at 375 coarse and editable at 768/1440', async ({ page, browser }) => {
  test.setTimeout(120_000)
  await authenticate(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/settings/telegram')
  await expect(page.getByTestId('telegram-token-input')).toBeEnabled()
  await expect(page.getByTestId('telegram-save')).toBeEnabled()

  const context = await browser.newContext({
    baseURL: BASE_URL,
    hasTouch: true,
    isMobile: true,
    viewport: { width: 375, height: 812 },
  })
  const mobile = await context.newPage()
  const diagnostics = collect(mobile)
  try {
    await authenticate(mobile)
    await mobile.goto('/settings/telegram')
    await expect(mobile.locator('section.system-page')).toHaveAttribute('data-readonly', 'true')
    await expect(mobile.locator('section.system-page').getByRole('note')).toContainText(
      READ_ONLY_NOTE,
    )
    await expect(mobile.getByTestId('telegram-token-input')).toBeDisabled()
    await expect(mobile.getByTestId('telegram-save')).toBeDisabled()
    await runAxeAudit(mobile, '/settings/telegram @ 375 coarse')

    await mobile.setViewportSize({ width: 768, height: 1024 })
    await mobile.goto('/settings/telegram')
    await expect(mobile.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')
    await expect(mobile.getByTestId('telegram-token-input')).toBeEnabled()

    const width = await mobile.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(width.document).toBeLessThanOrEqual(width.viewport)
    expectClean(diagnostics)
  } finally {
    await context.close()
  }
})

test('deleting the token disables pushes and reports honestly', async ({ page }) => {
  const diagnostics = collect(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/telegram')

  await page.getByTestId('telegram-token-delete').click()
  await expect(page.getByTestId('telegram-token-mask')).toHaveText('未配置')
  await expect(page.getByTestId('telegram-save-status')).toContainText('推送已禁用')
  await expect(page.getByTestId('telegram-config-status')).toHaveText('未配置')

  const api = await page.request.get('/api/v1/settings/telegram', {
    headers: { Accept: 'application/json' },
  })
  const body = (await api.json()) as { data: { token: { configured: boolean } } }
  expect(body.data.token.configured).toBe(false)

  // The receipt history is retained; deleting the token does not fabricate one.
  await expect(page.getByTestId('telegram-receipts')).toBeVisible()
  expectClean(diagnostics)
})

test('passes axe with no horizontal scroll at 375, 768, and 1440', async ({ page }) => {
  test.setTimeout(120_000)
  const diagnostics = collect(page)
  for (const viewport of [
    { width: 375, height: 812 },
    { width: 768, height: 1024 },
    { width: 1440, height: 900 },
  ] as const) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await authenticate(page)
    await page.goto('/settings/telegram')
    await expect(page.getByRole('heading', HEADING)).toBeVisible()
    // The coarse-pointer 375px read-only branch is asserted separately below
    // with a mobile context; this matrix proves responsive overflow and axe.
    await expectNoHorizontalScroll(page)
    await runAxeAudit(page, `/settings/telegram @ ${viewport.width}`)
  }
  expectClean(diagnostics)
})

test('completes the form with the keyboard only and never persists the token', async ({ page }) => {
  test.setTimeout(120_000)
  const diagnostics = collect(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/telegram')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()

  // Tab to the write-only token field; Enter submits without a pointer.
  await tabTo(page, 'telegram-token-input')
  await expectVisibleFocus(page, 'telegram-token-input')
  await page.keyboard.type(SENTINEL)
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('telegram-token-mask')).toHaveText('****stuv')
  await expect(page.getByTestId('telegram-token-input')).toHaveValue('')
  await expectTokenAbsentEverywhere(page)

  // Tab through both allowlist add fields, then the toggle and save button.
  await tabTo(page, 'telegram-chat-input')
  await expectVisibleFocus(page, 'telegram-chat-input')
  await page.keyboard.type('1001')
  await tabTo(page, 'telegram-chat-add')
  await page.keyboard.press('Enter')
  await tabTo(page, 'telegram-user-input')
  await page.keyboard.type('2002')
  await tabTo(page, 'telegram-user-add')
  await page.keyboard.press('Enter')

  await tabTo(page, 'telegram-toggle-scan_report')
  await expectVisibleFocus(page, 'telegram-toggle-scan_report')
  const before = await page.getByTestId('telegram-toggle-scan_report').isChecked()
  await page.keyboard.press('Space')
  await expect(page.getByTestId('telegram-toggle-scan_report')).toBeChecked({ checked: !before })

  await tabTo(page, 'telegram-save')
  await expectVisibleFocus(page, 'telegram-save')
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('telegram-save-status')).toContainText('已保存')
  await expect(page.getByTestId('telegram-receipts')).toBeVisible()

  // A reload renders the mask, never the value, in the DOM or in storage.
  await page.reload()
  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  await expect(page.getByTestId('telegram-token-mask')).toHaveText('****stuv')
  await expectTokenAbsentEverywhere(page)
  expectClean(diagnostics)
})
