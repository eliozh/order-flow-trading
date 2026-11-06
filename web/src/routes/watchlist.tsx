import { useEffect, useState } from 'react'

import {
  fetchSymbols,
  filterSymbols,
  groupSymbols,
  type SymbolContract,
  type SymbolListEnvelope,
} from '../api/symbols'

interface WatchlistPageProps {
  fetchSymbolsFn?: () => Promise<SymbolListEnvelope>
  isCompact?: boolean
}

interface WatchlistState {
  state: 'loading' | 'ready' | 'error'
  items: SymbolContract[]
  message?: string
}

function detectCompact(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia('(max-width: 767px)').matches
}

function useCompact(override?: boolean): boolean {
  const [matches, setMatches] = useState<boolean>(() => detectCompact())

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return
    }
    const query = window.matchMedia('(max-width: 767px)')
    const update = () => setMatches(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  return override ?? matches
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
  const [load, setLoad] = useState<WatchlistState>({ state: 'loading', items: [] })
  const [query, setQuery] = useState('')
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading', items: [] })
    fetchSymbolsFn()
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', items: [], message: 'Symbol registry response was empty.' })
          return
        }
        setLoad({ state: 'ready', items: data.items })
      })
      .catch((error: unknown) => {
        if (active) {
          setLoad({
            state: 'error',
            items: [],
            message: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      })
    return () => {
      active = false
    }
  }, [fetchSymbolsFn])

  const matched = filterSymbols(load.items, query)
  const groups = groupSymbols(matched)

  return (
    <section className="system-page" aria-labelledby="watchlist-heading" data-readonly={compact}>
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
          <span className="mono-muted" role="status">
            {matched.length} of {load.items.length} matches
          </span>
        </div>
        <div className="panel-foot">
          <input
            id="symbol-search"
            type="search"
            value={query}
            placeholder="BTCUSDT or gold"
            autoComplete="off"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      </section>

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          Loading symbol registry…
        </p>
      )}

      {load.state === 'error' && (
        <p className="status-message status-message--error" role="alert">
          Symbol registry unavailable: {load.message}
        </p>
      )}

      {load.state === 'ready' && load.items.length === 0 && (
        <p className="status-message" role="status">
          No symbols in the registry. Run <code>oft watchlist list</code> after the daemon ingests
          exchangeInfo.
        </p>
      )}

      {load.state === 'ready' && load.items.length > 0 && matched.length === 0 && (
        <p className="status-message" role="status">
          No matches for “{query}”. Try an exact symbol such as BTCUSDT.
        </p>
      )}

      {load.state === 'ready' && <SymbolGroup label="CRYPTO" items={groups.crypto} />}
      {load.state === 'ready' && <SymbolGroup label="TRADFI" items={groups.tradfi} />}
    </section>
  )
}
