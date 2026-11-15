/** The single source of truth for the persistent navigation (slice 33).
 *
 * Only routes that exist in `web/src` appear here. The desktop sidebar and the
 * 375px compact navigation both render from `NAV_GROUPS`, so a route is never
 * reachable from one surface but missing from the other.
 */

export type NavGroupId = 'monitor' | 'decide' | 'record' | 'config'

export type NavItemId =
  | 'watchlist'
  | 'scans'
  | 'reports'
  | 'opportunities'
  | 'triggers'
  | 'positions'
  | 'paper'
  | 'journal'
  | 'settings'
  | 'system'

export type NavIconName =
  | 'watch'
  | 'scan'
  | 'report'
  | 'opp'
  | 'trigger'
  | 'pos'
  | 'paper'
  | 'journal'
  | 'settings'
  | 'system'

export interface NavItem {
  id: NavItemId
  label: string
  href: string
  icon: NavIconName
  /** Route prefix that makes this item current, including its detail routes. */
  match: string
}

export interface NavGroup {
  id: NavGroupId
  label: string
  items: readonly NavItem[]
}

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    id: 'monitor',
    label: '监控',
    items: [
      {
        id: 'watchlist',
        label: '观察列表',
        href: '/watchlist',
        icon: 'watch',
        match: '/watchlist',
      },
      { id: 'scans', label: '扫描', href: '/scans', icon: 'scan', match: '/scans' },
      { id: 'reports', label: '报告', href: '/reports', icon: 'report', match: '/reports' },
    ],
  },
  {
    id: 'decide',
    label: '决策',
    items: [
      {
        id: 'opportunities',
        label: '机会台账',
        href: '/opportunities',
        icon: 'opp',
        match: '/opportunities',
      },
      { id: 'triggers', label: '入场复核', href: '/triggers', icon: 'trigger', match: '/triggers' },
      { id: 'positions', label: '持仓', href: '/positions', icon: 'pos', match: '/positions' },
    ],
  },
  {
    id: 'record',
    label: '记录',
    items: [
      { id: 'paper', label: 'Paper 账户', href: '/paper', icon: 'paper', match: '/paper' },
      { id: 'journal', label: '交易复盘', href: '/journal', icon: 'journal', match: '/journal' },
    ],
  },
  {
    id: 'config',
    label: '配置',
    items: [
      { id: 'settings', label: '设置', href: '/settings', icon: 'settings', match: '/settings' },
      { id: 'system', label: '系统', href: '/system', icon: 'system', match: '/system' },
    ],
  },
]

export interface ActiveNavMatch {
  group: NavGroup
  item: NavItem
}

function normalizeNavPath(pathname: string): string {
  if (!pathname) {
    return '/'
  }
  const trimmed = pathname.replace(/\/+$/, '')
  return trimmed === '' ? '/' : trimmed
}

function itemMatches(path: string, item: NavItem): boolean {
  return path === item.match || path.startsWith(`${item.match}/`)
}

/**
 * The active group and item for a route. `/` belongs to the system surface, and
 * detail routes inherit the section they belong to (`/scans/abc` -> 扫描,
 * `/settings/scan` -> 设置).
 */
export function resolveActiveNav(pathname: string): ActiveNavMatch | null {
  const normalized = normalizeNavPath(pathname)
  const path = normalized === '/' ? '/system' : normalized

  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (itemMatches(path, item)) {
        return { group, item }
      }
    }
  }
  return null
}
