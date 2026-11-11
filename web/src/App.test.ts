import { describe, expect, it } from 'vitest'

import { normalizePath, resolveRoute } from './routes/route-match'

describe('resolveRoute', () => {
  it('normalizes trailing slashes', () => {
    expect(normalizePath('/watchlist/search/')).toBe('/watchlist/search')
    expect(normalizePath('/')).toBe('/')
    expect(normalizePath('/watchlist/BTCUSDT///')).toBe('/watchlist/BTCUSDT')
  })

  it('routes /watchlist/search/ to the watchlist search surface', () => {
    const route = resolveRoute('/watchlist/search/')

    expect(route.isSearch).toBe(true)
    expect(route.isWatchlist).toBe(true)
    expect(route.inspectorSymbol).toBeNull()
  })

  it('routes a trailing-slash symbol path to the inspector', () => {
    const route = resolveRoute('/watchlist/BTCUSDT/')

    expect(route.inspectorSymbol).toBe('BTCUSDT')
    expect(route.isWatchlist).toBe(true)
  })

  it('keeps system routes on the system surface', () => {
    expect(resolveRoute('/system').isSystem).toBe(true)
    expect(resolveRoute('/').isSystem).toBe(true)
  })

  it('routes the opportunity ledger list', () => {
    const route = resolveRoute('/opportunities/')

    expect(route.isOpportunities).toBe(true)
    expect(route.opportunityId).toBeNull()
  })

  it('routes an opportunity detail id and decodes it', () => {
    const route = resolveRoute('/opportunities/OPP%2D1')

    expect(route.isOpportunities).toBe(true)
    expect(route.opportunityId).toBe('OPP-1')
  })

  it('does not treat an opportunity detail as a report or scan', () => {
    const route = resolveRoute('/opportunities/OPP-1')

    expect(route.isReports).toBe(false)
    expect(route.reportId).toBeNull()
    expect(route.isScans).toBe(false)
    expect(route.scanId).toBeNull()
  })

  it('treats only the exact list path as the ledger list', () => {
    expect(resolveRoute('/opportunities').isOpportunities).toBe(true)
    // A stale or fabricated deeper path must render the not-found panel, not
    // silently fall back to the list.
    const deep = resolveRoute('/opportunities/OPP-1/extra')
    expect(deep.isOpportunities).toBe(false)
    expect(deep.opportunityId).toBeNull()
  })
})
