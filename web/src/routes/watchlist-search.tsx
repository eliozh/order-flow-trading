import { useEffect, useMemo, useRef, useState } from 'react'

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

/**
 * Display cap for one search result set.
 *
 * The server already caps the watchlist view at ``_WATCHLIST_ROW_CAP`` (2,000
 * rows) even though ingest can hold 5,000 (``MAX_EXCHANGE_INFO_SYMBOLS``); the
 * symbols endpoint uses the matching ``_MAX_LIMIT`` of 2,000, so the client
 * never receives more than 2,000 rows either way. Rendering all of them
 * synchronously on every keystroke is still too much, so the page displays at
 * most 200 grouped matches and tells the user to refine the search.
 */
const SEARCH_RENDER_LIMIT = 200

interface WatchlistSearchPageProps {
  fetchWatchlistFn?: (signal?: AbortSignal) => Promise<WatchlistEnvelope>
  setStarFn?: (symbol: string, starred: boolean) => Promise<StarEnvelope>
  isNarrow?: boolean
}

interface SearchState {
  state: 'loading' | 'ready' | 'error'
  rows: WatchlistRow[]
  total: number
  truncated: boolean
  message?: string
}

const EMPTY_STATE: SearchState = { state: 'loading', rows: [], total: 0, truncated: false }

function tradable(row: WatchlistRow): boolean {
  return row.status === 'TRADING'
}

export function WatchlistSearchPage({
  fetchWatchlistFn = fetchWatchlist,
  setStarFn = setStar,
  isNarrow,
}: WatchlistSearchPageProps) {
  const [load, setLoad] = useState<SearchState>(EMPTY_STATE)
  const [query, setQuery] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [mutationError, setMutationError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  // True from a Retry click until the retry fetch settles; keeps the Retry
  // control mounted (aria-busy/aria-disabled) so focus is never dropped.
  const [retrying, setRetrying] = useState(false)
  const narrow = useNarrowCompact(isNarrow)
  const watchlistRef = useRef(fetchWatchlistFn)
  const setStarRef = useRef(setStarFn)
  const snapshotsRef = useRef<Map<string, WatchlistRow>>(new Map())
  const announcedQueryRef = useRef('')
  // Scope every focus query to this page; a stale frame must never touch
  // another route.
  const containerRef = useRef<HTMLElement | null>(null)
  // What should receive focus after the next ready load: a symbol, 'retry',
  // or null (nothing).
  const pendingFocusRef = useRef<string | null>(null)
  // The exact unknown-outcome alert message this refetch was started for. A
  // load success clears only that message; a newer rollback alert survives.
  const unknownOutcomeErrorRef = useRef<string | null>(null)

  useEffect(() => {
    watchlistRef.current = fetchWatchlistFn
  }, [fetchWatchlistFn])
  useEffect(() => {
    setStarRef.current = setStarFn
  }, [setStarFn])

  useEffect(() => {
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    watchlistRef
      .current(controller.signal)
      .then((envelope) => {
        const data = envelope.data
        if (!data) {
          throw new Error('The symbol search response was empty.')
        }
        setLoad({
          state: 'ready',
          rows: data.rows,
          total: data.total,
          truncated: data.truncated,
        })
        setRetrying(false)
        // Clear the alert this refetch was started for, but never a newer
        // mutation alert that landed while the request was in flight.
        const unknownOutcomeMessage = unknownOutcomeErrorRef.current
        setMutationError((current) =>
          current !== null && current === unknownOutcomeMessage ? null : current,
        )
        unknownOutcomeErrorRef.current = null
        setAnnouncement(`${data.total} symbols available to add to the next scan.`)
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
    return () => controller.abort()
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
      const message = `Could not confirm the ${symbol} change; refreshing the symbol list.`
      unknownOutcomeErrorRef.current = message
      setMutationError(message)
      // Return focus to the originating toggle when the refetch resolves.
      pendingFocusRef.current = symbol
      snapshotsRef.current.delete(symbol)
      setAttempt((n) => n + 1)
    },
  })

  // Rows survive a refetch: they are shown when ready or while a previous page
  // is still loaded, so the tables never blink out of the DOM.
  const showRows = load.state === 'ready' || load.rows.length > 0
  const matched = useMemo(() => filterSymbols(load.rows, query), [load.rows, query])
  const rendered = useMemo(() => matched.slice(0, SEARCH_RENDER_LIMIT), [matched])
  const groups = useMemo(() => groupSymbols(rendered), [rendered])
  const trimmedQuery = query.trim()
  const noMatches = `No matches for “${trimmedQuery}”. Try an exact symbol such as BTCUSDT.`

  // One polite announcement for filter-result changes.
  useEffect(() => {
    if (load.state !== 'ready') {
      return
    }
    if (trimmedQuery === announcedQueryRef.current) {
      return
    }
    announcedQueryRef.current = trimmedQuery
    if (trimmedQuery === '') {
      return
    }
    setAnnouncement(
      `${matched.length} ${matched.length === 1 ? 'match' : 'matches'} for “${trimmedQuery}”.`,
    )
  }, [load.state, trimmedQuery, matched.length])

  function retry(): void {
    setRetrying(true)
    pendingFocusRef.current = 'retry'
    setAnnouncement('Refreshing the symbol list…')
    setAttempt((n) => n + 1)
  }

  function toggle(row: WatchlistRow): void {
    // No mutation may overlap a refetch: a request issued before the toggle
    // commits could land later and desync the page from the server.
    if (load.state !== 'ready' || narrow || mutation.pending.has(row.symbol)) {
      return
    }
    snapshotsRef.current.set(row.symbol, row)
    mutation.toggle(row.symbol, !row.starred)
  }

  function group(label: string, items: WatchlistRow[]) {
    if (items.length === 0) {
      return null
    }
    return (
      <section className="panel" aria-labelledby={`search-group-${label.toLowerCase()}`}>
        <div className="panel-head">
          <h2 id={`search-group-${label.toLowerCase()}`}>
            {label} ({items.length})
          </h2>
        </div>
        <div
          className="table-wrap"
          role="region"
          aria-label={`${label} search results`}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
          tabIndex={0}
        >
          <table className="data-table">
            <caption className="visually-hidden">{label} symbol search results</caption>
            <thead>
              <tr>
                <th scope="col">Symbol</th>
                <th scope="col">Contract type</th>
                <th scope="col">Tick</th>
                <th scope="col">Step</th>
                <th scope="col">Status</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const isPending = mutation.pending.has(row.symbol)
                // A non-TRADING row may still be removed if it is already
                // starred; only the add direction is blocked.
                const blocked = !row.starred && !tradable(row)
                const reasonId = `search-reason-${row.symbol}`
                return (
                  <tr key={row.symbol}>
                    <td>
                      <a
                        className="rowlink mono"
                        href={`/watchlist/${encodeURIComponent(row.symbol)}`}
                      >
                        {row.symbol}
                      </a>
                    </td>
                    <td>{row.contract_type}</td>
                    <td className="mono">{row.tick_size}</td>
                    <td className="mono">{row.step_size}</td>
                    <td>
                      {row.status}
                      {!tradable(row) && (
                        <span id={reasonId} className="status-message">
                          {' '}
                          Cannot add: status is {row.status}.
                        </span>
                      )}
                    </td>
                    <td>
                      {narrow ? (
                        <StarState starred={row.starred} />
                      ) : (
                        <StarToggle
                          symbol={row.symbol}
                          starred={row.starred}
                          pending={isPending}
                          disabled={blocked || load.state !== 'ready'}
                          describedBy={blocked ? reasonId : undefined}
                          onToggle={() => toggle(row)}
                        />
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>
    )
  }

  return (
    <section
      ref={containerRef}
      className="system-page"
      lang="en"
      aria-labelledby="watchlist-heading"
      data-readonly={narrow}
    >
      <p className="visually-hidden" role="status" aria-live="polite" data-testid="search-live">
        {announcement}
      </p>
      <div className="page-head">
        <div>
          <p className="kicker">Watchlist / search</p>
          <h1 id="watchlist-heading">Watchlist</h1>
          <p className="page-sub">
            Search the registry and add symbols to the next scan. Adding only changes the local star
            flag; the registry stays upstream/read-only.
          </p>
        </div>
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
          {showRows && <span className="mono-muted">{matched.length} matches</span>}
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

      {load.state === 'loading' && (
        <p className="status-message">
          {retrying || load.rows.length > 0 ? 'Refreshing…' : 'Loading symbol registry…'}
        </p>
      )}

      {(load.state === 'error' || (load.state === 'loading' && retrying)) && (
        <div>
          {load.state === 'error' && (
            <p className="status-message status-message--error" role="alert">
              Symbol search unavailable: {load.message}
            </p>
          )}
          {/* Read-only recovery: the Retry button is not a star mutation, so it
              stays available at every width, including the 375 surface. It
              stays mounted through the retry (aria-disabled, not native
              disabled) so focus survives until the load resolves. */}
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

      {showRows && load.truncated && (
        <p className="status-message" data-testid="search-truncation-note">
          Showing first {load.rows.length} of {load.total} symbols.
        </p>
      )}

      {showRows && matched.length > rendered.length && (
        <p className="status-message" data-testid="search-result-limit-note">
          Showing the first {rendered.length} of {matched.length} matches. Refine the search to
          narrow the list.
        </p>
      )}

      {load.state === 'ready' && matched.length === 0 && (
        <p className="status-message">{noMatches}</p>
      )}

      {showRows && group('CRYPTO', groups.crypto)}
      {showRows && group('TRADFI', groups.tradfi)}

      <p className="status-message" style={{ marginTop: 12 }}>
        <a className="control-link" href="/watchlist">
          Back to watchlist
        </a>
      </p>
    </section>
  )
}
