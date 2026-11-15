import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

/**
 * Critical browser journeys for the settings surface: slice 32 `/settings/scan`
 * (F10d) and slice 34 `/settings/risk` (F10c).
 *
 * Preconditions (provided by `e2e/run-settings-e2e.sh`):
 * - one real daemon on a temporary `OFT_DATA` dir serves `web/dist` and starts
 *   with the default `scan.period=4h` and default risk caps (no persisted rows);
 * - `OFT_E2E_SETTINGS_BOOTSTRAP_FILE` mints the browser session;
 * - `OFT_E2E_SETTINGS_DATA_DIR` names the temp data dir so the test can count
 *   `config`/`audit_events` rows without going through the app.
 *
 * The journeys mutate only the scan period and the risk caps. They never touch
 * an exchange, a live credential, or any other setting.
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

interface RiskUtilizationMetric {
  used: number | null
  cap: number
  unit: string
  source: 'paper_account' | 'none'
  quality: 'COMPLETE' | 'PARTIAL' | 'UNAVAILABLE'
  note: string | null
}

interface RiskSettingsData {
  caps: Record<string, number>
  source: 'default' | 'user'
  defaults: Record<string, number>
  utilization: {
    source: 'paper_account' | 'none'
    quality: 'COMPLETE' | 'PARTIAL' | 'UNAVAILABLE'
    stale_inputs: string[]
    metrics: Record<string, RiskUtilizationMetric>
  }
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
  const read = async () => {
    const cookies = await page.context().cookies()
    return cookies.find((item) => item.name === 'oft_csrf')?.value
  }
  let value = await read()
  if (!value) {
    // The bootstrap response only mints the session cookie; the daemon sets
    // the CSRF cookie on its cookie-path GETs, so mint it before a raw PUT.
    await page.request.get('/health')
    value = await read()
  }
  expect(value, 'the browser session must carry the CSRF cookie').toBeTruthy()
  return value as string
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

async function readRisk(page: Page): Promise<RiskSettingsData> {
  const response = await page.request.get('/api/v1/settings/risk', {
    headers: { Accept: 'application/json' },
  })
  expect(response.status(), 'GET /api/v1/settings/risk').toBe(200)
  const body = (await response.json()) as Envelope<RiskSettingsData>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as RiskSettingsData
}

/** One authenticated, same-origin, CSRF-bearing risk-cap mutation. */
async function putRisk(
  page: Page,
  caps: Record<string, number>,
  confirm: boolean,
  idempotencyKey: string,
) {
  const csrf = await csrfToken(page)
  return page.request.put('/api/v1/settings/risk', {
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
      'X-CSRF-Token': csrf,
      Origin: ORIGIN,
    },
    data: { caps, confirm },
  })
}

/** Count persisted config rows, risk-cap rows, and audit events from SQLite. */
function databaseCounts(): {
  config: number
  audit: number
  risk_caps: number
  risk_audit: number
} {
  if (!DATA_DIR) {
    throw new Error('OFT_E2E_SETTINGS_DATA_DIR is required for the settings journey')
  }
  const script = [
    'import json, os, sqlite3',
    'database = os.path.join(os.environ["OFT_DATA_DIR"], "oft.db")',
    'connection = sqlite3.connect(database)',
    'config = connection.execute("SELECT COUNT(*) FROM config").fetchone()[0]',
    'audit = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]',
    'risk_caps = connection.execute("SELECT COUNT(*) FROM system_state WHERE key = \'risk_caps\'").fetchone()[0]',
    'risk_audit = connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = \'RISK_CAPS_UPDATED\'").fetchone()[0]',
    'connection.close()',
    'print(json.dumps({"config": config, "audit": audit, "risk_caps": risk_caps, "risk_audit": risk_audit}))',
  ].join('\n')
  const output = execFileSync('python3', ['-c', script], {
    cwd: ROOT,
    env: { ...process.env, OFT_DATA_DIR: DATA_DIR },
    encoding: 'utf8',
  })
  return JSON.parse(output.trim()) as {
    config: number
    audit: number
    risk_caps: number
    risk_audit: number
  }
}

interface RiskAuditEvent {
  actorId: string | null
  before: Record<string, number>
  after: Record<string, number>
  loosened: string[]
}

/** Read the persisted RISK_CAPS_UPDATED audit chain (oldest first). */
function riskAuditEvents(): RiskAuditEvent[] {
  if (!DATA_DIR) {
    throw new Error('OFT_E2E_SETTINGS_DATA_DIR is required for the settings journey')
  }
  const script = [
    'import json, os, sqlite3',
    'database = os.path.join(os.environ["OFT_DATA_DIR"], "oft.db")',
    'connection = sqlite3.connect(database)',
    'rows = connection.execute("SELECT payload_json FROM audit_events WHERE event_type = \'RISK_CAPS_UPDATED\' ORDER BY seq ASC").fetchall()',
    'connection.close()',
    'events = []',
    'for (raw,) in rows:',
    '    body = json.loads(raw)',
    '    payload = body.get("payload", {})',
    '    events.append({"actorId": (body.get("actor") or {}).get("id"), "before": payload.get("before", {}), "after": payload.get("after", {}), "loosened": payload.get("loosened", [])})',
    'print(json.dumps(events))',
  ].join('\n')
  const output = execFileSync('python3', ['-c', script], {
    cwd: ROOT,
    env: { ...process.env, OFT_DATA_DIR: DATA_DIR },
    encoding: 'utf8',
  })
  return JSON.parse(output.trim()) as RiskAuditEvent[]
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

// -- Slice 34: risk caps (F10c) ---------------------------------------------

const RISK_HEADING = { name: '风险上限', level: 1 } as const
const RISK_FORM = '修改风险上限'

test('tightens a risk cap with the keyboard and audits exactly one change', async ({ page }) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/risk')

  await expect(page.getByRole('heading', RISK_HEADING)).toBeVisible()
  await expect(page.getByTestId('risk-source')).toHaveText('source: default')
  await expect(page.getByTestId('risk-utilization-quality')).toBeVisible()
  const form = page.getByRole('form', { name: RISK_FORM })
  await expect(form).toBeVisible()

  const before = await readRisk(page)
  expect(before.caps.per_trade_risk_pct).toBe(0.8)
  expect(before.defaults.per_trade_risk_pct).toBe(0.8)
  expect(before.source).toBe('default')
  expect(before.updated_at).toBeNull()
  await expect(page.getByText('仍在使用默认值，尚未保存过。')).toBeVisible()
  await expect(page.getByTestId('risk-defaults')).toContainText('单笔风险 % 0.8')
  const beforeCounts = databaseCounts()

  // Keyboard-only: focus the cap, replace its value, submit with Enter.
  const perTrade = page.locator('#risk-per_trade_risk_pct')
  await perTrade.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('0.5')
  await page.keyboard.press('Enter')

  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('已保存')
  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('0.8')
  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('0.5')
  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('audit 已写入')
  await expect(page.getByTestId('risk-source')).toHaveText('source: user')

  const after = await readRisk(page)
  expect(after.caps.per_trade_risk_pct).toBe(0.5)
  expect(after.source).toBe('user')
  expect(after.updated_by).toBe('web')
  expect(after.updated_at).toMatch(UTC_TIME)

  const afterCounts = databaseCounts()
  expect(afterCounts.risk_caps).toBe(1)
  expect(afterCounts.risk_audit).toBe(beforeCounts.risk_audit + 1)

  // The single audit names the actor and records the real before/after values.
  const events = riskAuditEvents()
  expect(events).toHaveLength(beforeCounts.risk_audit + 1)
  const tightened = events[events.length - 1]
  expect(tightened.actorId).toBe('web')
  expect(tightened.before.per_trade_risk_pct).toBe(0.8)
  expect(tightened.after.per_trade_risk_pct).toBe(0.5)
  expect(tightened.loosened).not.toContain('per_trade_risk_pct')

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-risk-tighten-1440.png'),
    fullPage: true,
  })

  expectClean(diagnostics)
})

test('requires a diff confirmation to loosen and never writes on cancel or inline errors', async ({
  page,
}) => {
  test.setTimeout(90_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/risk')
  await expect(page.getByRole('heading', RISK_HEADING)).toBeVisible()

  const stateBefore = await readRisk(page)
  expect(stateBefore.caps.per_trade_risk_pct).toBe(0.5)
  const countsBefore = databaseCounts()

  // Loosening opens the diff dialog; Escape cancels and writes nothing.
  const perTrade = page.locator('#risk-per_trade_risk_pct')
  await perTrade.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('1.0')
  await page.keyboard.press('Enter')

  const dialog = page.getByRole('dialog', { name: '确认放宽风险上限' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('table', { name: '上限变更前后对比' })).toBeVisible()
  await expect(dialog).toContainText('单笔风险 %')
  await expect(dialog).toContainText('0.5')
  await expect(dialog).toContainText('1')
  expect(databaseCounts()).toEqual(countsBefore)

  // The kill switch stays outside the inert shell and is the final tab stop:
  // Shift+Tab from 取消 reaches the ACTIVATE control, then Tab returns.
  const cancel = dialog.getByRole('button', { name: '取消' })
  await expect(cancel).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.locator('[data-kill-switch-action="activate"]')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(cancel).toBeFocused()

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('未写入任何变更')
  // Focus returns to the control that opened the dialog, never lost to <body>.
  await expect(perTrade).toBeFocused()
  expect(databaseCounts()).toEqual(countsBefore)
  expect((await readRisk(page)).caps.per_trade_risk_pct).toBe(0.5)

  // The draft is retained; saving again opens the dialog, and confirming writes.
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-risk-loosen-dialog-1440.png'),
    fullPage: true,
  })
  await dialog.getByRole('button', { name: '确认放宽' }).click()

  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('audit 已写入')
  await expect(dialog).toBeHidden()
  const loosened = await readRisk(page)
  expect(loosened.caps.per_trade_risk_pct).toBe(1)
  const afterConfirm = databaseCounts()
  expect(afterConfirm.risk_audit).toBe(countsBefore.risk_audit + 1)
  expect(afterConfirm.risk_caps).toBe(1)

  // The confirmed loosening is audited with its before/after and loosened field.
  const confirmedEvents = riskAuditEvents()
  expect(confirmedEvents).toHaveLength(countsBefore.risk_audit + 1)
  const confirmed = confirmedEvents[confirmedEvents.length - 1]
  expect(confirmed.actorId).toBe('web')
  expect(confirmed.before.per_trade_risk_pct).toBe(0.5)
  expect(confirmed.after.per_trade_risk_pct).toBe(1)
  expect(confirmed.loosened).toContain('per_trade_risk_pct')

  // daily-loss below per-trade is blocked inline and calls no API.
  const daily = page.locator('#risk-daily_loss_pct')
  await daily.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('0.5')
  await page.getByRole('button', { name: '保存风控' }).click()

  await expect(page.getByTestId('settings-risk-error')).toContainText('无法保存')
  await expect(page.getByText('日亏损上限必须 ≥ 单笔风险')).toBeVisible()
  await expect(daily).toHaveAttribute('aria-invalid', 'true')
  await expect(dialog).toBeHidden()
  expect(databaseCounts()).toEqual(afterConfirm)
  expect((await readRisk(page)).caps.daily_loss_pct).toBe(3)

  // Out-of-range leverage must be blocked by the app's inline error, not the
  // browser's native constraint bubble (the form is ``noValidate``).
  const leverage = page.locator('#risk-max_leverage')
  await leverage.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('200')
  await page.getByRole('button', { name: '保存风控' }).click()

  const summary = page.getByTestId('settings-risk-error')
  await expect(summary).toContainText('无法保存')
  await expect(summary).toBeFocused()
  await expect(page.getByText('必须 ≤ 125')).toBeVisible()
  await expect(leverage).toHaveAttribute('aria-invalid', 'true')
  await expect(summary.getByRole('link', { name: '转到杠杆上限 x' })).toHaveAttribute(
    'href',
    '#risk-max_leverage',
  )
  expect(databaseCounts()).toEqual(afterConfirm)

  // Restore the invalid draft fields to their effective values before the
  // off-step save, so only per-trade changes.
  await leverage.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('5')

  // A server-valid off-step value (0.55) must be accepted by client and server.
  const dailyAgain = page.locator('#risk-daily_loss_pct')
  await dailyAgain.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('3')
  const perTradeAgain = page.locator('#risk-per_trade_risk_pct')
  await perTradeAgain.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('0.55')
  await page.getByRole('button', { name: '保存风控' }).click()

  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('已保存')
  expect((await readRisk(page)).caps.per_trade_risk_pct).toBe(0.55)
  expect(databaseCounts().risk_audit).toBe(afterConfirm.risk_audit + 1)

  // Two 422s are the designed confirmation-required refusals; Chromium logs
  // each as a failed resource. Any other page/console error is still a failure.
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(
    diagnostics.consoleErrors.filter((message) => !message.includes('status of 422')),
    'unexpected console errors',
  ).toEqual([])
})

test('replays risk idempotency and rejects a different payload under the same key', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  const key = 'e2e-risk-replay-key'
  const first = await putRisk(page, { max_leverage: 4.0 }, false, key)
  expect(first.status()).toBe(200)
  const firstBody = (await first.json()) as Envelope<RiskSettingsData>
  expect(firstBody.data?.caps.max_leverage).toBe(4)

  const replay = await putRisk(page, { max_leverage: 4.0 }, false, key)
  expect(replay.status()).toBe(200)
  expect(((await replay.json()) as Envelope<RiskSettingsData>).meta?.idempotent_replay).toBe(true)

  const conflict = await putRisk(page, { max_leverage: 5.0 }, false, key)
  expect(conflict.status()).toBe(409)
  expect(((await conflict.json()) as Envelope<RiskSettingsData>).error?.code).toBe(
    'IDEMPOTENCY_CONFLICT',
  )

  expectClean(diagnostics)
})

test('renders the risk screen at 375/768/1440 and reports no axe violations', async ({
  page,
  browser,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/risk')
  await expect(page.getByRole('heading', RISK_HEADING)).toBeVisible()
  await page.waitForLoadState('networkidle')

  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze()
  const summary = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  )
  expect(results.violations, `settings/risk axe violations:\n${summary.join('\n')}`).toEqual([])

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  fs.writeFileSync(
    path.join(SCREENS_DIR, 'settings-risk-axe.json'),
    JSON.stringify(
      {
        url: '/settings/risk',
        tags: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
        violationCount: results.violations.length,
        violations: results.violations.map((violation) => ({
          id: violation.id,
          impact: violation.impact ?? null,
          targets: violation.nodes.map((node) => node.target.join(' ')),
        })),
      },
      null,
      2,
    ),
  )

  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-risk-1440.png'),
    fullPage: true,
  })

  const touchContext = await browser.newContext({
    baseURL: BASE_URL,
    hasTouch: true,
    isMobile: true,
    viewport: { width: 375, height: 812 },
  })
  const touchPage = await touchContext.newPage()
  const touchDiagnostics = collectDiagnostics(touchPage)
  try {
    await authenticate(touchPage)

    await expectRiskViewport(touchPage, {
      width: 375,
      height: 812,
      readonly: true,
      name: 'settings-risk-375.png',
    })
    await expectRiskViewport(touchPage, {
      width: 768,
      height: 1024,
      readonly: false,
      name: 'settings-risk-768.png',
    })
  } finally {
    await touchContext.close()
  }

  expectClean(diagnostics)
  expectClean(touchDiagnostics)
})

interface RiskViewport {
  width: number
  height: number
  readonly: boolean
  name: string
}

async function expectRiskViewport(page: Page, viewport: RiskViewport): Promise<void> {
  await page.setViewportSize({ width: viewport.width, height: viewport.height })
  await page.goto('/settings/risk')
  await expect(page.getByRole('heading', RISK_HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute(
    'data-readonly',
    viewport.readonly ? 'true' : 'false',
  )

  const save = page.getByRole('button', { name: '保存风控' })
  const perTrade = page.locator('#risk-per_trade_risk_pct')
  if (viewport.readonly) {
    await expect(page.locator('section.system-page').getByRole('note')).toContainText(
      '在桌面端或 CLI 修改',
    )
    await expect(perTrade).toBeDisabled()
    await expect(save).toBeDisabled()
  } else {
    await expect(perTrade).toBeEnabled()
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
}
