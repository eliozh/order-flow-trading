import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Locator, type Page } from '@playwright/test'

/**
 * Critical browser journey for slice 30: the `/journal` list (stats strip,
 * definitions, exclusions, filters, OPEN rows, server export) and
 * `/journal/:tradeId` (timeline, snapshot, decision, fills, comparison, and
 * the advisory review band with a real CSRF + Idempotency-Key generate/retry).
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the reports daemon serves `web/dist` on OFT_E2E_REPORT_BASE_URL with a
 *   temporary data dir, the archive fixture dir, and the stub LLM provider;
 * - OFT_E2E_JOURNAL_TRADE_ID names one CLOSED trade seeded through the real
 *   scan -> trigger -> recheck -> accept -> stop-mark -> monitor path, marked
 *   COMPLETE, and OFT_E2E_POSITION_ID still names the OPEN position from the
 *   positions journey (so the list shows one OPEN row excluded from stats);
 * - OFT_E2E_REPORT_BOOTSTRAP_FILE mints the browser session.
 *
 * The journey mutates exactly one thing: the advisory review of the seeded
 * trade. No exchange, live credential, or rule is touched.
 */

const TRADE_ID = process.env.OFT_E2E_JOURNAL_TRADE_ID
const OPEN_POSITION_ID = process.env.OFT_E2E_POSITION_ID
const REPORTS_TOKEN = process.env.OFT_E2E_REPORT_API_TOKEN
const REPORT_ORIGIN = new URL(process.env.OFT_E2E_REPORT_BASE_URL ?? 'http://127.0.0.1:7642').origin
const SCREENS_DIR =
  process.env.OFT_E2E_JOURNAL_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'slice-30')
const STABLE_REVIEW_KEY = 'e2e-journal-review-key'
const REVIEW_KEY_PREFIX = 'oft.journal.review.'

interface Envelope<T> {
  ok: boolean
  data: T | null
  error?: { code?: string } | null
  meta?: Record<string, unknown>
}

interface StatsData {
  status: string
  sample_size: number
  exclusions: Record<string, number>
  period: { label: string } | null
  definitions: Record<string, string>
}

interface ListItem {
  trade_id: string
  symbol: string
  side: string
  net_pnl_usd: string
}

interface ListData {
  items: ListItem[]
  total: number
}

interface TradeData {
  trade_id: string
  symbol: string
  side: string
  net_pnl_usd: string
  review_decision_id: string | null
}

interface ReviewData {
  status: string
  decision_id: string | null
  advisory: boolean
  confidence: number | null
  validation_status: string | null
  rationale: string | null
  caveats: string[]
  idempotent_replay: boolean
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

/** Mint a browser session from the reports daemon's one-time bootstrap file. */
async function authenticate(page: Page): Promise<void> {
  const file = process.env.OFT_E2E_REPORT_BOOTSTRAP_FILE
  if (!file) {
    throw new Error('OFT_E2E_REPORT_BOOTSTRAP_FILE is required for the journal journey')
  }
  const secret = fs.readFileSync(file, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

async function apiGet<T>(page: Page, pathname: string): Promise<T> {
  const response = await page.request.get(pathname, {
    headers: { Authorization: `Bearer ${REPORTS_TOKEN}` },
  })
  expect(response.status(), pathname).toBe(200)
  const body = (await response.json()) as Envelope<T>
  expect(body.ok, pathname).toBe(true)
  expect(body.data, pathname).not.toBeNull()
  return body.data as T
}

async function csrfToken(page: Page): Promise<string> {
  const cookies = await page.context().cookies()
  const cookie = cookies.find((item) => item.name === 'oft_csrf')
  expect(cookie?.value, 'the browser session must carry the CSRF cookie').toBeTruthy()
  return cookie?.value as string
}

function statsRequests(page: Page): URL[] {
  const urls: URL[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/v1/journal/stats') {
      urls.push(url)
    }
  })
  return urls
}

function periodStat(page: Page): Locator {
  return page.getByTestId('journal-stats').locator('.stat').filter({ hasText: 'Period' })
}

async function expectNoHorizontalOverflow(page: Page, label: string): Promise<void> {
  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.document, `document overflow at ${label}`).toBeLessThanOrEqual(widths.viewport)
}

test.beforeAll(async ({ request }) => {
  expect(TRADE_ID, 'run-e2e.sh must export OFT_E2E_JOURNAL_TRADE_ID').toBeTruthy()
  expect(OPEN_POSITION_ID, 'run-e2e.sh must export OFT_E2E_POSITION_ID').toBeTruthy()
  expect(REPORTS_TOKEN, 'run-e2e.sh must export OFT_E2E_REPORT_API_TOKEN').toBeTruthy()
  const headers = { Authorization: `Bearer ${REPORTS_TOKEN}` }
  const detail = await request.get(`/api/v1/journal/${TRADE_ID}`, { headers })
  expect(detail.status()).toBe(200)
  const trade = (await detail.json()) as Envelope<TradeData>
  expect(trade.ok).toBe(true)
  expect(trade.data?.trade_id).toBe(TRADE_ID)
  const stats = await request.get('/api/v1/journal/stats?period=8w', { headers })
  expect(stats.status()).toBe(200)
  const statsBody = (await stats.json()) as Envelope<StatsData>
  expect(statsBody.data?.status).toBe('DEFINED')
  expect(statsBody.data?.sample_size).toBeGreaterThanOrEqual(1)
})

test('lists the closed trade with stats, definitions, filters, OPEN rows, and a server export', async ({
  page,
}) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  const stats = statsRequests(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/journal')

  await expect(page.getByRole('heading', { name: '交易日志与复盘', level: 1 })).toBeVisible()
  await expect(page.locator('section.system-page')).toHaveAttribute('data-readonly', 'false')

  // Stats strip with every metric, the definitions, and the exclusion counts.
  const strip = page.getByTestId('journal-stats')
  await expect(strip).toBeVisible()
  for (const label of ['Net PnL', 'Win rate', 'Expectancy', 'Avg R', 'Sample', 'Period']) {
    await expect(strip.getByText(label, { exact: true })).toBeVisible()
  }
  await expect(strip.getByText('closed', { exact: false }).first()).toBeVisible()
  await expect(page.getByTestId('journal-definitions')).toBeVisible()
  await expect(page.getByTestId('journal-definitions')).toContainText('win_rate')
  const exclusions = page.getByTestId('journal-exclusions')
  await expect(exclusions).toBeVisible()
  await expect(exclusions).toContainText('Open excluded')
  await expect(exclusions).toContainText('PARTIAL excluded')

  // The closed trade row and the OPEN row excluded from stats.
  const closedRow = page.getByRole('row').filter({ hasText: TRADE_ID as string })
  await expect(closedRow).toHaveCount(1)
  await expect(closedRow).toContainText('BTCUSDT')
  const openRow = page.getByTestId(`journal-open-row-${OPEN_POSITION_ID as string}`)
  await expect(openRow).toBeVisible()
  await expect(openRow).toContainText('OPEN excluded')

  // The export is produced by the server, not the client.
  const csvResponse = await page.request.get('/api/v1/journal/export/file?format=csv', {
    headers: { Authorization: `Bearer ${REPORTS_TOKEN}` },
  })
  expect(csvResponse.status()).toBe(200)
  expect(csvResponse.headers()['content-type']).toContain('text/csv')
  expect(csvResponse.headers()['content-disposition']).toContain('attachment')

  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 CSV' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/\.csv$/)
  const exportPath = path.join(SCREENS_DIR, 'journal-export.csv')
  await download.saveAs(exportPath)
  const exported = fs.readFileSync(exportPath, 'utf8')
  expect(exported.split('\n')[0]).toMatch(/^trade_id,position_id/)
  // The seeded formula tag was neutralized server-side before the file existed.
  expect(exported).toContain("'=SUM(1,1)")
  expect(exported).not.toContain(',=SUM(1,1)')

  // Filters: side and outcome scope the already-fetched table.
  await page.getByLabel('Side').selectOption('SHORT')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(page.getByText('没有匹配筛选条件的已平仓交易')).toBeVisible()

  await page.getByLabel('Side').selectOption('LONG')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(closedRow).toBeVisible()

  const list = await apiGet<ListData>(page, '/api/v1/journal')
  expect(list.total).toBe(1)
  const net = Number(list.items[0].net_pnl_usd)
  const expectedOutcome = net > 0 ? 'WIN' : net < 0 ? 'LOSS' : 'TIE'
  const oppositeOutcome = expectedOutcome === 'WIN' ? 'LOSS' : 'WIN'

  await page.getByLabel('Outcome').selectOption(expectedOutcome)
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(closedRow).toBeVisible()

  await page.getByLabel('Outcome').selectOption(oppositeOutcome)
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(page.getByText('没有匹配筛选条件的已平仓交易')).toBeVisible()

  // The period window scopes the stats strip AND the closed-trade table. The
  // seeded trade closed 30 days ago, so 4w excludes it while 8w/all include it.
  await page.getByRole('button', { name: '清除筛选条件' }).click()
  await expect(closedRow).toBeVisible()
  const beforePeriod = stats.length
  await page.getByLabel('Period').selectOption('4w')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect
    .poll(() => stats.slice(beforePeriod).some((url) => url.searchParams.get('period') === '4w'))
    .toBe(true)
  await expect(periodStat(page).locator('dd').first()).toHaveText('4w')
  await expect(closedRow).toHaveCount(0)
  await expect(page.getByText('没有匹配筛选条件的已平仓交易')).toBeVisible()
  await expect(openRow).toBeVisible()
  // The only closed row is out of period, not hidden by server truncation.
  const tableCaption = page.getByRole('region', { name: '交易日志表' }).locator('caption')
  await expect(tableCaption).toContainText('已平仓交易，0 条')
  await expect(tableCaption).not.toContainText('共 1 条')

  await page.getByLabel('Period').selectOption('all')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect.poll(() => stats.at(-1)?.searchParams.get('period') ?? null).toBeNull()
  await expect(periodStat(page).locator('dd').first()).toHaveText('全部')
  await expect(closedRow).toBeVisible()

  // Symbol scopes both the list and the stats sample; an empty sample is
  // undefined, never zeroed.
  await page.getByLabel('Symbol').fill('ETHUSDT')
  await page.getByRole('button', { name: '应用筛选' }).click()
  await expect(page.getByTestId('journal-metrics-undefined')).toBeVisible()
  await expect(page.getByText('0 MATCHED')).toBeVisible()
  await expect(closedRow).toHaveCount(0)

  await page.getByRole('button', { name: '清除筛选条件' }).click()
  await expect(closedRow).toBeVisible()
  await expect(openRow).toBeVisible()

  // Keyboard focus reaches the scrollable table and the global pause control.
  const region = page.getByRole('region', { name: '交易日志表' })
  await region.focus()
  await expect(region).toBeFocused()

  const statsBeforePause = stats.length
  const pause = page.getByRole('button', { name: '暂停实时更新' })
  await pause.focus()
  await expect(pause).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('statusbar-paused')).toHaveText('PAUSED')
  await expect(page.getByTestId('journal-paused-stamp')).toHaveText('PAUSED')
  await expect(page.getByTestId('journal-paused')).toBeVisible()
  await page.waitForTimeout(600)
  expect(stats.length).toBe(statsBeforePause)

  await page.getByRole('button', { name: '继续实时更新' }).click()
  await expect(page.getByTestId('statusbar-paused')).toHaveCount(0)
  await expect.poll(() => stats.length).toBeGreaterThan(statsBeforePause)

  expectClean(diagnostics)
})

/**
 * The advisory review is mutable shared state with no delete API, so these
 * tests share one daemon review and run in an explicit serial order:
 * 1. the generation test establishes the FOUND review (fail-fast: it asserts
 *    the band starts MISSING);
 * 2. the explicit-regenerate test proves a fresh key after an unknown outcome;
 * 3. the retry test replays the exact failed attempt (same key + refresh flag);
 * 4. the read-only test only reads.
 * Serial mode skips the remaining tests if one fails, so there is no accidental
 * file-order coupling.
 */
test.describe('journal detail review', () => {
  test.describe.configure({ mode: 'serial' })

  test('renders the detail timeline, comparison, and one CSRF + idempotent review generation', async ({
    page,
  }) => {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    // Seed the stable key before boot so the real POST must use this value.
    await page.addInitScript(
      ([prefix, id, key]) => {
        try {
          window.sessionStorage.setItem(`${prefix}${id}`, key)
        } catch {
          // sessionStorage unavailable: the in-memory fallback is exercised.
        }
      },
      [REVIEW_KEY_PREFIX, TRADE_ID as string, STABLE_REVIEW_KEY],
    )
    const reviewPosts: Array<{ headers: Promise<Record<string, string>>; url: URL }> = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (request.method() === 'POST' && /^\/api\/v1\/journal\/[^/]+\/review$/.test(url.pathname)) {
        reviewPosts.push({ headers: request.allHeaders(), url })
      }
    })
    await authenticate(page)
    await page.goto(`/journal/${encodeURIComponent(TRADE_ID as string)}`)

    await expect(page.getByRole('heading', { name: TRADE_ID as string, level: 1 })).toBeVisible()
    const timeline = page.getByRole('list', { name: '交易时间线' })
    await expect(timeline).toBeVisible()
    for (const stage of ['signal created', 'triggered', 'entry', 'exit']) {
      await expect(timeline).toContainText(stage)
    }
    await expect(page.getByRole('heading', { name: 'Signal snapshot' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Fills' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Signal vs Outcome 对照' })).toBeVisible()
    await expect(page.getByTestId('signal-comparison')).toContainText('Entry zone')
    await expect(page.getByRole('heading', { name: 'LLM 决策记录' })).toBeVisible()

    const band = page.getByTestId('review-band')
    await expect(band).toContainText('仅建议，不自动修改规则')
    await expect(band).toContainText('尚未生成复盘')

    await band.getByRole('button', { name: '生成复盘' }).click()
    await expect(band).toContainText('Decision')
    await expect(band).toContainText('Advisory only')

    expect(reviewPosts).toHaveLength(1)
    const post = reviewPosts[0]
    const headers = await post.headers
    expect(headers['idempotency-key']).toBe(STABLE_REVIEW_KEY)
    expect(headers['x-csrf-token']).toBeTruthy()
    expect(headers['origin']).toBe(REPORT_ORIGIN)
    expect(post.url.searchParams.get('refresh')).toBeNull()

    const review = await apiGet<ReviewData>(
      page,
      `/api/v1/journal/${encodeURIComponent(TRADE_ID as string)}/review`,
    )
    expect(review.status).toBe('FOUND')
    expect(review.advisory).toBe(true)
    expect(review.decision_id).toBeTruthy()
    await expect(band).toContainText(review.decision_id as string)

    const trade = await apiGet<TradeData>(
      page,
      `/api/v1/journal/${encodeURIComponent(TRADE_ID as string)}`,
    )
    expect(trade.review_decision_id).toBe(review.decision_id)

    // A same-origin replay under the same key returns the stored result.
    const csrf = await csrfToken(page)
    const replayResponse = await page.request.post(
      `/api/v1/journal/${encodeURIComponent(TRADE_ID as string)}/review`,
      {
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': STABLE_REVIEW_KEY,
          'X-CSRF-Token': csrf,
          Origin: REPORT_ORIGIN,
        },
        data: {},
      },
    )
    expect(replayResponse.status()).toBe(200)
    const replay = (await replayResponse.json()) as Envelope<ReviewData>
    expect(replay.ok).toBe(true)
    expect(replay.meta?.idempotent_replay).toBe(true)
    expect(replay.data?.decision_id).toBe(review.decision_id)

    expectClean(diagnostics)
  })

  test('an explicit regenerate after an unknown outcome rotates a fresh key', async ({ page }) => {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    const reviewPosts: Array<{ key: string; refresh: string | null }> = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (request.method() === 'POST' && /^\/api\/v1\/journal\/[^/]+\/review$/.test(url.pathname)) {
        reviewPosts.push({
          key: request.headers()['idempotency-key'] ?? '',
          refresh: url.searchParams.get('refresh'),
        })
      }
    })

    // Fail the first explicit refresh in-flight: the client sees an unknown
    // outcome but the server may have stored a review.
    let failedOnce = false
    await page.route(/\/api\/v1\/journal\/[^/]+\/review\?refresh=true$/, async (route) => {
      if (!failedOnce) {
        failedOnce = true
        await route.abort('failed')
        return
      }
      await route.fallback()
    })

    await authenticate(page)
    await page.goto(`/journal/${encodeURIComponent(TRADE_ID as string)}`)
    const band = page.getByTestId('review-band')
    // Precondition established by the serial generation test.
    await expect(band).toContainText('Decision')

    await band.getByRole('button', { name: '重新生成' }).click()
    const failure = band.getByTestId('journal-review-error')
    await expect(failure).toBeVisible()
    await expect(failure).toContainText('结果未知')
    expect(reviewPosts.length).toBeGreaterThanOrEqual(1)
    const staleKey = reviewPosts[0].key
    expect(staleKey).toBeTruthy()
    expect(reviewPosts[0].refresh).toBe('true')

    // A background read proves FOUND and clears the stale unknown-outcome error,
    // so the next explicit regenerate is no longer a retry of that attempt.
    await page.evaluate(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await expect(failure).toHaveCount(0)

    await band.getByRole('button', { name: '重新生成' }).click()
    await expect(band).toContainText('Decision')
    await expect.poll(() => reviewPosts.length).toBeGreaterThanOrEqual(2)
    // The explicit regenerate sends a NEW key and must never replay the stale one.
    expect(reviewPosts[1].key).not.toBe(staleKey)
    expect(reviewPosts[1].refresh).toBe('true')
    expect(reviewPosts.slice(1).some((post) => post.key === staleKey)).toBe(false)

    expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
    expect(
      diagnostics.consoleErrors.filter((message) => !/ERR_FAILED/.test(message)),
      'console errors',
    ).toEqual([])
  })

  test('a failed regeneration keeps the band and retries with the same idempotency key', async ({
    page,
  }) => {
    const diagnostics = collectDiagnostics(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    const reviewPosts: Array<{ key: string; refresh: string | null }> = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (request.method() === 'POST' && /^\/api\/v1\/journal\/[^/]+\/review$/.test(url.pathname)) {
        reviewPosts.push({
          key: request.headers()['idempotency-key'] ?? '',
          refresh: url.searchParams.get('refresh'),
        })
      }
    })

    // Fail the first refresh in-flight (an unknown outcome), then let the retry
    // reach the real daemon with the same key.
    let failedOnce = false
    await page.route(/\/api\/v1\/journal\/[^/]+\/review\?refresh=true$/, async (route) => {
      if (!failedOnce) {
        failedOnce = true
        await route.abort('failed')
        return
      }
      await route.fallback()
    })

    await authenticate(page)
    await page.goto(`/journal/${encodeURIComponent(TRADE_ID as string)}`)
    const band = page.getByTestId('review-band')
    await expect(band).toContainText('Decision')

    await band.getByRole('button', { name: '重新生成' }).click()
    const failure = band.getByTestId('journal-review-error')
    await expect(failure).toBeVisible()
    await expect(failure).toContainText('结果未知')
    // The retry control is the band's single stable action button.
    await expect(band.getByRole('button', { name: '重试生成' })).toBeVisible()

    await band.getByRole('button', { name: '重试生成' }).click()
    await expect(page.getByTestId('journal-review-error')).toHaveCount(0)
    await expect(band).toContainText('Decision')
    await expect(band).not.toContainText('复盘生成失败')

    expect(reviewPosts.length).toBeGreaterThanOrEqual(2)
    expect(reviewPosts[0].key).toBeTruthy()
    // The failed attempt was an explicit refresh; its retry replays the exact
    // request (same key and same refresh flag), never a new generation. The
    // retry-after-generate case, which sends no refresh, is covered by the
    // route unit test.
    expect(reviewPosts[1].key).toBe(reviewPosts[0].key)
    expect(reviewPosts[1].refresh).toBe(reviewPosts[0].refresh)

    // The one console error is the network failure this test deliberately
    // injects; page errors must still be empty.
    expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
    expect(
      diagnostics.consoleErrors.filter((message) => !/ERR_FAILED/.test(message)),
      'console errors',
    ).toEqual([])
  })

  test('is read-only at 375 and 768, keyboard reachable, and captures every width', async ({
    page,
  }) => {
    test.setTimeout(60_000)
    fs.mkdirSync(SCREENS_DIR, { recursive: true })
    const viewports = [
      { width: 1440, height: 900, readonly: false, name: 'journal-list-1440.png' },
      { width: 768, height: 1024, readonly: true, name: 'journal-list-768.png' },
      { width: 375, height: 812, readonly: true, name: 'journal-list-375.png' },
    ] as const

    for (const viewport of viewports) {
      const diagnostics = collectDiagnostics(page)
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      await authenticate(page)

      await page.goto('/journal')
      await expect(page.getByRole('heading', { name: '交易日志与复盘', level: 1 })).toBeVisible()
      await expect(page.locator('section.system-page')).toHaveAttribute(
        'data-readonly',
        viewport.readonly ? 'true' : 'false',
      )
      if (viewport.readonly) {
        await expect(page.getByText('移动端只读：交易日志为只读视图')).toBeVisible()
      }
      // Filters stay usable at every width; the page never scrolls sideways.
      const side = page.getByLabel('Side')
      await side.focus()
      await expect(side).toBeFocused()
      await expectNoHorizontalOverflow(page, `journal list ${viewport.width}`)
      await page.screenshot({
        path: path.join(SCREENS_DIR, viewport.name),
        fullPage: true,
      })

      await authenticate(page)
      await page.goto(`/journal/${encodeURIComponent(TRADE_ID as string)}`)
      await expect(page.getByRole('heading', { name: TRADE_ID as string, level: 1 })).toBeVisible()
      await expect(page.locator('section.system-page')).toHaveAttribute(
        'data-readonly',
        viewport.readonly ? 'true' : 'false',
      )
      const band = page.getByTestId('review-band')
      const generate = band.getByRole('button', { name: /生成复盘|重新生成/ })
      if (viewport.readonly) {
        await expect(band.getByText('移动端只读：复盘生成请在桌面端或 CLI 完成')).toBeVisible()
        // Read-only keeps the control in the tab order and explains why, instead
        // of removing it with the native disabled attribute. Playwright's
        // toBeDisabled() treats aria-disabled as disabled, so assert the native
        // DOM property (and that focus still works) rather than that matcher.
        await expect(generate).toHaveAttribute('aria-disabled', 'true')
        await expect(generate).toHaveJSProperty('disabled', false)
        await generate.focus()
        await expect(generate).toBeFocused()
      } else {
        await expect(generate).toBeEnabled()
      }
      await expectNoHorizontalOverflow(page, `journal detail ${viewport.width}`)
      await page.screenshot({
        path: path.join(SCREENS_DIR, viewport.name.replace('-list-', '-detail-')),
        fullPage: true,
      })
      expectClean(diagnostics)
    }
  })
})
