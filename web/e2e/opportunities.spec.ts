import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

/**
 * Read-only journey for slice 41: the opportunity ledger list and detail.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the reports daemon serves `web/dist` on `OFT_E2E_REPORT_BASE_URL` with a
 *   temporary data dir and already-seeded opportunities in TRIGGERED state;
 * - `OFT_E2E_REPORT_BOOTSTRAP_FILE` mints the browser session.
 *
 * The journey never accepts, rejects, triggers, or cancels anything: the ledger
 * has no such control, and this spec asserts that it stays that way.
 */

const ROOT = path.resolve(process.cwd(), '..')
const BOOTSTRAP_FILE = process.env.OFT_E2E_REPORT_BOOTSTRAP_FILE
const SCREENS_DIR =
  process.env.OFT_E2E_OPPORTUNITIES_SCREENS_DIR ??
  path.resolve(ROOT, '.claude', 'ecc-workflow', 'screens', 'e2e41')

const VIEWPORTS = [
  { width: 1440, height: 900, name: '1440' },
  { width: 768, height: 1024, name: '768' },
  { width: 375, height: 812, name: '375' },
] as const

const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] as const

const DISCOVERY_STATES = ['TRIGGERED', 'OPEN', 'EXPIRED', 'INVALIDATED'] as const

interface OpportunitySource {
  scan_id: string
  report_id: string
  decision_id: string
}

interface OpportunitySummary {
  opportunity_id: string
  symbol: string
  state: string
  created_at: string
  source: OpportunitySource
}

interface OpportunityListEnvelope {
  ok: boolean
  data: {
    items: OpportunitySummary[]
    total: number
    counts: Record<string, number>
  } | null
}

interface Discovered {
  item: OpportunitySummary
  counts: Record<string, number>
}

/** Mint a browser session from the reports daemon's one-time bootstrap file. */
async function authenticate(page: Page): Promise<void> {
  if (!BOOTSTRAP_FILE) {
    throw new Error('OFT_E2E_REPORT_BOOTSTRAP_FILE is required for the opportunities journey')
  }
  const secret = fs.readFileSync(BOOTSTRAP_FILE, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

/**
 * Find a seeded opportunity through the real API. The reports daemon's default
 * OPEN filter can legitimately be empty, so try every state and keep the
 * per-state counts from whichever response answers first.
 */
async function discoverOpportunity(page: Page): Promise<Discovered> {
  let counts: Record<string, number> | null = null
  for (const state of DISCOVERY_STATES) {
    const response = await page.request.get(`/api/v1/opportunities?state=${state}&limit=50`)
    expect(response.status(), `list ${state} status`).toBe(200)
    const body = (await response.json()) as OpportunityListEnvelope
    expect(body.ok, `list ${state} envelope`).toBe(true)
    expect(body.data, `list ${state} data`).not.toBeNull()
    const data = body.data!
    counts ??= data.counts
    if (data.items.length > 0) {
      return { item: data.items[0], counts }
    }
  }
  throw new Error('no seeded opportunities in any state')
}

function stateTab(page: Page, state: string) {
  return page.getByRole('tab', { name: new RegExp(`^${state}`, 'i') })
}

async function selectState(page: Page, state: string): Promise<void> {
  await stateTab(page, state).click()
  await expect(stateTab(page, state)).toHaveAttribute('aria-selected', 'true')
}

async function runAxe(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags([...AXE_TAGS]).analyze()
  const summary = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  )
  expect(results.violations, `${label} axe violations:\n${summary.join('\n')}`).toEqual([])
}

async function expectNoHorizontalOverflow(page: Page, label: string): Promise<void> {
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(scrollWidth, `${label} horizontal overflow`).toBeLessThanOrEqual(375)
}

test.describe('opportunities ledger', () => {
  test('list shows source and age, an honest default empty panel, and keyboard tabs', async ({
    page,
  }) => {
    test.setTimeout(120_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    await authenticate(page)
    const { item, counts } = await discoverOpportunity(page)

    await page.goto('/opportunities')
    const main = page.locator('#app-main')
    await expect(
      main.getByRole('heading', { level: 1, name: 'Opportunities', exact: true }),
    ).toBeVisible()

    const openTab = stateTab(page, 'OPEN')
    await expect(openTab).toHaveAttribute('aria-selected', 'true')

    // The default OPEN filter may be empty; when it is, the per-state panel
    // must say so rather than showing the never-scanned panel.
    if ((counts.OPEN ?? 0) === 0) {
      await expect(main.getByRole('heading', { name: /No open opportunities/i })).toBeVisible()
      await expect(main.getByText(/in this filter/i)).toBeVisible()
      await expect(main.getByRole('heading', { name: /No opportunities yet/i })).toHaveCount(0)
    } else {
      await expect(main.getByRole('table', { name: /Opportunity ledger/i })).toBeVisible()
    }

    // Roving tab keyboard model: ArrowRight/ArrowLeft wrap, Home/End jump.
    await openTab.focus()
    await expect(openTab).toBeFocused()
    await page.keyboard.press('ArrowRight')
    await expect(stateTab(page, 'TRIGGERED')).toBeFocused()
    await expect(stateTab(page, 'TRIGGERED')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('ArrowLeft')
    await expect(openTab).toBeFocused()
    await expect(openTab).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('End')
    await expect(stateTab(page, 'INVALIDATED')).toBeFocused()
    await expect(stateTab(page, 'INVALIDATED')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('Home')
    await expect(openTab).toBeFocused()
    await expect(openTab).toHaveAttribute('aria-selected', 'true')

    // A real row exposes the Source and Age columns backed by server fields.
    await selectState(page, item.state)
    await expect(main.getByRole('columnheader', { name: 'Source', exact: true })).toBeVisible()
    await expect(main.getByRole('columnheader', { name: 'Age', exact: true })).toBeVisible()

    const row = main.getByRole('row').filter({ hasText: item.opportunity_id })
    await expect(row).toHaveCount(1)

    const headerLabels = (await main.getByRole('columnheader').allTextContents()).map((text) =>
      text.trim().toLowerCase(),
    )
    const sourceIndex = headerLabels.indexOf('source')
    const ageIndex = headerLabels.indexOf('age')
    expect(sourceIndex, 'source column index').toBeGreaterThanOrEqual(0)
    expect(ageIndex, 'age column index').toBeGreaterThanOrEqual(0)

    const sourceCell = row.locator('td').nth(sourceIndex)
    if (item.source.scan_id) {
      const scanLink = sourceCell.getByRole('link', { name: item.source.scan_id, exact: true })
      await expect(scanLink).toHaveAttribute(
        'href',
        `/scans/${encodeURIComponent(item.source.scan_id)}`,
      )
      if (item.source.report_id) {
        await expect(
          sourceCell.getByRole('link', { name: item.source.report_id, exact: true }),
        ).toHaveAttribute('href', `/reports/${encodeURIComponent(item.source.report_id)}`)
      } else {
        await expect(sourceCell).toContainText('N/A')
      }
    } else {
      await expect(sourceCell).toHaveText('N/A')
    }

    const ageCell = row.locator('td').nth(ageIndex)
    await expect(ageCell).toHaveText(/^(\d+m|\d+h \d{2}m|\d+d \d{2}h)$/)
    await expect(ageCell).not.toHaveText('N/A')
  })

  test('detail exposes the read-only JSON export and no state-changing control', async ({
    page,
  }) => {
    test.setTimeout(60_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    await authenticate(page)
    const { item } = await discoverOpportunity(page)

    await page.goto(`/opportunities/${encodeURIComponent(item.opportunity_id)}`)
    const main = page.locator('#app-main')
    await expect(
      main.getByRole('heading', { level: 1, name: item.opportunity_id, exact: true }),
    ).toBeVisible()

    const exportPath = `/api/v1/opportunities/${encodeURIComponent(item.opportunity_id)}/export`
    const exportLink = main.getByRole('link', { name: 'Export JSON', exact: true })
    await expect(exportLink).toHaveAttribute('href', exportPath)
    await expect(exportLink).toHaveAttribute('download')
    await expect(main.getByText(/read-only contract snapshot/i)).toBeVisible()

    // No accept/reject/trigger/cancel control, link or button, exists in the
    // ledger body. (The global shell's pause/kill controls live outside main.)
    await expect(main.getByRole('button')).toHaveCount(0)
    await expect(main.getByRole('link', { name: /accept|reject|cancel/i })).toHaveCount(0)

    // The endpoint behind the link is a read-only attachment; do not click the
    // link (that would navigate away from the rendered detail).
    const download = await page.request.get(exportPath)
    expect(download.status()).toBe(200)
    const disposition = download.headers()['content-disposition'] ?? ''
    expect(disposition).toMatch(/attachment/)
    expect(disposition).toContain('.json')
  })

  test('captures list and detail at 375/768/1440 with zero axe violations and no 375 overflow', async ({
    page,
  }) => {
    test.setTimeout(180_000)
    await authenticate(page)
    const { item } = await discoverOpportunity(page)
    fs.mkdirSync(SCREENS_DIR, { recursive: true })
    const detailPath = `/opportunities/${encodeURIComponent(item.opportunity_id)}`

    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height })

      await page.goto('/opportunities')
      const main = page.locator('#app-main')
      await expect(main.getByRole('heading', { level: 1, name: 'Opportunities' })).toBeVisible()
      // Show a populated ledger rather than a legitimately empty OPEN filter.
      await selectState(page, item.state)
      await expect(main.getByRole('table', { name: /Opportunity ledger/i })).toBeVisible()
      await page.screenshot({
        path: path.join(SCREENS_DIR, `opportunities-list-${viewport.name}.png`),
        fullPage: true,
      })
      await runAxe(page, `/opportunities at ${viewport.name}`)
      if (viewport.width === 375) {
        await expectNoHorizontalOverflow(page, '/opportunities')
      }

      await page.goto(detailPath)
      await expect(
        main.getByRole('heading', { level: 1, name: item.opportunity_id, exact: true }),
      ).toBeVisible()
      await page.screenshot({
        path: path.join(SCREENS_DIR, `opportunity-detail-${viewport.name}.png`),
        fullPage: true,
      })
      await runAxe(page, `${detailPath} at ${viewport.name}`)
      if (viewport.width === 375) {
        await expectNoHorizontalOverflow(page, detailPath)
      }
    }
  })
})
