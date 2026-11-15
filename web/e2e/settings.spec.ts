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

/**
 * Delete the persisted risk-cap row so a journey starts from env defaults.
 *
 * The tighten and loosen journeys would otherwise inherit each other's caps;
 * deleting the one `system_state.risk_caps` row makes every journey independent
 * and keeps the audit-count assertions relative to a post-reset count.
 */
function resetRiskCaps(): void {
  if (!DATA_DIR) {
    throw new Error('OFT_E2E_SETTINGS_DATA_DIR is required for the settings journey')
  }
  const script = [
    'import os, sqlite3',
    'database = os.path.join(os.environ["OFT_DATA_DIR"], "oft.db")',
    'connection = sqlite3.connect(database)',
    'connection.execute("DELETE FROM system_state WHERE key = \'risk_caps\'")',
    'connection.commit()',
    'connection.close()',
    'print("reset")',
  ].join('\n')
  execFileSync('python3', ['-c', script], {
    cwd: ROOT,
    env: { ...process.env, OFT_DATA_DIR: DATA_DIR },
    encoding: 'utf8',
  })
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

interface PromptAuditEvent {
  event_type: string
  subjectId: string | null
  version: number | null
}

/** Read the prompt lifecycle audit chain (oldest first) from SQLite. */
function promptAuditEvents(): PromptAuditEvent[] {
  if (!DATA_DIR) {
    throw new Error('OFT_E2E_SETTINGS_DATA_DIR is required for the settings journey')
  }
  const script = [
    'import json, os, sqlite3',
    'database = os.path.join(os.environ["OFT_DATA_DIR"], "oft.db")',
    'connection = sqlite3.connect(database)',
    'rows = connection.execute("SELECT event_type, payload_json FROM audit_events WHERE event_type LIKE \'PROMPT_%\' ORDER BY seq ASC").fetchall()',
    'connection.close()',
    'events = []',
    'for event_type, raw in rows:',
    '    body = json.loads(raw)',
    '    payload = body.get("payload", {})',
    '    events.append({"event_type": event_type, "subjectId": (body.get("subject") or {}).get("id"), "version": payload.get("version")})',
    'print(json.dumps(events))',
  ].join('\n')
  const output = execFileSync('python3', ['-c', script], {
    cwd: ROOT,
    env: { ...process.env, OFT_DATA_DIR: DATA_DIR },
    encoding: 'utf8',
  })
  return JSON.parse(output.trim()) as PromptAuditEvent[]
}

function isPeriodRadioFocused(): boolean {  const active = document.activeElement
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
  resetRiskCaps()
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
  resetRiskCaps()
  await authenticate(page)
  await page.goto('/settings/risk')
  await expect(page.getByRole('heading', RISK_HEADING)).toBeVisible()

  const stateBefore = await readRisk(page)
  expect(stateBefore.caps.per_trade_risk_pct).toBe(0.8)
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
  await expect(dialog).toContainText('0.8')
  await expect(dialog).toContainText('1')
  expect(databaseCounts()).toEqual(countsBefore)

  // The kill switch stays outside the inert shell and joins the cycle: the
  // diff region is the dialog's first tab stop, so Shift+Tab from 取消 reaches
  // it, the next Shift+Tab wraps to the ACTIVATE control, and Tab returns.
  const cancel = dialog.getByRole('button', { name: '取消' })
  const diffRegion = dialog.getByRole('region', { name: '上限变更前后对比' })
  await expect(cancel).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(diffRegion).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.locator('[data-kill-switch-action="activate"]')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(diffRegion).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(cancel).toBeFocused()

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('未写入任何变更')
  // Focus returns to the control that opened the dialog, never lost to <body>.
  await expect(perTrade).toBeFocused()
  expect(databaseCounts()).toEqual(countsBefore)
  expect((await readRisk(page)).caps.per_trade_risk_pct).toBe(0.8)

  // The draft is retained; saving again opens the dialog, and confirming writes.
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-risk-loosen-dialog-1440.png'),
    fullPage: true,
  })

  // Hold the confirmed PUT in flight so the modal's busy focus contract is
  // observable in a real browser: native ``disabled`` would blur the control to
  // <body>, so the buttons must stay enabled with ``aria-disabled`` instead.
  await page.route('**/api/v1/settings/risk', async (route, request) => {
    if (request.method() !== 'PUT') {
      await route.continue()
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 400))
    await route.continue()
  })
  await dialog.getByRole('button', { name: '确认放宽' }).click()

  const busyState = await page.evaluate(() => {
    const panel = document.querySelector('[role="dialog"]')
    const buttons = panel ? Array.from(panel.querySelectorAll('button')) : []
    const confirm = buttons.find((button) => button.textContent?.includes('保存中'))
    const cancel = buttons.find((button) => button.textContent?.includes('取消'))
    return {
      focusInsideDialog: document.activeElement?.closest('[role="dialog"]') !== null,
      confirmAriaDisabled: confirm?.getAttribute('aria-disabled') ?? null,
      cancelAriaDisabled: cancel?.getAttribute('aria-disabled') ?? null,
      confirmNativeDisabled: confirm?.hasAttribute('disabled') ?? null,
      cancelNativeDisabled: cancel?.hasAttribute('disabled') ?? null,
    }
  })
  expect(busyState).toEqual({
    focusInsideDialog: true,
    confirmAriaDisabled: 'true',
    cancelAriaDisabled: 'true',
    confirmNativeDisabled: false,
    cancelNativeDisabled: false,
  })

  // Let the delayed PUT finish before removing the interception: unroute while
  // the handler is still sleeping makes its ``route.continue()`` invalid.
  await expect(page.getByTestId('settings-risk-save-visible')).toContainText('audit 已写入')
  await expect(dialog).toBeHidden()
  await page.unroute('**/api/v1/settings/risk')
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
  expect(confirmed.before.per_trade_risk_pct).toBe(0.8)
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
  resetRiskCaps()
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
  resetRiskCaps()
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

// -- slice 35: LLM providers, write-only key masking -------------------------

const LLM_HEADING = { name: 'LLM providers 与模型', level: 1 } as const
const LLM_KEY_SENTINEL = 'sk-e2e-SENTINEL-DO-NOT-RENDER-4f2a'

test('LLM settings keep the provider key write-only and mask it everywhere', async ({
  page,
  browser,
}) => {
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()

  await expect(page.getByTestId('llm-provider-mask-openai')).toHaveText('—')
  await expect(page.getByText('密钥只写：保存后仅显示固定掩码与最后 4 位').first()).toBeVisible()

  await page.getByTestId('llm-key-input-anthropic').fill(LLM_KEY_SENTINEL)
  await page.getByTestId('llm-key-save-anthropic').click()

  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('****4f2a')
  await expect(page.getByTestId('llm-key-input-anthropic')).toHaveValue('')
  const html = await page.content()
  expect(html).not.toContain(LLM_KEY_SENTINEL)
  expect(html).not.toContain(LLM_KEY_SENTINEL.slice(0, 12))

  const response = await page.request.get('/api/v1/settings/llm', {
    headers: { Accept: 'application/json' },
  })
  expect(response.status()).toBe(200)
  const text = await response.text()
  expect(text).not.toContain(LLM_KEY_SENTINEL)
  expect(text).not.toContain(LLM_KEY_SENTINEL.slice(0, 12))
  const body = JSON.parse(text) as {
    data: { providers: { provider: string; masked: string | null; key_source: string }[] }
  }
  const anthropic = body.data.providers.find((row) => row.provider === 'anthropic')
  expect(anthropic?.masked).toBe('****4f2a')
  // Either secure store is acceptable; the value must never come from the
  // environment or be missing once saved.
  expect(['keyring', 'encrypted_file']).toContain(anthropic?.key_source)

  for (const width of [1440, 768, 375]) {
    await page.setViewportSize({ width, height: width === 375 ? 812 : 1024 })
    await page.goto('/settings/llm')
    await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
    const widths = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(widths.document, `no horizontal overflow at ${width}`).toBeLessThanOrEqual(
      widths.viewport,
    )
    await page.screenshot({
      path: path.join(SCREENS_DIR, `settings-llm-${width}.png`),
      fullPage: true,
    })
  }

  const axe = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze()
  expect(
    axe.violations,
    JSON.stringify(
      axe.violations.map((item) => ({ id: item.id, target: item.nodes.map((n) => n.target) })),
    ),
  ).toEqual([])

  // Remove the test key so the journey leaves the daemon and either secret
  // backend clean (it is a temporary data dir, but a keyring backend is global).
  const csrf = await csrfToken(page)
  const deleted = await page.request.delete('/api/v1/settings/llm/anthropic/key', {
    headers: {
      Accept: 'application/json',
      'Idempotency-Key': 'e2e-llm-key-delete',
      'X-CSRF-Token': csrf,
      Origin: ORIGIN,
    },
  })
  expect(deleted.status(), 'DELETE /api/v1/settings/llm/anthropic/key').toBe(200)
  const afterDelete = (await deleted.json()).data
  // The stored key is gone; an operator-owned environment key may remain and is
  // reported as `env` with no last4. Neither case exposes a value.
  expect(['unset', 'env']).toContain(afterDelete.key_source)
  expect(afterDelete.masked).toBeNull()

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
    await touchPage.goto('/settings/llm')
    await expect(touchPage.getByRole('heading', LLM_HEADING)).toBeVisible()
    await expect(touchPage.locator('section.system-page')).toHaveAttribute('data-readonly', 'true')
    await expect(touchPage.locator('section.system-page').getByRole('note')).toContainText(
      '在桌面端或 CLI 修改',
    )
    await expect(touchPage.getByTestId('llm-key-input-anthropic')).toBeDisabled()
  } finally {
    await touchContext.close()
  }

  expectClean(diagnostics)
  expectClean(touchDiagnostics)
})

// -- Slice 35: full `/settings/llm` journeys --------------------------------

interface LLMProviderStatus {
  provider: string
  configured: boolean
  key_source: string
  masked: string | null
  fingerprint: string | null
  updated_at: string | null
  updated_by: string | null
  base_url: string
  capability: null | {
    status: string
    strict_schema: boolean
    degraded: boolean
    checked_at: string
    notes: string
  }
}

interface LLMSettingsPayload {
  provider: string | null
  providers: LLMProviderStatus[]
  roles: Record<string, string | null>
  base_urls: Record<string, string>
  allow_custom_base_url: boolean
  concurrency: { global: number; deepseek: number }
  source: 'default' | 'user'
  updated_at: string | null
  updated_by: string | null
}

const LLM_FAKE_BASE = process.env.OFT_E2E_LLM_FAKE_BASE_URL
const FORBIDDEN_REVEAL = /查看|显示原值|显示密钥|reveal|show key/i

async function readLLM(page: Page): Promise<LLMSettingsPayload> {
  const response = await page.request.get('/api/v1/settings/llm', {
    headers: { Accept: 'application/json' },
  })
  expect(response.status(), 'GET /api/v1/settings/llm').toBe(200)
  const body = (await response.json()) as Envelope<LLMSettingsPayload>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as LLMSettingsPayload
}

async function llmHeaders(page: Page, idempotencyKey: string): Promise<Record<string, string>> {
  const csrf = await csrfToken(page)
  return {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'Idempotency-Key': idempotencyKey,
    'X-CSRF-Token': csrf,
    Origin: ORIGIN,
  }
}

async function putLLMKey(page: Page, provider: string, apiKey: string, idempotencyKey: string) {
  return page.request.put(`/api/v1/settings/llm/${provider}/key`, {
    headers: await llmHeaders(page, idempotencyKey),
    data: { api_key: apiKey },
  })
}

async function putLLMSettings(page: Page, patch: Record<string, unknown>, idempotencyKey: string) {
  return page.request.put('/api/v1/settings/llm', {
    headers: await llmHeaders(page, idempotencyKey),
    data: patch,
  })
}

interface FakeRequests {
  count: number
  paths: string[]
  auth_present: boolean
}

async function fakeRequests(page: Page): Promise<FakeRequests> {
  if (!LLM_FAKE_BASE) {
    throw new Error('OFT_E2E_LLM_FAKE_BASE_URL is required for the LLM probe journey')
  }
  const response = await page.request.get(`${LLM_FAKE_BASE}/__requests`)
  expect(response.status(), 'GET fake provider /__requests').toBe(200)
  return (await response.json()) as FakeRequests
}

function daemonLog(): string {
  if (!DATA_DIR) {
    throw new Error('OFT_E2E_SETTINGS_DATA_DIR is required for the settings journey')
  }
  return fs.readFileSync(path.join(DATA_DIR, 'daemon.log'), 'utf8')
}

function expectCleanExceptRefusals(
  diagnostics: BrowserDiagnostics,
  refusalMarkers: readonly string[],
): void {
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(
    diagnostics.consoleErrors.filter(
      (message) => !refusalMarkers.some((marker) => message.includes(marker)),
    ),
    'unexpected console errors',
  ).toEqual([])
}

async function tabUntil(
  page: Page,
  matches: () => Promise<boolean>,
  description: string,
  max = 220,
): Promise<void> {
  for (let index = 0; index < max; index += 1) {
    if (await matches()) {
      return
    }
    await page.keyboard.press('Tab')
  }
  throw new Error(`Tab did not reach ${description} within ${max} presses`)
}

function activeIdIs(page: Page, id: string): Promise<boolean> {
  return page.evaluate(
    (target) =>
      document.activeElement instanceof HTMLElement && document.activeElement.id === target,
    id,
  )
}

function activeTestIdIs(page: Page, testId: string): Promise<boolean> {
  return page.evaluate(
    (target) =>
      document.activeElement instanceof HTMLElement &&
      document.activeElement.getAttribute('data-testid') === target,
    testId,
  )
}

test('LLM key add, reload, rotate, and delete stay write-only through the UI', async ({ page }) => {
  test.setTimeout(90_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-key-input-anthropic')).toBeVisible()

  const first = 'sk-test-SENTINEL-35-ADD-4f2a'
  const firstPrefix = first.slice(0, 18)
  const rotated = 'sk-test-SENTINEL-35-ROT-c3d4'
  const rotatedPrefix = rotated.slice(0, 18)

  // Add: the full value never survives the successful save.
  await page.getByTestId('llm-key-input-anthropic').fill(first)
  await page.getByTestId('llm-key-save-anthropic').click()
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('****4f2a')
  await expect(page.getByTestId('llm-key-input-anthropic')).toHaveValue('')
  expect(await page.content()).not.toContain(first)
  expect(await page.content()).not.toContain(firstPrefix)

  const afterAdd = await readLLM(page)
  const anthropic = afterAdd.providers.find((row) => row.provider === 'anthropic')
  expect(anthropic?.configured).toBe(true)
  expect(anthropic?.masked).toBe('****4f2a')
  expect(['keyring', 'encrypted_file', 'stored']).toContain(anthropic?.key_source)
  const getText = await (
    await page.request.get('/api/v1/settings/llm', { headers: { Accept: 'application/json' } })
  ).text()
  expect(getText).not.toContain(first)
  expect(getText).not.toContain(firstPrefix)
  expect(daemonLog()).not.toContain(first)

  // A reload renders only the mask; the DOM still has no key value.
  await page.reload()
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('****4f2a')
  await expect(page.getByTestId('llm-key-input-anthropic')).toHaveValue('')
  const reloadedHtml = await page.content()
  expect(reloadedHtml).not.toContain(first)
  expect(reloadedHtml).not.toContain(firstPrefix)

  // There is no reveal control anywhere on the screen.
  await expect(page.getByRole('button', { name: FORBIDDEN_REVEAL })).toHaveCount(0)
  await expect(page.locator('[data-testid*="reveal"]')).toHaveCount(0)

  // And no reveal endpoint: the key route only accepts PUT/DELETE.
  const reveal = await page.request.get('/api/v1/settings/llm/anthropic/key', {
    headers: { Accept: 'application/json' },
  })
  expect([404, 405]).toContain(reveal.status())
  expect(await reveal.text()).not.toContain(first)

  // Rotate through the same write-only input.
  await page.getByTestId('llm-key-input-anthropic').fill(rotated)
  await page.getByRole('button', { name: '替换密钥（Anthropic）' }).click()
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('****c3d4')
  await expect(page.getByTestId('llm-key-input-anthropic')).toHaveValue('')
  const rotatedHtml = await page.content()
  expect(rotatedHtml).not.toContain(rotated)
  expect(rotatedHtml).not.toContain(rotatedPrefix)
  const afterRotate = await readLLM(page)
  expect(afterRotate.providers.find((row) => row.provider === 'anthropic')?.masked).toBe('****c3d4')

  // Delete through the write-only control: the provider becomes unconfigured
  // and no other provider key is substituted.
  await page.getByRole('button', { name: '删除密钥（Anthropic）' }).click()
  const deleteDialog = page.getByRole('dialog')
  await expect(deleteDialog).toContainText('删除只移除 anthropic 存储的密钥')
  await deleteDialog.getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('—')
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('密钥已删除')
  const afterDelete = await readLLM(page)
  const deleted = afterDelete.providers.find((row) => row.provider === 'anthropic')
  expect(deleted?.configured).toBe(false)
  expect(deleted?.masked).toBeNull()
  expect(deleted?.key_source).toBe('unset')
  const deletedHtml = await page.content()
  expect(deletedHtml).not.toContain(first)
  expect(deletedHtml).not.toContain(rotated)

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-llm-key-deleted-1440.png'),
    fullPage: true,
  })

  expectClean(diagnostics)
})

test('LLM base URL rejects unsafe forms inline, writes nothing, and needs the opt-in', async ({
  page,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.locator('#llm-base-url-openai')).toBeVisible()

  const before = await readLLM(page)
  const beforeCounts = databaseCounts()
  const openAi = page.locator('#llm-base-url-openai')
  const inlineError = page.locator('#llm-base-url-error-openai')
  const save = page.getByRole('button', { name: '保存 LLM 设置' })

  const refusals: Array<{ value: string; pattern: RegExp }> = [
    {
      value: 'http://example.com/v1',
      pattern: /must use https; http is allowed only for loopback/,
    },
    {
      value: 'https://user:pass@api.openai.com/v1',
      pattern: /must not contain userinfo/,
    },
    { value: 'https://api.openai.com/v1?token=1', pattern: /must not contain a query/ },
    { value: 'https://api.openai.com/v1#frag', pattern: /must not contain a fragment/ },
  ]

  for (const refusal of refusals) {
    await openAi.fill(refusal.value)
    await save.click()
    await expect(inlineError).toBeVisible()
    await expect(inlineError).toHaveText(refusal.pattern)
    await expect(openAi).toHaveAttribute('aria-invalid', 'true')
  }

  // A non-official https host is refused until the explicit opt-in is checked.
  await openAi.fill('https://llm.example.test/v1')
  await save.click()
  await expect(inlineError).toHaveText(/official openai host or loopback/)
  await expect(inlineError).toHaveText(/opt-in/)

  const afterInvalid = await readLLM(page)
  expect(afterInvalid.base_urls.openai).toBe(before.base_urls.openai)
  expect(afterInvalid.updated_at).toBe(before.updated_at)
  expect(databaseCounts()).toEqual(beforeCounts)

  // The opt-in accepts the same https custom host, and only when checked.
  await page.locator('#llm-allow-custom').check()
  await save.click()
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('已保存 LLM 设置')
  const afterCustom = await readLLM(page)
  expect(afterCustom.base_urls.openai).toBe('https://llm.example.test/v1')
  expect(afterCustom.allow_custom_base_url).toBe(true)
  expect(databaseCounts().config).toBe(beforeCounts.config + 1)
  expect(databaseCounts().audit).toBe(beforeCounts.audit + 1)

  // Restore the official host and opt out so later journeys start clean.
  await page.locator('#llm-allow-custom').uncheck()
  await openAi.fill('https://api.openai.com')
  await save.click()
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('已保存 LLM 设置')
  const restored = await readLLM(page)
  expect(restored.base_urls.openai).toBe('https://api.openai.com')
  expect(restored.allow_custom_base_url).toBe(false)

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-llm-base-url-1440.png'),
    fullPage: true,
  })

  // The refusals are designed 422s; any other page/console error is a failure.
  expectCleanExceptRefusals(diagnostics, ['status of 422'])
})

test('LLM role mapping edits all four roles and persists concurrency without hidden columns', async ({
  page,
}) => {
  test.setTimeout(90_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.locator('#llm-role-ANALYSIS')).toBeVisible()

  // No temperature/timeout column or control exists in the role table.
  const roleHeaders = (
    await page.locator('[aria-label="角色模型映射"] thead th').allTextContents()
  ).map((value) => value.trim())
  expect(roleHeaders).toEqual(['Role', 'Provider', 'Model'])
  expect(roleHeaders.join(' ').toLowerCase()).not.toMatch(/temperature|timeout|温度|超时/)
  await expect(page.locator('input[id*="temperature"], input[id*="timeout"]')).toHaveCount(0)

  await page.locator('#llm-active-provider').selectOption('anthropic')
  const roleModels: Record<string, string> = {
    ANALYSIS: 'claude-e2e-analysis-saved',
    ENTRY_RECHECK: 'claude-e2e-entry-saved',
    MANAGEMENT: 'claude-e2e-management-saved',
    REVIEW: 'claude-e2e-review-saved',
  }
  for (const [role, model] of Object.entries(roleModels)) {
    await page.locator(`#llm-role-${role}`).fill(model)
  }
  await page.locator('#llm-max-concurrency').fill('5')
  await page.locator('#llm-max-concurrency-deepseek').fill('3')
  await page.getByRole('button', { name: '保存 LLM 设置' }).click()
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('已保存 LLM 设置')

  const saved = await readLLM(page)
  expect(saved.provider).toBe('anthropic')
  for (const [role, model] of Object.entries(roleModels)) {
    expect(saved.roles[role]).toBe(model)
  }
  expect(saved.concurrency).toEqual({ global: 5, deepseek: 3 })
  expect(Object.keys(saved.roles).sort()).toEqual([
    'ANALYSIS',
    'ENTRY_RECHECK',
    'MANAGEMENT',
    'REVIEW',
  ])

  // The saved document survives a reload.
  await page.reload()
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.locator('#llm-active-provider')).toHaveValue('anthropic')
  for (const [role, model] of Object.entries(roleModels)) {
    await expect(page.locator(`#llm-role-${role}`)).toHaveValue(model)
  }
  await expect(page.locator('#llm-max-concurrency')).toHaveValue('5')
  await expect(page.locator('#llm-max-concurrency-deepseek')).toHaveValue('3')

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-llm-roles-1440.png'),
    fullPage: true,
  })

  expectClean(diagnostics)
})

test('LLM test panel probes the saved key through the local fake and shows usage', async ({
  page,
}) => {
  test.setTimeout(120_000)
  expect(LLM_FAKE_BASE, 'run-llm-settings-e2e.sh must export the fake provider URL').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  // Configure the fake provider: key, active provider, analysis model, base URL.
  const sentinel = 'sk-test-SENTINEL-35-PROBE-9b71'
  const keyResponse = await putLLMKey(page, 'anthropic', sentinel, 'e2e-llm-probe-key')
  expect(keyResponse.status(), 'PUT anthropic key').toBe(200)
  const settingsResponse = await putLLMSettings(
    page,
    {
      provider: 'anthropic',
      roles: { ANALYSIS: 'claude-e2e-probe' },
      base_urls: { anthropic: LLM_FAKE_BASE },
    },
    'e2e-llm-probe-settings',
  )
  expect(settingsResponse.status(), 'PUT LLM settings').toBe(200)

  const configured = await readLLM(page)
  expect(configured.providers.find((row) => row.provider === 'anthropic')?.masked).toBe('****9b71')
  expect(configured.base_urls.anthropic).toBe(LLM_FAKE_BASE)

  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('****9b71')
  await expect(page.locator('#llm-base-url-anthropic')).toHaveValue(LLM_FAKE_BASE as string)

  const probes: Array<{ provider?: string; role?: string }> = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/llm/test') {
      probes.push(request.postDataJSON() as { provider?: string; role?: string })
    }
  })

  const beforeFake = await fakeRequests(page)
  const providerRow = page
    .locator('[aria-label="Providers"] tbody tr')
    .filter({ hasText: 'Anthropic' })
  await providerRow.getByRole('button', { name: '测试连接（Anthropic，角色 ANALYSIS）' }).click()

  await expect(page.getByTestId('llm-probe-anthropic')).toBeVisible()
  await expect(page.getByTestId('llm-probe-anthropic')).toHaveText(/STRICT|DEGRADED/)
  await expect(page.getByTestId('llm-probe-anthropic')).toHaveText(/\d+ ms/)

  expect(probes).toHaveLength(1)
  expect(probes[0].provider).toBe('anthropic')
  expect(probes[0].role).toBe('ANALYSIS')

  // The probe reached the local fake transport, never a real provider.
  await expect
    .poll(async () => (await fakeRequests(page)).count, { timeout: 10_000 })
    .toBeGreaterThan(beforeFake.count)
  const afterFake = await fakeRequests(page)
  expect(afterFake.paths).toContain('/v1/messages')
  expect(afterFake.auth_present).toBe(true)

  // Usage shows the three windows and the unpriced count; missing is never 0.
  await expect(page.getByText('Today', { exact: true })).toBeVisible()
  await expect(page.getByText('This week', { exact: true })).toBeVisible()
  await expect(page.getByText('This month', { exact: true })).toBeVisible()
  await expect(page.getByTestId('llm-usage-day')).toContainText('$')
  await expect(page.getByTestId('llm-usage-week')).toContainText('$')
  await expect(page.getByTestId('llm-usage-month')).toContainText('$')
  await expect(page.getByText('Unpriced calls', { exact: true })).toBeVisible()
  await expect(page.getByTestId('llm-usage-unpriced')).toBeVisible()

  // The fake key never reaches the DOM or the API response.
  expect(await page.content()).not.toContain(sentinel)
  const apiText = await (
    await page.request.get('/api/v1/settings/llm', { headers: { Accept: 'application/json' } })
  ).text()
  expect(apiText).not.toContain(sentinel)

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-llm-probe-usage-1440.png'),
    fullPage: true,
  })

  expectClean(diagnostics)
})

test('LLM settings and key rotation are fully keyboard operable', async ({ page }) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  // Seed an OpenAI key so the replace/delete controls exist before the keyboard
  // run. OpenAI is used here so this journey is independent of the Anthropic
  // capability probe's runtime refresh (see the dedicated regression below).
  const seeded = 'sk-test-SENTINEL-35-SEED-7a1b'
  const seedResponse = await putLLMKey(page, 'openai', seeded, 'e2e-llm-kbd-seed')
  expect(seedResponse.status(), 'PUT openai seed key').toBe(200)

  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-key-input-openai')).toBeVisible()

  // Reach the concurrency field with Tab, change it, Tab to save, Enter.
  await tabUntil(page, () => activeIdIs(page, 'llm-max-concurrency'), 'the concurrency input')
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('4')
  await page.keyboard.press('Tab')
  await expect(page.locator('#llm-max-concurrency-deepseek')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: '保存 LLM 设置' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('已保存 LLM 设置')
  expect((await readLLM(page)).concurrency.global).toBe(4)

  // Replace the key with the keyboard only (Enter submits the write-only input).
  const replacement = 'sk-test-SENTINEL-35-KBD-3c9d'
  await tabUntil(page, () => activeTestIdIs(page, 'llm-key-input-openai'), 'the openai key input')
  await page.keyboard.type(replacement)
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('llm-provider-mask-openai')).toHaveText('****3c9d')
  await expect(page.getByTestId('llm-key-input-openai')).toHaveValue('')

  // Open the delete confirmation and dismiss it with Escape; the invoking
  // control regains focus (DESIGN A7 / A11Y-02).
  const openAiField = page
    .locator('div.field[role="group"]')
    .filter({ has: page.getByTestId('llm-key-input-openai') })
  await page.keyboard.press('Tab')
  await expect(openAiField.getByRole('button', { name: '替换密钥（OpenAI）' })).toBeFocused()
  await page.keyboard.press('Tab')
  const deleteButton = openAiField.getByRole('button', { name: '删除密钥（OpenAI）' })
  await expect(deleteButton).toBeFocused()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const cancelButton = dialog.getByRole('button', { name: '取消' })
  await expect(cancelButton).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(deleteButton).toBeFocused()

  // Reopen it with the keyboard, confirm, and mark the provider unconfigured.
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await expect(cancelButton).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '确认删除' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('llm-provider-mask-openai')).toHaveText('—')
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('密钥已删除')
  await expect(page.getByTestId('llm-key-input-openai')).toBeFocused()
  expect((await readLLM(page)).providers.find((row) => row.provider === 'openai')?.configured).toBe(
    false,
  )

  expectClean(diagnostics)
})

test('LLM deleting a freshly probed provider key reports the provider unconfigured', async ({
  page,
}) => {
  test.setTimeout(90_000)
  expect(LLM_FAKE_BASE, 'run-llm-settings-e2e.sh must export the fake provider URL').toBeTruthy()
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  const sentinel = 'sk-test-SENTINEL-35-STALE-2d3e'
  const keyResponse = await putLLMKey(page, 'anthropic', sentinel, 'e2e-llm-stale-key')
  expect(keyResponse.status(), 'PUT anthropic key').toBe(200)
  const settingsResponse = await putLLMSettings(
    page,
    {
      provider: 'anthropic',
      roles: { ANALYSIS: 'claude-e2e-stale' },
      base_urls: { anthropic: LLM_FAKE_BASE },
    },
    'e2e-llm-stale-settings',
  )
  expect(settingsResponse.status(), 'PUT LLM settings').toBe(200)

  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('****2d3e')

  // The normal pre-delete flow: test the provider, which refreshes runtime.
  const providerRow = page
    .locator('[aria-label="Providers"] tbody tr')
    .filter({ hasText: 'Anthropic' })
  await providerRow.getByRole('button', { name: '测试连接（Anthropic，角色 ANALYSIS）' }).click()
  await expect(page.getByTestId('llm-probe-anthropic')).toBeVisible()

  const anthropicField = page
    .locator('div.field[role="group"]')
    .filter({ has: page.getByTestId('llm-key-input-anthropic') })
  await anthropicField.getByRole('button', { name: '删除密钥（Anthropic）' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByTestId('settings-llm-save-status')).toContainText('密钥已删除')

  // Required: the provider is unconfigured, not resurrected as an env key.
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('—')
  const after = await readLLM(page)
  const deleted = after.providers.find((row) => row.provider === 'anthropic')
  expect(deleted?.configured).toBe(false)
  expect(deleted?.key_source).toBe('unset')
})

test('LLM delete confirmation restores keyboard focus after both exits', async ({ page }) => {
  test.setTimeout(90_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  const seeded = 'sk-test-SENTINEL-35-FOCUS-5e6f'
  const seedResponse = await putLLMKey(page, 'anthropic', seeded, 'e2e-llm-focus-seed')
  expect(seedResponse.status(), 'PUT anthropic seed key').toBe(200)

  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-key-input-anthropic')).toBeVisible()

  await tabUntil(
    page,
    () => activeTestIdIs(page, 'llm-key-input-anthropic'),
    'the anthropic key input',
  )
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  const deleteButton = page.getByRole('button', { name: '删除密钥（Anthropic）' })
  await expect(deleteButton).toBeFocused()
  await page.keyboard.press('Enter')

  const dialog = page.getByRole('dialog')
  const cancelButton = dialog.getByRole('button', { name: '取消' })
  await expect(dialog).toBeVisible()
  await expect(cancelButton).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(dialog).toBeHidden()

  // Required: focus returns to the control that opened the confirmation.
  await expect(deleteButton).toBeFocused()

  // The confirm exit restores focus to the row's stable key input.
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await expect(cancelButton).toBeFocused()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('llm-provider-mask-anthropic')).toHaveText('—')
  await expect(page.getByTestId('llm-key-input-anthropic')).toBeFocused()
})

test('LLM settings are read-only at 375 with the desktop/CLI note and editable at 768/1440', async ({
  page,
  browser,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await authenticate(page)

  async function expectLLMViewport(
    target: Page,
    viewport: { width: number; height: number; readonly: boolean; name: string },
  ): Promise<void> {
    await target.setViewportSize({ width: viewport.width, height: viewport.height })
    await target.goto('/settings/llm')
    await expect(target.getByRole('heading', LLM_HEADING)).toBeVisible()
    await expect(target.getByTestId('llm-key-input-anthropic')).toBeVisible()
    await expect(target.locator('section.system-page')).toHaveAttribute(
      'data-readonly',
      viewport.readonly ? 'true' : 'false',
    )

    const keyInput = target.getByTestId('llm-key-input-anthropic')
    const save = target.getByRole('button', { name: '保存 LLM 设置' })
    const note = target.locator('section.system-page').getByRole('note')
    if (viewport.readonly) {
      await expect(note).toContainText('在桌面端或 CLI 修改')
      await expect(keyInput).toBeDisabled()
      await expect(save).toBeDisabled()
    } else {
      await expect(note).toHaveCount(0)
      await expect(keyInput).toBeEnabled()
      await expect(save).toBeEnabled()
    }

    const widths = await target.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(widths.document, `no horizontal overflow at ${viewport.width}`).toBeLessThanOrEqual(
      widths.viewport,
    )
    await target.screenshot({ path: path.join(SCREENS_DIR, viewport.name), fullPage: true })
  }

  await expectLLMViewport(page, {
    width: 1440,
    height: 900,
    readonly: false,
    name: 'settings-llm-viewport-1440.png',
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
    await expectLLMViewport(touchPage, {
      width: 375,
      height: 812,
      readonly: true,
      name: 'settings-llm-viewport-375.png',
    })
    await expectLLMViewport(touchPage, {
      width: 768,
      height: 1024,
      readonly: false,
      name: 'settings-llm-viewport-768.png',
    })
  } finally {
    await touchContext.close()
  }

  expectClean(diagnostics)
  expectClean(touchDiagnostics)
})

test('LLM settings report no axe WCAG 2.1 A/AA violations', async ({ page }) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/llm')
  await expect(page.getByRole('heading', LLM_HEADING)).toBeVisible()
  await expect(page.getByTestId('llm-key-input-anthropic')).toBeVisible()
  await page.waitForLoadState('networkidle')

  const tags = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
  const results = await new AxeBuilder({ page }).withTags(tags).analyze()
  const summary = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  )

  fs.writeFileSync(
    path.join(SCREENS_DIR, 'settings-llm-axe.json'),
    JSON.stringify(
      {
        url: '/settings/llm',
        tags,
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
    path: path.join(SCREENS_DIR, 'settings-llm-axe-1440.png'),
    fullPage: true,
  })

  expect(results.violations, `settings/llm axe violations:\n${summary.join('\n')}`).toEqual([])
  expectClean(diagnostics)
})

// -- Slice 36: prompt templates (F10b list, F10b.1 editor, F10b.2 history) ---

const PROMPT_LIST_HEADING = { name: 'Prompt 模板', level: 1 } as const
const PROMPT_EDITOR_HEADING = { name: 'Prompt 编辑器', level: 1 } as const
const PROMPT_HISTORY_HEADING = { name: 'Prompt 版本历史', level: 1 } as const
const PROMPT_ROLES = ['ANALYSIS', 'ENTRY_RECHECK', 'MANAGEMENT', 'REVIEW'] as const
const PROMPT_IDS = ['analysis', 'entry_recheck', 'management', 'review'] as const

const PROMPT_BODY = `Symbol: {{symbol}}
Timeframe: {{timeframe}}
<market_data>{{metrics}}</market_data>
<knowledge>{{knowledge}}</knowledge>
E2E PROMPT MARKER
Respond with JSON matching this schema:
{{schema}}
`

const PROMPT_HOSTILE_BODY = `${PROMPT_BODY}<script>alert(1)</script>
<a href="javascript:alert(1)">click</a>
`

interface PromptSettingsItem {
  prompt_id: string
  role: string
  active_version: number
  author: string
  updated_at: string
  version_count: number
  latest_version: number
  required_variables: string[]
  is_builtin_default: boolean
}

interface PromptSettingsPayload {
  items: PromptSettingsItem[]
  total: number
}

interface PromptVersionPayload {
  prompt_id: string
  version: number
  status: 'ACTIVE' | 'DRAFT' | 'SUPERSEDED'
  is_active: boolean
  body: string
  body_hash: string
  required_variables: string[]
  author: string
  reason: string
  is_default: boolean
  created_at: string
  system_prompt_id: string
  system_prompt_version: number
  system_prompt_hash: string
  knowledge_artifact_hash: string | null
}

interface PromptDetailPayload {
  prompt_id: string
  role: string
  kind: string
  active_version: number
  created_at: string
  updated_at: string
  versions: PromptVersionPayload[]
}

async function readPromptSettings(page: Page): Promise<PromptSettingsPayload> {
  const response = await page.request.get('/api/v1/settings/prompts', {
    headers: { Accept: 'application/json' },
  })
  expect(response.status(), 'GET /api/v1/settings/prompts').toBe(200)
  const body = (await response.json()) as Envelope<PromptSettingsPayload>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as PromptSettingsPayload
}

async function readPromptDetail(page: Page, promptId = 'analysis'): Promise<PromptDetailPayload> {
  const response = await page.request.get(`/api/v1/prompts/${promptId}`, {
    headers: { Accept: 'application/json' },
  })
  expect(response.status(), `GET /api/v1/prompts/${promptId}`).toBe(200)
  const body = (await response.json()) as Envelope<PromptDetailPayload>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as PromptDetailPayload
}

async function promptHeaders(page: Page, idempotencyKey: string): Promise<Record<string, string>> {
  const csrf = await csrfToken(page)
  return {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'Idempotency-Key': idempotencyKey,
    'X-CSRF-Token': csrf,
    Origin: ORIGIN,
  }
}

/** Create a NEW immutable version through the real prompt API. */
async function postPromptVersion(
  page: Page,
  promptId: string,
  body: string,
  idempotencyKey: string,
  activate = false,
) {
  return page.request.post(`/api/v1/prompts/${promptId}/versions`, {
    headers: await promptHeaders(page, idempotencyKey),
    data: { body, author: 'e2e-operator', reason: 'e2e prompt journey', activate },
  })
}

async function expectAxeClean(page: Page, label: string): Promise<void> {
  const tags = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
  const results = await new AxeBuilder({ page }).withTags(tags).analyze()
  const summary = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  )
  expect(results.violations, `${label} axe violations:\n${summary.join('\n')}`).toEqual([])
}

test('prompt list shows the four roles as built-in default v1 at 1440', async ({ page }) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/prompts')

  await expect(page.getByRole('heading', PROMPT_LIST_HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')
  await expect(page.getByTestId('prompts-builtin-banner')).toContainText('使用内置默认 v1')

  const settings = await readPromptSettings(page)
  expect(settings.items.map((item) => item.role)).toEqual([...PROMPT_ROLES])
  expect(settings.items.map((item) => item.prompt_id)).toEqual([...PROMPT_IDS])
  for (const item of settings.items) {
    expect(item.active_version).toBe(1)
    expect(item.author).toBe('system')
    expect(item.version_count).toBe(1)
    expect(item.is_builtin_default).toBe(true)
    expect(item.updated_at).toMatch(UTC_TIME)
    expect(item.required_variables).toContain('symbol')
  }

  for (const promptId of PROMPT_IDS) {
    const row = page.getByTestId(`prompt-row-${promptId}`)
    await expect(row).toBeVisible()
    await expect(row.getByTestId(`prompt-active-${promptId}`)).toHaveText('v1 active')
    await expect(row.getByTestId(`prompt-builtin-${promptId}`)).toHaveText('built-in default v1')
    await expect(row.getByTestId(`prompt-author-${promptId}`)).toHaveText('system')
    // The rendered author/time cell comes from the same active version.
    const item = settings.items.find((entry) => entry.prompt_id === promptId)
    await expect(row.getByTestId(`prompt-updated-${promptId}`)).toHaveText(
      item?.updated_at as string,
    )
  }

  await expectAxeClean(page, '/settings/prompts')
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-prompts-list-1440.png'),
    fullPage: true,
  })
  expectClean(diagnostics)
})

test('prompt editor blocks a missing variable with zero writes and renders hostile text literally', async ({
  page,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/prompts/analysis')
  await expect(page.getByRole('heading', PROMPT_EDITOR_HEADING)).toBeVisible()

  const versionPosts: string[] = []
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      new URL(request.url()).pathname === '/api/v1/prompts/analysis/versions'
    ) {
      versionPosts.push(request.postData() ?? '')
    }
  })

  const textarea = page.locator('#prompt-body')
  await expect(textarea).not.toBeEditable()
  await expect(page.getByRole('button', { name: '保存为新版本' })).toBeDisabled()

  // Editing is explicit; the body becomes editable and focus moves into it.
  await page.getByRole('button', { name: '编辑' }).click()
  await expect(textarea).toBeEditable()
  await expect(textarea).toBeFocused()

  // A missing required variable is blocked inline with no request at all.
  const missing = PROMPT_BODY.replace(
    '<market_data>{{metrics}}</market_data>',
    '<market_data>metrics</market_data>',
  )
  await textarea.fill(missing)
  await expect(page.getByText('缺少必需变量 {{metrics}}')).toBeVisible()
  await page.getByRole('button', { name: '保存为新版本' }).click()
  const summary = page.getByTestId('prompt-editor-error')
  await expect(summary).toContainText('无法保存')
  await expect(summary).toContainText('缺少必需变量 {{metrics}}')
  await expect(summary).toBeFocused()
  expect(versionPosts).toHaveLength(0)

  // The preview is the ACTIVE version only; an unsaved hostile draft is never
  // projected into it.
  await textarea.fill(PROMPT_HOSTILE_BODY)
  const preview = page.getByTestId('prompt-preview')
  await expect(preview).toContainText('[redacted: symbol]')
  await expect(preview).not.toContainText('E2E PROMPT MARKER')
  await expect(preview.locator('script')).toHaveCount(0)
  await expect(preview.locator('a')).toHaveCount(0)

  // Saving creates a new DRAFT version; the active version is unchanged.
  const before = await readPromptDetail(page)
  await page.getByRole('button', { name: '保存为新版本' }).click()
  const visible = page.getByTestId('prompt-editor-visible')
  await expect(visible).toContainText('已创建 v')
  await expect(visible).toContainText('DRAFT')
  await expect(visible).toContainText('既有决策仍绑定其原始版本')
  await expect(page.getByTestId('prompt-editor-status')).toContainText('已创建 v')
  expect(versionPosts).toHaveLength(1)
  expect(JSON.parse(versionPosts[0] as string).activate).toBe(false)

  const after = await readPromptDetail(page)
  expect(after.active_version).toBe(before.active_version)
  expect(after.versions.length).toBe(before.versions.length + 1)
  const latest = after.versions[after.versions.length - 1]
  expect(latest.status).toBe('DRAFT')
  expect(latest.body).toContain('<script>alert(1)</script>')
  expect(latest.body).toContain('<a href="javascript:alert(1)">click</a>')

  // Reload: the editor shows the active (unchanged) body, not the draft.
  await page.reload()
  await expect(page.getByRole('heading', PROMPT_EDITOR_HEADING)).toBeVisible()
  const activeBody = after.versions.find(
    (version) => version.version === after.active_version,
  )?.body
  await expect(page.locator('#prompt-body')).toHaveValue(activeBody as string)
  await expect(page.locator('#prompt-body')).not.toBeEditable()

  await expectAxeClean(page, '/settings/prompts/analysis')
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-prompt-editor-1440.png'),
    fullPage: true,
  })
  expectClean(diagnostics)
})

test('prompt history renders text-only diffs, activates explicitly, and restores as a new version', async ({
  page,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  // A custom active version makes the history and diff meaningful.
  const createdResponse = await postPromptVersion(
    page,
    'analysis',
    PROMPT_HOSTILE_BODY,
    'e2e-prompt-history-create',
    true,
  )
  expect(createdResponse.status()).toBe(201)
  const created = ((await createdResponse.json()) as Envelope<PromptVersionPayload>)
    .data as PromptVersionPayload
  expect(created.status).toBe('ACTIVE')

  // With the hostile body active, the redacted preview renders it literally:
  // no script element, no anchor, no execution.
  await page.goto('/settings/prompts/analysis')
  await expect(page.getByRole('heading', PROMPT_EDITOR_HEADING)).toBeVisible()
  const activePreview = page.getByTestId('prompt-preview')
  await expect(activePreview).toContainText('<script>alert(1)</script>')
  await expect(activePreview).toContainText('<a href="javascript:alert(1)">click</a>')
  await expect(activePreview).toContainText('[redacted: symbol]')
  await expect(activePreview.locator('script')).toHaveCount(0)
  await expect(activePreview.locator('a')).toHaveCount(0)

  await page.goto('/settings/prompts/analysis/history')
  await expect(page.getByRole('heading', PROMPT_HISTORY_HEADING)).toBeVisible()
  const activeRow = page.getByTestId(`prompt-version-${created.version}`)
  await expect(activeRow).toContainText('ACTIVE')
  await expect(activeRow).toContainText('e2e-operator')
  await expect(page.getByTestId('prompt-version-1')).toContainText('built-in default')

  // The default comparison is v1 -> active and stays literal text.
  const diffBody = page.getByTestId('prompt-diff-body')
  await expect(diffBody).toContainText('<script>alert(1)</script>')
  await expect(diffBody.locator('script')).toHaveCount(0)
  await expect(diffBody.locator('a')).toHaveCount(0)

  // Activation is explicit and confirmed.
  await page.getByRole('button', { name: '激活 v1' }).click()
  const activateDialog = page.getByRole('dialog', { name: '激活 Prompt 版本' })
  await expect(activateDialog).toBeVisible()
  // The shared dialog is parameterized for version changes, not risk caps.
  await expect(activateDialog.getByRole('table', { name: '版本变更前后对比' })).toBeVisible()
  await expect(activateDialog).toContainText('v1')
  await activateDialog.getByRole('button', { name: '激活' }).click()
  await expect(page.getByTestId('prompt-history-visible')).toContainText('已激活 v1')
  expect((await readPromptDetail(page)).active_version).toBe(1)

  // Restore requires confirmation, shows the current active version as
  // unchanged and the new DRAFT, and a cancelled restore writes nothing.
  const versionsBefore = (await readPromptDetail(page)).versions.length
  await page.getByRole('button', { name: '恢复内置默认' }).click()
  const restoreDialog = page.getByRole('dialog', { name: '恢复 Prompt 默认版本' })
  await expect(restoreDialog).toContainText('当前 active 版本（保持不变）')
  await expect(restoreDialog).toContainText('v1')
  await expect(restoreDialog).toContainText('新的 built-in default 版本（新建，DRAFT）')
  await page.keyboard.press('Escape')
  await expect(restoreDialog).toBeHidden()
  expect((await readPromptDetail(page)).versions.length).toBe(versionsBefore)

  const restoresBefore = promptAuditEvents().filter(
    (event) => event.event_type === 'PROMPT_DEFAULT_RESTORED',
  ).length
  await page.getByRole('button', { name: '恢复内置默认' }).click()
  await restoreDialog.getByRole('button', { name: '恢复默认' }).click()
  const restoreVisible = page.getByTestId('prompt-history-visible')
  await expect(restoreVisible).toContainText('已创建 v')
  await expect(restoreVisible).toContainText('（DRAFT）')
  await expect(restoreVisible).toContainText('active v1 保持不变')
  await expect(restoreVisible).toContainText('后才会切换')

  const afterRestore = await readPromptDetail(page)
  expect(afterRestore.active_version).toBe(1)
  const restored = afterRestore.versions[afterRestore.versions.length - 1]
  expect(restored.status).toBe('DRAFT')
  expect(restored.is_default).toBe(true)
  expect(afterRestore.versions.length).toBe(versionsBefore + 1)

  // The restore is audited under its own event type (not a plain create) and
  // carries the created draft's version.
  const restoreEvents = promptAuditEvents().filter(
    (event) => event.event_type === 'PROMPT_DEFAULT_RESTORED',
  )
  expect(restoreEvents.length).toBe(restoresBefore + 1)
  const restoreEvent = restoreEvents[restoreEvents.length - 1]
  expect(restoreEvent?.subjectId).toBe('analysis')
  expect(restoreEvent?.version).toBe(restored.version)

  // The restored default only takes effect after an explicit activation.
  const activateRestored = page.getByRole('button', { name: `激活 v${restored.version}` })
  await expect(activateRestored).toBeVisible()
  await activateRestored.click()
  await page
    .getByRole('dialog', { name: '激活 Prompt 版本' })
    .getByRole('button', { name: '激活' })
    .click()
  await expect(restoreVisible).toContainText(`已激活 v${restored.version}`)
  const final = await readPromptDetail(page)
  expect(final.active_version).toBe(restored.version)
  // History stays complete: every earlier version is still present.
  expect(final.versions.length).toBe(versionsBefore + 1)

  await expectAxeClean(page, '/settings/prompts/analysis/history')
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'settings-prompt-history-1440.png'),
    fullPage: true,
  })
  expectClean(diagnostics)
})

test('prompt screens are read-only at 375 and the editor is keyboard savable at 1440', async ({
  page,
  browser,
}) => {
  test.setTimeout(120_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/settings/prompts/analysis')
  await expect(page.getByRole('heading', PROMPT_EDITOR_HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')

  // Keyboard: list -> editor -> variable checklist -> save.
  const isEditButton = () =>
    page.evaluate(
      () =>
        document.activeElement instanceof HTMLElement &&
        document.activeElement.textContent?.trim() === '编辑',
    )
  await tabUntil(page, isEditButton, 'the 编辑 control')
  await page.keyboard.press('Enter')
  await expect(page.locator('#prompt-body')).toBeFocused()
  // author -> reason -> discard -> save
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: '保存为新版本' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('prompt-editor-visible')).toContainText('已创建 v')
  // The required-variable checklist stays in the same keyboard cycle.
  const variableRegion = page.getByRole('region', { name: '必需变量检查' })
  await tabUntil(
    page,
    () =>
      page.evaluate(() => document.activeElement?.getAttribute('aria-label') === '必需变量检查'),
    'the required-variable region',
  )
  await expect(variableRegion).toBeFocused()

  // 768 stays editable for the whole list -> editor -> history journey.
  await page.setViewportSize({ width: 768, height: 1024 })
  await page.goto('/settings/prompts')
  await expect(page.getByRole('heading', PROMPT_LIST_HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')
  await expect(page.getByTestId('prompt-row-analysis')).toBeVisible()

  await page.goto('/settings/prompts/analysis')
  await expect(page.getByRole('heading', PROMPT_EDITOR_HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')
  await expect(page.getByRole('button', { name: '编辑' })).toBeEnabled()

  await page.goto('/settings/prompts/analysis/history')
  await expect(page.getByRole('heading', PROMPT_HISTORY_HEADING)).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')
  await expect(page.getByRole('button', { name: '恢复内置默认' })).toBeEnabled()

  fs.mkdirSync(SCREENS_DIR, { recursive: true })

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

    await touchPage.goto('/settings/prompts')
    await expect(touchPage.getByRole('heading', PROMPT_LIST_HEADING)).toBeVisible()
    await expect(touchPage.locator('section.system-page')).toHaveAttribute('data-readonly', 'true')
    await expect(touchPage.locator('section.system-page').getByRole('note')).toContainText(
      '在桌面端或 CLI 修改',
    )
    await expect(touchPage.getByTestId('prompt-row-analysis')).toBeVisible()
    await touchPage.screenshot({
      path: path.join(SCREENS_DIR, 'settings-prompts-list-375.png'),
      fullPage: true,
    })

    await touchPage.goto('/settings/prompts/analysis')
    await expect(touchPage.getByRole('heading', PROMPT_EDITOR_HEADING)).toBeVisible()
    await expect(touchPage.locator('section.system-page')).toHaveAttribute('data-readonly', 'true')
    await expect(touchPage.locator('section.system-page').getByRole('note')).toContainText(
      '在桌面端或 CLI 修改',
    )
    const touchBody = touchPage.locator('#prompt-body')
    await expect(touchBody).not.toBeEditable()
    // SC 2.1.1: the read-only body stays focusable, scrollable, and copyable;
    // only the edit/save actions are disabled.
    await touchBody.focus()
    await expect(touchBody).toBeFocused()
    await touchPage.keyboard.press('Control+A')
    const bodySelection = await touchBody.evaluate((element) => {
      const textarea = element as HTMLTextAreaElement
      textarea.scrollTop = textarea.scrollHeight
      return {
        start: textarea.selectionStart,
        end: textarea.selectionEnd,
        length: textarea.value.length,
        scrollable: textarea.scrollHeight > 0,
        copyEnabled: document.queryCommandEnabled('copy'),
      }
    })
    expect(bodySelection.start).toBe(0)
    expect(bodySelection.end).toBe(bodySelection.length)
    expect(bodySelection.length).toBeGreaterThan(0)
    expect(bodySelection.scrollable).toBe(true)
    expect(bodySelection.copyEnabled).toBe(true)
    await expect(touchPage.getByRole('button', { name: '编辑' })).toBeDisabled()
    await expect(touchPage.getByRole('button', { name: '保存为新版本' })).toBeDisabled()
    await expect(touchPage.getByTestId('prompt-preview')).toBeVisible()
    await touchPage.screenshot({
      path: path.join(SCREENS_DIR, 'settings-prompt-editor-375.png'),
      fullPage: true,
    })

    await touchPage.goto('/settings/prompts/analysis/history')
    await expect(touchPage.getByRole('heading', PROMPT_HISTORY_HEADING)).toBeVisible()
    await expect(touchPage.locator('section.system-page')).toHaveAttribute('data-readonly', 'true')
    await expect(touchPage.getByRole('button', { name: '恢复内置默认' })).toBeDisabled()
    await expect(touchPage.getByTestId('prompt-version-1')).toBeVisible()

    for (const pathName of [
      '/settings/prompts',
      '/settings/prompts/analysis',
      '/settings/prompts/analysis/history',
    ]) {
      await touchPage.goto(pathName)
      const widths = await touchPage.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }))
      expect(widths.document, `no horizontal overflow at 375 on ${pathName}`).toBeLessThanOrEqual(
        widths.viewport,
      )
    }
    await touchPage.screenshot({
      path: path.join(SCREENS_DIR, 'settings-prompt-history-375.png'),
      fullPage: true,
    })
  } finally {
    await touchContext.close()
  }

  expectClean(diagnostics)
  expectClean(touchDiagnostics)
})
