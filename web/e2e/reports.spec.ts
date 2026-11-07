import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

/**
 * Critical journeys for slice 08: the metrics-only CALC report, its chart, and
 * the scans read path.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - a third daemon serves `web/dist` on OFT_E2E_REPORT_BASE_URL with a
 *   temporary data dir, the exchangeInfo fixture, and the archive fixture dir;
 * - a one-week BTCUSDT backfill and a metrics-only scan already produced
 *   OFT_E2E_REPORT_ID, OFT_E2E_REPORT_SCAN_ID, and their frozen report;
 * - OFT_E2E_MISSING_REPORT_ID and OFT_E2E_HOSTILE_REPORT_ID are fixture rows
 *   seeded into the same real SQLite store (no browser route interception);
 * - OFT_E2E_REPORT_BOOTSTRAP_FILE mints the browser session.
 *
 * Read-only: the browser never creates scans, orders, or LLM calls, and no
 * response body is rewritten in the browser.
 */

const SCREENS_DIR =
  process.env.OFT_E2E_REPORT_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e08')

const REPORT_ID = process.env.OFT_E2E_REPORT_ID
const SECOND_REPORT_ID = process.env.OFT_E2E_SECOND_REPORT_ID
const SCAN_ID = process.env.OFT_E2E_REPORT_SCAN_ID
const MISSING_REPORT_ID = process.env.OFT_E2E_MISSING_REPORT_ID
const HOSTILE_REPORT_ID = process.env.OFT_E2E_HOSTILE_REPORT_ID
const SYMBOL = 'BTCUSDT'

interface SnapshotBar {
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

interface FootprintCell {
  total_volume: string
}

interface FootprintBar {
  bar_start: string
  cells: FootprintCell[]
}

interface VolumeBin {
  price_low: string
  price_high: string
  volume: string
  is_poc: boolean
  in_value_area: boolean
}

interface ReportData {
  report_id: string
  scan_id: string
  symbol: string
  timeframe: string
  generated_at: string
  data_quality: string
  llm_decision_id: string | null
  narrative_ref: string | null
  llm: { configured: boolean; reason: string }
  metrics_manifest: unknown[]
  snapshot: { bars: SnapshotBar[]; data_quality: string }
  metrics: {
    cvd: { summary: { cvd_change: string }; points: unknown[] }
    volume_profile: {
      poc: string
      vah: string
      val: string
      value_area_pct: number
      bin_size: string
      bins: VolumeBin[]
    }
    footprint: { bars: FootprintBar[] }
    open_interest: { delta_over_window: string; sampling: string; points: unknown[] }
    funding: { current_rate: string; next_funding_time: string; history: unknown[] }
    liquidations: { count: number; coverage: string }
    gaps: unknown[]
    quality_reasons: string[]
  }
}

interface Envelope<T> {
  ok: boolean
  data: T | null
}

interface ScanData {
  scan_id: string
  symbol: string
  period: string
  status: string
  trigger_source: string
  metrics_only: boolean
  trade_count: number
  data_quality: string
  report_id: string | null
  source_mode: string
  metrics_manifest_json: string | null
}

interface BrowserDiagnostics {
  consoleErrors: string[]
  pageErrors: string[]
}

function reportPath(reportId: string): string {
  return `/reports/${encodeURIComponent(reportId)}`
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

function signed(value: string): string {
  if (value.startsWith('-') || value === '0') {
    return value
  }
  return `+${value}`
}

function footprintTotal(bar: FootprintBar): string {
  return bar.cells.reduce((sum, cell) => sum + Number(cell.total_volume), 0).toFixed(2)
}

async function openDataTables(page: Page): Promise<void> {
  const toggle = page.getByRole('button', { name: /view data table/i })
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') {
    await toggle.click()
  }
  await expect(page.getByRole('table', { name: 'Chart data table' })).toBeVisible()
  await expect(page.getByRole('table', { name: 'Volume profile data table' })).toBeVisible()
}

/**
 * A cell must not break words per character (the 375px regression). Space
 * wrapping is allowed: line boxes may not exceed the number of whitespace runs.
 */
async function expectNoPerCharacterWrap(page: Page, table: Locator): Promise<void> {
  const offenders = await table.locator('tbody td, tbody th').evaluateAll((nodes) =>
    nodes
      .map((node) => {
        const element = node as HTMLElement
        const style = getComputedStyle(element)
        const range = document.createRange()
        range.selectNodeContents(element)
        const tops = new Set(Array.from(range.getClientRects()).map((rect) => Math.round(rect.top)))
        const text = (element.textContent ?? '').trim()
        const spaces = (text.match(/\s+/g) ?? []).length
        return {
          text: text.slice(0, 40),
          lines: tops.size,
          allowed: spaces + 1,
          wordBreak: style.wordBreak,
          overflowWrap: style.overflowWrap,
        }
      })
      .filter(
        (cell) =>
          cell.lines > cell.allowed ||
          cell.wordBreak === 'break-all' ||
          cell.overflowWrap === 'anywhere',
      ),
  )
  expect(offenders, 'table text must not wrap per character').toEqual([])
}

/** Axis and pane labels must be visible, legible, unclipped, and non-overlapping. */
async function expectChartLabelsReadable(page: Page): Promise<void> {
  const axis = page.getByTestId('chart-axis-labels')
  await expect(axis).toBeVisible()
  const axisSpans = axis.locator('span')
  await expect(axisSpans).toHaveCount(3)
  for (let index = 0; index < 3; index += 1) {
    await expect(axisSpans.nth(index)).toBeVisible()
    expect((await axisSpans.nth(index).innerText()).trim()).toMatch(/^\d+\.\d{2}$/)
  }
  const paneLabels = page.locator('.chart-pane-labels span')
  await expect(paneLabels.filter({ hasText: /^Price$/ })).toHaveCount(1)
  await expect(paneLabels.filter({ hasText: /^Volume$/ })).toHaveCount(1)
  await expect(paneLabels.filter({ hasText: /^CVD$/ })).toHaveCount(1)
  await expect(paneLabels.filter({ hasText: /^POC / })).toHaveCount(1)

  const report = await page.evaluate(() => {
    function rectsFor(selector: string) {
      const labels = Array.from(document.querySelectorAll<HTMLElement>(selector))
      return labels.map((label) => {
        const rect = label.getBoundingClientRect()
        return {
          text: (label.textContent ?? '').trim(),
          fontSize: Number.parseFloat(getComputedStyle(label).fontSize),
          width: rect.width,
          height: rect.height,
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
          clipped: label.scrollWidth > label.clientWidth + 1,
        }
      })
    }
    function overlaps(rects: ReturnType<typeof rectsFor>): boolean {
      for (let i = 0; i < rects.length; i += 1) {
        for (let j = i + 1; j < rects.length; j += 1) {
          const a = rects[i]
          const b = rects[j]
          if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
            return true
          }
        }
      }
      return false
    }
    const axis = rectsFor('.chart-axis-labels span')
    const pane = rectsFor('.chart-pane-labels span')
    return { rects: [...axis, ...pane], axisOverlap: overlaps(axis), paneOverlap: overlaps(pane) }
  })

  expect(report.rects.length).toBeGreaterThanOrEqual(8)
  for (const label of report.rects) {
    expect(label.text.length, 'chart label text').toBeGreaterThan(0)
    expect(label.fontSize, `font size for ${label.text}`).toBeGreaterThanOrEqual(10)
    expect(label.width, `width for ${label.text}`).toBeGreaterThan(0)
    expect(label.height, `height for ${label.text}`).toBeGreaterThan(0)
    expect(label.clipped, `clipped label ${label.text}`).toBe(false)
  }
  expect(report.axisOverlap, 'axis labels must not overlap').toBe(false)
  expect(report.paneOverlap, 'pane labels must not overlap').toBe(false)
}

async function fetchReport(page: Page, reportId: string): Promise<ReportData> {
  const response = await page.request.get(`/api/v1/reports/${encodeURIComponent(reportId)}`)
  expect(response.status()).toBe(200)
  const body = (await response.json()) as Envelope<ReportData>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as ReportData
}

async function fetchScan(page: Page, scanId: string): Promise<ScanData> {
  const response = await page.request.get(`/api/v1/scans/${encodeURIComponent(scanId)}`)
  expect(response.status()).toBe(200)
  const body = (await response.json()) as Envelope<ScanData>
  expect(body.ok).toBe(true)
  expect(body.data).not.toBeNull()
  return body.data as ScanData
}

function calcRegion(page: Page) {
  return page.getByRole('region', { name: 'CALC metrics table' })
}

function calcRow(page: Page, label: string) {
  return calcRegion(page).locator('tbody tr').filter({ hasText: label })
}

test('renders the metrics-only CALC report with CVD, VP, footprint, OI, funding, and liquidations', async ({
  page,
}) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(reportPath(REPORT_ID as string))

  const report = await fetchReport(page, REPORT_ID as string)

  // No LLM surface was generated or fabricated; the disabled band is explicit.
  expect(report.llm.configured).toBe(false)
  expect(report.llm.reason).toMatch(/LLM not configured/i)
  expect(report.llm_decision_id).toBeNull()
  expect(report.narrative_ref).toBeNull()
  expect(report.metrics_manifest.length).toBeGreaterThan(0)

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(REPORT_ID as string)
  await expect(page.getByRole('heading', { name: /CALC · deterministic metrics/i })).toBeVisible()
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
  await expect(page.getByTestId('chart-candles')).toBeVisible()
  await expect(page.getByTestId('chart-volume')).toBeVisible()
  await expect(page.getByTestId('chart-cvd')).toBeVisible()
  await expect(page.getByTestId('chart-volume-profile')).toBeVisible()
  await expect(page.getByRole('region', { name: /Footprint for selected bar/i })).toBeVisible()
  await expect(page.getByRole('note', { name: /Provenance CALC/i }).first()).toBeVisible()

  await expect(calcRow(page, 'CVD (window)')).toContainText(
    signed(report.metrics.cvd.summary.cvd_change),
  )
  await expect(calcRow(page, 'POC')).toContainText(report.metrics.volume_profile.poc)
  await expect(calcRow(page, 'VAH / VAL')).toContainText(
    `${report.metrics.volume_profile.vah} / ${report.metrics.volume_profile.val}`,
  )
  await expect(calcRow(page, 'Volume Profile')).toContainText(
    `${report.metrics.volume_profile.bins.length} bins`,
  )
  await expect(calcRow(page, 'Footprint (selected bar)')).toContainText('cells')
  const oiMeasured = report.metrics.open_interest.points.length > 0
  const fundingMeasured = report.metrics.funding.history.length > 0
  const oiRow = calcRow(page, 'OI Δ')
  if (oiMeasured) {
    await expect(oiRow).toContainText(report.metrics.open_interest.delta_over_window)
  } else {
    // Unmeasured inputs are displayed explicitly, never as a fabricated zero.
    await expect(oiRow).toContainText('not measured')
    await expect(oiRow).toContainText('MISSING · no measured open-interest sample')
    expect(report.metrics.quality_reasons.join(' ')).toMatch(/open_interest not measured/)
  }
  const fundingRow = calcRow(page, 'Funding')
  if (fundingMeasured) {
    await expect(fundingRow).toContainText(report.metrics.funding.current_rate)
    await expect(fundingRow).toContainText('next ')
  } else {
    await expect(fundingRow).toContainText('not measured')
    await expect(fundingRow).toContainText('MISSING · no measured funding round')
    await expect(fundingRow).not.toContainText('next ')
    expect(report.metrics.quality_reasons.join(' ')).toMatch(/funding not measured/)
  }
  await expect(calcRow(page, 'Liquidations')).toContainText(
    `${report.metrics.liquidations.count} events`,
  )
  await expect(calcRow(page, 'Gaps')).toContainText(String(report.metrics.gaps.length))

  const llm = page.getByRole('region', { name: /LLM analysis/i })
  await expect(llm).toContainText('LLM not configured')
  await expect(llm).toContainText(report.llm.reason)
  await expect(llm).toContainText('no narrative')
  await expect(llm).not.toContainText('confidence')

  expect(await page.locator('main input, main select, main textarea, main form').count()).toBe(0)
  expectClean(diagnostics)
})

test('consecutive metrics-only scans produce distinct, readable report ids', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  expect(SECOND_REPORT_ID, 'OFT_E2E_SECOND_REPORT_ID is required').toBeTruthy()
  // Rapid sequential scans must never collide on the report primary key.
  expect(SECOND_REPORT_ID).not.toBe(REPORT_ID)

  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  const first = await fetchReport(page, REPORT_ID as string)
  const second = await fetchReport(page, SECOND_REPORT_ID as string)
  expect(second.report_id).toBe(SECOND_REPORT_ID)
  expect(second.scan_id).not.toBe(first.scan_id)
  expect(second.snapshot.bars.length).toBeGreaterThan(0)

  await page.goto(reportPath(SECOND_REPORT_ID as string))
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(SECOND_REPORT_ID as string)
  await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
  expectClean(diagnostics)
})

test('selecting a chart bar updates the footprint dock and matches the CALC table', async ({
  page,
}) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  await authenticate(page)
  await page.goto(reportPath(REPORT_ID as string))

  const report = await fetchReport(page, REPORT_ID as string)
  expect(report.snapshot.bars.length).toBeGreaterThan(1)
  expect(report.metrics.footprint.bars.length).toBeGreaterThan(1)

  const target = report.snapshot.bars[1]
  const label = utcLabel(target.bar_start)
  const footprintBar = report.metrics.footprint.bars.find(
    (bar) => bar.bar_start === target.bar_start,
  )
  expect(footprintBar, `footprint row for ${label}`).toBeTruthy()
  const footprint = footprintBar as FootprintBar
  const total = footprintTotal(footprint)

  const barButton = page.getByRole('button', { name: new RegExp(`${label} UTC`, 'i') })
  await barButton.click()
  await expect(barButton).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('.chart-bars button[aria-pressed="true"]')).toHaveCount(1)

  const calcSelected = calcRow(page, 'Footprint (selected bar)')
  await expect(calcSelected).toContainText(`${footprint.cells.length} cells`)
  await expect(calcSelected).toContainText(total)

  const dock = page.getByRole('region', { name: /Footprint for selected bar/i })
  await expect(dock).toContainText(label)
  await expect(dock).toContainText(`${footprint.cells.length} price levels`)
  await expect(dock).toContainText(total)

  // The chart head reflects the same selected bar as the CALC table.
  await expect(page.locator('.chart-head')).toContainText(target.close)
})

test('data-table alternative carries exactly the chart values', async ({ page }) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  await authenticate(page)
  await page.goto(reportPath(REPORT_ID as string))
  const report = await fetchReport(page, REPORT_ID as string)

  await openDataTables(page)
  await expect(page.getByRole('region', { name: /chart data table/i })).toBeVisible()
  const table = page.getByRole('table', { name: /chart data table/i })
  const rows = table.locator('tbody tr')
  await expect(rows).toHaveCount(report.snapshot.bars.length)

  for (const [index, bar] of report.snapshot.bars.entries()) {
    const cells = rows.nth(index).locator('td')
    await expect(cells.nth(0)).toHaveText(utcLabel(bar.bar_start))
    await expect(cells.nth(1)).toHaveText(bar.open)
    await expect(cells.nth(2)).toHaveText(bar.high)
    await expect(cells.nth(3)).toHaveText(bar.low)
    await expect(cells.nth(4)).toHaveText(bar.close)
    await expect(cells.nth(5)).toHaveText(bar.volume)
    await expect(cells.nth(6)).toHaveText(signed(bar.delta))
    await expect(cells.nth(7)).toHaveText(signed(bar.cvd))
    await expect(cells.nth(8)).toHaveText(Number(bar.close) >= Number(bar.open) ? 'up' : 'down')
  }

  const vpTable = page.getByRole('table', { name: 'Volume profile data table' })
  const vpRows = vpTable.locator('tbody tr')
  await expect(vpRows).toHaveCount(report.metrics.volume_profile.bins.length)
  for (const [index, bin] of report.metrics.volume_profile.bins.entries()) {
    const cells = vpRows.nth(index).locator('td')
    await expect(cells.nth(0)).toHaveText(bin.price_low)
    await expect(cells.nth(1)).toHaveText(bin.price_high)
    await expect(cells.nth(2)).toHaveText(bin.volume)
    await expect(cells.nth(3)).toHaveText(bin.is_poc ? 'POC' : '—')
    await expect(cells.nth(4)).toHaveText(bin.in_value_area ? 'yes' : 'no')
  }

  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(overflow.document).toBeLessThanOrEqual(overflow.viewport)
})

test('the chart is keyboard operable with continuous focus and drives the footprint dock', async ({
  page,
}) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  await authenticate(page)
  await page.goto(reportPath(REPORT_ID as string))
  const report = await fetchReport(page, REPORT_ID as string)
  const bars = report.snapshot.bars
  const dock = page.getByRole('region', { name: /Footprint for selected bar/i })

  // The default selection is the last bar. Repeated ArrowLeft presses must move
  // exactly one bar each time and keep DOM focus on the selected button.
  const pressed = page.locator('.chart-bars button[aria-pressed="true"]')
  await expect(pressed).toHaveCount(1)
  await pressed.focus()
  for (let step = 1; step <= 3; step += 1) {
    await page.keyboard.press('ArrowLeft')
    const expected = bars[bars.length - 1 - step]
    const label = utcLabel(expected.bar_start)
    const selected = page.getByRole('button', { name: new RegExp(`${label} UTC`, 'i') })
    await expect(selected).toHaveAttribute('aria-pressed', 'true')
    await expect(selected).toBeFocused()
    await expect(dock).toContainText(label)
  }

  // Home/End remain absolute jumps and keep focus on the new selection.
  const firstLabel = utcLabel(bars[0].bar_start)
  const lastLabel = utcLabel(bars[bars.length - 1].bar_start)
  await page.keyboard.press('End')
  const last = page.getByRole('button', { name: new RegExp(`${lastLabel} UTC`, 'i') })
  await expect(last).toHaveAttribute('aria-pressed', 'true')
  await expect(last).toBeFocused()
  await expect(dock).toContainText(lastLabel)

  await page.keyboard.press('Home')
  const first = page.getByRole('button', { name: new RegExp(`${firstLabel} UTC`, 'i') })
  await expect(first).toHaveAttribute('aria-pressed', 'true')
  await expect(first).toBeFocused()
  await expect(dock).toContainText(firstLabel)
})

test('renders an explanation instead of a chart for a real MISSING report', async ({ page }) => {
  expect(MISSING_REPORT_ID, 'OFT_E2E_MISSING_REPORT_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(reportPath(MISSING_REPORT_ID as string))

  const report = await fetchReport(page, MISSING_REPORT_ID as string)
  expect(report.data_quality).toBe('MISSING')
  expect(report.snapshot.bars).toHaveLength(0)

  const note = page.getByRole('note', { name: /Chart unavailable/i })
  await expect(note).toBeVisible()
  await expect(note).toContainText(/No chart/i)
  await expect(note).toContainText('MISSING')
  await expect(page.getByTestId('chart-plot')).toHaveCount(0)
  await expect(page.getByTestId('chart-candles')).toHaveCount(0)
  await expect(page.getByRole('table', { name: /chart data table/i })).toHaveCount(0)
  expect(await page.locator('main input, main select, main textarea, main form').count()).toBe(0)
  expectClean(diagnostics)
})

test('hostile symbol metadata is rendered as text, never as markup', async ({ page }) => {
  expect(HOSTILE_REPORT_ID, 'OFT_E2E_HOSTILE_REPORT_ID is required').toBeTruthy()
  const hostile = '<img src=x onerror="alert(1)">'
  await authenticate(page)
  await page.goto(reportPath(HOSTILE_REPORT_ID as string))

  const report = await fetchReport(page, HOSTILE_REPORT_ID as string)
  expect(report.symbol).toBe(hostile)

  await expect(page.getByText(hostile).first()).toBeVisible()
  await expect(page.locator('main img')).toHaveCount(0)
  expect(await page.evaluate(() => document.querySelectorAll('main script').length)).toBe(0)
})

test('/scans lists the metrics-only scan and links to its report', async ({ page }) => {
  expect(SCAN_ID, 'OFT_E2E_REPORT_SCAN_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto('/scans')

  const scan = await fetchScan(page, SCAN_ID as string)
  expect(scan.status).toBe('DONE')
  expect(scan.metrics_only).toBe(true)
  expect(scan.trigger_source).toBe('MANUAL')
  expect(scan.source_mode).toBe('BACKFILL')
  expect(scan.report_id).toBe(REPORT_ID)

  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Scan history')
  const table = page.getByRole('region', { name: 'Scans table' })
  await expect(table).toBeVisible()
  const row = table.locator('tbody tr').filter({ hasText: SCAN_ID as string })
  await expect(row).toContainText(SYMBOL)
  await expect(row).toContainText('4h')
  await expect(row).toContainText('DONE')
  await expect(row.locator('a', { hasText: SCAN_ID as string })).toHaveAttribute(
    'href',
    `/scans/${SCAN_ID as string}`,
  )
  await expect(row.locator('a', { hasText: REPORT_ID as string })).toHaveAttribute(
    'href',
    `/reports/${REPORT_ID as string}`,
  )
  expectClean(diagnostics)
})

test('/scans/:id shows the DATA -> METRICS -> REPORT metrics-only detail', async ({ page }) => {
  expect(SCAN_ID, 'OFT_E2E_REPORT_SCAN_ID is required').toBeTruthy()
  const diagnostics = collectDiagnostics(page)
  await authenticate(page)
  await page.goto(`/scans/${encodeURIComponent(SCAN_ID as string)}`)

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(SCAN_ID as string)
  await expect(page.getByText(`${SYMBOL} · 4h · trigger MANUAL · source BACKFILL`)).toBeVisible()

  const grid = page.locator('.scan-grid')
  await expect(grid).toContainText('DONE')
  await expect(grid).toContainText(REPORT_ID as string)

  const stages = page.getByRole('list', { name: 'Named scan stages' })
  for (const stage of ['DATA', 'METRICS', 'REPORT']) {
    const item = stages.getByRole('listitem').filter({ hasText: stage })
    await expect(item).toContainText('DONE')
  }
  await expect(page.getByText(/No LLM stage exists in this metrics-only slice/i)).toBeVisible()
  await expect(page.getByRole('link', { name: REPORT_ID as string })).toHaveAttribute(
    'href',
    `/reports/${REPORT_ID as string}`,
  )
  expectClean(diagnostics)
})

test('is read-only, keyboard operable, and overflow-free at 1440, 768, and 375', async ({
  page,
}) => {
  expect(REPORT_ID, 'OFT_E2E_REPORT_ID is required').toBeTruthy()
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  const viewports = [
    { width: 1440, height: 900, name: 'report-1440.png' },
    { width: 768, height: 1024, name: 'report-768.png' },
    { width: 375, height: 812, name: 'report-375.png' },
  ] as const

  for (const viewport of viewports) {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await authenticate(page)
    await page.goto(reportPath(REPORT_ID as string))
    await expect(page.getByRole('figure', { name: /BTCUSDT 4h chart/i })).toBeVisible()
    await expectChartLabelsReadable(page)

    if (viewport.width <= 768) {
      await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'true')
      await expect(page.getByText(/Read-only at this width/)).toBeVisible()
    }
    if (viewport.width === 375) {
      expect(
        await page.locator('main input, main select, main textarea, main form').count(),
        'read-only at 375: no inputs or forms',
      ).toBe(0)
      const target = await page.locator('.chart-bars button').first().boundingBox()
      expect(target?.height ?? 0).toBeGreaterThanOrEqual(44)
    }

    // Keyboard navigation keeps working at every width, with focus following
    // the selection across consecutive key presses.
    const pressed = page.locator('.chart-bars button[aria-pressed="true"]')
    await expect(pressed).toHaveCount(1)
    await pressed.focus()
    for (let step = 0; step < 2; step += 1) {
      const before = (await pressed.textContent()) ?? ''
      await page.keyboard.press('ArrowLeft')
      const after = page.locator('.chart-bars button[aria-pressed="true"]')
      await expect(after).toHaveCount(1)
      await expect(after).not.toHaveText(before)
      await expect(after).toBeFocused()
    }

    // Baseline screenshot with the chart, before the tables expand.
    await page.screenshot({ path: path.join(SCREENS_DIR, viewport.name), fullPage: true })

    // Open both data tables at every width; the page itself must not scroll
    // horizontally and table text must not wrap per character.
    await openDataTables(page)
    const calcTable = page.getByRole('region', { name: 'CALC metrics table' }).locator('table')
    await expectNoPerCharacterWrap(page, calcTable)
    await expectNoPerCharacterWrap(page, page.getByRole('table', { name: 'Chart data table' }))
    await expectNoPerCharacterWrap(
      page,
      page.getByRole('table', { name: 'Volume profile data table' }),
    )
    await expectChartLabelsReadable(page)

    const widths = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(widths.document, `document overflow at ${viewport.width}`).toBeLessThanOrEqual(
      widths.viewport,
    )
    expect(widths.body, `body overflow at ${viewport.width}`).toBeLessThanOrEqual(widths.viewport)

    await page.screenshot({
      path: path.join(SCREENS_DIR, viewport.name.replace('.png', '-tables.png')),
      fullPage: true,
    })
    expectClean(diagnostics)
  }
})
