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
})
