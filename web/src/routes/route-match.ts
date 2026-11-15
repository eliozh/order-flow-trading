import { resolveActiveNav, type NavGroupId, type NavItemId } from './nav-items'
import { normalizePath } from './path'

// Re-exported so existing callers (App tests, consumers) keep importing the
// single shared normalizer from the route module.
export { normalizePath }

export interface RouteMatch {
  path: string
  /** Active group and item from the shared navigation source, if any. */
  activeGroup: NavGroupId | null
  activeNavItem: NavItemId | null
  inspectorSymbol: string | null
  isWatchlist: boolean
  isSearch: boolean
  isSystem: boolean
  isScans: boolean
  scanId: string | null
  isReports: boolean
  reportId: string | null
  isOpportunities: boolean
  opportunityId: string | null
  isTriggers: boolean
  triggerId: string | null
  isPositions: boolean
  positionId: string | null
  isPaper: boolean
  isJournal: boolean
  journalTradeId: string | null
  isSettings: boolean
  isSettingsScan: boolean
  isSettingsRisk: boolean
  isSettingsBudget: boolean
  isSettingsLlm: boolean
  isSettingsPrompts: boolean
  isSettingsPromptDetail: boolean
  isSettingsPromptHistory: boolean
  promptId: string | null
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function resolveRoute(pathname: string): RouteMatch {
  const path = normalizePath(pathname)
  const isSearch = path === '/watchlist/search'
  const symbolMatch = /^\/watchlist\/([^/]+)$/.exec(path)
  const inspectorSymbol = symbolMatch !== null && !isSearch ? decodeSegment(symbolMatch[1]) : null
  const scanMatch = /^\/scans\/([^/]+)$/.exec(path)
  const reportMatch = /^\/reports\/([^/]+)$/.exec(path)
  const opportunityMatch = /^\/opportunities\/([^/]+)$/.exec(path)
  const triggerMatch = /^\/triggers\/([^/]+)$/.exec(path)
  const positionMatch = /^\/positions\/([^/]+)$/.exec(path)
  const journalMatch = /^\/journal\/([^/]+)$/.exec(path)
  const isScans = path === '/scans' || path.startsWith('/scans/')
  const isReports = path === '/reports' || path.startsWith('/reports/')
  // The list or exactly one detail segment; a deeper path is not an
  // opportunity route and must fall through to the not-found panel.
  const isOpportunities = path === '/opportunities' || opportunityMatch !== null
  const isTriggers = path === '/triggers'
  // Same rule as opportunities: only `/positions` or exactly one detail
  // segment is a position route.
  const isPositions = path === '/positions' || positionMatch !== null
  const isPaper = path === '/paper'
  // Only `/journal` or exactly one detail segment is a journal route; a
  // deeper path falls through to not-found.
  const isJournal = path === '/journal' || journalMatch !== null
  // The shell, scan, and risk sections exist; any deeper settings path falls
  // through to not-found until its slice lands.
  const isSettings = path === '/settings'
  const isSettingsScan = path === '/settings/scan'
  const isSettingsRisk = path === '/settings/risk'
  const isSettingsBudget = path === '/settings/budget'
  const isSettingsLlm = path === '/settings/llm'
  const isSettingsPrompts = path === '/settings/prompts'
  // One detail segment, or exactly one detail segment plus `/history`. A
  // deeper path falls through to the not-found panel.
  const promptDetailMatch = /^\/settings\/prompts\/([^/]+)$/.exec(path)
  const promptHistoryMatch = /^\/settings\/prompts\/([^/]+)\/history$/.exec(path)
  const isSettingsPromptDetail = promptDetailMatch !== null
  const isSettingsPromptHistory = promptHistoryMatch !== null
  const promptId =
    promptHistoryMatch !== null
      ? decodeSegment(promptHistoryMatch[1])
      : promptDetailMatch !== null
        ? decodeSegment(promptDetailMatch[1])
        : null
  const isWatchlist = path === '/watchlist' || path.startsWith('/watchlist/')
  const isSystem = path === '/' || path === '/system' || path.startsWith('/system/')

  // The App renders the not-found panel exactly when none of these route
  // shapes match. Derive the active nav item from the same resolution so a
  // fabricated deeper path never announces a section that is not displayed.
  const isResolved =
    inspectorSymbol !== null ||
    scanMatch !== null ||
    reportMatch !== null ||
    opportunityMatch !== null ||
    triggerMatch !== null ||
    positionMatch !== null ||
    journalMatch !== null ||
    isScans ||
    isReports ||
    isOpportunities ||
    isTriggers ||
    isPositions ||
    isJournal ||
    isPaper ||
    isWatchlist ||
    isSettingsScan ||
    isSettingsRisk ||
    isSettingsBudget ||
    isSettingsLlm ||
    isSettingsPrompts ||
    isSettingsPromptDetail ||
    isSettingsPromptHistory ||
    isSettings ||
    isSystem
  const activeNav = isResolved ? resolveActiveNav(path) : null
  return {
    path,
    activeGroup: activeNav?.group.id ?? null,
    activeNavItem: activeNav?.item.id ?? null,
    inspectorSymbol,
    isSearch,
    isWatchlist,
    isSystem,
    isScans,
    scanId: scanMatch !== null ? decodeSegment(scanMatch[1]) : null,
    isReports,
    reportId: reportMatch !== null ? decodeSegment(reportMatch[1]) : null,
    isOpportunities,
    opportunityId: opportunityMatch !== null ? decodeSegment(opportunityMatch[1]) : null,
    isTriggers,
    triggerId: triggerMatch !== null ? decodeSegment(triggerMatch[1]) : null,
    isPositions,
    positionId: positionMatch !== null ? decodeSegment(positionMatch[1]) : null,
    isPaper,
    isJournal,
    journalTradeId: journalMatch !== null ? decodeSegment(journalMatch[1]) : null,
    isSettings,
    isSettingsScan,
    isSettingsRisk,
    isSettingsBudget,
    isSettingsLlm,
    isSettingsPrompts,
    isSettingsPromptDetail,
    isSettingsPromptHistory,
    promptId,
  }
}
