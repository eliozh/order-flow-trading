import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../status'
import {
  fetchWatchlist,
  setStar,
  WatchlistApiError,
  type WatchlistRow,
} from '../watchlist'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-07T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const row: WatchlistRow = {
  symbol: 'BTCUSDT',
  base_asset: 'BTC',
  quote_asset: 'USDT',
  contract_type: 'PERPETUAL',
  underlying_type: 'COIN',
  status: 'TRADING',
  tick_size: '0.10',
  step_size: '0.001',
  min_qty: '0.001',
  min_notional: '5.00',
  price_precision: 2,
  quantity_precision: 3,
  onboard_date: 1567900800000,
  tradifi: false,
  listed_date: '2019-09-08',
  starred: true,
  starred_at: '2026-11-07T12:00:00.000Z',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fetchMockReturning(response: Response | (() => Response)): ReturnType<typeof vi.fn> {
  return vi.fn(async () => (typeof response === 'function' ? response() : response))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchWatchlist', () => {
  it('GETs the watchlist envelope with same-origin credentials', async () => {
    const fetchMock = fetchMockReturning(
      jsonResponse({
        ok: true,
        data: {
          groups: [],
          total: 1,
          rows: [row],
          starred_count: 1,
          next_scan_count: 1,
          star_mutation: true,
        },
        error: null,
        meta,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchWatchlist()

    expect(envelope.data?.rows).toEqual([row])
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/watchlist')
    expect(init.method).toBe('GET')
    expect(init.credentials).toBe('same-origin')
  })
})

describe('setStar', () => {
  it('uses POST to star and DELETE to unstar', async () => {
    const fetchMock = fetchMockReturning(
      () =>
        jsonResponse({
          ok: true,
          data: { symbol: 'BTCUSDT', starred: true, starred_at: null },
          error: null,
          meta,
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await setStar('BTCUSDT', true)
    await setStar('BTCUSDT', false)

    const starInit = fetchMock.mock.calls[0]?.[1] as RequestInit
    const unstarInit = fetchMock.mock.calls[1]?.[1] as RequestInit
    expect(starInit.method).toBe('POST')
    expect(unstarInit.method).toBe('DELETE')
  })

  it('encodes the symbol into the path', async () => {
    const fetchMock = fetchMockReturning(
      jsonResponse({
        ok: true,
        data: { symbol: 'BTC/USDT', starred: true, starred_at: null },
        error: null,
        meta,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await setStar('BTC/USDT', true)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/watchlist/BTC%2FUSDT')
  })

  it('maps a 404 envelope to a typed NOT_FOUND error', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        jsonResponse(
          {
            ok: false,
            data: null,
            error: {
              code: 'NOT_FOUND',
              message: 'Symbol NOPEUSDT does not exist.',
            },
            meta,
          },
          404,
        ),
      ),
    )

    const failure = await setStar('NOPEUSDT', true).catch((cause: unknown) => cause)

    expect(failure).toBeInstanceOf(WatchlistApiError)
    expect(failure).toMatchObject({
      name: 'WatchlistApiError',
      code: 'NOT_FOUND',
      message: 'Symbol NOPEUSDT does not exist.',
    })
  })

  it('reports a non-JSON body with the HTTP status and no error code', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(new Response('<html>gateway</html>', { status: 502 })),
    )

    const failure = await setStar('BTCUSDT', true).catch((cause: unknown) => cause)

    expect(failure).toBeInstanceOf(WatchlistApiError)
    expect(failure).toMatchObject({
      code: undefined,
      message: 'Watchlist request failed with HTTP 502',
    })
  })
})
