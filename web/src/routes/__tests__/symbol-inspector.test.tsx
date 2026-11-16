import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type {
  ApiMeta,
  SymbolContract,
  SymbolDetailEnvelope,
} from '../../api/symbols'
import type { ReportSummary } from '../../api/reports'
import type {
  StarEnvelope,
  WatchlistEnvelope,
  WatchlistRow,
} from '../../api/watchlist'
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

function latestReport(): ReportSummary {
  return {
    report_id: 'RPT-BTCUSDT-1',
    scan_id: 'SCAN-1',
    symbol: 'BTCUSDT',
    timeframe: '4h',
    generated_at: '2026-11-06T04:00:00.000Z',
    data_quality: 'PARTIAL',
    llm_decision_id: null,
    narrative_ref: null,
    llm_stage: 'SKIPPED',
    llm_tokens: 0,
    cost_usd: '0',
    cost_unpriced: false,
    llm: { configured: false, validation_status: null, opportunity_ids: [] },
  }
}

function watchlistRow(contract: SymbolContract, starred: boolean): WatchlistRow {
  return {
    ...contract,
    starred,
    starred_at: starred ? '2026-11-06T04:00:00.000Z' : null,
  }
}

function watchlistEnvelope(rows: WatchlistRow[], total = rows.length): WatchlistEnvelope {
  return {
    ok: true,
    data: {
      groups: [],
      total,
      rows,
      next_scan_count: rows.filter((item) => item.starred).length,
      truncated: false,
      star_mutation: true,
    },
    error: null,
    meta,
  }
}

function starEnvelope(symbol: string, starred: boolean): StarEnvelope {
  return {
    ok: true,
    data: {
      symbol,
      starred,
      starred_at: starred ? '2026-11-06T04:00:00.000Z' : null,
    },
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

  it('links to the latest report chart when a report exists', async () => {
    render(
      <SymbolInspectorPage
        symbol="BTCUSDT"
        fetchSymbolFn={async () => detailEnvelope(btc)}
        fetchLatestReportFn={async () => latestReport()}
        isCompact={false}
      />,
    )

    const link = await screen.findByRole('link', { name: /Open latest report chart/i })
    expect(link).toHaveAttribute('href', '/reports/RPT-BTCUSDT-1/chart')
  })

  it('renders no chart link when no report exists yet', async () => {
    render(
      <SymbolInspectorPage
        symbol="BTCUSDT"
        fetchSymbolFn={async () => detailEnvelope(btc)}
        fetchLatestReportFn={async () => null}
        isCompact={false}
      />,
    )

    await screen.findByRole('heading', { name: 'Symbol BTCUSDT', level: 1 })
    expect(screen.queryByRole('link', { name: /Open latest report chart/i })).not.toBeInTheDocument()
  })
})

// Migrated from the deleted legacy registry view: these cases now exercise the
// star-configured watchlist through ``fetchWatchlistFn``. The coverage is
// preserved (grouping, search filtering, narrow read-only, empty state, one
// live region); the obsolete "no star control" and server-truncation cases are
// replaced by star-toggle and rendered-count assertions.
describe('WatchlistPage search', () => {
  const baseRows = [watchlistRow(btc, true), watchlistRow(eth, true), watchlistRow(xau, true)]

  it('renders grouped CRYPTO and TRADFI registries', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope(baseRows)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow={false}
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
        fetchWatchlistFn={async () => watchlistEnvelope(baseRows)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow={false}
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

  it('renders star toggles with the row star state', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([watchlistRow(btc, true), watchlistRow(eth, false), watchlistRow(xau, true)])
        }
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow={false}
      />,
    )

    const toggles = await screen.findAllByRole('button', { name: /in next scan/ })
    expect(toggles).toHaveLength(2)
    expect(toggles[0]).toHaveAttribute('aria-pressed', 'true')
    expect(toggles[1]).toHaveAttribute('aria-pressed', 'true')
  })

  it('marks the 375px watchlist read-only', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope([watchlistRow(btc, true)])}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow
      />,
    )

    expect(await screen.findByRole('note')).toHaveTextContent('Read-only at this width')
  })

  it('renders an empty watchlist without treating it as an error', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope([watchlistRow(eth, false)])}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow={false}
      />,
    )

    expect(
      await screen.findByRole('heading', { name: 'No symbols in the next scan' }),
    ).toBeInTheDocument()
  })

  it('uses one live region and renders the count from the starred rows', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([watchlistRow(btc, true), watchlistRow(eth, true), watchlistRow(xau, false)], 3)
        }
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow={false}
      />,
    )

    expect(await screen.findByText('2 of 3 symbols included in next scan')).toBeInTheDocument()
    expect(screen.getAllByRole('status')).toHaveLength(1)
    // The rendered table carries exactly the two starred rows.
    expect(screen.getAllByRole('button', { name: /in next scan/ })).toHaveLength(2)
    expect(screen.queryByText('XAUUSDT')).not.toBeInTheDocument()
  })

  it('filters locally without refetching the watchlist', async () => {
    const fetcher = vi.fn(async () => watchlistEnvelope(baseRows))
    render(
      <WatchlistPage
        fetchWatchlistFn={fetcher}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow={false}
      />,
    )
    const search = await screen.findByLabelText('Search symbols')

    fireEvent.change(search, { target: { value: 'xau' } })

    expect(screen.getByText('XAUUSDT')).toBeInTheDocument()
    // Filtering is client-side; the removed server-side debounced read is gone.
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
  })

  it('renders a retryable error state', async () => {
    let calls = 0
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => {
          calls += 1
          throw new Error('registry locked')
        }}
        isNarrow={false}
      />,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('registry locked')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls).toBe(2))
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })
})

