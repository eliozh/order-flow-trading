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
  truncated: boolean
  message?: string
}

const EMPTY_STATE: WatchlistState = {
  state: 'loading',
  rows: [],
  total: 0,
  truncated: false,
}

function SymbolGroup({
  label,
  items,
  pending,
  narrow,
  disabled,
  onToggle,
}: {
  label: string
  items: WatchlistRow[]
  pending: ReadonlySet<string>
  narrow: boolean
  disabled: boolean
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
                        disabled={disabled}
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
  // True from a Retry click until the retry fetch settles; keeps the Retry
  // control mounted (aria-busy/aria-disabled) so focus is never dropped.
  const [retrying, setRetrying] = useState(false)
  // The server's authoritative next-scan count for a truncated page.
  // Optimistic toggles apply +-1, a rollback reverses it, and every successful
  // load replaces it. When the page is complete the display derives the count
  // from the row flags instead, so the two can never disagree.
  const [serverNextScanCount, setServerNextScanCount] = useState(0)
  // Symbols whose mutation outcome is unknown: their rows stay visible until
  // the refetch replaces the row set, so focus is not unmounted mid-refetch.
  const [unresolved, setUnresolved] = useState<ReadonlySet<string>>(new Set())
  const narrow = useNarrowCompact(isNarrow)
  const fetcherRef = useRef(fetchWatchlistFn)
  const setStarRef = useRef(setStarFn)
  // The rendered order before a toggle, so focus can move after a row unmounts.
  const orderRef = useRef<string[]>([])
  const snapshotsRef = useRef<Map<string, WatchlistRow>>(new Map())
  const announcedQueryRef = useRef('')
  // Scope every focus query to this page; a stale frame must never touch
  // another route.
  const containerRef = useRef<HTMLElement | null>(null)
  const focusFrameRef = useRef<number | null>(null)
  // What should receive focus after the next ready load: a symbol, 'retry',
  // or null (nothing).
  const pendingFocusRef = useRef<string | null>(null)
  // The exact unknown-outcome alert message this refetch was started for. A
  // load success clears only that message; a newer rollback alert survives.
  const unknownOutcomeErrorRef = useRef<string | null>(null)

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
        // next_scan_count is the server's authoritative inclusive count; the
        // optimistic toggle deltas are provisional until this replaces them.
        setLoad({
          state: 'ready',
          rows: data.rows,
          total: data.total,
          truncated: data.truncated,
        })
        setServerNextScanCount(data.next_scan_count)
        setUnresolved(new Set())
        setRetrying(false)
        // Clear the alert this refetch was started for, but never a newer
        // mutation alert that landed while the request was in flight.
        const unknownOutcomeMessage = unknownOutcomeErrorRef.current
        setMutationError((current) =>
          current !== null && current === unknownOutcomeMessage ? null : current,
        )
        unknownOutcomeErrorRef.current = null
        setAnnouncement(
          data.next_scan_count === 0
            ? 'No symbols are included in the next scan.'
            : `${data.next_scan_count} of ${data.total} symbols included in next scan.`,
        )
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) {
          return
        }
        // Keep any previously loaded rows visible while showing the error.
        setLoad((previous) => ({
          ...previous,
          state: 'error',
          message: cause instanceof Error ? cause.message : 'Unknown error',
        }))
        setRetrying(false)
      })
    return () => {
      controller.abort()
    }
  }, [attempt])

  // Restore focus once a refetch resolves: the originating toggle, else the
  // first results region, else the search box. The target is consumed once.
  useEffect(() => {
    if (load.state !== 'ready') {
      return
    }
    const target = pendingFocusRef.current
    if (target === null) {
      return
    }
    pendingFocusRef.current = null
    const container = containerRef.current
    if (container === null) {
      return
    }
    // Never steal focus the user moved while the refetch was in flight. The
    // Retry button unmounts on success (activeElement falls back to <body>),
    // so the intended restore still happens when nobody else owns focus.
    const active = document.activeElement
    if (active instanceof HTMLElement && active !== document.body && active.isConnected) {
      return
    }
    if (target !== 'retry') {
      const toggles = Array.from(container.querySelectorAll<HTMLElement>('[data-star-symbol]'))
      const match = toggles.find((element) => element.dataset.starSymbol === target)
      if (match) {
        match.focus()
        return
      }
    }
    const region = container.querySelector<HTMLElement>('.table-wrap')
    if (region) {
      region.focus()
      return
    }
    container.querySelector<HTMLElement>('#symbol-search')?.focus()
  }, [load.state])

  // A pending focus frame must not fire after unmount or a route change.
  useEffect(
    () => () => {
      if (focusFrameRef.current !== null) {
        cancelAnimationFrame(focusFrameRef.current)
        focusFrameRef.current = null
      }
    },
    [],
  )

  const mutation = useStarMutation({
    setStarFn: (symbol, starred) => setStarRef.current(symbol, starred),
    successMessage: (symbol, starred) =>
      `${symbol} ${starred ? 'added to' : 'removed from'} next scan.`,
    onAnnounce: setAnnouncement,
    onOptimistic: (symbol, starred) => {
      setMutationError(null)
      setServerNextScanCount((current) => current + (starred ? 1 : -1))
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
        // Undo the optimistic delta in the opposite direction.
        setServerNextScanCount((current) => current + (snapshot.starred ? 1 : -1))
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
      const message = `Could not confirm the ${symbol} change; refreshing the watchlist.`
      unknownOutcomeErrorRef.current = message
      setMutationError(message)
      // Keep the row (and its focused toggle) mounted until the refetch
      // replaces the row set, then return focus to the originating toggle.
      setUnresolved((current) => new Set(current).add(symbol))
      pendingFocusRef.current = symbol
      snapshotsRef.current.delete(symbol)
      setAttempt((n) => n + 1)
    },
  })

  // A row being unstarred stays visible until the mutation resolves so the
  // pending state and any rollback remain on screen; an unresolved row also
  // stays visible until the refetch replaces it.
  const displayedRows = load.rows.filter(
    (row) => row.starred || mutation.pending.has(row.symbol) || unresolved.has(row.symbol),
  )
  const matched = filterSymbols(displayedRows, query)
  const groups = groupSymbols(matched)
  const searching = query.trim() !== ''
  // Rows survive a refetch: they are shown when ready or while a previous page
  // is still loaded, so the tables never blink out of the DOM.
  const showRows = load.state === 'ready' || load.rows.length > 0
  // A complete page's count must agree with the row flags it renders; only a
  // truncated page needs the server's whole-registry count.
  const rowStarredCount = load.rows.filter((item) => item.starred).length
  const nextScanCount = load.truncated ? serverNextScanCount : rowStarredCount
  const countLabel = `${nextScanCount} of ${load.total} symbols included in next scan`

  // One polite announcement for filter-result changes; filter results, load
  // summaries, retry notices, and star successes all write here.
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
    setAnnouncement(`${matched.length} of ${displayedRows.length} starred match “${trimmed}”.`)
  }, [load.state, query, matched.length, displayedRows.length])

  function moveFocusAfterRemoval(removed: string): void {
    const active = document.activeElement
    // Only restore when the user was operating the removed control; a click
    // that never focused it must leave focus alone.
    if (!(active instanceof HTMLElement) || active.dataset.starSymbol !== removed) {
      return
    }
    const order = orderRef.current
    const index = order.indexOf(removed)
    if (focusFrameRef.current !== null) {
      cancelAnimationFrame(focusFrameRef.current)
    }
    focusFrameRef.current = requestAnimationFrame(() => {
      focusFrameRef.current = null
      const container = containerRef.current
      if (container === null || !container.isConnected) {
        return
      }
      // Never steal focus the user has already moved elsewhere; only the
      // removed (now detached) control, <body>, or nothing counts as ours.
      const current = document.activeElement
      if (
        current instanceof HTMLElement &&
        current !== document.body &&
        current !== active &&
        current.isConnected
      ) {
        return
      }
      const toggles = Array.from(container.querySelectorAll<HTMLElement>('[data-star-symbol]'))
      const find = (symbol: string): HTMLElement | undefined =>
        toggles.find((element) => element.dataset.starSymbol === symbol)
      const after = order
        .slice(index + 1)
        .map(find)
        .find(Boolean)
      const before = order.slice(0, Math.max(0, index)).reverse().map(find).find(Boolean)
      const target = after ?? before
      if (target) {
        target.focus()
        return
      }
      container.querySelector<HTMLElement>('#symbol-search')?.focus()
    })
  }

  function retry(): void {
    setRetrying(true)
    pendingFocusRef.current = 'retry'
    setAnnouncement('Refreshing the watchlist…')
    setAttempt((n) => n + 1)
  }

  function toggle(row: WatchlistRow): void {
    // No mutation may overlap a refetch: a request issued before the toggle
    // commits could land later and desync rows from the count.
    if (load.state !== 'ready' || narrow || mutation.pending.has(row.symbol)) {
      return
    }
    orderRef.current = displayedRows.map((item) => item.symbol)
    snapshotsRef.current.set(row.symbol, row)
    mutation.toggle(row.symbol, !row.starred)
  }

  return (
    <section
      ref={containerRef}
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
          {showRows && (
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

      {showRows && (
        <p className="status-message" data-testid="watchlist-count">
          {countLabel}
        </p>
      )}

      {showRows && load.truncated && (
        <p className="status-message" data-testid="watchlist-truncation-note">
          Showing first {load.rows.length} of {load.total} symbols.
        </p>
      )}

      {load.state === 'loading' && (
        <p className="status-message">
          {retrying || load.rows.length > 0 ? 'Refreshing…' : 'Loading symbol registry…'}
        </p>
      )}

      {(load.state === 'error' || (load.state === 'loading' && retrying)) && (
        <div>
          {load.state === 'error' && (
            <p className="status-message status-message--error" role="alert">
              Watchlist unavailable: {load.message}
            </p>
          )}
          {/* Read-only recovery: this is not a star mutation, so it stays
              available at every width, including the 375 read-only surface.
              The control stays mounted through the retry (aria-disabled, not
              native disabled) so focus survives until the load resolves. */}
          <button
            type="button"
            className="control-btn"
            aria-busy={retrying && load.state === 'loading' ? 'true' : undefined}
            aria-disabled={retrying && load.state === 'loading' ? 'true' : undefined}
            onClick={() => {
              if (retrying && load.state === 'loading') {
                return
              }
              retry()
            }}
          >
            Retry
          </button>
        </div>
      )}

      {mutationError !== null && (
        <p className="status-message status-message--error" role="alert">
          {mutationError}
        </p>
      )}

      {load.state === 'ready' && displayedRows.length === 0 && nextScanCount === 0 && (
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

      {load.state === 'ready' &&
        displayedRows.length === 0 &&
        load.truncated &&
        nextScanCount > 0 && (
          <div className="panel">
            <div className="panel-head">
              <h2>Starred symbols are outside this page</h2>
            </div>
            <p className="panel-foot">
              The next scan includes {nextScanCount} of {load.total} symbols, but none of them fall
              in the first {load.rows.length} registry rows shown here. Manage them with the CLI
              (<code>oft watchlist add|remove</code>) or a narrower registry page.
            </p>
          </div>
        )}

      {load.state === 'ready' && displayedRows.length > 0 && searching && matched.length === 0 && (
        <p className="status-message">
          No matches for “{query}”. Try an exact symbol such as BTCUSDT.
        </p>
      )}

      {showRows && (
        <SymbolGroup
          label="CRYPTO"
          items={groups.crypto}
          pending={mutation.pending}
          narrow={narrow}
          disabled={load.state !== 'ready'}
          onToggle={toggle}
        />
      )}
      {showRows && (
        <SymbolGroup
          label="TRADFI"
          items={groups.tradfi}
          pending={mutation.pending}
          narrow={narrow}
          disabled={load.state !== 'ready'}
          onToggle={toggle}
        />
      )}
    </section>
  )
}
