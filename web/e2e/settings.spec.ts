import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

/**
 * Critical browser journey for slice 32: `/settings/scan` (F10d).
 *
 * Preconditions (provided by `e2e/run-settings-e2e.sh`):
 * - one real daemon on a temporary `OFT_DATA` dir serves `web/dist` and starts
 *   with the default `scan.period=4h` (no persisted config row);
 * - `OFT_E2E_SETTINGS_BOOTSTRAP_FILE` mints the browser session;
 * - `OFT_E2E_SETTINGS_DATA_DIR` names the temp data dir so the test can count
 *   `config`/`audit_events` rows without going through the app.
 *
 * The journey mutates exactly one setting. It never touches an exchange, a
 * live credential, or any other setting.
 */

const BOOTSTRAP_FILE = process.env.OFT_E2E_SETTINGS_BOOTSTRAP_FILE
const DATA_DIR = process.env.OFT_E2E_SETTINGS_DATA_DIR
const BASE_URL = process.env.OFT_E2E_SETTINGS_BASE_URL ?? 'http://127.0.0.1:7652'
const ORIGIN = new URL(BASE_URL).origin
const ROOT = path.resolve(process.cwd(), '..')
const SCREENS_DIR =
  process.env.OFT_E2E_SETTINGS_SCREENS_DIR ??
  path.resolve(ROOT, '.claude', 'ecc-workflow', 'screens', 'slice-32-e2e')

const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/
const READ_ONLY_NOTE = /在桌面端或 CLI 修改/
const HEADING = { name: '扫描周期与排程', level: 1 } as const
const FORM_NAME = '修改扫描周期'

interface ErrorDetail {
  field?: string | null
  code?: string
  message?: string
}

interface Envelope<T> {
  ok: boolean
  data: T | null
  error: { code?: string; message?: string; details?: ErrorDetail[] } | null
  meta?: { idempotent_replay?: boolean; request_id?: string }
}

interface ScanSettingsData {
  key: string
  value: string
  source: 'default' | 'user'
  supported: string[]
  next_run: string
  timezone: string
  updated_at: string | null
  updated_by: string | null
}

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

function expectClean(diagnostics: BrowserDiagnostics): void {
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(diagnostics.consoleErrors, 'console errors').toEqual([])
}

/** Mint a browser session from the daemon's one-time bootstrap file. */
async function authenticate(page: Page): Promise<void> {
  if (!BOOTSTRAP_FILE) {
    throw new Error('OFT_E2E_SETTINGS_BOOTSTRAP_FILE is required for the settings journey')
  }
  const secret = fs.readFileSync(BOOTSTRAP_FILE, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

async function csrfToken(page: Page): Promise<string> {
  const cookies = await page.context().cookies()
  const cookie = cookies.find((item) => item.name === 'oft_csrf')
  expect(cookie?.value, 'the browser session must carry the CSRF cookie').toBeTruthy()
  return cookie?.value as string
}

async function readScan(page: Page): Promise<ScanSettingsData> {
  const response = await page.request.get('/api/v1/settings/scan', {
    headers: { Accept: 'application/json' },
  })
  expect(response.status(), 'GET /api/v1/settings/scan').toBe(200)
  const body = (await response.json()) as Envelope<ScanSettingsData>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as ScanSettingsData
}

/** One authenticated, same-origin, CSRF-bearing scan-period mutation. */
async function putScan(page: Page, period: string, idempotencyKey: string) {
  const csrf = await csrfToken(page)
  return page.request.put('/api/v1/settings/scan', {
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
      'X-CSRF-Token': csrf,
      Origin: ORIGIN,
    },
    data: { period },
  })
}

/** Count the persisted config rows and audit events straight from SQLite. */
function databaseCounts(): { config: number; audit: number } {
  if (!DATA_DIR) {
    throw new Error('OFT_E2E_SETTINGS_DATA_DIR is required for the settings journey')
  }
  const script = [
    'import json, os, sqlite3',
    'database = os.path.join(os.environ["OFT_DATA_DIR"], "oft.db")',
    'connection = sqlite3.connect(database)',
    'config = connection.execute("SELECT COUNT(*) FROM config").fetchone()[0]',
    'audit = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]',
    'connection.close()',
    'print(json.dumps({"config": config, "audit": audit}))',
  ].join('\n')
  const output = execFileSync('python3', ['-c', script], {
    cwd: ROOT,
    env: { ...process.env, OFT_DATA_DIR: DATA_DIR },
    encoding: 'utf8',
  })
  return JSON.parse(output.trim()) as { config: number; audit: number }
}

function isPeriodRadioFocused(): boolean {
  const active = document.activeElement
  return (
    active instanceof HTMLInputElement && active.type === 'radio' && active.name === 'scan-period'
  )
}

/** Tab from the page start until a period radio receives focus (keyboard only). */
async function tabToPeriodRadio(page: Page, max = 60): Promise<void> {
  for (let index = 0; index < max; index += 1) {
    await page.keyboard.press('Tab')
    if (await page.evaluate(isPeriodRadioFocused)) {
      return
    }
  }
  throw new Error(`Tab did not reach the scan-period radio group within ${max} presses`)
}

test.beforeAll(async ({ request }) => {
  expect(BOOTSTRAP_FILE, 'run-settings-e2e.sh must export the bootstrap file').toBeTruthy()
  expect(DATA_DIR, 'run-settings-e2e.sh must export the data dir').toBeTruthy()
  expect(fs.existsSync(BOOTSTRAP_FILE as string), 'the bootstrap file must exist').toBe(true)

  // Fail fast if the daemon is not the fresh one this journey expects.
  const response = await request.get('/health')
  expect(response.status()).toBe(200)
})

test.describe.configure({ mode: 'serial' })

test('loads the default 4h scan setting with its source and next UTC boundary', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/scan')

  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')
  await expect(page.getByTestId('scan-source')).toHaveText('source: default')
  await expect(page.getByTestId('scan-current-value').locator('strong')).toHaveText('4h')
  await expect(page.getByText(/可用值 4h, 8h/)).toBeVisible()
  await expect(page.getByText('仍在使用默认值，尚未保存过。')).toBeVisible()
  await expect(page.getByTestId('scan-timezone')).toHaveText('UTC')

  const displayedNextRun = (await page.getByTestId('scan-next-run').textContent())?.trim() ?? ''
  expect(displayedNextRun, 'next run is a UTC timestamp').toMatch(UTC_TIME)

  const form = page.getByRole('form', { name: FORM_NAME })
  await expect(form).toBeVisible()
  await expect(form.getByRole('radio', { name: '4h' })).toBeChecked()
  await expect(form.getByRole('radio', { name: '8h' })).not.toBeChecked()
  await expect(form.getByRole('button', { name: '保存排程' })).toBeEnabled()

  const api = await readScan(page)
  expect(api.key).toBe('scan.period')
  expect(api.value).toBe('4h')
  expect(api.source).toBe('default')
  expect(api.supported).toEqual(['4h', '8h'])
  expect(api.timezone).toBe('UTC')
  expect(api.updated_at).toBeNull()
  expect(api.next_run).toBe(displayedNextRun)

  expectClean(diagnostics)
})

test('saves a new period with the keyboard only and announces the result', async ({ page }) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  // ``allHeaders()`` includes browser-added headers (Origin); ``headers()``
  // only returns the headers the page set itself.
  const puts: Array<Promise<Record<string, string>>> = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (request.method() === 'PUT' && url.pathname === '/api/v1/settings/scan') {
      puts.push(request.allHeaders())
    }
  })

  await authenticate(page)
  await page.goto('/settings/scan')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()

  const form = page.getByRole('form', { name: FORM_NAME })
  const radio4h = form.getByRole('radio', { name: '4h' })
  const radio8h = form.getByRole('radio', { name: '8h' })
  await expect(radio4h).toBeChecked()

  // Keyboard-only: Tab to the radio group, change it, Tab to save, Enter.
  await tabToPeriodRadio(page)
  await expect(radio4h).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(radio8h).toBeChecked()
  await expect(radio8h).toBeFocused()

  const saveButton = form.getByRole('button', { name: '保存排程' })
  await page.keyboard.press('Tab')
  await expect(saveButton).toBeFocused()
  await expect(saveButton).toBeEnabled()
  await page.keyboard.press('Enter')

  // The save is announced through the polite status region.
  await expect(form.getByRole('status')).toContainText('已保存：扫描周期 4h → 8h')
  await expect(page.getByTestId('scan-source')).toHaveText('source: user')
  await expect(page.getByTestId('scan-current-value').locator('strong')).toHaveText('8h')
  await expect(page.getByText(/上次修改/)).toBeVisible()

  const displayedNextRun = (await page.getByTestId('scan-next-run').textContent())?.trim() ?? ''
  expect(displayedNextRun, 'next run is a UTC timestamp').toMatch(UTC_TIME)

  const persisted = await readScan(page)
  expect(persisted.value).toBe('8h')
  expect(persisted.source).toBe('user')
  expect(persisted.updated_by).toBe('web')
  expect(persisted.updated_at).toMatch(UTC_TIME)
  expect(displayedNextRun).toBe(persisted.next_run)

  // Exactly one mutation, carrying CSRF, the idempotency key, and the origin.
  expect(puts).toHaveLength(1)
  const putHeaders = await puts[0]
  expect(putHeaders['idempotency-key']).toBeTruthy()
  expect(putHeaders['x-csrf-token']).toBeTruthy()
  expect(putHeaders.origin).toBe(ORIGIN)

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-scan-saved-1440.png'),
    fullPage: true,
  })

  expectClean(diagnostics)
})

test('reloads the persisted user value and the API agrees', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/scan')

  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  await expect(page.getByTestId('scan-source')).toHaveText('source: user')
  await expect(page.getByTestId('scan-current-value').locator('strong')).toHaveText('8h')

  await page.reload()
  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  await expect(page.getByTestId('scan-source')).toHaveText('source: user')
  await expect(page.getByTestId('scan-current-value').locator('strong')).toHaveText('8h')

  const api = await readScan(page)
  expect(api.value).toBe('8h')
  expect(api.source).toBe('user')

  expectClean(diagnostics)
})

test('saves the already-effective value and writes no row or audit', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/scan')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()

  const form = page.getByRole('form', { name: FORM_NAME })
  await expect(form.getByRole('radio', { name: '8h' })).toBeChecked()
  const before = await readScan(page)
  const beforeCounts = databaseCounts()
  expect(before.value).toBe('8h')

  // Saving without changing the value is a deterministic no-op: the copy says
  // so explicitly and no new config/audit row is written.
  await form.getByRole('button', { name: '保存排程' }).click()
  const status = form.getByRole('status')
  await expect(status).toContainText('未变更，未写入新 audit')
  await expect(status).not.toContainText('audit 已写入')
  await expect(page.getByTestId('scan-current-value').locator('strong')).toHaveText('8h')

  const after = await readScan(page)
  expect(after.value).toBe(before.value)
  expect(after.source).toBe(before.source)
  expect(after.updated_at).toBe(before.updated_at)
  expect(databaseCounts()).toEqual(beforeCounts)

  expectClean(diagnostics)
})

test('replays an identical idempotent save and rejects a different payload under the same key', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/scan')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()

  const key = 'e2e-settings-replay-key'
  const first = await putScan(page, '4h', key)
  expect(first.status()).toBe(200)
  const firstBody = (await first.json()) as Envelope<ScanSettingsData>
  expect(firstBody.ok).toBe(true)
  expect(firstBody.data?.value).toBe('4h')
  expect(firstBody.data?.source).toBe('user')
  expect(firstBody.meta?.idempotent_replay).toBe(false)

  // The exact same payload under the same key replays the stored envelope.
  const second = await putScan(page, '4h', key)
  expect(second.status()).toBe(200)
  const secondBody = (await second.json()) as Envelope<ScanSettingsData>
  expect(secondBody.ok).toBe(true)
  expect(secondBody.meta?.idempotent_replay).toBe(true)
  expect(secondBody.data?.value).toBe('4h')
  expect(secondBody.data?.updated_at).toBe(firstBody.data?.updated_at)

  // A different payload under the same key is a hard conflict, never a write.
  const conflict = await putScan(page, '8h', key)
  expect(conflict.status()).toBe(409)
  const conflictBody = (await conflict.json()) as Envelope<ScanSettingsData>
  expect(conflictBody.ok).toBe(false)
  expect(conflictBody.error?.code).toBe('IDEMPOTENCY_CONFLICT')

  const persisted = await readScan(page)
  expect(persisted.value).toBe('4h')
  expect(persisted.source).toBe('user')

  expectClean(diagnostics)
})

test('rejects an unsupported period with a field detail and writes nothing', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/scan')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()

  const before = await readScan(page)
  const beforeCounts = databaseCounts()
  expect(before.value).toBe('4h')

  const response = await putScan(page, '1h', 'e2e-settings-invalid-key')
  expect(response.status()).toBe(422)
  const body = (await response.json()) as Envelope<ScanSettingsData>
  expect(body.ok).toBe(false)
  expect(body.error?.code).toBe('VALIDATION_FAILED')
  const fields = (body.error?.details ?? []).map((detail) => detail.field)
  expect(fields).toContain('period')

  // Nothing was written: same effective value, same updated_at, same row counts.
  const after = await readScan(page)
  expect(after.value).toBe(before.value)
  expect(after.source).toBe(before.source)
  expect(after.updated_at).toBe(before.updated_at)
  expect(databaseCounts()).toEqual(beforeCounts)

  expectClean(diagnostics)
})

interface ScanViewport {
  width: number
  height: number
  readonly: boolean
  name: string
}

async function expectScanViewport(page: Page, viewport: ScanViewport): Promise<void> {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: viewport.width, height: viewport.height })
  await page.goto('/settings/scan')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute(
    'data-readonly',
    viewport.readonly ? 'true' : 'false',
  )

  const form = page.getByRole('form', { name: FORM_NAME })
  const save = form.getByRole('button', { name: '保存排程' })
  const radio8h = form.getByRole('radio', { name: '8h' })

  if (viewport.readonly) {
    // Slice 33 adds a second `role="note"` (the compact nav's read-only note),
    // so the settings-surface note is scoped to its own page section.
    await expect(page.locator('section.system-page').getByRole('note')).toContainText(
      READ_ONLY_NOTE,
    )
    await expect(page.getByText('请在桌面端或 CLI 修改。', { exact: true })).toBeVisible()
    await expect(radio8h).toBeDisabled()
    await expect(save).toBeDisabled()
  } else {
    await expect(page.locator('section.system-page').getByRole('note')).toHaveCount(0)
    await expect(radio8h).toBeEnabled()
    await expect(save).toBeEnabled()
  }

  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.document, `no horizontal overflow at ${viewport.width}`).toBeLessThanOrEqual(
    widths.viewport,
  )

  await page.screenshot({ path: path.join(SCREENS_DIR, viewport.name), fullPage: true })
  expectClean(diagnostics)
}

test('focuses the scan-period group when the error summary link is followed', async ({ page }) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  // Force one definitive refusal so the field-error summary renders. The PUT is
  // intercepted before it reaches the daemon, so nothing is written.
  await page.route('**/api/v1/settings/scan', async (route, request) => {
    if (request.method() !== 'PUT') {
      await route.continue()
      return
    }
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: false,
        data: null,
        error: {
          code: 'VALIDATION_FAILED',
          message: 'scan.period must be one of: 4h, 8h',
          category: 'VALIDATION',
          http_status: 422,
          retryable: false,
          reference_id: 'REQ-E2E-FOCUS',
          details: [{ field: 'period', code: 'unsupported', message: 'invalid period' }],
        },
        meta: {},
      }),
    })
  })

  await page.goto('/settings/scan')
  await expect(page.getByRole('heading', HEADING)).toBeVisible()
  const form = page.getByRole('form', { name: FORM_NAME })
  const radio4h = form.getByRole('radio', { name: '4h' })
  const radio8h = form.getByRole('radio', { name: '8h' })
  if (await radio8h.isChecked()) {
    await radio4h.check()
  } else {
    await radio8h.check()
  }
  await form.getByRole('button', { name: '保存排程' }).click()

  const summary = page.getByRole('alert')
  await expect(summary).toContainText('错误：')
  await summary.getByRole('link', { name: '转到扫描周期' }).click()

  // The native in-page link moves focus to the focusable radio fieldset.
  await expect(page.getByRole('group', { name: '扫描周期' })).toBeFocused()
  // The intercepted 422 is the intended outcome; Chromium logs it as a failed
  // resource. Any other page/console error is still a failure.
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(
    diagnostics.consoleErrors.filter((message) => !message.includes('status of 422')),
    'unexpected console errors',
  ).toEqual([])
})

test('is read-only only on phone-width touch and keeps fine-pointer zoom editable', async ({
  page,
  browser,
}) => {
  test.setTimeout(90_000)
  fs.mkdirSync(SCREENS_DIR, { recursive: true })

  // A fine-pointer desktop stays editable at 1440 and at 720 CSS px (200% zoom).
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await expectScanViewport(page, {
    width: 1440,
    height: 900,
    readonly: false,
    name: 'settings-scan-1440.png',
  })
  await expectScanViewport(page, {
    width: 720,
    height: 900,
    readonly: false,
    name: 'settings-scan-720-fine.png',
  })

  // A coarse-pointer phone is read-only, while a true 768px tablet is editable.
  const touchContext = await browser.newContext({
    baseURL: BASE_URL,
    hasTouch: true,
    isMobile: true,
    viewport: { width: 375, height: 812 },
  })
  const touchPage = await touchContext.newPage()
  try {
    await authenticate(touchPage)
    await expectScanViewport(touchPage, {
      width: 375,
      height: 812,
      readonly: true,
      name: 'settings-scan-375.png',
    })
    await expectScanViewport(touchPage, {
      width: 768,
      height: 1024,
      readonly: false,
      name: 'settings-scan-768.png',
    })
  } finally {
    await touchContext.close()
  }
})
