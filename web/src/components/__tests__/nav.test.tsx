import {
  act,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../../App'
import { NAV_GROUPS, resolveActiveNav } from '../../routes/nav-items'
import { resolveRoute } from '../../routes/route-match'
import { NAV_FOCUS_KEY, Nav, RouteFocus } from '../nav'

const ROUTES: { path: string; activeItem: string; activeLabel: string }[] = [
  { path: '/', activeItem: 'system', activeLabel: '系统' },
  { path: '/system', activeItem: 'system', activeLabel: '系统' },
  { path: '/watchlist', activeItem: 'watchlist', activeLabel: '观察列表' },
  { path: '/watchlist/search', activeItem: 'watchlist', activeLabel: '观察列表' },
  { path: '/watchlist/BTCUSDT', activeItem: 'watchlist', activeLabel: '观察列表' },
  { path: '/scans', activeItem: 'scans', activeLabel: '扫描' },
  { path: '/scans/SCAN-1', activeItem: 'scans', activeLabel: '扫描' },
  { path: '/reports', activeItem: 'reports', activeLabel: '报告' },
  { path: '/reports/RPT-1', activeItem: 'reports', activeLabel: '报告' },
  { path: '/opportunities', activeItem: 'opportunities', activeLabel: '机会台账' },
  { path: '/opportunities/OPP-1', activeItem: 'opportunities', activeLabel: '机会台账' },
  { path: '/triggers', activeItem: 'triggers', activeLabel: '入场复核' },
  { path: '/triggers/TRG-1', activeItem: 'triggers', activeLabel: '入场复核' },
  { path: '/positions', activeItem: 'positions', activeLabel: '持仓' },
  { path: '/positions/POS-1', activeItem: 'positions', activeLabel: '持仓' },
  { path: '/paper', activeItem: 'paper', activeLabel: 'Paper 账户' },
  { path: '/journal', activeItem: 'journal', activeLabel: '交易复盘' },
  { path: '/journal/TRD-1', activeItem: 'journal', activeLabel: '交易复盘' },
  { path: '/settings', activeItem: 'settings', activeLabel: '设置' },
  { path: '/settings/scan', activeItem: 'settings', activeLabel: '设置' },
]

function activeLinks(nav: HTMLElement): HTMLElement[] {
  return within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
}

async function renderAppAt(path: string): Promise<ReturnType<typeof render>> {
  window.history.pushState({}, '', path)
  let view!: ReturnType<typeof render>
  await act(async () => {
    view = render(<App />)
  })
  return view
}

beforeEach(() => {
  sessionStorage.clear()
  // Every probe fails fast; pages render their error/loading shells without
  // any dangling timers or dependent network state.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new Error('offline in navigation test')
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
  window.history.pushState({}, '', '/')
})

describe('navigation shell', () => {
  it('exposes every nav group and item from one source, with no routes that do not exist', () => {
    const labels = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.label))
    expect(labels).toEqual([
      '观察列表',
      '扫描',
      '报告',
      '机会台账',
      '入场复核',
      '持仓',
      'Paper 账户',
      '交易复盘',
      '设置',
      '系统',
    ])
    const hrefs = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.href))
    expect(hrefs).not.toContain('/telegram')
    expect(hrefs).not.toContain('/cli')
    expect(hrefs).not.toContain('/onboarding')
  })

  it('marks exactly the matching section active on every route, including detail routes', () => {
    for (const route of ROUTES) {
      const match = resolveRoute(route.path)
      expect(match.activeNavItem).toBe(route.activeItem)
    }

    expect(resolveActiveNav('/')).toEqual(
      expect.objectContaining({ item: expect.objectContaining({ id: 'system' }) }),
    )
    expect(resolveActiveNav('/settings/scan')?.group.id).toBe('config')
    expect(resolveActiveNav('/positions/POS-1')?.item.label).toBe('持仓')
    expect(resolveActiveNav('/not-a-route')).toBeNull()
  })

  it('marks no section current on a fabricated or deeper not-found path', () => {
    const notFoundPaths = [
      '/paper/x',
      '/journal/a/b',
      '/positions/a/b',
      '/triggers/a/b',
      '/opportunities/a/b',
      '/settings/x',
    ]
    for (const path of notFoundPaths) {
      const match = resolveRoute(path)
      expect(match.activeNavItem, path).toBeNull()
      expect(match.activeGroup, path).toBeNull()
    }
  })

  it('renders every existing route inside the shell with exactly one aria-current item', async () => {
    for (const route of ROUTES) {
      const view = await renderAppAt(route.path)
      const nav = screen.getByRole('navigation', { name: '主导航' })
      const main = document.getElementById('app-main')

      expect(main).not.toBeNull()
      expect(nav).toBeInTheDocument()
      expect(
        nav.compareDocumentPosition(main as Node) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy()

      const current = activeLinks(nav)
      expect(current).toHaveLength(1)
      expect(current[0]).toHaveAccessibleName(route.activeLabel)

      view.unmount()
    }
  })

  it('renders the not-found panel with no current nav item on a fabricated path', async () => {
    for (const path of ['/paper/x', '/journal/a/b', '/settings/x']) {
      const view = await renderAppAt(path)
      expect(screen.getByRole('heading', { name: 'Not found' })).toBeInTheDocument()
      const nav = screen.getByRole('navigation', { name: '主导航' })
      expect(activeLinks(nav), path).toHaveLength(0)
      view.unmount()
    }
  })

  it('orders the first three chrome tab stops: skip links then the kill switch', async () => {
    const view = await renderAppAt('/system')

    const skipMain = screen.getByRole('link', { name: '跳到主内容' })
    const skipNav = screen.getByRole('link', { name: '跳到导航' })
    const killSwitch = screen.getByRole('button', { name: /Kill switch/ })
    const pause = screen.getByRole('button', { name: /暂停实时更新/ })

    // DOM order is the tab order: skip links, kill switch, then the pause
    // control (DESIGN A2.2 / A12 #7).
    const follows = (first: Node, second: Node): boolean =>
      Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)
    expect(follows(skipMain, skipNav)).toBe(true)
    expect(follows(skipNav, killSwitch)).toBe(true)
    expect(follows(killSwitch, pause)).toBe(true)

    view.unmount()
  })

  it('renders group labels as headings that name their lists', () => {
    render(<Nav activeItemId="system" />)
    const nav = screen.getByRole('navigation', { name: '主导航' })

    for (const group of NAV_GROUPS) {
      expect(within(nav).getByRole('heading', { level: 2, name: group.label })).toBeInTheDocument()
      expect(within(nav).getByRole('list', { name: group.label })).toBeInTheDocument()
    }
  })

  it('uses same-origin relative hrefs and gives every link an accessible name', () => {
    render(<Nav activeItemId="system" />)
    const links = within(screen.getByRole('navigation', { name: '主导航' })).getAllByRole('link')

    expect(links).toHaveLength(10)
    for (const link of links) {
      const href = link.getAttribute('href') ?? ''
      expect(href.startsWith('/')).toBe(true)
      expect(href.startsWith('//')).toBe(false)
      expect(href.startsWith('http')).toBe(false)
      expect(link).toHaveAccessibleName()
    }
  })

  it('keeps the skip links first and moves focus to main and the nav landmark', async () => {
    const view = await renderAppAt('/system')

    const links = screen.getAllByRole('link')
    expect(links[0]).toHaveAccessibleName('跳到主内容')
    expect(links[1]).toHaveAccessibleName('跳到导航')

    fireEvent.click(links[0])
    expect(document.activeElement).toBe(document.getElementById('app-main'))

    fireEvent.click(links[1])
    expect(document.activeElement).toBe(document.getElementById('app-nav'))

    view.unmount()
  })

  it('keeps the kill switch available above the shell at every width', async () => {
    const view = await renderAppAt('/system')

    expect(screen.getByRole('button', { name: /Kill switch/ })).toBeEnabled()

    view.unmount()
  })

  it('renders a compact grouped nav at 375px that still exposes every route and the read-only note', () => {
    render(<Nav activeItemId="scans" compact />)
    const nav = screen.getByRole('navigation', { name: '主导航' })

    expect(within(nav).getAllByRole('link')).toHaveLength(10)
    expect(within(nav).getByRole('note')).toHaveTextContent('移动端为只读界面')
    expect(within(nav).getByRole('note')).toHaveTextContent('主入口为 Telegram')
    expect(within(nav).getByRole('note')).toHaveTextContent(
      'Use a desktop viewport or the CLI for actions',
    )

    const current = activeLinks(nav)
    expect(current).toHaveLength(1)
    expect(current[0]).toHaveAccessibleName('扫描')

    for (const group of NAV_GROUPS) {
      expect(within(nav).getByRole('list', { name: group.label })).toBeInTheDocument()
    }
  })

  it('has no focus trap: Escape does not move focus and the nav has no modal semantics', () => {
    render(<Nav activeItemId="system" />)
    const nav = screen.getByRole('navigation', { name: '主导航' })
    const link = within(nav).getByRole('link', { name: '观察列表' })

    link.focus()
    fireEvent.keyDown(link, { key: 'Escape' })

    expect(document.activeElement).toBe(link)
    expect(nav).not.toHaveAttribute('aria-modal')
    expect(within(nav).queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('records a one-shot focus flag on a plain nav-link activation', () => {
    render(<Nav activeItemId="system" />)
    const link = screen.getByRole('link', { name: '扫描' })
    const event = createEvent.click(link)
    event.preventDefault()

    fireEvent(link, event)

    expect(sessionStorage.getItem(NAV_FOCUS_KEY)).toBe('1')
  })

  it('does not set the focus flag for a modified click that opens another tab', () => {
    render(<Nav activeItemId="system" />)
    const link = screen.getByRole('link', { name: '扫描' })
    const event = createEvent.click(link, { ctrlKey: true })
    event.preventDefault()

    fireEvent(link, event)

    expect(sessionStorage.getItem(NAV_FOCUS_KEY)).toBeNull()
  })

  it('moves focus to the page h1 on a flagged route change and consumes the flag', async () => {
    sessionStorage.setItem(NAV_FOCUS_KEY, '1')
    render(
      <div>
        <main id="app-main">
          <h1>Scan history</h1>
        </main>
        <RouteFocus />
      </div>,
    )

    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Scan history' }))
    })
    expect(sessionStorage.getItem(NAV_FOCUS_KEY)).toBeNull()
  })

  it('falls back to one shell live region when the destination has no h1', async () => {
    sessionStorage.setItem(NAV_FOCUS_KEY, '1')
    render(
      <div>
        <main id="app-main">
          <p>Loading</p>
        </main>
        <RouteFocus />
      </div>,
    )

    await waitFor(() => {
      expect(document.activeElement).toBe(document.getElementById('route-announcer'))
    })
    expect(document.getElementById('route-announcer')).toHaveAttribute('role', 'status')
  })

  it('does not steal focus when the route was not reached through the nav', () => {
    render(<RouteFocus />)

    expect(document.activeElement).toBe(document.body)
  })
})
