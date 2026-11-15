import fs from 'node:fs'
import path from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * Slice 38: watchlist star/unstar and search-add journeys.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the SYMBOLS daemon serves `web/dist` and its exchangeInfo fixture is
 *   seeded; BTCUSDT/ETHUSDT/XAUUSDT are starred before the symbols suite runs
 *   so `/watchlist` (which now renders only starred rows) still shows them;
 * - `OFT_E2E_SYMBOLS_BOOTSTRAP_FILE` mints the browser session;
 * - `OFT_E2E_SYMBOLS_API_TOKEN` is used only for server-side evidence and
 *   deterministic setup, never to fake browser auth.
 *
 * The only route interception is deliberate: one mutation is delayed to prove
 * the pending state and one is answered with a 503 envelope to prove rollback.
 * Authentication is never intercepted.
 */

const ROOT = path.resolve(process.cwd(), '..')
const BOOTSTRAP_FILE = process.env.OFT_E2E_SYMBOLS_BOOTSTRAP_FILE
const SCREENS_DIR =
  process.env.OFT_E2E_WATCHLIST_SCREENS_DIR ??
  path.resolve(ROOT, '.claude', 'ecc-workflow', 'screens', 'slice-38-e2e')

const MUTATION_ROUTE = '**/api/v1/watchlist/*'
const FIXTURE_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'XAUUSDT'] as const

async function authenticate(page: Page): Promise<void> {
  if (!BOOTSTRAP_FILE) {
    throw new Error('OFT_E2E_SYMBOLS_BOOTSTRAP_FILE is required for the watchlist journey')
  }
  const secret = fs.readFileSync(BOOTSTRAP_FILE, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

function apiHeaders(): Record<string, string> {
  const token = process.env.OFT_E2E_SYMBOLS_API_TOKEN
  expect(token, 'OFT_E2E_SYMBOLS_API_TOKEN is required for server evidence').toBeTruthy()
  return { Authorization: `Bearer ${token ?? ''}` }
}

async function seedStar(
  request: APIRequestContext,
  symbol: string,
  starred: boolean,
): Promise<void> {
  const response = await request.patch(`/api/v1/symbols/${symbol}/star`, {
    headers: { ...apiHeaders(), 'Content-Type': 'application/json' },
    data: { starred },
  })
  expect(response.ok(), `seeding ${symbol} starred=${starred}`).toBeTruthy()
}

async function resetStars(request: APIRequestContext): Promise<void> {
  for (const symbol of FIXTURE_SYMBOLS) {
    await seedStar(request, symbol, true)
  }
}

interface WatchlistRowBody {
  symbol: string
  starred: boolean
  starred_at: string | null
}

interface WatchlistBody {
  ok: boolean
  data: {
    rows: WatchlistRowBody[]
    starred_count: number
    next_scan_count: number
    star_mutation: boolean
  } | null
  error: { code: string } | null
}

async function fetchWatchlistBody(request: APIRequestContext): Promise<WatchlistBody> {
  const response = await request.get('/api/v1/watchlist', { headers: apiHeaders() })
  expect(response.status()).toBe(200)
  return (await response.json()) as WatchlistBody
}

async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const widths = await page.evaluate(() => {
    const viewport = window.innerWidth
    return {
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      viewport,
    }
  })
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
  expect(widths.body).toBeLessThanOrEqual(widths.viewport)
}

test.describe('slice 38 watchlist star journeys', () => {
  test('desktop toggle shows a pending state and persists across reload', async ({
    page,
    request,
  }) => {
    await resetStars(request)
    await authenticate(page)

    // Delay the real mutation so the pending state is observable; the request
    // is still answered by the daemon (`continue`, never a fake success).
    await page.route(MUTATION_ROUTE, async (route) => {
      if (['POST', 'DELETE'].includes(route.request().method())) {
        await new Promise((resolve) => setTimeout(resolve, 1500))
      }
      await route.continue()
    })

    await page.goto('/watchlist')
    await expect(page.getByRole('heading', { name: 'CRYPTO (2)' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'TRADFI (1)' })).toBeVisible()

    const toggle = page.getByRole('button', { name: 'ETHUSDT in next scan' })
    await expect(toggle).toHaveAttribute('aria-pressed', 'true')
    await toggle.click()

    await expect(toggle).toHaveAttribute('aria-busy', 'true')
    // aria-disabled (not native disabled) keeps focus on the control.
    await expect(toggle).toHaveAttribute('aria-disabled', 'true')
    await expect(page.getByTestId('watchlist-live')).toContainText(
      'ETHUSDT removed from next scan.',
      { timeout: 15_000 },
    )
    await expect(page.getByRole('heading', { name: 'CRYPTO (1)' })).toBeVisible()

    await page.unroute(MUTATION_ROUTE)
    await page.reload()
    await expect(page.getByRole('heading', { name: 'CRYPTO (1)' })).toBeVisible()
    await expect(
      page.getByRole('button', { name: 'ETHUSDT in next scan' }),
    ).toHaveCount(0)

    const body = await fetchWatchlistBody(request)
    const eth = body.data?.rows.find((row) => row.symbol === 'ETHUSDT')
    expect(eth?.starred).toBe(false)
    expect(body.data?.next_scan_count).toBe(2)

    await seedStar(request, 'ETHUSDT', true)
  })

  test('a refused mutation rolls back visibly with an alert', async ({ page, request }) => {
    await resetStars(request)
    await authenticate(page)

    let refused = false
    await page.route(MUTATION_ROUTE, async (route) => {
      if (!refused && ['POST', 'DELETE'].includes(route.request().method())) {
        refused = true
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: false,
            data: null,
            error: {
              code: 'DATA_UNAVAILABLE',
              message: 'injected watchlist failure',
              category: 'DATA',
              http_status: 503,
              retryable: true,
              reference_id: 'REQ-E2E',
              details: [],
            },
            meta: {},
          }),
        })
        return
      }
      await route.continue()
    })

    await page.goto('/watchlist')
    const toggle = page.getByRole('button', { name: 'BTCUSDT in next scan' })
    await toggle.click()

    await expect(page.getByRole('alert')).toContainText('Could not remove BTCUSDT')
    await expect(page.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    // A definitive failure is announced once, by the visible alert; the polite
    // region must not duplicate the rollback sentence.
    await expect(page.getByTestId('watchlist-live')).not.toContainText('was not removed')
    await page.unroute(MUTATION_ROUTE)

    const body = await fetchWatchlistBody(request)
    expect(body.data?.rows.find((row) => row.symbol === 'BTCUSDT')?.starred).toBe(true)
  })

  test('search adds a symbol and the server records the star', async ({ page, request }) => {
    await resetStars(request)
    await seedStar(request, 'ETHUSDT', false)
    await authenticate(page)

    await page.goto('/watchlist/search')
    await expect(page.getByRole('heading', { level: 1, name: 'Watchlist' })).toBeVisible()
    const add = page.getByRole('button', { name: 'ETHUSDT in next scan' })
    await expect(add).toHaveAttribute('aria-pressed', 'false')
    await add.click()

    await expect(page.getByTestId('search-live')).toContainText(
      'ETHUSDT added to next scan.',
      { timeout: 15_000 },
    )
    await expect(page.getByRole('button', { name: 'ETHUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    const body = await fetchWatchlistBody(request)
    expect(body.data?.star_mutation).toBe(true)
    expect(body.data?.rows.find((row) => row.symbol === 'ETHUSDT')?.starred).toBe(true)

    await seedStar(request, 'ETHUSDT', true)
  })

  test('a populated watchlist links to the symbol search surface', async ({ page, request }) => {
    await resetStars(request)
    await authenticate(page)
    await page.goto('/watchlist')

    // Rows are present, so this is the non-empty state the review flagged.
    await expect(page.getByRole('button', { name: 'BTCUSDT in next scan' })).toBeVisible()
    const searchLink = page.getByRole('link', { name: 'Search symbols' })
    await expect(searchLink).toHaveAttribute('href', '/watchlist/search')
    await searchLink.click()

    await expect(page).toHaveURL(/\/watchlist\/search$/)
    await expect(
      page.locator('#app-main').getByRole('heading', { level: 1, name: 'Watchlist' }),
    ).toBeVisible()
  })

  test('keyboard Space and Enter toggle the star and announce the change', async ({
    page,
    request,
  }) => {
    await resetStars(request)
    await authenticate(page)
    await page.goto('/watchlist')

    const xau = page.getByRole('button', { name: 'XAUUSDT in next scan' })
    await xau.focus()
    await page.keyboard.press('Space')
    await expect(page.getByTestId('watchlist-live')).toContainText(
      'XAUUSDT removed from next scan.',
    )

    const btc = page.getByRole('button', { name: 'BTCUSDT in next scan' })
    await btc.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('watchlist-live')).toContainText(
      'BTCUSDT removed from next scan.',
    )

    // A success is announced; delay-free so no wait strategy is needed beyond
    // the live region, which proves both keys reached the real daemon.
    await seedStar(request, 'XAUUSDT', true)
    await seedStar(request, 'BTCUSDT', true)
  })

  test('focus moves to the next star toggle after a successful unstar', async ({
    page,
    request,
  }) => {
    await resetStars(request)
    await authenticate(page)
    await page.goto('/watchlist')

    const eth = page.getByRole('button', { name: 'ETHUSDT in next scan' })
    await eth.focus()
    await expect(eth).toBeFocused()
    await page.keyboard.press('Space')

    await expect(page.getByTestId('watchlist-live')).toContainText(
      'ETHUSDT removed from next scan.',
      { timeout: 15_000 },
    )
    // The focused button's row unmounted; focus lands on the next (TRADFI) star
    // instead of falling back to <body>.
    await expect(page.getByRole('button', { name: 'XAUUSDT in next scan' })).toBeFocused()

    await seedStar(request, 'ETHUSDT', true)
  })

  test('375 is read-only: star text, no buttons, CLI note, kill switch, no overflow', async ({
    page,
    request,
  }) => {
    await resetStars(request)
    await authenticate(page)
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/watchlist')

    const surface = page.locator('section.system-page')
    await expect(surface).toHaveAttribute('data-readonly', 'true')
    await expect(surface.getByRole('note')).toContainText('Read-only at this width')
    await expect(surface.getByRole('note')).toContainText('oft watchlist add|remove')
    await expect(page.locator('main.app-main').locator('button')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /Kill switch/ })).toBeVisible()
    await expect(page.getByRole('link', { name: 'BTCUSDT' })).toBeVisible()
    await expectNoHorizontalScroll(page)
  })

  test('axe reports no WCAG A/AA (2.2) violations at 1440, 768, and 375', async ({
    page,
    request,
  }) => {
    test.setTimeout(240_000)
    await resetStars(request)
    await authenticate(page)

    const viewports = [
      { width: 1440, height: 900 },
      { width: 768, height: 1024 },
      { width: 375, height: 812 },
    ] as const

    for (const viewport of viewports) {
      await page.setViewportSize(viewport)
      for (const pathname of ['/watchlist', '/watchlist/search']) {
        await page.goto(pathname)
        await page.waitForLoadState('networkidle')
        await expect(
          page.locator('#app-main').getByRole('heading', { level: 1, name: 'Watchlist' }),
        ).toBeVisible()
        await expect(page.getByText(/^Loading symbol/)).toHaveCount(0)
        // Scope to the route content: unrelated shell chrome is not this slice.
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
          `${pathname} @ ${viewport.width}px axe violations:\n${summary.join('\n')}`,
        ).toEqual([])
      }
    }
  })

  test('captures /watchlist and /watchlist/search at 1440, 768, and 375', async ({
    page,
    request,
  }) => {
    await resetStars(request)
    await authenticate(page)
    fs.mkdirSync(SCREENS_DIR, { recursive: true })

    const viewports = [
      { width: 1440, height: 900 },
      { width: 768, height: 1024 },
      { width: 375, height: 812 },
    ] as const
    const routes = [
      { pathname: '/watchlist', name: 'watchlist' },
      { pathname: '/watchlist/search', name: 'watchlist-search' },
    ] as const

    for (const viewport of viewports) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      for (const route of routes) {
        await page.goto(route.pathname)
        await expect(
          page.locator('#app-main').getByRole('heading', { level: 1, name: 'Watchlist' }),
        ).toBeVisible()
        await expect(page.getByText(/^Loading symbol/)).toHaveCount(0)
        await page.screenshot({
          path: path.join(SCREENS_DIR, `${route.name}-${viewport.width}.png`),
          fullPage: true,
        })
      }
    }
  })
})
