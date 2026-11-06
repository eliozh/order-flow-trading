import { useEffect, useRef, useState } from 'react'

import {
  fetchSymbols,
  filterSymbols,
  groupSymbols,
  type SymbolContract,
  type SymbolListEnvelope,
  type SymbolQuery,
} from '../api/symbols'
import { useCompact } from '../hooks/use-compact'

const SEARCH_DEBOUNCE_MS = 200

interface WatchlistPageProps {
  fetchSymbolsFn?: (query?: SymbolQuery) => Promise<SymbolListEnvelope>
  isCompact?: boolean
}

interface WatchlistState {
  state: 'loading' | 'ready' | 'error'
  items: SymbolContract[]
  total: number
  message?: string
}

function SymbolGroup({ label, items }: { label: string; items: SymbolContract[] }) {
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
      <div className="table-wrap" role="region" aria-label={`${label} symbols table`} tabIndex={0}>
        <table className="data-table">
          <caption className="visually-hidden">{label} symbols in the registry</caption>
          <thead>
            <tr>
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
            {items.map((item) => (
              <tr key={item.symbol}>
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
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function WatchlistPage({ fetchSymbolsFn = fetchSymbols, isCompact }: WatchlistPageProps) {
  const [load, setLoad] = useState<WatchlistState>({
    state: 'loading',
    items: [],
    total: 0,
  })
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [attempt, setAttempt] = useState(0)
  const compact = useCompact(isCompact)
  const fetcherRef = useRef(fetchSymbolsFn)

  useEffect(() => {
    fetcherRef.current = fetchSymbolsFn
  }, [fetchSymbolsFn])

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query.trim()), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  useEffect(() => {
    let active = true
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    const request: SymbolQuery | undefined = debouncedQuery ? { q: debouncedQuery } : undefined
    fetcherRef
      .current(request)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({
            state: 'error',
            items: [],
            total: 0,
            message: 'Symbol registry response was empty.',
          })
          setAnnouncement('Symbol registry unavailable.')
          return
        }
        setLoad({ state: 'ready', items: data.items, total: data.total })
        setAnnouncement(
          data.total === 0
            ? 'Symbol registry is empty.'
            : `${data.items.length} of ${data.total} symbols shown.`,
        )
      })
      .catch((error: unknown) => {
        if (!active) {
          return
        }
        setLoad({
          state: 'error',
          items: [],
          total: 0,
          message: error instanceof Error ? error.message : 'Unknown error',
        })
        setAnnouncement('Symbol registry unavailable.')
      })
    return () => {
      active = false
    }
  }, [debouncedQuery, attempt])

  const matched = filterSymbols(load.items, query)
  const groups = groupSymbols(matched)
  const truncated = load.items.length < load.total
  const searching = query.trim() !== ''

  return (
    <section className="system-page" aria-labelledby="watchlist-heading" data-readonly={compact}>
      <p className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>
      <div className="page-head">
        <div>
          <p className="kicker">Watchlist / symbols</p>
          <h1 id="watchlist-heading">Watchlist</h1>
          <p className="page-sub">
            Read-only symbol registry grouped by Crypto and TradFi. Star mutation is not part of
            this slice.
          </p>
        </div>
        {compact && (
          <p className="readonly-note" role="note">
            Read-only at this width. Use a desktop viewport or the CLI for actions.
          </p>
        )}
      </div>

      <section className="panel" aria-label="Symbol search">
        <div className="panel-head">
          <label htmlFor="symbol-search">Search symbols</label>
          <span className="mono-muted">
            {matched.length} of {load.total} symbols
          </span>
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

      {truncated && (
        <p className="status-message">
          Showing first {load.items.length} of {load.total} symbols.
        </p>
      )}

      {load.state === 'loading' && <p className="status-message">Loading symbol registry…</p>}

      {load.state === 'error' && (
        <div>
          <p className="status-message status-message--error" role="alert">
            Symbol registry unavailable: {load.message}
          </p>
          <button type="button" className="control-btn" onClick={() => setAttempt((n) => n + 1)}>
            Retry
          </button>
        </div>
      )}

      {load.state === 'ready' && !searching && load.items.length === 0 && (
        <p className="status-message">
          No symbols in the registry. Run <code>oft watchlist list</code> after the daemon ingests
          exchangeInfo.
        </p>
      )}

      {load.state === 'ready' && searching && matched.length === 0 && (
        <p className="status-message">
          No matches for “{query}”. Try an exact symbol such as BTCUSDT.
        </p>
      )}

      {load.state === 'ready' && <SymbolGroup label="CRYPTO" items={groups.crypto} />}
      {load.state === 'ready' && <SymbolGroup label="TRADFI" items={groups.tradfi} />}
    </section>
  )
}
