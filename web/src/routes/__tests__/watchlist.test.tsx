import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { RequestTimeoutError } from '../../api/request-timeout'
import type { ApiMeta } from '../../api/status'
import type { SymbolContract } from '../../api/symbols'
import {
  WatchlistApiError,
  type StarEnvelope,
  type WatchlistEnvelope,
  type WatchlistRow,
} from '../../api/watchlist'
import { isUnknownStarOutcome } from '../../hooks/use-star-mutation'
import { WatchlistPage } from '../watchlist'
import { WatchlistSearchPage } from '../watchlist-search'

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

function contract(symbol: string, overrides: Partial<SymbolContract> = {}): SymbolContract {
  const tradifi = symbol === 'XAUUSDT'
  return {
    symbol,
    base_asset: symbol.replace('USDT', ''),
    quote_asset: 'USDT',
    contract_type: tradifi ? 'TRADIFI_PERPETUAL' : 'PERPETUAL',
    underlying_type: tradifi ? null : 'COIN',
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
    ...overrides,
  }
}

function row(symbol: string, starred: boolean, overrides: Partial<WatchlistRow> = {}): WatchlistRow {
  return {
    ...contract(symbol),
    starred,
    starred_at: starred ? '2026-11-07T12:00:00.000Z' : null,
    ...overrides,
  }
}

function watchlistEnvelope(rows: WatchlistRow[], total = rows.length): WatchlistEnvelope {
  return {
    ok: true,
    error: null,
    meta,
    data: {
      groups: [],
      total,
      rows,
      starred_count: rows.filter((item) => item.starred).length,
      next_scan_count: rows.filter((item) => item.starred).length,
      star_mutation: true,
    },
  }
}

function starEnvelope(symbol: string, starred: boolean): StarEnvelope {
  return {
    ok: true,
    error: null,
    meta,
    data: {
      symbol,
      starred,
      starred_at: starred ? '2026-11-07T12:00:00.000Z' : null,
    },
  }
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (cause: unknown) => void
} {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

const baseRows = [row('BTCUSDT', true), row('ETHUSDT', false), row('XAUUSDT', true)]

describe('isUnknownStarOutcome', () => {
  it('classifies a timeout or network failure as unknown, not a refusal', () => {
    expect(isUnknownStarOutcome(new RequestTimeoutError())).toBe(true)
    expect(isUnknownStarOutcome(new TypeError('Failed to fetch'))).toBe(true)
    expect(isUnknownStarOutcome(new WatchlistApiError('nope', 'NOT_FOUND'))).toBe(false)
  })
})

describe('WatchlistPage stars', () => {
  it('shows only starred rows grouped by Crypto/TradFi with the next-scan count', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope(baseRows, 3)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByRole('heading', { name: 'CRYPTO (1)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'TRADFI (1)' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'BTCUSDT' })).toHaveAttribute(
      'href',
      '/watchlist/BTCUSDT',
    )
    expect(screen.queryByRole('link', { name: 'ETHUSDT' })).not.toBeInTheDocument()
    expect(screen.getByText('2 of 3 symbols included in next scan')).toBeInTheDocument()
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('offers the add-from-search link while rows are already starred', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope([row('BTCUSDT', true)], 1)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByRole('button', { name: 'BTCUSDT in next scan' })).toBeInTheDocument()
    // A populated watchlist still has an in-app path to the add flow; the
    // empty state is not the only route in.
    expect(screen.getByRole('link', { name: 'Search symbols' })).toHaveAttribute(
      'href',
      '/watchlist/search',
    )
  })

  it('flips optimistically, blocks a second request, then removes the row', async () => {
    const pending = deferred<StarEnvelope>()
    const setStarFn = vi.fn(() => pending.promise)
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope([row('BTCUSDT', true)], 1)}
        setStarFn={setStarFn}
      />,
    )

    const toggle = await screen.findByRole('button', { name: 'BTCUSDT in next scan' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(toggle)
    fireEvent.click(toggle)

    // One request: the pending toggle is aria-disabled, not natively disabled,
    // so it keeps focus while the guarded click ignores the repeat.
    expect(setStarFn).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-busy',
      'true',
    )
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toBeEnabled()
    expect(screen.getByText('0 of 1 symbols included in next scan')).toBeInTheDocument()

    await act(async () => {
      pending.resolve(starEnvelope('BTCUSDT', false))
    })
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'BTCUSDT in next scan' }),
      ).not.toBeInTheDocument(),
    )
    expect(screen.getByTestId('watchlist-live')).toHaveTextContent(
      'BTCUSDT removed from next scan.',
    )
  })

  it('moves focus to the next star toggle after a row is removed', async () => {
    const pending = deferred<StarEnvelope>()
    render(
      <WatchlistPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([row('BTCUSDT', true), row('ETHUSDT', true)], 2)
        }
        setStarFn={() => pending.promise}
      />,
    )

    const btc = await screen.findByRole('button', { name: 'BTCUSDT in next scan' })
    btc.focus()
    expect(btc).toHaveFocus()
    fireEvent.click(btc)

    await act(async () => {
      pending.resolve(starEnvelope('BTCUSDT', false))
    })

    const eth = screen.getByRole('button', { name: 'ETHUSDT in next scan' })
    await waitFor(() => expect(eth).toHaveFocus())
  })

  it('rolls back visibly and announces when the mutation is refused', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope([row('BTCUSDT', true)], 1)}
        setStarFn={async () => {
          throw new WatchlistApiError('Symbol BTCUSDT does not exist.', 'NOT_FOUND')
        }}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not remove BTCUSDT: Symbol BTCUSDT does not exist.',
    )
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('1 of 1 symbols included in next scan')).toBeInTheDocument()
    // A refusal is announced once, through the visible alert only.
    expect(screen.getByTestId('watchlist-live')).not.toHaveTextContent('was not removed')
  })

  it('refetches instead of rolling back when the outcome is unknown', async () => {
    const fetchWatchlistFn = vi.fn(async () => watchlistEnvelope([row('BTCUSDT', true)], 1))
    render(
      <WatchlistPage
        fetchWatchlistFn={fetchWatchlistFn}
        setStarFn={async () => {
          throw new RequestTimeoutError()
        }}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not confirm the BTCUSDT change; refreshing the watchlist.',
    )
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))
    // The server state reconciles the optimistic flip; there is no blind rollback copy.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    )
  })

  it('keeps two rows pending independently and rolls both back', async () => {
    const rejecters = new Map<string, (cause: unknown) => void>()
    const setStarFn = vi.fn(
      (symbol: string) =>
        new Promise<StarEnvelope>((_resolve, reject) => {
          rejecters.set(symbol, reject)
        }),
    )
    render(
      <WatchlistPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([row('BTCUSDT', true), row('ETHUSDT', true)], 2)
        }
        setStarFn={setStarFn}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))
    fireEvent.click(screen.getByRole('button', { name: 'ETHUSDT in next scan' }))
    expect(setStarFn).toHaveBeenCalledTimes(2)

    await act(async () => {
      rejecters.get('BTCUSDT')?.(new WatchlistApiError('no store', 'DATA_UNAVAILABLE'))
      rejecters.get('ETHUSDT')?.(new WatchlistApiError('no store', 'DATA_UNAVAILABLE'))
    })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
      expect(screen.getByRole('button', { name: 'ETHUSDT in next scan' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
    })
    expect(screen.getByText('2 of 2 symbols included in next scan')).toBeInTheDocument()
  })

  it('is read-only at the phone width: no buttons, stars as text, and a CLI note', async () => {
    const { container } = render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope(baseRows, 3)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow
      />,
    )

    expect(await screen.findByRole('heading', { name: 'CRYPTO (1)' })).toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByRole('note')).toHaveTextContent('Read-only at this width')
    expect(screen.getByRole('note')).toHaveTextContent('oft watchlist add|remove')
    // Navigation is not a mutation, so the search link stays reachable at 375.
    expect(screen.getByRole('link', { name: 'Search symbols' })).toHaveAttribute(
      'href',
      '/watchlist/search',
    )
    expect(screen.getAllByText('★').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Search symbols')).toBeInTheDocument()
    expect(container.querySelector('section.system-page')).toHaveAttribute('lang', 'en')
  })

  it('keeps Retry reachable at the phone width when the load fails', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => {
          throw new Error('daemon down')
        }}
        isNarrow
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('Watchlist unavailable: daemon down')
    // The mutation guarantee holds: no star control is a button.
    expect(screen.queryAllByRole('button', { name: /in next scan/ })).toHaveLength(0)
    // Retry is a read-only recovery action, not a star mutation, so it stays.
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('renders an empty watchlist without treating it as an error', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope([row('ETHUSDT', false)], 1)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByRole('heading', { name: 'No symbols in the next scan' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('does not render a 0 of 0 count while loading', () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={() => new Promise<WatchlistEnvelope>(() => undefined)}
      />,
    )

    expect(screen.queryByTestId('watchlist-count')).not.toBeInTheDocument()
    expect(screen.queryByText(/0 of 0/)).not.toBeInTheDocument()
  })

  it('retries a failed load and renders the rows', async () => {
    let calls = 0
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => {
          calls += 1
          if (calls === 1) {
            throw new Error('registry locked')
          }
          return watchlistEnvelope([row('BTCUSDT', true)], 1)
        }}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('registry locked')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('link', { name: 'BTCUSDT' })).toBeInTheDocument()
    expect(calls).toBe(2)
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('announces filter result changes through the single polite region', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([row('BTCUSDT', true), row('XAUUSDT', true)], 2)
        }
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )
    const search = await screen.findByLabelText('Search symbols')

    fireEvent.change(search, { target: { value: 'xau' } })
    await waitFor(() =>
      expect(screen.getByTestId('watchlist-live')).toHaveTextContent(
        '1 of 2 starred match “xau”.',
      ),
    )

    fireEvent.change(search, { target: { value: 'zzz' } })
    await waitFor(() =>
      expect(screen.getByTestId('watchlist-live')).toHaveTextContent(
        '0 of 2 starred match “zzz”.',
      ),
    )
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })
})

describe('WatchlistSearchPage add', () => {
  const items = [row('BTCUSDT', false), row('ETHUSDT', false), row('XAUUSDT', false)]

  it('adds a symbol optimistically and confirms the added state', async () => {
    const setStarFn = vi.fn(async (symbol: string, starred: boolean) =>
      starEnvelope(symbol, starred),
    )
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope(items)}
        setStarFn={setStarFn}
      />,
    )

    const add = await screen.findByRole('button', { name: 'BTCUSDT in next scan' })
    expect(add).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(add)

    expect(setStarFn).toHaveBeenCalledWith('BTCUSDT', true)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    )
    expect(screen.getByTestId('search-live')).toHaveTextContent('BTCUSDT added to next scan.')
  })

  it('rolls an add back and shows the API reason in one alert', async () => {
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope(items)}
        setStarFn={async () => {
          throw new WatchlistApiError('Symbol BTCUSDT does not exist.', 'NOT_FOUND')
        }}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not add BTCUSDT: Symbol BTCUSDT does not exist.',
    )
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('blocks only the add direction for a non-TRADING row', async () => {
    const settling = row('FTTUSDT', false, { status: 'SETTLING' })
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope([...items, settling])}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    const blocked = await screen.findByRole('button', { name: 'FTTUSDT in next scan' })
    // aria-disabled keeps the control focusable so its reason is reachable.
    expect(blocked).toBeEnabled()
    expect(blocked).toHaveAttribute('aria-disabled', 'true')
    expect(blocked.getAttribute('aria-describedby')).toContain('search-reason-FTTUSDT')
    expect(screen.getByText('Cannot add: status is SETTLING.')).toBeInTheDocument()
  })

  it('still allows removing a starred row that is no longer TRADING', async () => {
    const setStarFn = vi.fn(async (symbol: string, starred: boolean) =>
      starEnvelope(symbol, starred),
    )
    const settling = row('FTTUSDT', true, { status: 'SETTLING' })
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope([settling])}
        setStarFn={setStarFn}
      />,
    )

    const remove = await screen.findByRole('button', { name: 'FTTUSDT in next scan' })
    expect(remove).not.toHaveAttribute('aria-disabled')
    fireEvent.click(remove)
    expect(setStarFn).toHaveBeenCalledWith('FTTUSDT', false)
    await waitFor(() =>
      expect(screen.getByTestId('search-live')).toHaveTextContent(
        'FTTUSDT removed from next scan.',
      ),
    )
    expect(screen.getByRole('button', { name: 'FTTUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('sends one request when the add control is double-submitted', async () => {
    const pending = deferred<StarEnvelope>()
    const setStarFn = vi.fn(() => pending.promise)
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope(items)}
        setStarFn={setStarFn}
      />,
    )

    const add = await screen.findByRole('button', { name: 'ETHUSDT in next scan' })
    fireEvent.click(add)
    fireEvent.click(add)

    expect(setStarFn).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'ETHUSDT in next scan' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('browses without buttons at the phone width', async () => {
    const { container } = render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([row('BTCUSDT', true), row('ETHUSDT', false)])
        }
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
        isNarrow
      />,
    )

    expect(await screen.findByRole('heading', { name: 'Watchlist' })).toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByText('Included in next scan')).toBeInTheDocument()
    expect(screen.getByText('Not included in next scan')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('Read-only at this width')
    expect(container.querySelector('section.system-page')).toHaveAttribute('lang', 'en')
  })

  it('keeps Retry reachable at the phone width when the load fails', async () => {
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => {
          throw new Error('daemon down')
        }}
        isNarrow
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Symbol search unavailable: daemon down',
    )
    expect(screen.queryAllByRole('button', { name: /in next scan/ })).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('announces filter result changes through the single polite region', async () => {
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope(items)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )
    const search = await screen.findByLabelText('Search symbols')

    fireEvent.change(search, { target: { value: 'eth' } })
    await waitFor(() =>
      expect(screen.getByTestId('search-live')).toHaveTextContent('1 match for “eth”.'),
    )

    fireEvent.change(search, { target: { value: 'zzz' } })
    await waitFor(() =>
      expect(screen.getByTestId('search-live')).toHaveTextContent('0 matches for “zzz”.'),
    )
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })
})
