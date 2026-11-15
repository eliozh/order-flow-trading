import { useEffect, type MouseEvent, type ReactNode } from 'react'

import { NAV_GROUPS, type NavIconName, type NavItem, type NavItemId } from '../routes/nav-items'

/**
 * Session flag written when a nav link starts a full-page navigation and
 * consumed once by `RouteFocus` on the destination page. The app has no client
 * router, so sessionStorage is the only channel that survives the reload.
 */
export const NAV_FOCUS_KEY = 'oft.nav-focus'

const COMPACT_NOTE_ZH =
  '移动端为只读界面（Kill switch 除外）。主入口为 Telegram；编辑请在桌面端或 CLI 完成。'
const COMPACT_NOTE_EN = 'Use a desktop viewport or the CLI for actions.'

const ICONS: Record<NavIconName, ReactNode> = {
  watch: (
    <>
      <path d="M2 8s2.4-4 6-4 6 4 6 4-2.4 4-6 4-6-4-6-4Z" />
      <circle cx="8" cy="8" r="1.8" />
    </>
  ),
  scan: <path d="M2 8h3l1.5-4 3 8L11 8h3" />,
  report: (
    <>
      <path d="M4 2h6l2 2v10H4z" />
      <path d="M6 6h4M6 9h4M6 12h2" />
    </>
  ),
  opp: (
    <>
      <path d="M2 12l4-4 3 2 5-6" />
      <path d="M10 4h4v4" />
    </>
  ),
  trigger: (
    <>
      <path d="M8 2v5M8 11v3" />
      <circle cx="8" cy="8" r="1.5" />
    </>
  ),
  pos: (
    <>
      <rect x="2" y="4" width="12" height="8" />
      <path d="M4 8h8" />
    </>
  ),
  paper: (
    <>
      <path d="M3 4h10v9H3z" />
      <path d="M3 7h10M6 10h1" />
    </>
  ),
  journal: (
    <>
      <path d="M4 2h7l1 1v11H4z" />
      <path d="M6 5h4M6 8h4M6 11h2" />
    </>
  ),
  settings: (
    <>
      <circle cx="8" cy="8" r="2" />
      <path d="M8 2v2M8 12v2M2 8h2M12 8h2M4 4l1.5 1.5M10.5 10.5L12 12M12 4l-1.5 1.5M5.5 10.5L4 12" />
    </>
  ),
  system: (
    <>
      <path d="M2 3h12v4H2zM2 9h12v4H2z" />
      <path d="M4 5h1M4 11h1" />
    </>
  ),
}

function NavIcon({ name }: { name: NavIconName }) {
  return (
    <span className="nav-ico" aria-hidden="true">
      <svg viewBox="0 0 16 16" focusable="false">
        {ICONS[name]}
      </svg>
    </span>
  )
}

function markNavFocus(): void {
  try {
    sessionStorage.setItem(NAV_FOCUS_KEY, '1')
  } catch {
    // Storage can be unavailable (private mode); focus-on-route-change is a
    // progressive enhancement, never a navigation blocker.
  }
}

function consumeNavFocus(): boolean {
  try {
    if (sessionStorage.getItem(NAV_FOCUS_KEY) === null) {
      return false
    }
    sessionStorage.removeItem(NAV_FOCUS_KEY)
    return true
  } catch {
    return false
  }
}

function handleNavClick(event: MouseEvent<HTMLAnchorElement>): void {
  // Modified clicks open a new tab/window where this page's one-shot flag must
  // not leak into the current tab.
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return
  }
  markNavFocus()
}

function focusSkipTarget(id: string): (event: MouseEvent<HTMLAnchorElement>) => void {
  return (event) => {
    const target = document.getElementById(id)
    if (!target) {
      return
    }
    event.preventDefault()
    target.focus()
  }
}

/**
 * Skip links are the first tab stops on every route (DESIGN A2.2). The targets
 * are programmatically focusable and receive focus when the links activate.
 */
export function SkipLinks() {
  return (
    <div className="skip-links">
      <a className="skip-link" href="#app-main" onClick={focusSkipTarget('app-main')}>
        跳到主内容
      </a>
      <a className="skip-link skip-link--nav" href="#app-nav" onClick={focusSkipTarget('app-nav')}>
        跳到导航
      </a>
    </div>
  )
}

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
  return (
    <li>
      <a
        className="nav-link"
        href={item.href}
        aria-current={active ? 'page' : undefined}
        title={item.label}
        onClick={handleNavClick}
      >
        <NavIcon name={item.icon} />
        <span className="nav-label">
          {item.segments
            ? item.segments.map((segment, index) =>
                segment.lang ? (
                  <span key={`${segment.lang}-${index}`} lang={segment.lang}>
                    {segment.text}
                  </span>
                ) : (
                  segment.text
                ),
              )
            : item.label}
        </span>
      </a>
    </li>
  )
}

interface NavGroupsProps {
  activeItemId: NavItemId | null
  /**
   * Appended to each group heading id. Compact and rail navs can coexist in the
   * accessibility tree during breakpoint transitions, so the ids must be unique.
   */
  labelSuffix?: string
  /** Compact groups render horizontal sections with the modifier class. */
  compact?: boolean
}

/** One render tree for the desktop/rail/compact navigation groups. */
function NavGroups({ activeItemId, labelSuffix = '', compact = false }: NavGroupsProps) {
  return (
    <>
      {NAV_GROUPS.map((group) => {
        const labelId = `nav-group-${group.id}${labelSuffix}`
        return (
          <section
            className={compact ? 'nav-group nav-group--compact' : 'nav-group'}
            key={group.id}
          >
            <h2 className="nav-group-label" id={labelId}>
              {group.label}
            </h2>
            {/* Safari/VoiceOver drops list semantics for `list-style: none`, so
                the explicit role is intentional (WCAG 1.3.1). */}
            {/* eslint-disable-next-line jsx-a11y/no-redundant-roles */}
            <ul className="nav-list" role="list" aria-labelledby={labelId}>
              {group.items.map((item) => (
                <NavLink key={item.id} item={item} active={activeItemId === item.id} />
              ))}
            </ul>
          </section>
        )
      })}
    </>
  )
}

interface NavProps {
  /**
   * The resolved current item from `resolveRoute`. It is `null` on a not-found
   * path, so no link claims `aria-current="page"` for a page that is not shown.
   */
  activeItemId: NavItemId | null
  /** 375px read-only grouped navigation. */
  compact?: boolean
}

/**
 * The persistent navigation landmark. The desktop sidebar (208px), the 56px
 * icon rail, and the 375px compact navigation all render the same groups from
 * `nav-items.ts`; only CSS changes the presentation. Exactly one link ever
 * carries `aria-current="page"`, and a not-found path carries none.
 */
export function Nav({ activeItemId, compact = false }: NavProps) {
  if (compact) {
    return (
      <nav id="app-nav" className="nav nav--compact" aria-label="主导航" tabIndex={-1}>
        <p className="nav-compact-note" role="note">
          <strong>{COMPACT_NOTE_ZH}</strong> <span lang="en">{COMPACT_NOTE_EN}</span>
        </p>
        <div className="nav-compact-groups">
          <NavGroups activeItemId={activeItemId} labelSuffix="-compact" compact />
        </div>
      </nav>
    )
  }

  return (
    <nav id="app-nav" className="nav nav--rail" aria-label="主导航" tabIndex={-1}>
      <NavGroups activeItemId={activeItemId} />
    </nav>
  )
}

/**
 * Consumes the one-shot nav flag after a full-page navigation and focuses a
 * meaningful destination target. Precedence: the destination `h1`, then the
 * always-present and programmatically focusable `#app-main`, and only as a last
 * resort the dedicated shell live region. Taking `#app-main` when no h1 exists
 * synchronously keeps `/` and `/system` (whose loading/error branches render no
 * heading at mount) from focusing the permanently empty announcer.
 * Never installs a global key handler and never traps focus.
 */
export function RouteFocus() {
  useEffect(() => {
    if (!consumeNavFocus()) {
      return
    }
    const main = document.getElementById('app-main')
    const heading =
      main?.querySelector<HTMLElement>('h1') ?? document.querySelector<HTMLElement>('main h1')
    const target = heading ?? main ?? document.getElementById('route-announcer')
    if (!target) {
      return
    }
    if (!target.hasAttribute('tabindex')) {
      target.setAttribute('tabindex', '-1')
    }
    target.focus()
  }, [])

  return (
    <div
      id="route-announcer"
      className="visually-hidden"
      role="status"
      aria-live="polite"
      tabIndex={-1}
    />
  )
}
