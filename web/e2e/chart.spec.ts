import fs from 'node:fs'
import path from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

/**
 * Slice 39 chart-panel journeys against the reports daemon.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the reports daemon serves `web/dist` with a one-week BTCUSDT backfill and
 *   at least one metrics-only 4h report;
 * - no 15m or 1h report has been published, so those switch controls must be
 *   disabled with an accessible reason and the panel must never invent a
 *   series for them;
 * - OFT_E2E_REPORT_BOOTSTRAP_FILE mints the browser session.
 *
 * Read-only: the browser never creates reports, orders, or LLM calls.
 */

const REPORT_ID = process.env.OFT_E2E_REPORT_ID
const MISSING_REPORT_ID = process.env.OFT_E2E_MISSING_REPORT_ID

const SCREENS_DIR =
  process.env.OFT_E2E_CHART_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e39')

interface ChartBar {
  bar_start: string
  open: string
  high: string
  low: string
  close: string
  volume: string
  delta: string
  cvd: string
}

interface ChartData {
  report_id: string
  symbol: string
  native_period: string
  requested_period: string
  status: 'AVAILABLE' | 'MISSING'
  available_periods: string[]
  bars: ChartBar[]
  metrics: Record<string, unknown> | null
  reason: string | null
  data_quality: string
}

interface ChartEnvelope {
  ok: boolean
  data: ChartData | null
}

interface BrowserDiagnostics {
  consoleErrors: string[]
  pageErrors: string[]
}

function chartPath(reportId: string, period?: string): string {
  const base = `/reports/${encodeURIComponent(reportId)}/chart`
  return period ? `${base}?period=${period}` : base
}

function chartApiPath(reportId: string, period?: string): string {
  const base = `/api/v1/reports/${encodeURIComponent(reportId)}/chart`
  return period ? `${base}?period=${period}` : base
}

async function authenticate(page: Page): Promise<void> {
  const file = process.env.OFT_E2E_REPORT_BOOTSTRAP_FILE
  if (!file) {
    return
  }
  const secret = fs.readFileSync(file, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

function collectDiagnostics(page: Page): BrowserDiagnostics {
  const diagnostics: BrowserDiagnostics = { consoleErrors: [], pageErrors: [] }
  page.on('console', (message) => {
    if (message.type() === 'error') {
      diagnostics.consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => {
    diagnostics.pageErrors.push(error.message)
  })
  return diagnostics
}

function expectClean(diagnostics: BrowserDiagnostics): void {
  expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
  expect(diagnostics.consoleErrors, 'console errors').toEqual([])
}

function utcLabel(iso: string): string {
  return `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')}`
}

async function fetchChart(page: Page, reportId: string, period?: string): Promise<ChartData> {
  const response = await page.request.get(chartApiPath(reportId, period))
  expect(response.status()).toBe(200)
  const body = (await response.json()) as ChartEnvelope
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as ChartData
}

function timeframeGroup(page: Page) {
  return page.getByRole('group', { name: 'Timeframe' })
}

test('deep link renders the full-width chart with the native period active', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(REPORT_ID as string))

  const chart = await fetchChart(page, REPORT_ID as string)
  expect(chart.status).toBe('AVAILABLE')
  expect(chart.native_period).toBe('4h')
  expect(chart.bars.length).toBeGreaterThan(0)

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(/BTCUSDT chart/i)
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
  await expect(page.getByTestId('chart-candles')).toBeVisible()
  await expect(page.getByTestId('chart-cvd')).toBeVisible()
  await expect(page.getByRole('note', { name: /Provenance CALC/i }).first()).toBeVisible()
  expect((await page.locator('main.app-main').getAttribute('class')) ?? '').toContain(
    'app-main--wide',
  )

  const group = timeframeGroup(page)
  await expect(group.getByRole('button', { name: '4h' })).toHaveAttribute('aria-pressed', 'true')
  for (const period of ['15m', '1h']) {
    const button = group.getByRole('button', { name: period })
    // Unavailable controls stay focusable and expose their state and reason.
    await expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(await button.getAttribute('disabled')).toBeNull()
    const describedBy = await button.getAttribute('aria-describedby')
    expect(describedBy, `${period} reason id`).toBeTruthy()
    await expect(page.locator(`#${describedBy}`)).toContainText(`No published ${period}`)
    expect(await button.getAttribute('title')).toContain(`No published ${period}`)
  }

  expectClean(diagnostics)
})

test('an unpublished period shows an honest unavailable panel and no invented chart', async ({
  page,
}) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(REPORT_ID as string, '1h'))

  const chart = await fetchChart(page, REPORT_ID as string, '1h')
  expect(chart.status).toBe('MISSING')
  expect(chart.bars).toEqual([])
  expect(chart.metrics).toBeNull()
  expect(chart.reason).toBeTruthy()

  const note = page.getByRole('note', { name: /Chart unavailable for 1h/i })
  await expect(note).toBeVisible()
  await expect(note).toContainText('No published 1h bars')
  await expect(note).toContainText('never upsamples or interpolates')
  await expect(note).toContainText('Available periods: 4h')
  await expect(page.getByTestId('chart-plot')).toHaveCount(0)
  await expect(page.locator('.chart-frame')).toHaveCount(0)
  await expect(page.getByRole('figure')).toHaveCount(0)
  await expect(page.getByRole('note', { name: /Data quality MISSING/i })).toBeVisible()
  await expect(timeframeGroup(page).getByRole('button', { name: '1h' })).toHaveAttribute(
    'aria-disabled',
    'true',
  )

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'chart-unavailable-1h.png'),
    fullPage: true,
  })
  expectClean(diagnostics)
})

test('the data table carries exactly the chart values and follows the selection', async ({
  page,
}) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  await authenticate(page)
  await page.goto(chartPath(REPORT_ID as string))
  const chart = await fetchChart(page, REPORT_ID as string)

  const toggle = page.getByRole('button', { name: /View data table/i })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')

  const table = page.getByRole('table', { name: 'Chart data table' })
  await expect(table.locator('tbody tr')).toHaveCount(chart.bars.length)
  for (const [index, bar] of chart.bars.entries()) {
    const cells = table.locator('tbody tr').nth(index).locator('td')
    await expect(cells.nth(0)).toHaveText(utcLabel(bar.bar_start))
    await expect(cells.nth(1)).toHaveText(bar.open)
    await expect(cells.nth(2)).toHaveText(bar.high)
    await expect(cells.nth(3)).toHaveText(bar.low)
    await expect(cells.nth(4)).toHaveText(bar.close)
  }

  // The default selection is the last bar; the selected row matches the head.
  const last = chart.bars[chart.bars.length - 1]
  const selected = table.locator('tbody tr[aria-current="true"]')
  await expect(selected).toHaveCount(1)
  await expect(selected).toHaveAttribute('data-selected', 'true')
  await expect(selected.locator('td').first()).toHaveText(utcLabel(last.bar_start))
  await expect(page.locator('.chart-head')).toContainText(last.close)

  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(overflow.document).toBeLessThanOrEqual(overflow.viewport + 1)
})

test('keyboard selection moves aria-pressed and the table row together', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  await authenticate(page)
  await page.goto(chartPath(REPORT_ID as string))
  const chart = await fetchChart(page, REPORT_ID as string)

  await page.getByRole('button', { name: /View data table/i }).click()
  const table = page.getByRole('table', { name: 'Chart data table' })
  const pressed = page.locator('.chart-bars button[aria-pressed="true"]')
  await expect(pressed).toHaveCount(1)
  await pressed.focus()

  const expected = chart.bars[chart.bars.length - 2]
  await page.keyboard.press('ArrowLeft')
  const label = utcLabel(expected.bar_start)
  const next = page.getByRole('button', { name: new RegExp(`${label} UTC`, 'i') })
  await expect(next).toHaveAttribute('aria-pressed', 'true')
  await expect(next).toBeFocused()
  await expect(table.locator('tbody tr[aria-current="true"]').locator('td').first()).toHaveText(
    label,
  )
  await expect(page.locator('.chart-head')).toContainText(expected.close)

  // ArrowRight moves one bar forward again, keeping the table row in step.
  const forward = chart.bars[chart.bars.length - 1]
  const forwardLabel = utcLabel(forward.bar_start)
  await page.keyboard.press('ArrowRight')
  await expect(
    page.getByRole('button', { name: new RegExp(`${forwardLabel} UTC`, 'i') }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(table.locator('tbody tr[aria-current="true"]').locator('td').first()).toHaveText(
    forwardLabel,
  )
  await expect(page.locator('.chart-head')).toContainText(forward.close)

  await page.keyboard.press('Home')
  const first = chart.bars[0]
  await expect(
    page.getByRole('button', { name: new RegExp(`${utcLabel(first.bar_start)} UTC`, 'i') }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(table.locator('tbody tr[aria-current="true"]').locator('td').first()).toHaveText(
    utcLabel(first.bar_start),
  )

  await page.keyboard.press('End')
  await expect(
    page.getByRole('button', {
      name: new RegExp(`${utcLabel(chart.bars[chart.bars.length - 1].bar_start)} UTC`, 'i'),
    }),
  ).toHaveAttribute('aria-pressed', 'true')
})

test('renders read-only and overflow-free at 1440, 768, and 375', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  const viewports = [
    { width: 1440, height: 900 },
    { width: 768, height: 1024 },
    { width: 375, height: 812 },
  ] as const

  for (const viewport of viewports) {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await authenticate(page)
    await page.goto(chartPath(REPORT_ID as string))
    await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()

    const widths = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(widths.document, `document overflow at ${viewport.width}`).toBeLessThanOrEqual(
      widths.viewport + 1,
    )
    expect(widths.body, `body overflow at ${viewport.width}`).toBeLessThanOrEqual(
      widths.viewport + 1,
    )

    const frame = await page.locator('.chart-frame').boundingBox()
    expect(frame, `chart frame at ${viewport.width}`).not.toBeNull()
    expect(frame?.x ?? -1).toBeGreaterThanOrEqual(-1)
    expect((frame?.x ?? 0) + (frame?.width ?? 0)).toBeLessThanOrEqual(widths.viewport + 1)

    // The drawn pane itself must stay inside the viewport, not only its frame.
    const plot = await page.getByTestId('chart-plot').boundingBox()
    expect(plot, `chart plot at ${viewport.width}`).not.toBeNull()
    expect(plot?.x ?? -1).toBeGreaterThanOrEqual(-1)
    expect((plot?.x ?? 0) + (plot?.width ?? 0)).toBeLessThanOrEqual(widths.viewport + 1)

    if (viewport.width === 375) {
      await expect(page.locator('section.chart-panel')).toHaveAttribute('data-readonly', 'true')
      await expect(page.getByText(/Read-only at this width/)).toBeVisible()
      expect(
        await page.locator('#app-main input, #app-main select, #app-main textarea').count(),
        'read-only at 375: no inputs',
      ).toBe(0)
      // The period switch is a read action and stays keyboard operable.
      const barButton = await page.locator('.chart-bars button').first().boundingBox()
      expect(barButton?.height ?? 0).toBeGreaterThanOrEqual(24)
    }

    await page.screenshot({
      path: path.join(SCREENS_DIR, `chart-${viewport.width}.png`),
      fullPage: true,
    })

    expectClean(diagnostics)
  }
})

test('reduced motion leaves the chart frame with no transition or animation', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await authenticate(page)
  await page.goto(chartPath(REPORT_ID as string))
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()

  const durations = await page.locator('.chart-frame').evaluate((element) => {
    const style = getComputedStyle(element)
    return { transition: style.transitionDuration, animation: style.animationDuration }
  })
  expect(durations.transition).toBe('0s')
  expect(durations.animation).toBe('0s')
})

test('the report detail link reaches the chart route', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(`/reports/${encodeURIComponent(REPORT_ID as string)}`)

  const link = page.getByRole('link', { name: /Open chart panel/i })
  await expect(link).toBeVisible()
  await link.click()

  await expect(page).toHaveURL(new RegExp(`/reports/${REPORT_ID as string}/chart`))
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
  expectClean(diagnostics)
})

test('an unknown report shows the error panel with links back', async ({ page }) => {
  await authenticate(page)
  await page.goto(chartPath('RPT-DOES-NOT-EXIST'))

  const alert = page.getByRole('alert')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText(/does not exist/i)
  await expect(alert.getByRole('link', { name: /^Back to report$/i })).toHaveAttribute(
    'href',
    '/reports/RPT-DOES-NOT-EXIST',
  )
  await expect(alert.getByRole('link', { name: /^Back to reports$/i })).toHaveAttribute(
    'href',
    '/reports',
  )
})

test('a report with no recorded market data renders an honest note, never a chart', async ({
  page,
}) => {
  expect(MISSING_REPORT_ID, 'OFT_E2E_MISSING_REPORT_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(MISSING_REPORT_ID as string))

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(/BTCUSDT chart/i)
  const note = page.getByRole('note', { name: /Chart unavailable/i })
  await expect(note).toBeVisible()
  await expect(note).toContainText('The market data required for a chart is MISSING')
  await expect(note).toContainText('Data quality: MISSING')
  await expect(page.getByTestId('chart-plot')).toHaveCount(0)
  await expect(page.getByRole('figure')).toHaveCount(0)
  await expect(timeframeGroup(page).getByRole('button', { name: '4h' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await page.screenshot({
    path: path.join(SCREENS_DIR, 'chart-missing-report.png'),
    fullPage: true,
  })
  expectClean(diagnostics)
})

test('axe reports no WCAG 2.2 A/AA violations at 1440, 768, and 375', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  test.setTimeout(240_000)
  await authenticate(page)

  const viewports = [
    { width: 1440, height: 900 },
    { width: 768, height: 1024 },
    { width: 375, height: 812 },
  ] as const

  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    for (const period of [undefined, '1h'] as const) {
      await page.goto(chartPath(REPORT_ID as string, period))
      await page.waitForLoadState('networkidle')
      if (period === '1h') {
        await expect(page.getByRole('note', { name: /Chart unavailable for 1h/i })).toBeVisible()
      } else {
        await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
      }

      const results = await new AxeBuilder({ page })
        .include('#app-main')
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22a', 'wcag22aa'])
        .analyze()
      const summary = results.violations.map(
        (violation) =>
          `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
            .map((node) => node.target.join(' '))
            .join(' | ')}`,
      )
      expect(
        results.violations,
        `${period ?? 'native'} @ ${viewport.width}px axe violations:\n${summary.join('\n')}`,
      ).toEqual([])
    }
  }
})
