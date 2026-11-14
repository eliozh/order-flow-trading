import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

/**
 * Critical browser journey for slice 29: the positions list, position detail,
 * the global pause control, and one paper management request.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the reports daemon serves `web/dist` on OFT_E2E_REPORT_BASE_URL with a
 *   temporary data dir, the archive fixture dir, and the stub LLM provider;
 * - the valid batch scan's opportunity was driven through
 *   evaluate -> recheck -> accept, so OFT_E2E_POSITION_ID names one OPEN paper
 *   position and OFT_E2E_REPORT_API_TOKEN can read it back;
 * - OFT_E2E_REPORT_BOOTSTRAP_FILE mints the browser session.
 *
 * The journey mutates exactly one paper position through the real Paper
 * management path; it never touches an exchange or a live credential.
 */

const POSITION_ID = process.env.OFT_E2E_POSITION_ID
const REPORTS_TOKEN = process.env.OFT_E2E_REPORT_API_TOKEN
const SCREENS_DIR =
  process.env.OFT_E2E_REPORT_SCREENS_DIR ??
  path.resolve(process.cwd(), '..', '.claude', 'ecc-workflow', 'screens', 'e2e08')
const STABLE_KEY = 'e2e-stable-manage-key'
const STABLE_KEY_PREFIX = 'oft.position.manage.'

interface Envelope<T> {
  ok: boolean
  data: T | null
}

interface PositionItem {
  position_id: string
  symbol: string
  state: string
}

interface PositionListData {
  items: PositionItem[]
  total: number
}

/** Mint a browser session from the reports daemon's one-time bootstrap file. */
async function authenticate(page: Page): Promise<void> {
  const file = process.env.OFT_E2E_REPORT_BOOTSTRAP_FILE
  if (!file) {
    throw new Error('OFT_E2E_REPORT_BOOTSTRAP_FILE is required for the positions journey')
  }
  const secret = fs.readFileSync(file, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

test.beforeAll(async ({ request }) => {
  expect(POSITION_ID, 'run-e2e.sh must export OFT_E2E_POSITION_ID').toBeTruthy()
  expect(REPORTS_TOKEN, 'run-e2e.sh must export OFT_E2E_REPORT_API_TOKEN').toBeTruthy()
  // The seeded position must be readable through the real API before the UI runs.
  const response = await request.get(`/api/v1/positions/${POSITION_ID}`, {
    headers: { Authorization: `Bearer ${REPORTS_TOKEN}` },
  })
  expect(response.status()).toBe(200)
  const body = (await response.json()) as Envelope<PositionItem>
  expect(body.ok).toBe(true)
  expect(body.data?.position_id).toBe(POSITION_ID)

  const listResponse = await request.get('/api/v1/positions?state=OPEN', {
    headers: { Authorization: `Bearer ${REPORTS_TOKEN}` },
  })
  const listBody = (await listResponse.json()) as Envelope<PositionListData>
  expect(listBody.data?.items.some((item) => item.position_id === POSITION_ID)).toBe(true)
})

test('lists the seeded position, opens its detail, and requests one paper management decision', async ({
  page,
}) => {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => pageErrors.push(error.message))

  // H4: a reload mid-flight keeps the same per-position key. Seed it before the
  // app boots so the real request must use this exact value.
  await page.addInitScript(
    ([prefix, id, key]) => {
      try {
        window.sessionStorage.setItem(`${prefix}${id}`, key)
      } catch {
        // sessionStorage unavailable: the in-memory fallback is exercised.
      }
    },
    [STABLE_KEY_PREFIX, POSITION_ID as string, STABLE_KEY],
  )

  let positionPolls = 0
  const manageRequests: Array<Record<string, string>> = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (
      request.method() === 'GET' &&
      url.pathname === '/api/v1/positions' &&
      url.searchParams.get('state') === 'OPEN'
    ) {
      positionPolls += 1
    }
    if (request.method() === 'POST' && /^\/api\/v1\/positions\/[^/]+\/manage$/.test(url.pathname)) {
      manageRequests.push(request.headers())
    }
  })

  await authenticate(page)
  await page.goto('/positions')

  await expect(page.getByRole('heading', { name: '未平仓持仓', level: 1 })).toBeVisible()
  const row = page.getByRole('row').filter({ hasText: POSITION_ID as string })
  await expect(row).toHaveCount(1)
  await expect(row).toContainText('PAPER')

  // H1: the global pause freezes the 5s poll and stamps PAUSED; resuming
  // performs one immediate revalidation.
  await expect.poll(() => positionPolls).toBeGreaterThan(0)
  const pollsBeforePause = positionPolls
  await page.getByRole('button', { name: '暂停实时更新' }).click()
  await expect(page.getByTestId('statusbar-paused')).toHaveText('PAUSED')
  await expect(page.getByTestId('positions-paused')).toBeVisible()
  await page.waitForTimeout(6_000)
  expect(positionPolls).toBe(pollsBeforePause)

  await page.getByRole('button', { name: '继续实时更新' }).click()
  await expect(page.getByTestId('statusbar-paused')).toHaveCount(0)
  await expect(page.getByTestId('positions-paused')).toHaveCount(0)
  await expect.poll(() => positionPolls).toBeGreaterThan(pollsBeforePause)

  // Detail: PnL breakdown (resolved-mark based) and the management section.
  await row.getByRole('link').first().click()
  await expect(page.getByRole('heading', { name: POSITION_ID as string, level: 1 })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'PnL 分解' })).toBeVisible()
  await expect(page.getByText('Price PnL (unrealized)')).toBeVisible()
  await expect(page.getByRole('heading', { name: '管理决策' })).toBeVisible()
  await expect(page.getByText(/mark as of .* UTC/).first()).toBeVisible()

  const manageButton = page.getByRole('button', { name: '请求管理决策' })
  await expect(manageButton).not.toHaveAttribute('aria-disabled', 'true')
  await manageButton.click()

  const dialog = page.getByRole('dialog', { name: /请求管理决策/ })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('Idempotency-Key')
  await dialog.getByRole('button', { name: /确认请求管理/ }).click()

  await expect(dialog).toHaveCount(0)
  await expect(page.getByTestId('manage-live')).toContainText('管理决策已返回')
  await expect(page.getByText('管理决策阶段')).toBeVisible()

  // Exactly one mutation, carrying CSRF and the session-persisted key.
  expect(manageRequests).toHaveLength(1)
  const headers = manageRequests[0]
  expect(headers['x-csrf-token']).toBeTruthy()
  expect(headers['idempotency-key']).toBe(STABLE_KEY)

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('keeps the global pause control unclipped at 375 and 768', async ({ page }) => {
  for (const width of [375, 768]) {
    await page.setViewportSize({ width, height: 812 })
    await authenticate(page)
    await page.goto('/positions')

    const bar = page.locator('.statusbar')
    await expect(bar).toBeVisible()
    const box = await bar.boundingBox()
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44)
    expect(await bar.evaluate((element) => getComputedStyle(element).overflow)).toBe('visible')

    const pause = page.getByRole('button', { name: '暂停实时更新' })
    await pause.focus()
    await expect(pause).toBeFocused()

    const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth)
    expect(pageWidth).toBeLessThanOrEqual(width)
  }
})

test('captures the positions list and detail', async ({ page }) => {
  fs.mkdirSync(SCREENS_DIR, { recursive: true })
  await authenticate(page)
  await page.goto('/positions')
  await expect(page.getByRole('heading', { name: '未平仓持仓', level: 1 })).toBeVisible()
  await page.screenshot({ path: path.join(SCREENS_DIR, 'positions-list.png'), fullPage: true })

  const row = page.getByRole('row').filter({ hasText: POSITION_ID as string })
  await row.getByRole('link').first().click()
  await expect(page.getByRole('heading', { name: POSITION_ID as string, level: 1 })).toBeVisible()
  await page.screenshot({ path: path.join(SCREENS_DIR, 'position-detail.png'), fullPage: true })
})
