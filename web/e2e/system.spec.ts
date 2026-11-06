import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

/**
 * Critical journey for slice 01: the `/system` walking-skeleton shell.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the daemon serves `web/dist` on OFT_E2E_BASE_URL with a temporary data dir;
 * - the fixture scan has already run through `POST /api/v1/scans`;
 * - OFT_E2E_DATA_DIR / OFT_E2E_SCAN_ID / OFT_E2E_SCAN_DELTA mirror that state.
 *
 * The suite is read-only: it never creates or mutates scans through the browser.
 */

const SCREENS_DIR =
  process.env.OFT_E2E_SYSTEM_SCREENS_DIR ??
  process.env.OFT_E2E_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e')

const GOLDEN_DELTA = '1.50'
const EXPECTED_CHECKS = ['config', 'storage.sqlite', 'storage.parquet', 'clock', 'market_data']

interface ReadyCheck {
  name: string
  status: string
  detail: string
}

interface ScanRecord {
  scan_id: string
  symbol: string
  period: string
  status: string
  trade_count: number
  buy_volume: string
  sell_volume: string
  delta: string
  cvd: string
  data_quality: string
  manifest_path: string
  parquet_rows: number
}

interface ReadinessData {
  status: string
  version: string
  env: string
  data_dir: string
  checked_at: string
  checks: ReadyCheck[]
  latest_scan: ScanRecord | null
}

function statValue(page: Page, label: string) {
  return page
    .locator('.stat')
    .filter({ has: page.locator('dt', { hasText: label }) })
    .locator('dd')
}

function scanValue(page: Page, label: string) {
  return page
    .locator('.scan-grid > div')
    .filter({ has: page.locator('dt', { hasText: label }) })
    .locator('dd')
}

async function waitForLoadedSystem(page: Page): Promise<void> {
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Latest fixture scan' })).toBeVisible()
  await expect(page.locator('.scan-grid')).toBeVisible()
}

async function fetchReadiness(page: Page): Promise<ReadinessData> {
  const response = await page.request.get('/ready')
  expect(response.status()).toBe(200)
  const body = (await response.json()) as { ok: boolean; data: ReadinessData | null }
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as ReadinessData
}

test('loads /system with no console or page errors', async ({ page }) => {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []

  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => {
    pageErrors.push(error.message)
  })

  await page.goto('/system')
  await waitForLoadedSystem(page)

  await expect(page.getByRole('region', { name: 'System status bar' })).toBeVisible()
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('shows health OK, readiness OK with named checks, version, data dir, and the fixture scan delta', async ({
  page,
}) => {
  const readiness = await fetchReadiness(page)
  const healthResponse = await page.request.get('/health')
  expect(healthResponse.status()).toBe(200)
  const healthBody = (await healthResponse.json()) as {
    ok: boolean
    data: { status: string; version: string } | null
  }
  expect(healthBody.ok).toBe(true)
  expect(healthBody.data?.status).toBe('ok')

  const scan = readiness.latest_scan
  expect(scan, 'the fixture scan must exist before the UI journey runs').not.toBeNull()
  if (!scan) {
    return
  }

  // Golden values: the CLI-provided delta and the fixture README golden value.
  expect(scan.delta).toBe(GOLDEN_DELTA)
  if (process.env.OFT_E2E_SCAN_DELTA) {
    expect(scan.delta).toBe(process.env.OFT_E2E_SCAN_DELTA)
  }
  if (process.env.OFT_E2E_SCAN_ID) {
    expect(scan.scan_id).toBe(process.env.OFT_E2E_SCAN_ID)
  }
  if (process.env.OFT_E2E_DATA_DIR) {
    expect(readiness.data_dir).toBe(process.env.OFT_E2E_DATA_DIR)
  }

  await page.goto('/system')
  await waitForLoadedSystem(page)

  // The status bar reflects the real /ready probe, not hardcoded health.
  const statusbar = page.getByRole('region', { name: 'System status bar' })
  await expect(statusbar).toContainText('UP')
  await expect(statusbar).toContainText('READY')
  await expect(statusbar).toContainText(readiness.version)
  await expect(statusbar).toContainText(readiness.env.toUpperCase())

  await expect(statValue(page, 'Health')).toHaveText('OK')
  await expect(statValue(page, 'Readiness')).toHaveText('READY')
  await expect(statValue(page, 'Version')).toHaveText(readiness.version)
  await expect(statValue(page, 'Data directory')).toHaveText(readiness.data_dir)
  await expect(page.getByText(`${readiness.checks.length} named checks`)).toBeVisible()

  // Every named readiness check is rendered with its own state.
  const rows = page.locator('table.data-table tbody tr')
  await expect(rows).toHaveCount(readiness.checks.length)
  for (const name of EXPECTED_CHECKS) {
    const check = readiness.checks.find((candidate) => candidate.name === name)
    expect(check, `readiness check ${name} must exist`).toBeDefined()
    const row = rows.filter({ hasText: name })
    await expect(row).toHaveCount(1)
    await expect(row.locator('td').nth(1)).toHaveText('OK')
  }

  // Scan panel matches the API/CLI record.
  await expect(scanValue(page, 'Scan ID')).toHaveText(scan.scan_id)
  await expect(scanValue(page, 'Symbol')).toHaveText(`${scan.symbol} · ${scan.period}`)
  await expect(scanValue(page, 'Status')).toHaveText(scan.status)
  const deltaText = scan.delta.startsWith('-') ? `▼ ${scan.delta}` : `▲ +${scan.delta}`
  await expect(scanValue(page, 'Delta')).toHaveText(deltaText)
  await expect(scanValue(page, 'CVD')).toHaveText(scan.cvd)
  await expect(scanValue(page, 'Trades')).toHaveText(String(scan.trade_count))
  await expect(scanValue(page, 'Buy / sell volume')).toHaveText(
    `${scan.buy_volume} / ${scan.sell_volume}`,
  )
  await expect(scanValue(page, 'Quality')).toHaveText(scan.data_quality)
  await expect(page.locator('.panel-foot', { hasText: 'Parquet part' })).toContainText(
    scan.manifest_path,
  )
  await expect(page.locator('.panel-foot', { hasText: 'Parquet part' })).toContainText(
    `${scan.parquet_rows} rows`,
  )
})

test('at 375px the shell is read-only and has no page-level horizontal scroll', async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/system')
  await waitForLoadedSystem(page)

  const systemPage = page.locator('section.system-page')
  await expect(systemPage).toHaveAttribute('data-readonly', 'true')
  await expect(page.getByRole('note')).toContainText('Read-only at this width')

  // The environment chip stays visible and in the accessibility tree at 375px.
  const readiness = await fetchReadiness(page)
  const envLabel = readiness.env.toUpperCase()
  const envChip = page.locator('.statusbar .sb-env')
  await expect(envChip).toBeVisible()
  await expect(envChip).toHaveText(envLabel)
  await expect(page.getByRole('region', { name: 'System status bar' })).toContainText(envLabel)

  // No mutation controls in the page content (the global kill-switch chrome is
  // inert and lives outside <main>; it is asserted separately below).
  const main = page.locator('main.app-main')
  for (const selector of ['button', 'input', 'select', 'textarea', 'form']) {
    await expect(main.locator(selector)).toHaveCount(0)
  }
  await expect(page.locator('[contenteditable="true"]')).toHaveCount(0)

  // The kill switch is rendered but is explicitly non-functional.
  const killSwitch = page.getByRole('button', { name: /Kill switch/ })
  await expect(killSwitch).toBeVisible()
  await expect(killSwitch).toHaveAttribute('title', /not wired|not available/i)

  const widths = await page.evaluate(() => {
    const viewport = window.innerWidth
    const offenders: string[] = []
    for (const element of document.querySelectorAll('*')) {
      const rect = element.getBoundingClientRect()
      if (rect.right > viewport + 0.5 || rect.left < -0.5) {
        const label = `${element.tagName.toLowerCase()}${element.className ? `.${String(element.className).split(' ').join('.')}` : ''}`
        offenders.push(
          `${label} [${Math.round(rect.left)}..${Math.round(rect.right)}] "${(element.textContent ?? '').trim().slice(0, 60)}"`,
        )
      }
    }
    return {
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      viewport,
      offenders: offenders.slice(0, 5),
    }
  })
  const context = `viewport ${widths.viewport}; overflowing elements: ${widths.offenders.join(' | ') || 'none'}`
  expect(
    widths.document,
    `document scrollWidth ${widths.document} > ${widths.viewport} (${context})`,
  ).toBeLessThanOrEqual(widths.viewport)
  expect(
    widths.body,
    `body scrollWidth ${widths.body} > ${widths.viewport} (${context})`,
  ).toBeLessThanOrEqual(widths.viewport)
})

test('renders a stable API error state when readiness is blocked', async ({ page }) => {
  const catalogError = {
    code: 'DATA_UNAVAILABLE',
    message: 'Required data does not exist or cannot be fetched.',
    category: 'DATA',
    http_status: 503,
    retryable: true,
    reference_id: 'REQ-E2E-ERROR',
    details: [],
  }
  let interceptedCode = ''

  await page.route('**/ready', async (route) => {
    interceptedCode = catalogError.code
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: false,
        data: null,
        error: catalogError,
        meta: {
          request_id: catalogError.reference_id,
          generated_at: new Date().toISOString(),
          duration_ms: 0,
          provenance: 'NONE',
          data_quality: null,
          warnings: [],
          pagination: null,
          idempotent_replay: false,
        },
      }),
    })
  })

  await page.goto('/system')

  const alert = page.getByRole('alert')
  await expect(alert).toHaveText(
    'System status unavailable: Required data does not exist or cannot be fetched.',
  )
  expect(interceptedCode).toBe('DATA_UNAVAILABLE')
  // The daemon answered (it served the page), so the bar shows UP/NOT_READY.
  const statusbar = page.getByRole('region', { name: 'System status bar' })
  await expect(statusbar).toContainText('UP')
  await expect(statusbar).toContainText('NOT_READY')
  // The shell remains usable and shows no scan/diagnostic panels on failure.
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeHidden()
})

test('captures the /system shell at 1440, 768, and 375', async ({ page }) => {
  fs.mkdirSync(SCREENS_DIR, { recursive: true })

  const viewports = [
    { width: 1440, height: 900, name: 'system-1440.png' },
    { width: 768, height: 1024, name: 'system-768.png' },
    { width: 375, height: 812, name: 'system-375.png' },
  ] as const

  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await page.goto('/system')
    await waitForLoadedSystem(page)
    await page.screenshot({
      path: path.join(SCREENS_DIR, viewport.name),
      fullPage: true,
    })
  }
})
