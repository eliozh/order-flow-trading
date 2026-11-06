import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Locator, type Page } from '@playwright/test'

/**
 * Critical journeys for slice 02: browsing the read-only symbol registry and
 * the symbol inspector.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the daemon serves `web/dist` on OFT_E2E_SYMBOLS_BASE_URL (port 7632) with a
 *   temporary data dir and the committed exchangeInfo fixture seeded;
 * - OFT_E2E_SYMBOLS_API_TOKEN is the daemon's bearer token, used only for
 *   server-side API evidence (the browser has no auth flow yet).
 *
 * The browser journeys are intentionally unauthenticated: if the built SPA
 * receives 401 from `/api/v1/symbols`, these tests fail with the captured
 * response as evidence. Authentication is never faked with route interception.
 */

const SCREENS_DIR =
  process.env.OFT_E2E_SYMBOLS_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e02')

const EXPECTED_CRYPTO = 2
const EXPECTED_TRADFI = 1

interface ObservedSymbolResponse {
  url: string
  status: number
  errorCode: string | null
}

interface BrowserDiagnostics {
  responses: ObservedSymbolResponse[]
  consoleErrors: string[]
  pageErrors: string[]
  requestFailures: string[]
}

interface SymbolContractJson {
  symbol: string
  contract_type: string
  tick_size: string
  step_size: string
  min_qty: string
  min_notional: string
  price_precision: number
  quantity_precision: number
  listed_date: string
  tradifi: boolean
}

interface SymbolListBody {
  ok: boolean
  data: { items: SymbolContractJson[]; total: number } | null
  error: { code: string } | null
}

interface SymbolDetailBody {
  ok: boolean
  data: {
    contract: SymbolContractJson
    coverage: { status: string; book_ticker: string; liquidation_history: string }
  } | null
  error: { code: string } | null
}

function collectDiagnostics(page: Page): BrowserDiagnostics {
  const diagnostics: BrowserDiagnostics = {
    responses: [],
    consoleErrors: [],
    pageErrors: [],
    requestFailures: [],
  }

  page.on('response', (response) => {
    if (!response.url().includes('/api/v1/symbols')) {
      return
    }
    const entry: ObservedSymbolResponse = {
      url: response.url(),
      status: response.status(),
      errorCode: null,
    }
    diagnostics.responses.push(entry)
    response
      .json()
      .then((body: unknown) => {
        const envelope = body as { error?: { code?: string } | null }
        entry.errorCode = envelope.error?.code ?? null
      })
      .catch(() => undefined)
  })
  page.on('console', (message) => {
    if (message.type() === 'error') {
      diagnostics.consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => {
    diagnostics.pageErrors.push(error.message)
  })
  page.on('requestfailed', (request) => {
    if (request.url().includes('/api/v1/symbols')) {
      diagnostics.requestFailures.push(
        `${request.url()} :: ${request.failure()?.errorText ?? 'unknown failure'}`,
      )
    }
  })

  return diagnostics
}

async function gotoWithSymbolsResponse(
  page: Page,
  pathname: string,
): Promise<ObservedSymbolResponse> {
  const responsePromise = page.waitForResponse((response) =>
    response.url().includes('/api/v1/symbols'),
  )
  await page.goto(pathname)
  const response = await responsePromise
  let errorCode: string | null = null
  try {
    const body = (await response.json()) as { error?: { code?: string } | null }
    errorCode = body.error?.code ?? null
  } catch {
    errorCode = null
  }
  return { url: response.url(), status: response.status(), errorCode }
}

/** Wait until the route has rendered its loaded or stable error state. */
async function waitForSettled(page: Page): Promise<void> {
  await expect(page.locator('h1, [role="alert"]').first()).toBeVisible()
  await expect(page.getByText(/^Loading symbol/)).toHaveCount(0)
}

function scanValue(scope: Locator, label: string): Locator {
  return scope.locator('.scan-grid > div').filter({ hasText: label }).locator('dd')
}

async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const widths = await page.evaluate(() => {
    const viewport = window.innerWidth
    const offenders: string[] = []
    for (const element of document.querySelectorAll('*')) {
      const rect = element.getBoundingClientRect()
      if (rect.right > viewport + 0.5 || rect.left < -0.5) {
        const label = `${element.tagName.toLowerCase()}${
          element.className ? `.${String(element.className).split(' ').join('.')}` : ''
        }`
        offenders.push(
          `${label} [${Math.round(rect.left)}..${Math.round(rect.right)}] "${(
            element.textContent ?? ''
          )
            .trim()
            .slice(0, 60)}"`,
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
  const context = `viewport ${widths.viewport}; overflowing elements: ${
    widths.offenders.join(' | ') || 'none'
  }`
  expect(
    widths.document,
    `document scrollWidth ${widths.document} > ${widths.viewport} (${context})`,
  ).toBeLessThanOrEqual(widths.viewport)
  expect(
    widths.body,
    `body scrollWidth ${widths.body} > ${widths.viewport} (${context})`,
  ).toBeLessThanOrEqual(widths.viewport)
}

test.describe('slice 02 symbol registry journeys', () => {
  test('journey 1: /watchlist lists crypto and TradFi symbols from the fixture', async ({
    page,
  }, testInfo) => {
    const diagnostics = collectDiagnostics(page)
    try {
      const observed = await gotoWithSymbolsResponse(page, '/watchlist')
      await waitForSettled(page)
      await testInfo.attach('symbols-request.json', {
        body: JSON.stringify(observed, null, 2),
        contentType: 'application/json',
      })

      expect(
        observed.status,
        `GET ${observed.url} returned HTTP ${observed.status} (error code: ${
          observed.errorCode ?? 'none'
        })`,
      ).toBe(200)

      await expect(page.getByRole('heading', { name: `CRYPTO (${EXPECTED_CRYPTO})` })).toBeVisible()
      await expect(page.getByRole('heading', { name: `TRADFI (${EXPECTED_TRADFI})` })).toBeVisible()
      await expect(page.getByRole('link', { name: 'BTCUSDT' })).toBeVisible()
      await expect(page.getByRole('link', { name: 'ETHUSDT' })).toBeVisible()
      await expect(page.getByRole('link', { name: 'XAUUSDT' })).toBeVisible()
    } finally {
      await testInfo.attach('browser-diagnostics.json', {
        body: JSON.stringify(diagnostics, null, 2),
        contentType: 'application/json',
      })
    }
  })

  test('journey 2: search finds BTCUSDT by exact code and substring without console errors', async ({
    page,
  }, testInfo) => {
    const diagnostics = collectDiagnostics(page)
    try {
      const observed = await gotoWithSymbolsResponse(page, '/watchlist')
      await waitForSettled(page)
      await testInfo.attach('symbols-request.json', {
        body: JSON.stringify(observed, null, 2),
        contentType: 'application/json',
      })

      expect(
        observed.status,
        `GET ${observed.url} returned HTTP ${observed.status} (error code: ${
          observed.errorCode ?? 'none'
        })`,
      ).toBe(200)
      expect(diagnostics.pageErrors, 'uncaught page errors').toEqual([])
      expect(diagnostics.consoleErrors, 'console errors').toEqual([])

      const search = page.getByLabel('Search symbols')
      const cryptoRows = page.locator('section[aria-labelledby="group-crypto"] tbody tr')
      const allRows = page.locator('tbody tr')

      // Exact code.
      await search.fill('BTCUSDT')
      await expect(cryptoRows).toHaveCount(1)
      await expect(cryptoRows.first()).toContainText('BTCUSDT')

      // Substring (case-insensitive).
      await search.fill('btc')
      await expect(cryptoRows).toHaveCount(1)
      await expect(cryptoRows.first()).toContainText('BTCUSDT')

      // Substring matching every symbol.
      await search.fill('USDT')
      await expect(allRows).toHaveCount(EXPECTED_CRYPTO + EXPECTED_TRADFI)

      // Substring matching the TradFi base asset.
      await search.fill('xau')
      await expect(allRows).toHaveCount(1)
      await expect(allRows.first()).toContainText('XAUUSDT')

      // No-match state stays stable.
      await search.fill('ZZZZ')
      await expect(page.getByText(/No matches for/)).toBeVisible()
    } finally {
      await testInfo.attach('browser-diagnostics.json', {
        body: JSON.stringify(diagnostics, null, 2),
        contentType: 'application/json',
      })
    }
  })

  test('journey 3: /watchlist/BTCUSDT renders contract specs, listing date, and coverage placeholders', async ({
    page,
  }, testInfo) => {
    const diagnostics = collectDiagnostics(page)
    try {
      const observed = await gotoWithSymbolsResponse(page, '/watchlist/BTCUSDT')
      await waitForSettled(page)
      await testInfo.attach('symbols-request.json', {
        body: JSON.stringify(observed, null, 2),
        contentType: 'application/json',
      })

      expect(
        observed.status,
        `GET ${observed.url} returned HTTP ${observed.status} (error code: ${
          observed.errorCode ?? 'none'
        })`,
      ).toBe(200)

      await expect(page.getByRole('heading', { name: 'Symbol BTCUSDT', level: 1 })).toBeVisible()

      const contract = page.locator('section[aria-labelledby="contract-heading"]')
      await expect(scanValue(contract, 'Contract type')).toHaveText('PERPETUAL')
      await expect(scanValue(contract, 'Underlying')).toHaveText('COIN')
      await expect(scanValue(contract, 'Tick size')).toHaveText('0.10')
      await expect(scanValue(contract, 'Step size')).toHaveText('0.001')
      await expect(scanValue(contract, 'Min qty')).toHaveText('0.001')
      await expect(scanValue(contract, 'Min notional')).toHaveText('5.00')
      await expect(scanValue(contract, 'Price precision')).toHaveText('2')
      await expect(scanValue(contract, 'Quantity precision')).toHaveText('3')
      await expect(scanValue(contract, 'Listing date')).toHaveText('2019-09-08')

      const coverage = page.locator('section[aria-labelledby="coverage-heading"]')
      await expect(coverage).toContainText('placeholders')
      await expect(scanValue(coverage, 'Coverage status')).toHaveText('UNKNOWN')
      await expect(scanValue(coverage, 'Coverage %')).toHaveText('—')
      await expect(scanValue(coverage, 'bookTicker')).toHaveText('N/A')
      await expect(scanValue(coverage, 'Liquidation history')).toHaveText('SINCE_DEPLOYMENT')
    } finally {
      await testInfo.attach('browser-diagnostics.json', {
        body: JSON.stringify(diagnostics, null, 2),
        contentType: 'application/json',
      })
    }
  })

  test('journey 4a: at 375px the search surface is read-only with no horizontal scroll', async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 375, height: 812 })
    const observed = await gotoWithSymbolsResponse(page, '/watchlist')
    await waitForSettled(page)
    await testInfo.attach('symbols-request.json', {
      body: JSON.stringify(observed, null, 2),
      contentType: 'application/json',
    })

    // Prove the registry actually loaded before checking layout: the old
    // assertion passed against the 401 error state and hid the table overflow.
    expect(
      observed.status,
      `GET ${observed.url} returned HTTP ${observed.status} (error code: ${
        observed.errorCode ?? 'none'
      })`,
    ).toBe(200)
    await expect(page.getByRole('heading', { name: `CRYPTO (${EXPECTED_CRYPTO})` })).toBeVisible()
    await expect(page.getByRole('link', { name: 'BTCUSDT' })).toBeVisible()

    const surface = page.locator('section.system-page')
    await expect(surface).toHaveAttribute('data-readonly', 'true')
    await expect(page.getByRole('note')).toContainText('Read-only at this width')

    // The search input is present, but no mutation controls are.
    await expect(page.getByLabel('Search symbols')).toBeVisible()
    const main = page.locator('main.app-main')
    for (const selector of ['button', 'select', 'textarea', 'form']) {
      await expect(main.locator(selector)).toHaveCount(0)
    }
    await expect(page.locator('[contenteditable="true"]')).toHaveCount(0)

    await expectNoHorizontalScroll(page)
  })

  test('journey 4b: at 375px the inspector is read-only with no horizontal scroll', async ({
    page,
  }, testInfo) => {
    const diagnostics = collectDiagnostics(page)
    try {
      await page.setViewportSize({ width: 375, height: 812 })
      const observed = await gotoWithSymbolsResponse(page, '/watchlist/BTCUSDT')
      await waitForSettled(page)
      await testInfo.attach('symbols-request.json', {
        body: JSON.stringify(observed, null, 2),
        contentType: 'application/json',
      })

      expect(
        observed.status,
        `GET ${observed.url} returned HTTP ${observed.status} (error code: ${
          observed.errorCode ?? 'none'
        })`,
      ).toBe(200)

      const surface = page.locator('section.system-page')
      await expect(surface).toHaveAttribute('data-readonly', 'true')
      await expect(page.getByRole('note')).toContainText('Read-only at this width')

      const main = page.locator('main.app-main')
      for (const selector of ['button', 'select', 'textarea', 'form']) {
        await expect(main.locator(selector)).toHaveCount(0)
      }

      await expectNoHorizontalScroll(page)
    } finally {
      await testInfo.attach('browser-diagnostics.json', {
        body: JSON.stringify(diagnostics, null, 2),
        contentType: 'application/json',
      })
    }
  })

  test('journey 5: an unknown symbol shows a stable NOT_FOUND error state', async ({
    page,
  }, testInfo) => {
    const diagnostics = collectDiagnostics(page)
    try {
      const observed = await gotoWithSymbolsResponse(page, '/watchlist/NOPEUSDT')
      await waitForSettled(page)

      const alert = page.getByRole('alert')
      await expect(alert).toBeVisible()
      const firstText = (await alert.textContent()) ?? ''
      expect(firstText.length, 'the error state must render a message').toBeGreaterThan(0)

      // The error state is stable across reloads.
      await page.reload()
      await expect(page.getByRole('alert')).toHaveText(firstText)

      await testInfo.attach('symbols-request.json', {
        body: JSON.stringify(observed, null, 2),
        contentType: 'application/json',
      })
      expect(
        observed.status,
        `GET ${observed.url} returned HTTP ${observed.status} (error code: ${
          observed.errorCode ?? 'none'
        })`,
      ).toBe(404)
      expect(observed.errorCode).toBe('NOT_FOUND')
      await expect(page.getByRole('alert')).toContainText('does not exist')
    } finally {
      await testInfo.attach('browser-diagnostics.json', {
        body: JSON.stringify(diagnostics, null, 2),
        contentType: 'application/json',
      })
    }
  })

  test('evidence (server-side bearer, not browser auth): registry specs and NOT_FOUND', async ({
    request,
  }) => {
    const token = process.env.OFT_E2E_SYMBOLS_API_TOKEN
    test.skip(!token, 'OFT_E2E_SYMBOLS_API_TOKEN is not set')

    const headers = { Authorization: `Bearer ${token}` }
    const list = await request.get('/api/v1/symbols', { headers })
    expect(list.status()).toBe(200)
    const listBody = (await list.json()) as SymbolListBody
    expect(listBody.ok).toBe(true)
    const items = listBody.data?.items ?? []
    expect(items.map((item) => item.symbol)).toEqual(
      expect.arrayContaining(['BTCUSDT', 'ETHUSDT', 'XAUUSDT']),
    )

    const btc = items.find((item) => item.symbol === 'BTCUSDT')
    expect(btc).toMatchObject({
      contract_type: 'PERPETUAL',
      tick_size: '0.10',
      step_size: '0.001',
      min_notional: '5.00',
      price_precision: 2,
      quantity_precision: 3,
      listed_date: '2019-09-08',
      tradifi: false,
    })

    const xau = items.find((item) => item.symbol === 'XAUUSDT')
    expect(xau).toMatchObject({
      contract_type: 'TRADIFI_PERPETUAL',
      listed_date: '2025-12-11',
      tradifi: true,
    })

    const detail = await request.get('/api/v1/symbols/BTCUSDT', { headers })
    expect(detail.status()).toBe(200)
    const detailBody = (await detail.json()) as SymbolDetailBody
    expect(detailBody.data?.contract.tick_size).toBe('0.10')
    expect(detailBody.data?.coverage.status).toBe('UNKNOWN')

    const unknown = await request.get('/api/v1/symbols/NOPEUSDT', { headers })
    expect(unknown.status()).toBe(404)
    const unknownBody = (await unknown.json()) as SymbolDetailBody
    expect(unknownBody.error?.code).toBe('NOT_FOUND')
  })

  test('captures /watchlist and /watchlist/BTCUSDT at 1440, 768, and 375', async ({ page }) => {
    fs.mkdirSync(SCREENS_DIR, { recursive: true })

    const viewports = [
      { width: 1440, height: 900 },
      { width: 768, height: 1024 },
      { width: 375, height: 812 },
    ] as const
    const routes = [
      { pathname: '/watchlist', name: 'watchlist' },
      { pathname: '/watchlist/BTCUSDT', name: 'inspector-btcusdt' },
    ] as const

    for (const viewport of viewports) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      for (const route of routes) {
        await page.goto(route.pathname)
        await waitForSettled(page)
        await page.screenshot({
          path: path.join(SCREENS_DIR, `${route.name}-${viewport.width}.png`),
          fullPage: true,
        })
      }
    }
  })
})
