import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { RequestTimeoutError } from '../../api/request-timeout'
import type { ApiMeta } from '../../api/status'
import type { SymbolContract } from '../../api/symbols'
import {
  WatchlistApiError,
  WatchlistOutcomeUnknownError,
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

function watchlistEnvelope(
  rows: WatchlistRow[],
  total = rows.length,
  truncated = false,
): WatchlistEnvelope {
  return {
    ok: true,
    error: null,
    meta,
    data: {
      groups: [],
      total,
      rows,
      next_scan_count: rows.filter((item) => item.starred).length,
      truncated,
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
  it('classifies a timeout, network failure, or unusable 2xx body as unknown', () => {
    expect(isUnknownStarOutcome(new RequestTimeoutError())).toBe(true)
    expect(isUnknownStarOutcome(new TypeError('Failed to fetch'))).toBe(true)
    expect(
      isUnknownStarOutcome(new WatchlistOutcomeUnknownError('truncated', new SyntaxError('x'))),
    ).toBe(true)
    // A typed non-2xx refusal is definitive and rolls back.
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

  it('does not move focus when the removed control was not focused', async () => {
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
    // Click without focusing the control: focus restoration must stay out.
    fireEvent.click(btc)

    await act(async () => {
      pending.resolve(starEnvelope('BTCUSDT', false))
    })

    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'BTCUSDT in next scan' }),
      ).not.toBeInTheDocument(),
    )
    const eth = screen.getByRole('button', { name: 'ETHUSDT in next scan' })
    expect(eth).not.toHaveFocus()
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
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      return calls === 1 ? watchlistEnvelope([row('BTCUSDT', true)], 1) : refresh.promise
    })
    render(
      <WatchlistPage
        fetchWatchlistFn={fetchWatchlistFn}
        setStarFn={async () => {
          throw new RequestTimeoutError()
        }}
      />,
    )

    const toggle = await screen.findByRole('button', { name: 'BTCUSDT in next scan' })
    toggle.focus()
    fireEvent.click(toggle)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not confirm the BTCUSDT change; refreshing the watchlist.',
    )
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))
    // The originating row and its toggle stay mounted during the refetch, so
    // focus is not dropped to <body>.
    const stillMounted = screen.getByRole('button', { name: 'BTCUSDT in next scan' })
    expect(stillMounted).toBeInTheDocument()

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', true)], 1))
    })

    // The server state reconciles the optimistic flip, the stale mutation
    // alert clears, and focus returns to the originating toggle.
    await waitFor(() =>
      expect(screen.queryByRole('alert')).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveFocus(),
    )
  })

  it('refetches instead of rolling back when a 2xx body is unusable', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      return calls === 1 ? watchlistEnvelope([row('BTCUSDT', true)], 1) : refresh.promise
    })
    const setStarFn = vi.fn(async () => {
      throw new WatchlistOutcomeUnknownError('truncated 2xx body', new SyntaxError('unexpected end'))
    })
    render(<WatchlistPage fetchWatchlistFn={fetchWatchlistFn} setStarFn={setStarFn} />)

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not confirm the BTCUSDT change; refreshing the watchlist.',
    )
    // A definitive refusal would have rolled back and shown "Could not remove";
    // an unknown outcome refetches instead.
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))
    expect(setStarFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', true)], 1))
    })
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

  it('does not steal focus from the search box when a delayed retry resolves', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('registry locked')
      }
      return refresh.promise
    })
    render(<WatchlistPage fetchWatchlistFn={fetchWatchlistFn} />)

    await screen.findByRole('button', { name: 'Retry' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('Refreshing…')).toBeInTheDocument())

    // The user moves focus while the retry is in flight.
    const search = screen.getByLabelText('Search symbols')
    search.focus()
    expect(search).toHaveFocus()

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', true)], 1))
    })

    await screen.findByRole('link', { name: 'BTCUSDT' })
    // Focus was not yanked to the results region.
    expect(search).toHaveFocus()
  })

  it('makes star toggles non-interactive while a retry is in flight', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        return watchlistEnvelope([row('BTCUSDT', true)], 1)
      }
      if (calls === 2) {
        // The unknown-outcome refetch fails; rows stay mounted.
        throw new Error('daemon down')
      }
      return refresh.promise
    })
    const setStarFn = vi.fn(async () => {
      throw new RequestTimeoutError()
    })
    render(<WatchlistPage fetchWatchlistFn={fetchWatchlistFn} setStarFn={setStarFn} />)

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(3))

    // Rows are retained through the retry, but no overlap mutation is allowed.
    const retained = screen.getByRole('button', { name: 'BTCUSDT in next scan' })
    expect(retained).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(retained)
    expect(setStarFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', true)], 1))
    })
  })

  it('derives the count label from rows when a deferred refetch resolves', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      return calls === 1
        ? watchlistEnvelope([row('BTCUSDT', true), row('ETHUSDT', false)], 2)
        : refresh.promise
    })
    render(
      <WatchlistPage
        fetchWatchlistFn={fetchWatchlistFn}
        setStarFn={async () => {
          throw new RequestTimeoutError()
        }}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))

    // The response's whole-registry count is stale; the complete page must
    // agree with the row flags it renders.
    const stale = watchlistEnvelope(
      [row('BTCUSDT', true), row('ETHUSDT', false), row('XAUUSDT', false)],
      3,
    )
    stale.data = { ...stale.data!, next_scan_count: 7 }
    await act(async () => {
      refresh.resolve(stale)
    })

    await waitFor(() =>
      expect(screen.getByTestId('watchlist-count')).toHaveTextContent(
        '1 of 3 symbols included in next scan',
      ),
    )
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('explains a truncated page whose starred rows are all outside it', async () => {
    const envelope = watchlistEnvelope(
      [row('ETHUSDT', false), row('SOLUSDT', false), row('DOGEUSDT', false)],
      5,
      true,
    )
    envelope.data = { ...envelope.data!, next_scan_count: 2 }
    render(<WatchlistPage fetchWatchlistFn={async () => envelope} />)

    expect(
      await screen.findByRole('heading', { name: 'Starred symbols are outside this page' }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('watchlist-count')).toHaveTextContent(
      '2 of 5 symbols included in next scan',
    )
    expect(screen.queryByText('No symbols in the next scan')).not.toBeInTheDocument()
  })

  it('cancels a pending focus-restore frame when the page unmounts', async () => {
    const pending = deferred<StarEnvelope>()
    let frameId = 0
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => {
      frameId += 1
      return frameId
    })
    const cancelSpy = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)

    const { unmount } = render(
      <WatchlistPage
        fetchWatchlistFn={async () =>
          watchlistEnvelope([row('BTCUSDT', true), row('ETHUSDT', true)], 2)
        }
        setStarFn={() => pending.promise}
      />,
    )

    const btc = await screen.findByRole('button', { name: 'BTCUSDT in next scan' })
    btc.focus()
    fireEvent.click(btc)
    await act(async () => {
      pending.resolve(starEnvelope('BTCUSDT', false))
    })
    expect(rafSpy).toHaveBeenCalled()

    unmount()
    // The focus-restore frame scheduled by the removal was cancelled, so it
    // can never fire against a route that is gone.
    expect(cancelSpy).toHaveBeenCalledWith(rafSpy.mock.results.at(-1)?.value)

    rafSpy.mockRestore()
    cancelSpy.mockRestore()

    render(<button type="button">sibling</button>)
    const sibling = screen.getByRole('button', { name: 'sibling' })
    sibling.focus()
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    })
    expect(sibling).toHaveFocus()
  })

  it('keeps Retry mounted, focused, and busy until the retry load resolves', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('registry locked')
      }
      return refresh.promise
    })
    render(<WatchlistPage fetchWatchlistFn={fetchWatchlistFn} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('registry locked')
    const retry = screen.getByRole('button', { name: 'Retry' })
    retry.focus()
    fireEvent.click(retry)

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Retry' })).toHaveAttribute(
        'aria-busy',
        'true',
      ),
    )
    const pendingRetry = screen.getByRole('button', { name: 'Retry' })
    expect(pendingRetry).toHaveAttribute('aria-disabled', 'true')
    // Focusable, not natively disabled, and still focused: focus survives.
    expect(pendingRetry).toBeEnabled()
    expect(pendingRetry).toHaveFocus()
    expect(screen.getByText('Refreshing…')).toBeInTheDocument()

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', true)], 1))
    })

    expect(await screen.findByRole('link', { name: 'BTCUSDT' })).toBeInTheDocument()
    const region = screen.getByRole('region', { name: 'CRYPTO symbols table' })
    await waitFor(() => expect(region).toHaveFocus())
  })

  it('warns when the bounded page omits registry symbols', async () => {
    render(
      <WatchlistPage
        fetchWatchlistFn={async () => watchlistEnvelope(baseRows.slice(0, 2), 3, true)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByTestId('watchlist-truncation-note')).toHaveTextContent(
      'Showing first 2 of 3 symbols.',
    )
    // A plain paragraph, not a second polite live region.
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

  it('keeps Retry mounted and focused through the retry, then focuses the results', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('daemon down')
      }
      return refresh.promise
    })
    render(<WatchlistSearchPage fetchWatchlistFn={fetchWatchlistFn} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Symbol search unavailable: daemon down',
    )
    const retry = screen.getByRole('button', { name: 'Retry' })
    retry.focus()
    fireEvent.click(retry)

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Retry' })).toHaveAttribute(
        'aria-busy',
        'true',
      ),
    )
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveFocus()
    expect(screen.getByText('Refreshing…')).toBeInTheDocument()

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', false)]))
    })

    const region = await screen.findByRole('region', { name: 'CRYPTO search results' })
    await waitFor(() => expect(region).toHaveFocus())
  })

  it('returns focus to the originating add toggle after an unknown-outcome refetch', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      return calls === 1
        ? watchlistEnvelope([row('BTCUSDT', false), row('ETHUSDT', false)])
        : refresh.promise
    })
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={fetchWatchlistFn}
        setStarFn={async () => {
          throw new TypeError('Failed to fetch')
        }}
      />,
    )

    const add = await screen.findByRole('button', { name: 'BTCUSDT in next scan' })
    add.focus()
    fireEvent.click(add)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not confirm the BTCUSDT change; refreshing the symbol list.',
    )
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toBeInTheDocument()

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', false), row('ETHUSDT', false)]))
    })

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'BTCUSDT in next scan' })).toHaveFocus(),
    )
  })

  it('does not steal focus from the search box when a delayed retry resolves', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('daemon down')
      }
      return refresh.promise
    })
    render(<WatchlistSearchPage fetchWatchlistFn={fetchWatchlistFn} />)

    await screen.findByRole('button', { name: 'Retry' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('Refreshing…')).toBeInTheDocument())

    const search = screen.getByLabelText('Search symbols')
    search.focus()
    expect(search).toHaveFocus()

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', false)]))
    })

    await screen.findByRole('link', { name: 'BTCUSDT' })
    expect(search).toHaveFocus()
  })

  it('makes add toggles non-interactive while a retry is in flight', async () => {
    const refresh = deferred<WatchlistEnvelope>()
    let calls = 0
    const fetchWatchlistFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        return watchlistEnvelope([row('BTCUSDT', false)], 1)
      }
      if (calls === 2) {
        throw new Error('daemon down')
      }
      return refresh.promise
    })
    const setStarFn = vi.fn(async () => {
      throw new RequestTimeoutError()
    })
    render(<WatchlistSearchPage fetchWatchlistFn={fetchWatchlistFn} setStarFn={setStarFn} />)

    fireEvent.click(await screen.findByRole('button', { name: 'BTCUSDT in next scan' }))
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(2))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(fetchWatchlistFn).toHaveBeenCalledTimes(3))

    const retained = screen.getByRole('button', { name: 'BTCUSDT in next scan' })
    expect(retained).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(retained)
    expect(setStarFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      refresh.resolve(watchlistEnvelope([row('BTCUSDT', false)], 1))
    })
  })

  it('warns when the bounded registry page omits symbols', async () => {
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope(items, 5, true)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByTestId('search-truncation-note')).toHaveTextContent(
      'Showing first 3 of 5 symbols.',
    )
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('caps rendered matches at 200 while keeping the header count truthful', async () => {
    const many = Array.from({ length: 250 }, (_, index) => row(`S${index}USDT`, false))
    render(
      <WatchlistSearchPage
        fetchWatchlistFn={async () => watchlistEnvelope(many)}
        setStarFn={async (symbol, starred) => starEnvelope(symbol, starred)}
      />,
    )

    expect(await screen.findByTestId('search-result-limit-note')).toHaveTextContent(
      'Showing the first 200 of 250 matches. Refine the search to narrow the list.',
    )
    expect(screen.getAllByRole('button', { name: /in next scan/ })).toHaveLength(200)
    expect(screen.getByText('250 matches')).toBeInTheDocument()
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
