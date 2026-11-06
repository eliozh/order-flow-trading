import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  fetchSymbol,
  fetchSymbols,
  filterSymbols,
  groupSymbols,
  SymbolApiError,
  type SymbolContract,
  type SymbolListEnvelope,
} from './symbols'

const meta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T00:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'MISSING',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const emptyEnvelope: SymbolListEnvelope = {
  ok: true,
  data: { items: [], total: 0, query: null, group: null },
  error: null,
  meta,
}

const contract = (symbol: string, base: string, tradifi = false): SymbolContract => ({
  symbol,
  base_asset: base,
  quote_asset: 'USDT',
  contract_type: tradifi ? 'TRADIFI_PERPETUAL' : 'PERPETUAL',
  underlying_type: tradifi ? 'COMMODITY' : 'COIN',
  status: 'TRADING',
  tick_size: '0.10',
  step_size: '0.001',
  min_qty: '0.001',
  min_notional: '5.00',
  price_precision: 2,
  quantity_precision: 3,
  onboard_date: 1567900800000,
  tradifi,
  listed_date: '2019-09-08',
})

function stubFetch(body: unknown, status = 200): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('fetchSymbols', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('requests enough rows for the 942-symbol universe', async () => {
    const fetchMock = stubFetch(emptyEnvelope)

    await fetchSymbols()

    expect(String(fetchMock.mock.calls[0][0])).toContain('limit=2000')
  })

  it('preserves an explicit limit and server-side query', async () => {
    const fetchMock = stubFetch(emptyEnvelope)

    await fetchSymbols({ limit: 25, q: 'xau' })

    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('limit=25')
    expect(url).toContain('q=xau')
  })
})

describe('fetchSymbol', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('surfaces a NOT_FOUND envelope as a typed error', async () => {
    stubFetch(
      {
        ok: false,
        data: null,
        error: {
          code: 'NOT_FOUND',
          message: 'Symbol NOPEUSDT does not exist.',
          category: 'VALIDATION',
          http_status: 404,
          retryable: false,
          reference_id: 'REQ-TEST',
          details: [],
        },
        meta,
      },
      404,
    )

    const error = await fetchSymbol('NOPEUSDT').catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(SymbolApiError)
    expect((error as SymbolApiError).code).toBe('NOT_FOUND')
    expect((error as SymbolApiError).message).toBe('Symbol NOPEUSDT does not exist.')
  })
})

describe('filterSymbols', () => {
  const items = [
    contract('ETHUSDT', 'ETH'),
    contract('BTCUSDT', 'BTC'),
    contract('XAUUSDT', 'XAU', true),
  ]

  it('returns everything for an empty query', () => {
    expect(filterSymbols(items, '   ')).toHaveLength(3)
  })

  it('matches symbol and base asset case-insensitively', () => {
    expect(filterSymbols(items, 'xau').map((item) => item.symbol)).toEqual(['XAUUSDT'])
    expect(filterSymbols(items, 'usdt')).toHaveLength(3)
  })

  it('orders an exact symbol match first', () => {
    const withPrefix = [contract('BTCUSDT', 'BTC'), contract('BTC', 'BTC')]
    expect(filterSymbols(withPrefix, 'BTC').map((item) => item.symbol)).toEqual(['BTC', 'BTCUSDT'])
  })
})

describe('groupSymbols', () => {
  it('splits crypto and TradFi contracts', () => {
    const grouped = groupSymbols([contract('BTCUSDT', 'BTC'), contract('XAUUSDT', 'XAU', true)])

    expect(grouped.crypto.map((item) => item.symbol)).toEqual(['BTCUSDT'])
    expect(grouped.tradfi.map((item) => item.symbol)).toEqual(['XAUUSDT'])
  })
})
