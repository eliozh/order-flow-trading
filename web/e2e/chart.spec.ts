import fs from 'node:fs'
import path from 'node:path'

import { runAxeAudit } from './a11y'
import { expect, test, type Page } from '@playwright/test'

/**
 * Slice 39 chart-panel journeys and slice 40 overlay/crosshair journeys against
 * the reports daemon.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the reports daemon serves `web/dist` with a one-week BTCUSDT backfill and
 *   at least one metrics-only 4h report;
 * - no 15m or 1h report has been published, so those switch controls must be
 *   disabled with an accessible reason and the panel must never invent a
 *   series for them;
 * - the batch report (`OFT_E2E_CHART_OVERLAY_REPORT_ID`) has a linked
 *   opportunity and an accepted paper position, so its chart carries
 *   server-derived zones and markers;
 * - OFT_E2E_REPORT_BOOTSTRAP_FILE mints the browser session.
 *
 * Overlay/crosshair assertions compare the UI with the real chart payload
 * fetched through `page.request`; no overlay value is fabricated client-side.
 *
 * Read-only: the browser never creates reports, orders, or LLM calls.
 */

const REPORT_ID = process.env.OFT_E2E_REPORT_ID
const MISSING_REPORT_ID = process.env.OFT_E2E_MISSING_REPORT_ID
const OVERLAY_REPORT_ID = process.env.OFT_E2E_CHART_OVERLAY_REPORT_ID

const SCREENS_DIR =
  process.env.OFT_E2E_CHART_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e39')
const OVERLAY_SCREENS_DIR =
  process.env.OFT_E2E_CHART_OVERLAY_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e40')

/** Matches `PRICE_TOP`/`PRICE_HEIGHT` in `web/src/components/chart-plate.tsx`. */
const PRICE_TOP = 18
const PRICE_HEIGHT = 210
/** Matches `MAX_OVERLAY_LABEL` in `web/src/components/chart-overlays.tsx`. */
const OVERLAY_LABEL_MAX = 80

interface ChartBar {
  bar_start: string
  bar_end: string
  open: string
  high: string
  low: string
  close: string
  volume: string
  delta: string
  cvd: string
}

interface ChartZone {
  opportunity_id: string
  side: string
  state: string
  low: string
  high: string
  trigger_price: string
  label: string
}

interface ChartMarker {
  kind: 'entry' | 'exit' | 'stop' | 'target'
  price: string
  label: string
  position_id: string | null
}

interface ChartGap {
  gap_id: string | null
  data_type: string
  start: string
  end: string
  reason: string
  severity: string
  resolved: boolean
}

interface ChartData {
  report_id: string
  symbol: string
  native_period: string
  requested_period: string
  status: 'AVAILABLE' | 'MISSING'
  available_periods: string[]
  source_report_id: string | null
  generated_at: string | null
  bars: ChartBar[]
  metrics: Record<string, unknown> | null
  gaps?: ChartGap[]
  zones?: ChartZone[]
  markers?: ChartMarker[]
  quality_reasons?: string[]
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

function overlayReportId(): string {
  expect(OVERLAY_REPORT_ID, 'OFT_E2E_CHART_OVERLAY_REPORT_ID is required').toBeTruthy()
  return OVERLAY_REPORT_ID as string
}

function parsePrice(value: string): number {
  return Number(value)
}

/** Mirrors the client's 80-character overlay label cap. */
function clampOverlayLabel(label: string): string {
  return label.slice(0, OVERLAY_LABEL_MAX)
}

/** The nearest-position y for a price, from the rendered axis extremes. */
function svgY(price: number, rangeMax: number, rangeMin: number): number {
  const span = rangeMax - rangeMin || 1
  return PRICE_TOP + ((rangeMax - price) / span) * PRICE_HEIGHT
}

async function priceAxis(page: Page): Promise<{ max: number; min: number }> {
  const max = Number(await page.getByTestId('price-label-max').innerText())
  const min = Number(await page.getByTestId('price-label-min').innerText())
  expect(Number.isFinite(max), 'price axis max').toBe(true)
  expect(Number.isFinite(min), 'price axis min').toBe(true)
  expect(max).toBeGreaterThan(min)
  return { max, min }
}

/** Mirrors `overlapsGap` in `web/src/components/chart-overlays.tsx`. */
function overlapsGap(bar: ChartBar, gap: ChartGap): boolean {
  const barStart = Date.parse(bar.bar_start)
  const barEnd = Date.parse(bar.bar_end)
  const gapStart = Date.parse(gap.start)
  const gapEnd = Date.parse(gap.end)
  if (![barStart, barEnd, gapStart, gapEnd].every(Number.isFinite)) {
    return false
  }
  return barStart < gapEnd && barEnd > gapStart
}

function gapBarIndices(bars: ChartBar[], gaps: ChartGap[]): number[] {
  const indices: number[] = []
  bars.forEach((bar, index) => {
    if (gaps.some((gap) => overlapsGap(bar, gap))) {
      indices.push(index)
    }
  })
  return indices
}

/** Mirrors `gapBoundaryIndices`: break-before indices for gaps between bars. */
function gapBoundaryIndices(bars: ChartBar[], gaps: ChartGap[]): number[] {
  const indices: number[] = []
  for (let index = 1; index < bars.length; index += 1) {
    const before = bars[index - 1]
    const after = bars[index]
    const beforeEnd = Date.parse(before.bar_end)
    const afterStart = Date.parse(after.bar_start)
    const bracketed = gaps.some((gap) => {
      const gapStart = Date.parse(gap.start)
      const gapEnd = Date.parse(gap.end)
      if (![beforeEnd, afterStart, gapStart, gapEnd].every(Number.isFinite)) {
        return false
      }
      return beforeEnd <= gapStart && gapEnd <= afterStart
    })
    if (bracketed) {
      indices.push(index)
    }
  }
  return indices
}

/** Mirrors `cvdSegments`: one run per contiguous non-gap stretch. */
function cvdRuns(
  bars: ChartBar[],
  gapIndices: ReadonlySet<number>,
  boundaryIndices: ReadonlySet<number>,
): number[][] {
  const runs: number[][] = []
  let run: number[] = []
  bars.forEach((_bar, index) => {
    if (gapIndices.has(index)) {
      if (run.length > 0) {
        runs.push(run)
        run = []
      }
      return
    }
    if (index > 0 && boundaryIndices.has(index) && run.length > 0) {
      runs.push(run)
      run = []
    }
    run.push(index)
  })
  if (run.length > 0) {
    runs.push(run)
  }
  return runs
}

/** Data-table cell text for the selected row, in the tooltip's shared order. */
async function selectedRowCells(page: Page) {
  const row = page.locator('table[aria-label="Chart data table"] tbody tr[aria-current="true"]')
  await expect(row).toHaveCount(1)
  const cells = row.locator('td')
  return {
    time: await cells.nth(0).innerText(),
    open: await cells.nth(1).innerText(),
    high: await cells.nth(2).innerText(),
    low: await cells.nth(3).innerText(),
    close: await cells.nth(4).innerText(),
    volume: await cells.nth(5).innerText(),
    delta: await cells.nth(6).innerText(),
    cvd: await cells.nth(7).innerText(),
    quality: await cells.nth(9).innerText(),
  }
}

/** Assert the tooltip and aria-live region carry the selected row's values. */
async function expectTooltipMatchesSelectedRow(page: Page): Promise<void> {
  const expected = await selectedRowCells(page)
  const tooltip = page.getByTestId('chart-tooltip')
  await expect(tooltip).toBeVisible()
  await expect(tooltip.locator('.chart-tooltip-head')).toHaveText(`${expected.time} UTC`)
  const values = tooltip.locator('dd')
  await expect(values.nth(0)).toHaveText(expected.open)
  await expect(values.nth(1)).toHaveText(expected.high)
  await expect(values.nth(2)).toHaveText(expected.low)
  await expect(values.nth(3)).toHaveText(expected.close)
  await expect(values.nth(4)).toHaveText(expected.volume)
  await expect(values.nth(5)).toHaveText(expected.delta)
  await expect(values.nth(6)).toHaveText(expected.cvd)
  await expect(values.nth(7)).toHaveText(expected.quality)

  const live = page.locator('.chart-frame [aria-live="polite"]')
  await expect(live).toContainText(
    `O ${expected.open} H ${expected.high} L ${expected.low} C ${expected.close}.`,
  )
  await expect(live).toContainText(`Vol ${expected.volume}.`)
  await expect(live).toContainText(`Delta ${expected.delta}.`)
  await expect(live).toContainText(`CVD ${expected.cvd}.`)
  await expect(live).toContainText(`Quality ${expected.quality}.`)
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
  // Overlays are always present on the payload (server-derived, possibly empty).
  expect(Array.isArray(chart.zones)).toBe(true)
  expect(Array.isArray(chart.markers)).toBe(true)

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
        // Expand the accessible data alternative so the audit covers the two
        // tables (and their focusable scroll region), not only the chart.
        const toggle = page.getByRole('button', { name: /View data table/i })
        await toggle.click()
        await expect(toggle).toHaveAttribute('aria-expanded', 'true')
        await expect(page.getByRole('table', { name: /chart data table/i })).toBeVisible()
        await expect(page.getByRole('table', { name: 'Volume profile data table' })).toBeVisible()
      }

      await runAxeAudit(page, `${period ?? 'native'} @ ${viewport.width}px`, {
        include: '#app-main',
      })
    }
  }
})

test('server-derived zones and markers render at their exact payload prices', async ({ page }) => {
  const reportId = overlayReportId()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(reportId))
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()

  const chart = await fetchChart(page, reportId)
  const zones = chart.zones ?? []
  const markers = chart.markers ?? []
  expect(chart.status).toBe('AVAILABLE')
  expect(zones.length).toBeGreaterThan(0)
  expect(markers.length).toBeGreaterThan(0)

  const axis = await priceAxis(page)

  for (const zone of zones) {
    const group = page.getByTestId(`chart-zone-${zone.opportunity_id}`)
    await expect(group).toBeVisible()
    await expect(group).toHaveAttribute('data-state', zone.state)
    await expect(group.locator('.chart-zone-state')).toHaveText(zone.state)
    await expect(group.locator('.chart-zone-label')).toHaveText(clampOverlayLabel(zone.label))
    await expect(group.locator('.chart-zone-band')).toBeVisible()

    // Band edges and the trigger line sit at the exact stored prices.
    const edges = group.locator('.chart-zone-edge')
    await expect(edges).toHaveCount(2)
    const edgeYs = [
      Number(await edges.nth(0).getAttribute('y1')),
      Number(await edges.nth(1).getAttribute('y1')),
    ]
    const highY = svgY(parsePrice(zone.high), axis.max, axis.min)
    const lowY = svgY(parsePrice(zone.low), axis.max, axis.min)
    expect(Math.abs(Math.min(...edgeYs) - Math.min(highY, lowY))).toBeLessThanOrEqual(1.5)
    expect(Math.abs(Math.max(...edgeYs) - Math.max(highY, lowY))).toBeLessThanOrEqual(1.5)

    const trigger = group.getByTestId('chart-trigger-line')
    await expect(trigger).toBeVisible()
    await expect(trigger.locator('.chart-trigger-label')).toHaveText(
      `TRIGGER ${zone.trigger_price}`,
    )
    const triggerY = Number(await trigger.locator('line').getAttribute('y1'))
    expect(
      Math.abs(triggerY - svgY(parsePrice(zone.trigger_price), axis.max, axis.min)),
      `trigger line y for ${zone.opportunity_id}`,
    ).toBeLessThanOrEqual(1.5)
    // LONG triggers at zone.high, SHORT at zone.low.
    const bandY = Number(await group.locator('.chart-zone-band').getAttribute('y'))
    const bandHeight = Number(await group.locator('.chart-zone-band').getAttribute('height'))
    if (zone.side === 'LONG') {
      expect(Math.abs(triggerY - bandY), `LONG trigger at band top`).toBeLessThanOrEqual(0.01)
    } else if (zone.side === 'SHORT') {
      expect(
        Math.abs(triggerY - (bandY + bandHeight)),
        `SHORT trigger at band bottom`,
      ).toBeLessThanOrEqual(0.01)
    }
  }

  await expect(page.locator('[data-testid="chart-markers"] > g')).toHaveCount(markers.length)
  for (const marker of markers) {
    const node = page.locator(`[data-kind="${marker.kind}"][data-price="${marker.price}"]`)
    await expect(node).toHaveCount(1)
    await expect(node).toHaveAttribute('data-kind', marker.kind)
    await expect(node).toHaveAttribute('data-price', marker.price)
    await expect(node.locator('.chart-marker-label')).toHaveText(clampOverlayLabel(marker.label))
    const markerY = Number(await node.getAttribute('data-y'))
    expect(
      Math.abs(markerY - svgY(parsePrice(marker.price), axis.max, axis.min)),
      `${marker.kind} marker y at ${marker.price}`,
    ).toBeLessThanOrEqual(1.5)
  }

  // A marker kind the payload does not contain is absent from the SVG.
  const kinds = new Set(markers.map((marker) => marker.kind))
  for (const kind of ['entry', 'exit', 'stop', 'target'] as const) {
    if (kinds.has(kind)) {
      continue
    }
    const testId = kind === 'entry' ? 'chart-entry-marker' : `chart-marker-${kind}`
    await expect(page.getByTestId(testId), `${kind} marker absent from payload`).toHaveCount(0)
  }

  expectClean(diagnostics)
})

test('gaps are hatched breaks and the CVD is split into contiguous non-gap runs', async ({
  page,
}) => {
  const reportId = overlayReportId()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(reportId))
  await expect(page.getByTestId('chart-cvd')).toBeVisible()

  const chart = await fetchChart(page, reportId)
  const gaps = chart.gaps ?? []
  const gapIndices = new Set(gapBarIndices(chart.bars, gaps))
  const boundaryIndices = new Set(gapBoundaryIndices(chart.bars, gaps))
  const runs = cvdRuns(chart.bars, gapIndices, boundaryIndices)

  const segments = page.locator('[data-testid^="chart-cvd-segment-"]')
  await expect(segments).toHaveCount(runs.length)
  let drawnPoints = 0
  for (const [index, run] of runs.entries()) {
    const points = await page.getByTestId(`chart-cvd-segment-${index}`).getAttribute('points')
    const count = (points ?? '').trim().split(/\s+/).filter(Boolean).length
    expect(count, `CVD segment ${index} point count`).toBe(run.length)
    drawnPoints += count
  }
  expect(drawnPoints).toBe(chart.bars.length - gapIndices.size)

  // One hatched break band per gap window that covers at least one bar.
  const gapGroups = gaps
    .map((gap) => ({
      gap,
      indices: chart.bars
        .map((bar, index) => (overlapsGap(bar, gap) ? index : -1))
        .filter((index) => index >= 0),
    }))
    .filter((group) => group.indices.length > 0)
  const breaks = page.locator('[data-testid="chart-gap-break"]')
  if (gapGroups.length > 0) {
    await expect(page.getByTestId('chart-gaps')).toBeVisible()
    await expect(breaks).toHaveCount(gapGroups.length)
    for (let index = 0; index < gapGroups.length; index += 1) {
      await expect(breaks.nth(index).locator('.chart-gap-band')).toHaveAttribute(
        'fill',
        /^url\(#.+\)$/,
      )
      await expect(breaks.nth(index).locator('.chart-gap-label')).toHaveText(/^GAP /)
    }
    // A gap inside the series can never be bridged by one polyline.
    if (gapIndices.size > 0 && gapIndices.size < chart.bars.length) {
      expect(runs.length, 'CVD runs around a gap').toBeGreaterThanOrEqual(2)
    }
  } else if (gapBoundaryIndices(chart.bars, gaps).length === 0) {
    await expect(page.getByTestId('chart-gaps')).toHaveCount(0)
    await expect(breaks).toHaveCount(0)
    await expect(segments).toHaveCount(1)
  }

  expectClean(diagnostics)
})

test('a server gap renders a hatched break, split CVD runs, and a MISSING stamp', async ({
  page,
}) => {
  // The seeded overlay report has `gaps: []`, so this branch is exercised with
  // a route-intercepted payload that carries one server gap. Application code
  // is unchanged; only the API response for this page is replaced.
  const reportId = overlayReportId()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)

  const response = await page.request.get(chartApiPath(reportId))
  expect(response.status()).toBe(200)
  const envelope = (await response.json()) as ChartEnvelope
  const chart = envelope.data as ChartData
  expect(chart.bars.length).toBeGreaterThanOrEqual(3)

  const gapBar = chart.bars[1]
  const gap: ChartGap = {
    gap_id: 'E2E-GAP-1',
    data_type: 'aggTrades',
    start: gapBar.bar_start,
    end: gapBar.bar_end,
    reason: 'e2e synthetic gap',
    severity: 'WARN',
    resolved: false,
  }
  const injected: ChartData = { ...chart, gaps: [gap] }
  await page.route(chartApiPath(reportId), async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...envelope, data: injected }),
    })
  })

  try {
    await page.goto(chartPath(reportId))
    await expect(page.getByTestId('chart-cvd')).toBeVisible()

    // Exactly one hatched break band over the one covered bar.
    await expect(page.getByTestId('chart-gaps')).toBeVisible()
    const breaks = page.locator('[data-testid="chart-gap-break"]')
    await expect(breaks).toHaveCount(1)
    await expect(breaks.locator('.chart-gap-band')).toHaveAttribute('fill', /^url\(#.+\)$/)
    await expect(breaks.locator('.chart-gap-label')).toHaveText(/^GAP /)

    // The gap bar is a break: the CVD is two contiguous runs, never one line
    // interpolated across the missing bar.
    expect(gapBarIndices(chart.bars, [gap])).toEqual([1])
    await expect(page.locator('[data-testid^="chart-cvd-segment-"]')).toHaveCount(2)

    // The table stamps the gap bar MISSING while its neighbours stay COMPLETE.
    await page.getByRole('button', { name: /View data table/i }).click()
    const rows = page.locator('table[aria-label="Chart data table"] tbody tr')
    await expect(rows.nth(1).locator('td').nth(9)).toHaveText('MISSING')
    await expect(rows.nth(0).locator('td').nth(9)).not.toHaveText('MISSING')
    expectClean(diagnostics)
  } finally {
    await page.unroute(chartApiPath(reportId))
  }
})

test('a gap bracketed by two present bars breaks the CVD without a false MISSING stamp', async ({
  page,
}) => {
  // The seeded overlay report has `gaps: []`, so drop one middle snapshot bar
  // and inject a gap covering exactly its window. Both neighbours are present;
  // the missing window is between them. Only the API response is replaced.
  const reportId = overlayReportId()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)

  const response = await page.request.get(chartApiPath(reportId))
  expect(response.status()).toBe(200)
  const envelope = (await response.json()) as ChartEnvelope
  const chart = envelope.data as ChartData
  expect(chart.bars.length).toBeGreaterThanOrEqual(3)

  const dropped = chart.bars[1]
  const remaining = chart.bars.filter((_bar, index) => index !== 1)
  const gap: ChartGap = {
    gap_id: 'E2E-GAP-BRACKET',
    data_type: 'aggTrades',
    start: dropped.bar_start,
    end: dropped.bar_end,
    reason: 'e2e synthetic bracketed gap',
    severity: 'WARN',
    resolved: false,
  }
  const injected: ChartData = { ...chart, bars: remaining, gaps: [gap] }
  await page.route(chartApiPath(reportId), async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...envelope, data: injected }),
    })
  })

  try {
    // Mirror the client segmentation: no bar overlaps the gap, one boundary.
    expect(gapBarIndices(remaining, [gap])).toEqual([])
    const boundaryIndices = new Set(gapBoundaryIndices(remaining, [gap]))
    expect([...boundaryIndices]).toEqual([1])
    const runs = cvdRuns(remaining, new Set<number>(), boundaryIndices)
    expect(runs).toHaveLength(2)

    await page.goto(chartPath(reportId))
    await expect(page.getByTestId('chart-cvd')).toBeVisible()

    // A boundary break, not an overlap band over a present bar.
    await expect(page.getByTestId('chart-gap-boundary')).toHaveCount(1)
    await expect(page.getByTestId('chart-gap-boundary').locator('.chart-gap-label')).toHaveText(
      /^GAP /,
    )
    await expect(page.locator('[data-testid="chart-gap-break"]')).toHaveCount(0)

    // Two disjoint polylines, and every present bar is drawn exactly once: no
    // interpolated point bridges the missing window.
    const segments = page.locator('[data-testid^="chart-cvd-segment-"]')
    await expect(segments).toHaveCount(2)
    let drawnPoints = 0
    for (const [index, run] of runs.entries()) {
      const points = await page
        .getByTestId(`chart-cvd-segment-${index}`)
        .getAttribute('points')
      const count = (points ?? '').trim().split(/\s+/).filter(Boolean).length
      expect(count, `CVD segment ${index} point count`).toBe(run.length)
      drawnPoints += count
    }
    expect(drawnPoints).toBe(remaining.length)

    // The neighbouring bars are present, so none is stamped MISSING.
    await page.getByRole('button', { name: /View data table/i }).click()
    const rows = page.locator('table[aria-label="Chart data table"] tbody tr')
    await expect(rows).toHaveCount(remaining.length)
    for (let index = 0; index < remaining.length; index += 1) {
      await expect(rows.nth(index).locator('td').nth(9)).not.toHaveText('MISSING')
    }

    expectClean(diagnostics)
  } finally {
    await page.unroute(chartApiPath(reportId))
  }
})

test('keyboard crosshair matches the data table and Escape hides only the crosshair', async ({
  page,
}) => {
  const reportId = overlayReportId()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(reportId))
  const chart = await fetchChart(page, reportId)
  expect(chart.bars.length).toBeGreaterThan(4)

  await page.getByRole('button', { name: /View data table/i }).click()
  const pressed = page.locator('.chart-bars button[aria-pressed="true"]')
  await expect(pressed).toHaveCount(1)
  await pressed.focus()

  const last = chart.bars.length - 1
  const steps = [
    { key: 'ArrowLeft', index: last - 1 },
    { key: 'ArrowRight', index: last },
    { key: 'Home', index: 0 },
    { key: 'End', index: last },
  ] as const

  for (const step of steps) {
    await page.keyboard.press(step.key)
    const bar = chart.bars[step.index]
    await expect(page.locator('.chart-bars button[aria-pressed="true"]')).toHaveAttribute(
      'data-index',
      String(step.index),
    )
    await expect(
      page.locator('table[aria-label="Chart data table"] tbody tr[aria-current="true"] td').first(),
    ).toHaveText(utcLabel(bar.bar_start))
    await expectTooltipMatchesSelectedRow(page)
  }

  // Escape hides the crosshair and tooltip but keeps the bar selection/table row.
  const selectedIndex = await page
    .locator('.chart-bars button[aria-pressed="true"]')
    .getAttribute('data-index')
  const selectedTime = await selectedRowCells(page)
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('chart-tooltip')).toHaveCount(0)
  await expect(page.getByTestId('chart-crosshair')).toHaveCount(0)
  await expect(page.locator('.chart-bars button[aria-pressed="true"]')).toHaveAttribute(
    'data-index',
    selectedIndex ?? '',
  )
  await expect(
    page.locator('table[aria-label="Chart data table"] tbody tr[aria-current="true"] td').first(),
  ).toHaveText(selectedTime.time)

  expectClean(diagnostics)
})

test('pointer hover opens the tooltip without stealing keyboard focus', async ({ page }) => {
  const reportId = overlayReportId()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(chartPath(reportId))
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()

  await page.getByRole('button', { name: /View data table/i }).click()
  const pressed = page.locator('.chart-bars button[aria-pressed="true"]')
  await pressed.focus()
  const focusIndex = await page.evaluate(
    () => (document.activeElement as HTMLElement | null)?.dataset.index ?? null,
  )
  expect(focusIndex).not.toBeNull()
  const selectedRowTime = await page
    .locator('table[aria-label="Chart data table"] tbody tr[aria-current="true"] td')
    .first()
    .innerText()

  const chart = await fetchChart(page, reportId)
  const canvas = page.getByTestId('chart-canvas')
  const box = await canvas.boundingBox()
  expect(box, 'chart canvas box').not.toBeNull()
  if (box === null) {
    return
  }
  await page.mouse.move(box.x + box.width * 0.35, box.y + box.height * 0.35)
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.4)
  const tooltip = page.getByTestId('chart-tooltip')
  await expect(tooltip).toBeVisible()

  // Hover moves only the crosshair/tooltip to the bar under the pointer; the
  // keyboard selection, tab stop, and table row do not move.
  const slot = (760 - 2 * 12) / chart.bars.length
  const hoveredIndex = Math.min(
    Math.max(Math.floor(((0.4 * 760) - 12) / slot), 0),
    chart.bars.length - 1,
  )
  await expect(tooltip.locator('.chart-tooltip-head')).toHaveText(
    `${utcLabel(chart.bars[hoveredIndex].bar_start)} UTC`,
  )

  const active = await page.evaluate(() => {
    const element = document.activeElement as HTMLElement | null
    return {
      className: element?.className ?? '',
      index: element?.dataset.index ?? null,
    }
  })
  expect(active.className).toContain('bar-select')
  expect(active.index).toBe(focusIndex)
  await expect(pressed).toHaveAttribute('data-index', String(focusIndex))
  await expect(pressed).toHaveAttribute('tabindex', '0')
  await expect(
    page.locator('table[aria-label="Chart data table"] tbody tr[aria-current="true"] td').first(),
  ).toHaveText(selectedRowTime)
  expectClean(diagnostics)
})

test('overlay labels stay inside the frame and the page never overflows at 1440, 768, and 375', async ({
  page,
}) => {
  const reportId = overlayReportId()
  test.setTimeout(120_000)
  fs.mkdirSync(OVERLAY_SCREENS_DIR, { recursive: true })

  const viewports = [
    { width: 1440, height: 900 },
    { width: 768, height: 1024 },
    { width: 375, height: 812 },
  ] as const

  for (const viewport of viewports) {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await authenticate(page)
    await page.goto(chartPath(reportId))
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
    const labels = page.locator(
      '.chart-frame .chart-zone-state, .chart-frame .chart-zone-label, ' +
        '.chart-frame .chart-trigger-label, .chart-frame .chart-marker-label, ' +
        '.chart-frame .chart-gap-label',
    )
    const count = await labels.count()
    expect(count, `overlay labels at ${viewport.width}`).toBeGreaterThan(0)
    for (let index = 0; index < count; index += 1) {
      const box = await labels.nth(index).boundingBox()
      expect(box, `overlay label ${index} box at ${viewport.width}`).not.toBeNull()
      if (box === null || frame === null) {
        continue
      }
      expect(box.x, `label ${index} left at ${viewport.width}`).toBeGreaterThanOrEqual(frame.x - 1)
      expect(box.x + box.width, `label ${index} right at ${viewport.width}`).toBeLessThanOrEqual(
        frame.x + frame.width + 1,
      )
      expect(box.y, `label ${index} top at ${viewport.width}`).toBeGreaterThanOrEqual(frame.y - 1)
      expect(box.y + box.height, `label ${index} bottom at ${viewport.width}`).toBeLessThanOrEqual(
        frame.y + frame.height + 1,
      )
    }

    await page.screenshot({
      path: path.join(OVERLAY_SCREENS_DIR, `chart-overlays-${viewport.width}.png`),
      fullPage: true,
    })
    expectClean(diagnostics)
  }
})

test('reduced motion removes transitions from the tooltip and overlay elements', async ({
  page,
}) => {
  const reportId = overlayReportId()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await authenticate(page)
  await page.goto(chartPath(reportId))
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()

  await page.locator('.chart-bars button[aria-pressed="true"]').focus()
  await page.keyboard.press('ArrowLeft')
  await expect(page.getByTestId('chart-tooltip')).toBeVisible()

  const targets = [
    page.getByTestId('chart-tooltip'),
    page.getByTestId('chart-crosshair'),
    page.locator('.chart-zone-band').first(),
    page.locator('.chart-marker-shape').first(),
  ]
  for (const [index, target] of targets.entries()) {
    const durations = await target.evaluate((element) => {
      const style = getComputedStyle(element)
      return { transition: style.transitionDuration, animation: style.animationDuration }
    })
    expect(durations.transition, `target ${index} transition`).toBe('0s')
    expect(durations.animation, `target ${index} animation`).toBe('0s')
  }
})

test('axe reports no WCAG 2.2 A/AA violations on the overlay report at all widths', async ({
  page,
}) => {
  const reportId = overlayReportId()
  test.setTimeout(240_000)
  await authenticate(page)

  const viewports = [
    { width: 1440, height: 900 },
    { width: 768, height: 1024 },
    { width: 375, height: 812 },
  ] as const

  for (const viewport of viewports) {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await page.goto(chartPath(reportId))
    await page.waitForLoadState('networkidle')
    await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
    await expect(page.locator('[data-testid="chart-zones"]')).toBeVisible()
    await expect(page.locator('[data-testid="chart-markers"]')).toBeVisible()

    // Open the data tables first so the audit covers the accessible alternative
    // (tables, scroll region, selected row state) alongside the overlays.
    const toggle = page.getByRole('button', { name: /View data table/i })
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByRole('table', { name: /chart data table/i })).toBeVisible()
    await expect(page.getByRole('table', { name: 'Volume profile data table' })).toBeVisible()

    await runAxeAudit(page, `overlay report @ ${viewport.width}px`, {
      include: '#app-main',
    })
    expectClean(diagnostics)
  }
})
