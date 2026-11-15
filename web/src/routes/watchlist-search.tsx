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

interface WatchlistSearchPageProps {
  fetchWatchlistFn?: (signal?: AbortSignal) => Promise<WatchlistEnvelope>
  setStarFn?: (symbol: string, starred: boolean) => Promise<StarEnvelope>
  isNarrow?: boolean
}

interface SearchState {
  state: 'loading' | 'ready' | 'error'
  rows: WatchlistRow[]
  total: number
  message?: string
}

const EMPTY_STATE: SearchState = { state: 'loading', rows: [], total: 0 }

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
  const narrow = useNarrowCompact(isNarrow)
  const watchlistRef = useRef(fetchWatchlistFn)
  const setStarRef = useRef(setStarFn)
  const snapshotsRef = useRef<Map<string, WatchlistRow>>(new Map())
  const announcedQueryRef = useRef('')

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
        setLoad({ state: 'ready', rows: data.rows, total: data.total })
        setAnnouncement(`${data.rows.length} symbols available to add to the next scan.`)
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
    return () => controller.abort()
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
      setMutationError(`Could not confirm the ${symbol} change; refreshing the symbol list.`)
      snapshotsRef.current.delete(symbol)
      setAttempt((n) => n + 1)
    },
  })

  const matched = filterSymbols(load.rows, query)
  const groups = groupSymbols(matched)
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

  function toggle(row: WatchlistRow): void {
    if (narrow || mutation.pending.has(row.symbol)) {
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
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
        <div className="table-wrap" role="region" aria-label={`${label} search results`} tabIndex={0}>
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
                          disabled={blocked}
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
            Search the registry and add symbols to the next scan. Adding only changes the local
            star flag; the registry stays upstream/read-only.
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
          {load.state === 'ready' && <span className="mono-muted">{matched.length} matches</span>}
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

      {load.state === 'loading' && <p className="status-message">Loading symbol registry…</p>}

      {load.state === 'error' && (
        <div>
          <p className="status-message status-message--error" role="alert">
            Symbol search unavailable: {load.message}
          </p>
          {/* Read-only recovery: the Retry button is not a star mutation, so it
              stays available at every width, including the 375 surface. */}
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

      {load.state === 'ready' && matched.length === 0 && (
        <p className="status-message">{noMatches}</p>
      )}

      {load.state === 'ready' && group('CRYPTO', groups.crypto)}
      {load.state === 'ready' && group('TRADFI', groups.tradfi)}

      <p className="status-message" style={{ marginTop: 12 }}>
        <a className="control-link" href="/watchlist">
          Back to watchlist
        </a>
      </p>
    </section>
  )
}
