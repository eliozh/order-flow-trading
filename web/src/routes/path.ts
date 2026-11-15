/**
 * The single URL-path normalizer shared by route resolution and navigation.
 *
 * Collapses trailing slashes so `/watchlist/search/` and `/watchlist/search`
 * are the same route, and maps empty or slash-only input (`''`, `/`, `///`)
 * to `/`. Keeping one implementation means the nav mapper and `resolveRoute`
 * can never diverge on edge inputs.
 */
export function normalizePath(pathname: string): string {
  if (!pathname) {
    return '/'
  }
  const trimmed = pathname.replace(/\/+$/, '')
  return trimmed === '' ? '/' : trimmed
}
