export interface RouteMatch {
  path: string
  inspectorSymbol: string | null
  isWatchlist: boolean
  isSearch: boolean
  isSystem: boolean
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
  return {
    path,
    inspectorSymbol,
    isSearch,
    isWatchlist: path === '/watchlist' || path.startsWith('/watchlist/'),
    isSystem: path === '/' || path === '/system' || path.startsWith('/system/'),
  }
}
