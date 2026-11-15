/** The single source of truth for the persistent navigation (slice 33).
 *
 * Only routes that exist in `web/src` appear here. The desktop sidebar and the
 * 375px compact navigation both render from `NAV_GROUPS`, so a route is never
 * reachable from one surface but missing from the other.
 */

import { normalizePath } from './path'

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

/** A visible label fragment. `lang` marks an English token inside Chinese copy. */
export interface NavLabelSegment {
  text: string
  lang?: string
}

export interface NavItem {
  id: NavItemId
  label: string
  href: string
  icon: NavIconName
  /**
   * Optional labelled segments for mixed-language labels. When present the
   * rendered link text is the concatenation of `text`, which must still equal
   * `label` so the accessible name is unchanged.
   */
  segments?: readonly NavLabelSegment[]
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
      { id: 'watchlist', label: '观察列表', href: '/watchlist', icon: 'watch' },
      { id: 'scans', label: '扫描', href: '/scans', icon: 'scan' },
      { id: 'reports', label: '报告', href: '/reports', icon: 'report' },
    ],
  },
  {
    id: 'decide',
    label: '决策',
    items: [
      { id: 'opportunities', label: '机会台账', href: '/opportunities', icon: 'opp' },
      { id: 'triggers', label: '入场复核', href: '/triggers', icon: 'trigger' },
      { id: 'positions', label: '持仓', href: '/positions', icon: 'pos' },
    ],
  },
  {
    id: 'record',
    label: '记录',
    items: [
      {
        id: 'paper',
        label: 'Paper 账户',
        href: '/paper',
        icon: 'paper',
        segments: [{ text: 'Paper', lang: 'en' }, { text: ' 账户' }],
      },
      { id: 'journal', label: '交易复盘', href: '/journal', icon: 'journal' },
    ],
  },
  {
    id: 'config',
    label: '配置',
    items: [
      { id: 'settings', label: '设置', href: '/settings', icon: 'settings' },
      { id: 'system', label: '系统', href: '/system', icon: 'system' },
    ],
  },
]

export interface ActiveNavMatch {
  group: NavGroup
  item: NavItem
}

function itemMatches(path: string, item: NavItem): boolean {
  return path === item.href || path.startsWith(`${item.href}/`)
}

/**
 * Pure section mapper: returns the nav group/item whose `href` owns a path.
 *
 * This helper does not know which route shapes actually render, so it must only
 * be called with a path that `resolveRoute` has already resolved. `resolveRoute`
 * owns `isResolved`, and calls this helper only then; a fabricated deeper path
 * (for example `/triggers/a/b`) is not-found even though this bare mapper would
 * still match the `/triggers` section. Do not broaden this mapper to compensate.
 */
export function resolveActiveNav(pathname: string): ActiveNavMatch | null {
  // `/` is the system surface even though its canonical nav href is `/system`.
  const normalized = normalizePath(pathname)
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
