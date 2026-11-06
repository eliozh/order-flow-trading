import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type {
  ApiMeta,
  SymbolContract,
  SymbolDetailEnvelope,
  SymbolListEnvelope,
} from '../../api/symbols'
import { SymbolInspectorPage } from '../symbol-inspector'
import { WatchlistPage } from '../watchlist'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T04:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const btc: SymbolContract = {
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
}

const eth: SymbolContract = {
  ...btc,
  symbol: 'ETHUSDT',
  base_asset: 'ETH',
  tick_size: '0.01',
  min_notional: '5.00',
}

const xau: SymbolContract = {
  ...btc,
  symbol: 'XAUUSDT',
  base_asset: 'XAU',
  contract_type: 'TRADIFI_PERPETUAL',
  underlying_type: 'COMMODITY',
  quantity_precision: 2,
  onboard_date: 1765411200000,
  tradifi: true,
  listed_date: '2025-12-11',
}

function detailEnvelope(contract: SymbolContract): SymbolDetailEnvelope {
  return {
    ok: true,
    data: {
      contract,
      coverage: {
        status: 'UNKNOWN',
        coverage_pct: null,
        history_start: contract.listed_date,
        book_ticker: 'N/A',
        liquidation_history: 'SINCE_DEPLOYMENT',
        latest_metric_at: null,
        notes: ['Coverage is measured after backfill; no backfill runs in this slice.'],
      },
    },
    error: null,
    meta,
  }
}

function listEnvelope(items: SymbolContract[]): SymbolListEnvelope {
  return {
    ok: true,
    data: { items, total: items.length, query: null, group: null },
    error: null,
    meta,
  }
}

describe('SymbolInspectorPage', () => {
  it('shows a loading state before the request settles', () => {
    render(
      <SymbolInspectorPage
        symbol="BTCUSDT"
        fetchSymbolFn={() => new Promise<SymbolDetailEnvelope>(() => {})}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent('Loading symbol')
    expect(screen.getByRole('heading', { name: 'Symbol BTCUSDT', level: 1 })).toBeInTheDocument()
  })

  it('renders contract type, tick/step, min notional, listing date, and coverage placeholders', async () => {
    render(
      <SymbolInspectorPage
        symbol="BTCUSDT"
        fetchSymbolFn={async () => detailEnvelope(btc)}
        isCompact={false}
      />,
    )

    expect(await screen.findByRole('heading', { name: /BTCUSDT/ })).toBeInTheDocument()
    expect(screen.getAllByText('PERPETUAL').length).toBeGreaterThan(0)
    expect(screen.getByText('0.10')).toBeInTheDocument()
    expect(screen.getAllByText('0.001').length).toBeGreaterThan(0)
    expect(screen.getByText('5.00')).toBeInTheDocument()
    expect(screen.getAllByText('2019-09-08').length).toBeGreaterThan(0)
    expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0)
    expect(screen.getByText('SINCE_DEPLOYMENT')).toBeInTheDocument()
    expect(screen.getByText('N/A')).toBeInTheDocument()
  })

  it('renders TradFi contract metadata as plain text', async () => {
    render(
      <SymbolInspectorPage
        symbol="XAUUSDT"
        fetchSymbolFn={async () => detailEnvelope(xau)}
        isCompact={false}
      />,
    )

    expect(await screen.findByRole('heading', { name: /XAUUSDT/ })).toBeInTheDocument()
    expect(screen.getAllByText('TRADIFI_PERPETUAL').length).toBeGreaterThan(0)
    expect(screen.getByText('COMMODITY')).toBeInTheDocument()
    expect(screen.getAllByText('2025-12-11').length).toBeGreaterThan(0)
  })

  it('marks the 375px inspector read-only', async () => {
    render(
      <SymbolInspectorPage
        symbol="BTCUSDT"
        fetchSymbolFn={async () => detailEnvelope(btc)}
        isCompact
      />,
    )

    expect(await screen.findByRole('note')).toHaveTextContent('Read-only at this width')
    expect(screen.getByRole('heading', { name: /BTCUSDT/ }).closest('section')).toHaveAttribute(
      'data-readonly',
      'true',
    )
  })

  it('renders metadata as text, never as raw HTML', async () => {
    const malicious: SymbolContract = {
      ...btc,
      symbol: '<img src=x onerror=alert(1)>',
      base_asset: '<script>alert(1)</script>',
    }

    const { container } = render(
      <SymbolInspectorPage
        symbol="EVILUSDT"
        fetchSymbolFn={async () => detailEnvelope(malicious)}
        isCompact={false}
      />,
    )

    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
  })

  it('renders an error state when the symbol is unknown', async () => {
    render(
      <SymbolInspectorPage
        symbol="NOPEUSDT"
        fetchSymbolFn={async () => {
          throw new Error('Symbol NOPEUSDT does not exist.')
        }}
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('NOPEUSDT does not exist')
  })

  it('keeps the page heading, focuses the alert, and offers retry/back on error', async () => {
    let calls = 0
    const fetcher = async (): Promise<SymbolDetailEnvelope> => {
      calls += 1
      throw new Error('daemon down')
    }
    render(<SymbolInspectorPage symbol="BTCUSDT" fetchSymbolFn={fetcher} isCompact={false} />)

    const alert = await screen.findByRole('alert')
    expect(screen.getByRole('heading', { name: 'Symbol BTCUSDT', level: 1 })).toBeInTheDocument()
    expect(alert).toHaveFocus()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls).toBe(2))
    expect(screen.getByRole('link', { name: 'Back to watchlist' })).toBeInTheDocument()
  })

  it('does not leave the previous contract on screen when the symbol changes', async () => {
    const fetcher = async (symbol: string): Promise<SymbolDetailEnvelope> =>
      symbol === 'BTCUSDT' ? detailEnvelope(btc) : detailEnvelope(xau)

    const { rerender } = render(
      <SymbolInspectorPage symbol="BTCUSDT" fetchSymbolFn={fetcher} isCompact={false} />,
    )
    expect(await screen.findByText('COIN')).toBeInTheDocument()

    rerender(<SymbolInspectorPage symbol="XAUUSDT" fetchSymbolFn={fetcher} isCompact={false} />)

    expect(await screen.findByText('COMMODITY')).toBeInTheDocument()
    expect(screen.queryByText('COIN')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Symbol XAUUSDT', level: 1 })).toBeInTheDocument()
  })
})

describe('WatchlistPage search', () => {
  it('renders grouped CRYPTO and TRADFI registries', async () => {
    render(
      <WatchlistPage
        fetchSymbolsFn={async () => listEnvelope([btc, eth, xau])}
        isCompact={false}
      />,
    )

    expect(await screen.findByRole('heading', { name: 'Watchlist' })).toBeInTheDocument()
    expect(screen.getByText('CRYPTO (2)')).toBeInTheDocument()
    expect(screen.getByText('TRADFI (1)')).toBeInTheDocument()
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
    expect(screen.getByText('XAUUSDT')).toBeInTheDocument()
  })

  it('finds a symbol by exact code and by substring', async () => {
    render(
      <WatchlistPage
        fetchSymbolsFn={async () => listEnvelope([btc, eth, xau])}
        isCompact={false}
      />,
    )
    const search = await screen.findByLabelText('Search symbols')

    fireEvent.change(search, { target: { value: 'xau' } })
    expect(screen.getByText('XAUUSDT')).toBeInTheDocument()
    expect(screen.queryByText('BTCUSDT')).not.toBeInTheDocument()

    fireEvent.change(search, { target: { value: 'USDT' } })
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
    expect(screen.getByText('ETHUSDT')).toBeInTheDocument()
    expect(screen.getByText('XAUUSDT')).toBeInTheDocument()

    fireEvent.change(search, { target: { value: 'BTCUSDT' } })
    const table = screen.getAllByRole('table')[0]
    const rows = within(table).getAllByRole('row')
    expect(within(rows[1]).getByText('BTCUSDT')).toBeInTheDocument()
  })

  it('does not render any star mutation control', async () => {
    render(
      <WatchlistPage
        fetchSymbolsFn={async () => listEnvelope([btc, eth, xau])}
        isCompact={false}
      />,
    )

    await screen.findByRole('heading', { name: 'Watchlist' })
    expect(screen.queryByRole('button', { name: /star/i })).not.toBeInTheDocument()
  })

  it('marks the 375px watchlist read-only', async () => {
    render(<WatchlistPage fetchSymbolsFn={async () => listEnvelope([btc])} isCompact />)

    expect(await screen.findByRole('note')).toHaveTextContent('Read-only at this width')
  })

  it('renders an empty registry without treating it as an error', async () => {
    render(<WatchlistPage fetchSymbolsFn={async () => listEnvelope([])} isCompact={false} />)

    expect(await screen.findByText(/No symbols in the registry/)).toBeInTheDocument()
  })

  it('uses a single live region and reports truncation with the server total', async () => {
    const many = Array.from({ length: 500 }, (_, index) => ({
      ...btc,
      symbol: `SYM${String(index).padStart(4, '0')}USDT`,
      base_asset: `SYM${index}`,
    }))
    render(
      <WatchlistPage
        fetchSymbolsFn={async () => ({
          ...listEnvelope(many),
          data: { items: many, total: 942, query: null, group: null },
        })}
        isCompact={false}
      />,
    )

    expect(await screen.findByText('Showing first 500 of 942 symbols.')).toBeInTheDocument()
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByText('500 of 942 symbols')).toBeInTheDocument()
  })

  it('sends the debounced search term to the server', async () => {
    const fetcher = vi.fn(async () => listEnvelope([xau]))
    render(<WatchlistPage fetchSymbolsFn={fetcher} isCompact={false} />)
    const search = await screen.findByLabelText('Search symbols')

    fireEvent.change(search, { target: { value: 'xau' } })

    await waitFor(() => expect(fetcher).toHaveBeenCalledWith({ q: 'xau' }))
  })

  it('renders a retryable error state', async () => {
    let calls = 0
    render(
      <WatchlistPage
        fetchSymbolsFn={async () => {
          calls += 1
          throw new Error('registry locked')
        }}
        isCompact={false}
      />,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('registry locked')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls).toBe(2))
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })
})
