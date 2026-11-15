import { useEffect, useRef, useState } from 'react'

import { filterSymbols, groupSymbols } from '../api/symbols'
import {
  fetchWatchlist,
  setStar,
  type StarEnvelope,
  type WatchlistEnvelope,
  type WatchlistRow,
} from '../api/watchlist'
import { StarState, StarToggle } from '../components/star-toggle'
import { useNarrowCompact } from '../hooks/use-compact'
import { useStarMutation } from '../hooks/use-star-mutation'

interface WatchlistPageProps {
  fetchWatchlistFn?: (signal?: AbortSignal) => Promise<WatchlistEnvelope>
  setStarFn?: (symbol: string, starred: boolean) => Promise<StarEnvelope>
  isNarrow?: boolean
}

interface WatchlistState {
  state: 'loading' | 'ready' | 'error'
  rows: WatchlistRow[]
  total: number
  message?: string
}

const EMPTY_STATE: WatchlistState = {
  state: 'loading',
  rows: [],
  total: 0,
}

function SymbolGroup({
  label,
  items,
  pending,
  narrow,
  onToggle,
}: {
  label: string
  items: WatchlistRow[]
  pending: ReadonlySet<string>
  narrow: boolean
  onToggle: (row: WatchlistRow) => void
}) {
  if (items.length === 0) {
    return null
  }
  return (
    <section className="panel" aria-labelledby={`group-${label.toLowerCase()}`}>
      <div className="panel-head">
        <h2 id={`group-${label.toLowerCase()}`}>
          {label} ({items.length})
        </h2>
      </div>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
      <div className="table-wrap" role="region" aria-label={`${label} symbols table`} tabIndex={0}>
        <table className="data-table">
          <caption className="visually-hidden">{label} symbols in the next scan</caption>
          <thead>
            <tr>
              <th scope="col">
                <span aria-hidden="true">★</span>
                <span className="visually-hidden">In next scan</span>
              </th>
              <th scope="col">Symbol</th>
              <th scope="col">Contract type</th>
              <th scope="col">Tick</th>
              <th scope="col">Step</th>
              <th scope="col">Min notional</th>
              <th scope="col">Status</th>
              <th scope="col">Listed</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const isPending = pending.has(item.symbol)
              return (
                <tr key={item.symbol}>
                  <td className="star-cell">
                    {narrow ? (
                      <StarState starred={item.starred} />
                    ) : (
                      <StarToggle
                        symbol={item.symbol}
                        starred={item.starred}
                        pending={isPending}
                        onToggle={() => onToggle(item)}
                      />
                    )}
                  </td>
                  <td>
                    <a
                      className="rowlink mono"
                      href={`/watchlist/${encodeURIComponent(item.symbol)}`}
                    >
                      {item.symbol}
                    </a>
                  </td>
                  <td>{item.contract_type}</td>
                  <td className="mono">{item.tick_size}</td>
                  <td className="mono">{item.step_size}</td>
                  <td className="mono">{item.min_notional}</td>
                  <td>{item.status}</td>
                  <td className="mono">{item.listed_date}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function WatchlistPage({
  fetchWatchlistFn = fetchWatchlist,
  setStarFn = setStar,
  isNarrow,
}: WatchlistPageProps) {
  const [load, setLoad] = useState<WatchlistState>(EMPTY_STATE)
  const [query, setQuery] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [mutationError, setMutationError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const narrow = useNarrowCompact(isNarrow)
  const fetcherRef = useRef(fetchWatchlistFn)
  const setStarRef = useRef(setStarFn)
  // The rendered order before a toggle, so focus can move after a row unmounts.
  const orderRef = useRef<string[]>([])
  const snapshotsRef = useRef<Map<string, WatchlistRow>>(new Map())
  const announcedQueryRef = useRef('')

  useEffect(() => {
    fetcherRef.current = fetchWatchlistFn
  }, [fetchWatchlistFn])

  useEffect(() => {
    setStarRef.current = setStarFn
  }, [setStarFn])

  useEffect(() => {
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetcherRef
      .current(controller.signal)
      .then((envelope) => {
        const data = envelope.data
        if (!data) {
          throw new Error('Watchlist response was empty.')
        }
        // The rendered count derives from these rows; the server's
        // next_scan_count is only an initial cross-check of the same value.
        setLoad({ state: 'ready', rows: data.rows, total: data.total })
        const included = data.rows.filter((row) => row.starred).length
        setAnnouncement(
          included === 0
            ? 'No symbols are included in the next scan.'
            : `${included} of ${data.total} symbols included in next scan.`,
        )
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) {
          return
        }
        setLoad({
          state: 'error',
          rows: [],
          total: 0,
          message: cause instanceof Error ? cause.message : 'Unknown error',
        })
      })
    return () => {
      controller.abort()
    }
  }, [attempt])

  const mutation = useStarMutation({
    setStarFn: (symbol, starred) => setStarRef.current(symbol, starred),
    successMessage: (symbol, starred) =>
      `${symbol} ${starred ? 'added to' : 'removed from'} next scan.`,
    onAnnounce: setAnnouncement,
    onOptimistic: (symbol, starred) => {
      setMutationError(null)
      setLoad((current) => ({
        ...current,
        rows: current.rows.map((item) =>
          item.symbol === symbol
            ? { ...item, starred, starred_at: starred ? item.starred_at : null }
            : item,
        ),
      }))
    },
    onConfirmed: (symbol, starred, starredAt) => {
      setLoad((current) => ({
        ...current,
        rows: current.rows.map((item) =>
          item.symbol === symbol ? { ...item, starred, starred_at: starredAt } : item,
        ),
      }))
      if (!starred) {
        moveFocusAfterRemoval(symbol)
      }
      snapshotsRef.current.delete(symbol)
    },
    onRollback: (symbol, cause) => {
      const snapshot = snapshotsRef.current.get(symbol)
      if (snapshot) {
        setLoad((current) => ({
          ...current,
          rows: current.rows.map((item) => (item.symbol === symbol ? snapshot : item)),
        }))
      }
      const message = cause instanceof Error ? cause.message : 'Unknown error'
      setMutationError(`Could not ${snapshot?.starred ? 'remove' : 'add'} ${symbol}: ${message}`)
      snapshotsRef.current.delete(symbol)
    },
    onUnknownOutcome: (symbol) => {
      setMutationError(`Could not confirm the ${symbol} change; refreshing the watchlist.`)
      snapshotsRef.current.delete(symbol)
      setAttempt((n) => n + 1)
    },
  })

  // A row being unstarred stays visible until the mutation resolves so the
  // pending state and any rollback remain on screen.
  const displayedRows = load.rows.filter((row) => row.starred || mutation.pending.has(row.symbol))
  const matched = filterSymbols(displayedRows, query)
  const groups = groupSymbols(matched)
  const searching = query.trim() !== ''
  // Derived from the same rows the user sees, not separate state.
  const nextScanCount = load.rows.filter((row) => row.starred).length
  const countLabel = `${nextScanCount} of ${load.total} symbols included in next scan`

  // One polite announcement for filter-result changes; the mount-time load
  // summary above is the only other write to this region.
  useEffect(() => {
    if (load.state !== 'ready') {
      return
    }
    const trimmed = query.trim()
    if (trimmed === announcedQueryRef.current) {
      return
    }
    announcedQueryRef.current = trimmed
    if (trimmed === '') {
      return
    }
    setAnnouncement(
      `${matched.length} of ${displayedRows.length} starred match “${trimmed}”.`,
    )
  }, [load.state, query, matched.length, displayedRows.length])

  function moveFocusAfterRemoval(removed: string): void {
    const order = orderRef.current
    const index = order.indexOf(removed)
    requestAnimationFrame(() => {
      // Never steal focus the user has already moved elsewhere in this frame;
      // a removed focused button falls back to <body>.
      const active = document.activeElement
      if (active !== null && active !== document.body) {
        return
      }
      const toggles = Array.from(
        document.querySelectorAll<HTMLElement>('[data-star-symbol]'),
      )
      const find = (symbol: string): HTMLElement | undefined =>
        toggles.find((element) => element.dataset.starSymbol === symbol)
      const after = order.slice(index + 1).map(find).find(Boolean)
      const before = order
        .slice(0, Math.max(0, index))
        .reverse()
        .map(find)
        .find(Boolean)
      const target = after ?? before
      if (target) {
        target.focus()
        return
      }
      document.getElementById('symbol-search')?.focus()
    })
  }

  function toggle(row: WatchlistRow): void {
    if (narrow || mutation.pending.has(row.symbol)) {
      return
    }
    orderRef.current = displayedRows.map((item) => item.symbol)
    snapshotsRef.current.set(row.symbol, row)
    mutation.toggle(row.symbol, !row.starred)
  }

  return (
    <section
      className="system-page"
      lang="en"
      aria-labelledby="watchlist-heading"
      data-readonly={narrow}
    >
      <p className="visually-hidden" role="status" aria-live="polite" data-testid="watchlist-live">
        {announcement}
      </p>
      <div className="page-head">
        <div>
          <p className="kicker">Watchlist / symbols</p>
          <h1 id="watchlist-heading">Watchlist</h1>
          <p className="page-sub">
            Starred symbols grouped by Crypto and TradFi; the star decides whether a symbol is
            included in the next scan.
          </p>
        </div>
        {/* Always offered while ready, not only from the empty state: a
            populated watchlist still needs the add-from-search path. A plain
            anchor keeps it usable at the 375 read-only surface. */}
        {load.state === 'ready' && (
          <div className="action-row">
            <a className="btn" href="/watchlist/search">
              Search symbols
            </a>
          </div>
        )}
        {narrow && (
          <p className="readonly-note" role="note">
            Read-only at this width. Use a desktop viewport or the CLI (`oft watchlist add|remove`)
            for actions.
          </p>
        )}
      </div>

      <section className="panel" aria-label="Symbol search">
        <div className="panel-head">
          <label htmlFor="symbol-search">Search symbols</label>
          {load.state === 'ready' && (
            <span className="mono-muted">
              {matched.length} of {displayedRows.length} starred
            </span>
          )}
        </div>
        <div className="panel-foot">
          <input
            id="symbol-search"
            type="search"
            value={query}
            placeholder="BTCUSDT or XAU"
            autoComplete="off"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      </section>

      {load.state === 'ready' && (
        <p className="status-message" data-testid="watchlist-count">
          {countLabel}
        </p>
      )}

      {load.state === 'loading' && <p className="status-message">Loading symbol registry…</p>}

      {load.state === 'error' && (
        <div>
          <p className="status-message status-message--error" role="alert">
            Watchlist unavailable: {load.message}
          </p>
          {/* Read-only recovery: this is not a star mutation, so it stays
              available at every width, including the 375 read-only surface. */}
          <button type="button" className="control-btn" onClick={() => setAttempt((n) => n + 1)}>
            Retry
          </button>
        </div>
      )}

      {mutationError !== null && (
        <p className="status-message status-message--error" role="alert">
          {mutationError}
        </p>
      )}

      {load.state === 'ready' && displayedRows.length === 0 && (
        <div className="panel">
          <div className="panel-head">
            <h2>No symbols in the next scan</h2>
          </div>
          <p className="panel-foot">
            Star at least one symbol, or add one from the CLI with{' '}
            <code>oft watchlist add BTCUSDT</code>.
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <a className="btn" href="/watchlist/search">
              Search symbols
            </a>
          </div>
        </div>
      )}

      {load.state === 'ready' && displayedRows.length > 0 && searching && matched.length === 0 && (
        <p className="status-message">
          No matches for “{query}”. Try an exact symbol such as BTCUSDT.
        </p>
      )}

      {load.state === 'ready' && (
        <SymbolGroup
          label="CRYPTO"
          items={groups.crypto}
          pending={mutation.pending}
          narrow={narrow}
          onToggle={toggle}
        />
      )}
      {load.state === 'ready' && (
        <SymbolGroup
          label="TRADFI"
          items={groups.tradfi}
          pending={mutation.pending}
          narrow={narrow}
          onToggle={toggle}
        />
      )}
    </section>
  )
}
