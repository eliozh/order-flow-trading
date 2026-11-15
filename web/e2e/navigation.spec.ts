import fs from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

/**
 * Critical read-only journey for slice 33: the persistent navigation shell.
 *
 * Preconditions (provided by `e2e/run-e2e.sh`):
 * - the reports daemon serves `web/dist` on `OFT_E2E_REPORT_BASE_URL` with a
 *   temporary data dir and already-seeded reports/positions/journal rows;
 * - `OFT_E2E_REPORT_BOOTSTRAP_FILE` mints the browser session.
 *
 * The journey is strictly read-only: it opens every existing route directly at
 * 1440/768/375 and exercises the shell with the keyboard. It never submits a
 * form, activates the kill switch, or mutates any record.
 */

const ROOT = path.resolve(process.cwd(), '..')
const BASE_URL = process.env.OFT_E2E_REPORT_BASE_URL ?? 'http://127.0.0.1:7642'
const BOOTSTRAP_FILE = process.env.OFT_E2E_REPORT_BOOTSTRAP_FILE
const SCREENS_DIR =
  process.env.OFT_E2E_NAVIGATION_SCREENS_DIR ??
  path.resolve(ROOT, '.claude', 'ecc-workflow', 'screens', 'slice-33-e2e')

const GROUP_LABELS = ['监控', '决策', '记录', '配置'] as const

const NAV_ITEMS = [
  { label: '观察列表', href: '/watchlist' },
  { label: '扫描', href: '/scans' },
  { label: '报告', href: '/reports' },
  { label: '机会台账', href: '/opportunities' },
  { label: '入场复核', href: '/triggers' },
  { label: '持仓', href: '/positions' },
  { label: 'Paper 账户', href: '/paper' },
  { label: '交易复盘', href: '/journal' },
  { label: '设置', href: '/settings' },
  { label: '系统', href: '/system' },
] as const

/** Every existing route, with the nav item that must be current and its h1. */
const ROUTES = [
  { path: '/', current: '系统', heading: 'System status' },
  { path: '/system', current: '系统', heading: 'System status' },
  { path: '/watchlist', current: '观察列表', heading: 'Watchlist' },
  { path: '/watchlist/search', current: '观察列表', heading: 'Watchlist' },
  { path: '/scans', current: '扫描', heading: 'Scan history' },
  { path: '/reports', current: '报告', heading: 'CALC reports' },
  { path: '/opportunities', current: '机会台账', heading: 'Opportunities' },
  { path: '/triggers', current: '入场复核', heading: '入场复核队列' },
  { path: '/positions', current: '持仓', heading: '未平仓持仓' },
  { path: '/paper', current: 'Paper 账户', heading: 'Paper 交易账户' },
  { path: '/journal', current: '交易复盘', heading: '交易日志与复盘' },
  { path: '/settings', current: '设置', heading: '设置 / Settings' },
  { path: '/settings/scan', current: '设置', heading: '扫描周期与排程' },
] as const

/**
 * Runtime axe baseline, per audited route. Every list is empty except for the
 * known, pre-existing `/system` `definition-list` violation: the `.stat-note`
 * `<p>` elements sit inside the `<dl><div>` wrappers, which the rule rejects.
 * That is tracked outside slice 33; every other rule and route must be clean.
 * Do not extend this list without a review.
 */
const AXE_BASELINE: Record<string, readonly string[]> = {
  '/system': ['definition-list'],
  '/watchlist': [],
  '/reports': [],
  '/positions': [],
  '/journal': [],
  '/settings': [],
}

const AXE_AUDIT_ROUTES = [
  { path: '/system', heading: 'System status' },
  { path: '/watchlist', heading: 'Watchlist' },
  { path: '/reports', heading: 'CALC reports' },
  { path: '/positions', heading: '未平仓持仓' },
  { path: '/journal', heading: '交易日志与复盘' },
  { path: '/settings', heading: '设置 / Settings' },
] as const

const VIEWPORTS = [
  { width: 1440, height: 900, name: '1440' },
  { width: 768, height: 1024, name: '768' },
  { width: 375, height: 812, name: '375' },
] as const

const COMPACT_NOTE_ZH = '移动端为只读界面'
const COMPACT_NOTE_EN = 'Use a desktop viewport or the CLI for actions.'

interface BrowserDiagnostics {
  consoleErrors: string[]
  pageErrors: string[]
}

interface FocusStop {
  tag: string
  text: string
  label: string | null
  id: string
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
  if (!BOOTSTRAP_FILE) {
    throw new Error('OFT_E2E_REPORT_BOOTSTRAP_FILE is required for the navigation journey')
  }
  const secret = fs.readFileSync(BOOTSTRAP_FILE, 'utf8').trim()
  const response = await page.request.get(`/?bootstrap=${encodeURIComponent(secret)}`)
  expect(response.status()).toBe(200)
}

function navLandmark(page: Page) {
  return page.getByRole('navigation', { name: '主导航' })
}

async function activeFocus(page: Page): Promise<FocusStop> {
  return page.evaluate(() => {
    const element = document.activeElement as HTMLElement | null
    return {
      tag: element?.tagName ?? '',
      text: (element?.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 80),
      label: element?.getAttribute('aria-label') ?? null,
      id: element?.id ?? '',
    }
  })
}

/** The shell guarantees that hold on every route at every width. */
async function expectShell(
  page: Page,
  route: { path: string; current: string; heading: string },
): Promise<void> {
  await page.goto(route.path)
  const nav = navLandmark(page)
  const main = page.locator('#app-main')
  await expect(nav, `nav landmark on ${route.path}`).toBeVisible()
  await expect(main, `main on ${route.path}`).toBeVisible()

  // The route renders its own page identity, not the not-found panel, inside
  // the main landmark: a mis-wired branch cannot pass on shell presence alone.
  await expect(
    main.getByRole('heading', { level: 1, name: route.heading, exact: true }),
    `h1 "${route.heading}" on ${route.path}`,
  ).toBeVisible()
  await expect(
    page.getByRole('heading', { level: 1, name: 'Not found', exact: true }),
    `no not-found heading on ${route.path}`,
  ).toHaveCount(0)

  // Every existing route is reachable from the same grouped source.
  await expect(nav.getByRole('link'), `link count on ${route.path}`).toHaveCount(NAV_ITEMS.length)
  for (const item of NAV_ITEMS) {
    await expect(nav.locator(`a[href="${item.href}"]`), `link ${item.href}`).toHaveCount(1)
  }

  // Hrefs are relative and same-origin: no absolute URL can leave the daemon.
  const hrefs = await nav.getByRole('link').evaluateAll((elements) =>
    elements.map((element) => {
      const anchor = element as HTMLAnchorElement
      return {
        attribute: anchor.getAttribute('href') ?? '',
        origin: new URL(anchor.href, window.location.href).origin,
        documentOrigin: window.location.origin,
        linkText: (anchor.textContent ?? '').replace(/\s+/g, ' ').trim(),
      }
    }),
  )
  expect(hrefs).toHaveLength(NAV_ITEMS.length)
  for (const link of hrefs) {
    expect(link.attribute, 'nav href is a relative path').toMatch(/^\/(?!\/)/)
    expect(link.attribute).not.toContain('://')
    expect(link.origin, `same-origin href ${link.attribute}`).toBe(link.documentOrigin)
    expect(link.linkText, `accessible link text ${link.attribute}`).not.toBe('')
  }

  // Exactly one current item inside the navigation landmark; detail routes
  // inherit their section (`/settings/scan` -> 设置).
  const current = nav.locator('[aria-current="page"]')
  await expect(current, `current item count on ${route.path}`).toHaveCount(1)
  await expect(current).toHaveAccessibleName(route.current)

  // The four groups are real headings in the accessibility tree, and each
  // names the list it belongs to.
  for (const label of GROUP_LABELS) {
    await expect(
      nav.getByRole('heading', { name: label, exact: true, level: 2 }),
      `group label ${label} on ${route.path}`,
    ).toBeVisible()
    await expect(nav.getByRole('list', { name: label }), `group list ${label}`).toBeVisible()
  }
}

test.describe('navigation shell reachability', () => {
  for (const viewport of VIEWPORTS) {
    test(`reaches every route inside the shell at ${viewport.name}`, async ({ page }) => {
      test.setTimeout(120_000)
      const diagnostics = collectDiagnostics(page)
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      await authenticate(page)
      fs.mkdirSync(SCREENS_DIR, { recursive: true })

      for (const route of ROUTES) {
        await expectShell(page, route)
        if (route.path === '/system') {
          await page.screenshot({
            path: path.join(SCREENS_DIR, `navigation-${viewport.name}.png`),
            fullPage: true,
          })
        }
      }

      expectClean(diagnostics)
    })
  }
})

test('reports no new axe violations on the six audited routes at 1440', async ({ page }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)

  for (const route of AXE_AUDIT_ROUTES) {
    await page.goto(route.path)
    await page.waitForLoadState('networkidle')
    await expect(
      page
        .locator('#app-main')
        .getByRole('heading', { level: 1, name: route.heading, exact: true }),
      `page identity on ${route.path}`,
    ).toBeVisible()

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()
    const allowed = AXE_BASELINE[route.path] ?? []
    const violations = results.violations.filter((violation) => !allowed.includes(violation.id))
    const summary = violations.map(
      (violation) =>
        `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
          .map((node) => node.target.join(' '))
          .join(' | ')}`,
    )
    expect(violations, `${route.path} axe violations:\n${summary.join('\n')}`).toEqual([])
  }
})

test('keeps the skip links first, reaches the kill switch before nav links, and moves focus on activation', async ({
  page,
}) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/system')
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()

  // Tab from a cold load: the documented first three stops are
  // 跳到主内容 -> 跳到导航 -> Kill switch (DESIGN A2.2 / A12 #7). The pause
  // control follows the kill switch in DOM order.
  const stops: FocusStop[] = []
  for (let index = 0; index < 6; index += 1) {
    await page.keyboard.press('Tab')
    stops.push(await activeFocus(page))
  }
  expect(stops[0].text).toBe('跳到主内容')
  expect(stops[1].text).toBe('跳到导航')

  const killIndex = stops.findIndex(
    (stop) => stop.text.includes('Kill switch') || (stop.label ?? '').includes('Kill switch'),
  )
  const firstNavLinkIndex = stops.findIndex((stop) => stop.text === '观察列表')
  expect(killIndex, 'the kill switch is the third tab stop').toBe(2)
  expect(firstNavLinkIndex, 'a nav link follows the kill switch').toBeGreaterThan(killIndex)
  expect(stops[3].text, 'the pause control follows the kill switch').toMatch(
    /暂停实时更新|继续实时更新/,
  )

  // 跳到主内容 moves focus to the main landmark. It is visibly offscreen until
  // it receives focus (WCAG 2.4.1): assert the geometry, not just focus.
  await page.reload()
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()
  const skipMain = page.getByRole('link', { name: '跳到主内容' })
  const hiddenBox = await skipMain.boundingBox()
  expect(hiddenBox, 'skip link has a box before focus').not.toBeNull()
  expect(
    hiddenBox!.y + hiddenBox!.height,
    'skip link bottom is offscreen before focus',
  ).toBeLessThanOrEqual(0)

  await page.keyboard.press('Tab')
  await expect(skipMain).toBeFocused()

  const viewport = page.viewportSize()
  expect(viewport, 'viewport is known').not.toBeNull()
  // The skip link slides in over 120ms; poll until it settles, then assert the
  // final geometry is fully inside the viewport.
  await expect
    .poll(
      async () => {
        const box = await skipMain.boundingBox()
        if (!box || !viewport) {
          return false
        }
        return (
          box.y >= 0 &&
          box.x >= 0 &&
          box.x + box.width <= viewport.width &&
          box.y + box.height <= viewport.height
        )
      },
      { message: 'focused skip link settles fully inside the viewport' },
    )
    .toBe(true)

  const focusedBox = await skipMain.boundingBox()
  expect(focusedBox, 'focused skip link has a box').not.toBeNull()
  expect(focusedBox!.y, 'focused skip link top is inside the viewport').toBeGreaterThanOrEqual(0)
  expect(focusedBox!.x, 'focused skip link left is inside the viewport').toBeGreaterThanOrEqual(0)
  expect(
    focusedBox!.x + focusedBox!.width,
    'focused skip link right is inside the viewport',
  ).toBeLessThanOrEqual(viewport!.width)
  expect(
    focusedBox!.y + focusedBox!.height,
    'focused skip link bottom is inside the viewport',
  ).toBeLessThanOrEqual(viewport!.height)

  await page.keyboard.press('Enter')
  await expect(page.locator('#app-main')).toBeFocused()

  // 跳到导航 moves focus to the navigation landmark; the next Tab enters the
  // first nav link, so the resulting order is logical.
  await page.reload()
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: '跳到导航' })).toBeFocused()
  await page.keyboard.press('Enter')
  const nav = navLandmark(page)
  await expect(nav).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(nav.getByRole('link', { name: '观察列表' })).toBeFocused()

  // Escape does not trap focus: the current stop stays focusable and the next
  // Tab still advances to the following nav link.
  await page.keyboard.press('Escape')
  await expect(nav.getByRole('link', { name: '观察列表' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(nav.getByRole('link', { name: '扫描' })).toBeFocused()

  expectClean(diagnostics)
})

test('moves focus to the destination h1 after a nav-link route change', async ({ page }) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await authenticate(page)
  await page.goto('/system')
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()

  const nav = navLandmark(page)
  await nav.getByRole('link', { name: '观察列表' }).click()
  await expect(page).toHaveURL(/\/watchlist$/)
  await expect(page.getByRole('heading', { name: 'Watchlist', level: 1 })).toBeFocused()

  await navLandmark(page).getByRole('link', { name: '扫描' }).click()
  await expect(page).toHaveURL(/\/scans$/)
  await expect(page.getByRole('heading', { name: 'Scan history', level: 1 })).toBeFocused()

  // H1: /system's loading branch has no h1 at mount, so focus lands on the
  // always-present #app-main instead of the permanently empty announcer.
  await navLandmark(page).getByRole('link', { name: '系统' }).click()
  await expect(page).toHaveURL(/\/system$/)
  await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()
  await expect(page.locator('#app-main')).toBeFocused()
  const systemFocus = await activeFocus(page)
  expect(systemFocus.tag).not.toBe('BODY')
  expect(systemFocus.id).not.toBe('route-announcer')

  // Focus never lands on body after a nav-driven route change.
  const focused = await activeFocus(page)
  expect(focused.tag).not.toBe('BODY')

  expectClean(diagnostics)
})

test('375px compact shell is read-only, explains desktop/CLI actions, and keeps the kill switch available', async ({
  page,
}) => {
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 375, height: 812 })
  await authenticate(page)
  await page.goto('/system')

  const nav = navLandmark(page)
  await expect(nav).toBeVisible()
  await expect(nav).toHaveClass(/nav--compact/)
  await expect(nav.locator('.nav-compact-note')).toHaveCount(1)
  const note = nav.getByRole('note')
  await expect(note).toContainText(COMPACT_NOTE_ZH)
  await expect(note).toContainText('主入口为 Telegram')
  await expect(note).toContainText(COMPACT_NOTE_EN)
  await expect(note).toContainText('桌面端或 CLI')

  const killSwitch = page.getByRole('button', { name: /Kill switch/ })
  await expect(killSwitch).toBeVisible()
  await expect(killSwitch).toBeEnabled()

  expectClean(diagnostics)
})

test('touch widths keep every destination visible with 44px targets and 8px separation', async ({
  page,
}) => {
  test.setTimeout(60_000)
  const diagnostics = collectDiagnostics(page)
  await page.setViewportSize({ width: 375, height: 812 })
  await authenticate(page)
  await page.goto('/system')

  const nav = navLandmark(page)
  await expect(nav).toHaveClass(/nav--compact/)

  // Every compact destination is visible without an internal horizontal scroll.
  const compact = await nav.evaluate((element) => {
    const groups = element.querySelector<HTMLElement>('.nav-compact-groups')
    const list = element.querySelector<HTMLElement>('.nav-group--compact .nav-list')
    const label = element.querySelector<HTMLElement>('.nav-group-label')
    const links = Array.from(element.querySelectorAll<HTMLElement>('.nav-compact-groups .nav-link'))
    return {
      groupScrollWidth: groups?.scrollWidth ?? 0,
      groupClientWidth: groups?.clientWidth ?? 0,
      viewport: window.innerWidth,
      boxes: links.map((link) => {
        const rect = link.getBoundingClientRect()
        return { left: rect.left, right: rect.right, height: rect.height }
      }),
      linkGap: list ? parseFloat(getComputedStyle(list).columnGap) : 0,
      groupGap: groups ? parseFloat(getComputedStyle(groups).columnGap) : 0,
      labelFont: label ? parseFloat(getComputedStyle(label).fontSize) : 0,
    }
  })
  expect(compact.groupScrollWidth, 'no internal horizontal scroll').toBeLessThanOrEqual(
    compact.groupClientWidth + 1,
  )
  expect(compact.boxes).toHaveLength(NAV_ITEMS.length)
  for (const box of compact.boxes) {
    expect(box.left, 'compact link starts inside the viewport').toBeGreaterThanOrEqual(0)
    expect(box.right, 'compact link ends inside the viewport').toBeLessThanOrEqual(compact.viewport)
    expect(box.height, 'compact target is 44px').toBeGreaterThanOrEqual(44)
  }
  expect(compact.linkGap, 'compact link separation').toBeGreaterThanOrEqual(8)
  expect(compact.groupGap, 'compact group separation').toBeGreaterThanOrEqual(8)
  expect(compact.labelFont, 'compact group label floor').toBeGreaterThanOrEqual(11)

  // 768 rail: same 44px target and 8px separation floors.
  await page.setViewportSize({ width: 768, height: 1024 })
  await expect(nav).not.toHaveClass(/nav--compact/)
  const rail = await nav.evaluate((element) => {
    const links = Array.from(element.querySelectorAll<HTMLElement>('.nav-link'))
    const list = element.querySelector<HTMLElement>('.nav-list')
    const label = element.querySelector<HTMLElement>('.nav-group-label')
    return {
      heights: links.map((link) => link.getBoundingClientRect().height),
      gap: list ? parseFloat(getComputedStyle(list).rowGap) : 0,
      labelFont: label ? parseFloat(getComputedStyle(label).fontSize) : 0,
    }
  })
  expect(rail.heights).toHaveLength(NAV_ITEMS.length)
  for (const height of rail.heights) {
    expect(height, 'rail target is 44px').toBeGreaterThanOrEqual(44)
  }
  expect(rail.gap, 'rail link separation').toBeGreaterThanOrEqual(8)
  expect(rail.labelFont, 'rail group label floor').toBeGreaterThanOrEqual(11)

  expectClean(diagnostics)
})

test('reduced motion removes non-essential nav motion and the shell geometry does not shift', async ({
  browser,
}) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    reducedMotion: 'reduce',
    viewport: { width: 1440, height: 900 },
  })
  const page = await context.newPage()
  const diagnostics = collectDiagnostics(page)
  try {
    await authenticate(page)
    await page.goto('/system')
    await expect(page.getByRole('heading', { name: 'System status', level: 1 })).toBeVisible()
    await expect(navLandmark(page)).toBeVisible()

    // Every non-essential transition/animation on the nav and skip links is
    // disabled under prefers-reduced-motion.
    const motion = await page.evaluate(() => {
      const read = (selector: string) => {
        const element = document.querySelector(selector)
        if (!element) {
          return null
        }
        const style = getComputedStyle(element)
        return { transition: style.transitionDuration, animation: style.animationDuration }
      }
      return {
        nav: read('#app-nav'),
        link: read('#app-nav .nav-link'),
        icon: read('#app-nav .nav-ico'),
        skip: read('.skip-link'),
      }
    })
    const allZero = (durations: string): boolean =>
      durations
        .split(',')
        .map((value) => value.trim())
        .every((value) => /^0(\.0+)?s$/.test(value))
    for (const [name, value] of Object.entries(motion)) {
      expect(value, `${name} exists`).not.toBeNull()
      expect(allZero((value as { transition: string }).transition), `${name} transition`).toBe(true)
      expect(allZero((value as { animation: string }).animation), `${name} animation`).toBe(true)
    }

    // The nav column is reserved synchronously by CSS grid (no measurement), so
    // the nav and main rectangles are identical across consecutive frames.
    const readGeometry = () =>
      page.evaluate(() => {
        const navElement = document.getElementById('app-nav') as HTMLElement
        const mainElement = document.getElementById('app-main') as HTMLElement
        const bodyElement = document.querySelector('.app-body') as HTMLElement
        const navRect = navElement.getBoundingClientRect()
        const mainRect = mainElement.getBoundingClientRect()
        return {
          nav: [navRect.x, navRect.y, navRect.width, navRect.height],
          main: [mainRect.x, mainRect.y, mainRect.width, mainRect.height],
          columns: getComputedStyle(bodyElement).gridTemplateColumns,
        }
      })

    const before = await readGeometry()
    expect(before.nav[2], '208px nav column').toBeGreaterThanOrEqual(207)
    expect(before.nav[2], '208px nav column').toBeLessThanOrEqual(209)
    expect(before.columns.startsWith('208px'), 'grid reserves the nav column').toBe(true)

    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
        }),
    )
    const after = await readGeometry()
    expect(after.nav, 'nav geometry is stable after mount').toEqual(before.nav)
    expect(after.main, 'main geometry is stable after mount').toEqual(before.main)
    expect(after.columns).toBe(before.columns)

    expectClean(diagnostics)
  } finally {
    await context.close()
  }
})
