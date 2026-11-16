import { describe, expect, it } from 'vitest'

import { normalizePath, resolveRoute } from './routes/route-match'

describe('resolveRoute', () => {
  it('normalizes trailing slashes', () => {
    expect(normalizePath('/watchlist/search/')).toBe('/watchlist/search')
    expect(normalizePath('/')).toBe('/')
    expect(normalizePath('/watchlist/BTCUSDT///')).toBe('/watchlist/BTCUSDT')
  })

  it('normalizes empty and slash-only paths to the root', () => {
    expect(normalizePath('')).toBe('/')
    expect(normalizePath('/')).toBe('/')
    expect(normalizePath('///')).toBe('/')
    expect(normalizePath('/watchlist/')).toBe('/watchlist')
    expect(resolveRoute('///').isSystem).toBe(true)
  })

  it('routes /watchlist/search/ to the watchlist search surface', () => {
    const route = resolveRoute('/watchlist/search/')

    expect(route.isSearch).toBe(true)
    expect(route.isWatchlist).toBe(true)
    expect(route.inspectorSymbol).toBeNull()
  })

  it('keeps one search flag and resolves the search path ahead of the table', () => {
    const search = resolveRoute('/watchlist/search')
    const table = resolveRoute('/watchlist')

    // The search route is a watchlist path too; App must branch on isSearch
    // first. There is no second, duplicate flag to keep in sync.
    expect(search.isSearch).toBe(true)
    expect(search.isWatchlist).toBe(true)
    expect(search.inspectorSymbol).toBeNull()
    expect(table.isSearch).toBe(false)
    expect(table.isWatchlist).toBe(true)
    expect('isWatchlistSearch' in search).toBe(false)
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

  it('routes the journal list and exactly one trade detail segment', () => {
    const list = resolveRoute('/journal')
    expect(list.isJournal).toBe(true)
    expect(list.journalTradeId).toBeNull()

    const detail = resolveRoute('/journal/TRD%2D1')
    expect(detail.isJournal).toBe(true)
    expect(detail.journalTradeId).toBe('TRD-1')

    // A deeper path is not a journal route; it falls through to not-found.
    const deep = resolveRoute('/journal/TRD-1/extra')
    expect(deep.isJournal).toBe(false)
    expect(deep.journalTradeId).toBeNull()
  })

  it('keeps the journal route distinct from paper and positions', () => {
    expect(resolveRoute('/journal').isPaper).toBe(false)
    expect(resolveRoute('/journal').isPositions).toBe(false)
    expect(resolveRoute('/paper').isJournal).toBe(false)
    expect(resolveRoute('/positions/POS-1').isJournal).toBe(false)
  })

  it('routes the chart panel without a report detail id', () => {
    const route = resolveRoute('/reports/RPT-1/chart')

    expect(route.isChartPanel).toBe(true)
    expect(route.chartReportId).toBe('RPT-1')
    expect(route.reportId).toBeNull()
  })

  it('decodes the chart report id', () => {
    const route = resolveRoute('/reports/RPT%2D1/chart')

    expect(route.isChartPanel).toBe(true)
    expect(route.chartReportId).toBe('RPT-1')
  })

  it('does not treat a deeper reports path as the chart panel', () => {
    const route = resolveRoute('/reports/RPT-1/chart/extra')

    expect(route.isChartPanel).toBe(false)
    expect(route.chartReportId).toBeNull()
    expect(route.reportId).toBeNull()
  })
})
