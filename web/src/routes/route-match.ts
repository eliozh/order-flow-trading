export interface RouteMatch {
  path: string
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
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

/** Collapse trailing slashes so `/watchlist/search/` is the same route. */
export function normalizePath(pathname: string): string {
  if (!pathname || pathname === '/') {
    return '/'
  }
  return pathname.replace(/\/+$/, '')
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
  return {
    path,
    inspectorSymbol,
    isSearch,
    isWatchlist: path === '/watchlist' || path.startsWith('/watchlist/'),
    isSystem: path === '/' || path === '/system' || path.startsWith('/system/'),
    isScans: path === '/scans' || path.startsWith('/scans/'),
    scanId: scanMatch !== null ? decodeSegment(scanMatch[1]) : null,
    isReports: path === '/reports' || path.startsWith('/reports/'),
    reportId: reportMatch !== null ? decodeSegment(reportMatch[1]) : null,
    // The list or exactly one detail segment; a deeper path is not an
    // opportunity route and must fall through to the not-found panel.
    isOpportunities: path === '/opportunities' || opportunityMatch !== null,
    opportunityId: opportunityMatch !== null ? decodeSegment(opportunityMatch[1]) : null,
    isTriggers: path === '/triggers',
    triggerId: triggerMatch !== null ? decodeSegment(triggerMatch[1]) : null,
    // Same rule as opportunities: only `/positions` or exactly one detail
    // segment is a position route.
    isPositions: path === '/positions' || positionMatch !== null,
    positionId: positionMatch !== null ? decodeSegment(positionMatch[1]) : null,
    isPaper: path === '/paper',
    // Only `/journal` or exactly one detail segment is a journal route; a
    // deeper path falls through to not-found.
    isJournal: path === '/journal' || journalMatch !== null,
    journalTradeId: journalMatch !== null ? decodeSegment(journalMatch[1]) : null,
  }
}
