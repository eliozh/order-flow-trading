import { useEffect, useState } from 'react'

import {
  fetchSymbol,
  type SymbolContract,
  type SymbolCoverage,
  type SymbolDetailEnvelope,
} from '../api/symbols'

interface SymbolInspectorPageProps {
  symbol: string
  fetchSymbolFn?: (symbol: string) => Promise<SymbolDetailEnvelope>
  isCompact?: boolean
}

interface InspectorState {
  state: 'loading' | 'ready' | 'error'
  contract?: SymbolContract
  coverage?: SymbolCoverage
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

function valueOrDash(value: string | null): string {
  return value === null || value === '' ? '—' : value
}

export function SymbolInspectorPage({
  symbol,
  fetchSymbolFn = fetchSymbol,
  isCompact,
}: SymbolInspectorPageProps) {
  const [load, setLoad] = useState<InspectorState>({ state: 'loading' })
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    fetchSymbolFn(symbol)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', message: 'Symbol response was empty.' })
          return
        }
        setLoad({ state: 'ready', contract: data.contract, coverage: data.coverage })
      })
      .catch((error: unknown) => {
        if (active) {
          setLoad({
            state: 'error',
            message: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      })
    return () => {
      active = false
    }
  }, [fetchSymbolFn, symbol])

  if (load.state === 'loading') {
    return (
      <p className="status-message" role="status">
        Loading symbol…
      </p>
    )
  }

  if (load.state === 'error') {
    return (
      <p className="status-message status-message--error" role="alert">
        Symbol unavailable: {load.message}
      </p>
    )
  }

  const contract = load.contract
  const coverage = load.coverage
  if (!contract || !coverage) {
    return (
      <p className="status-message status-message--error" role="alert">
        Symbol unavailable: empty response
      </p>
    )
  }

  return (
    <section className="system-page" aria-labelledby="symbol-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">Watchlist / symbol inspector</p>
          <h1 id="symbol-heading">Symbol {contract.symbol}</h1>
          <p className="page-sub">
            Contract specs, listing date, and data-coverage placeholders. Read-only upstream data.
          </p>
        </div>
        {compact && (
          <p className="readonly-note" role="note">
            Read-only at this width. Use a desktop viewport or the CLI for actions.
          </p>
        )}
      </div>

      <section className="panel" aria-labelledby="contract-heading">
        <div className="panel-head">
          <h2 id="contract-heading">Contract specs</h2>
          <span className="chip chip--calc">CALC</span>
        </div>
        <dl className="scan-grid">
          <div>
            <dt>Symbol</dt>
            <dd className="mono">{contract.symbol}</dd>
          </div>
          <div>
            <dt>Base / quote</dt>
            <dd>
              {contract.base_asset} / {contract.quote_asset}
            </dd>
          </div>
          <div>
            <dt>Contract type</dt>
            <dd>{contract.contract_type}</dd>
          </div>
          <div>
            <dt>Underlying</dt>
            <dd>{valueOrDash(contract.underlying_type)}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>
              <span className="chip chip--ok">{contract.status}</span>
            </dd>
          </div>
          <div>
            <dt>Tick size</dt>
            <dd className="mono">{contract.tick_size}</dd>
          </div>
          <div>
            <dt>Step size</dt>
            <dd className="mono">{contract.step_size}</dd>
          </div>
          <div>
            <dt>Min qty</dt>
            <dd className="mono">{contract.min_qty}</dd>
          </div>
          <div>
            <dt>Min notional</dt>
            <dd className="mono">{contract.min_notional}</dd>
          </div>
          <div>
            <dt>Price precision</dt>
            <dd>{contract.price_precision}</dd>
          </div>
          <div>
            <dt>Quantity precision</dt>
            <dd>{contract.quantity_precision}</dd>
          </div>
          <div>
            <dt>Listing date</dt>
            <dd className="mono">{contract.listed_date}</dd>
          </div>
        </dl>
      </section>

      <section className="panel" aria-labelledby="coverage-heading">
        <div className="panel-head">
          <h2 id="coverage-heading">Data coverage</h2>
          <span className="mono-muted">placeholders</span>
        </div>
        <dl className="scan-grid">
          <div>
            <dt>Coverage status</dt>
            <dd>{coverage.status}</dd>
          </div>
          <div>
            <dt>Coverage %</dt>
            <dd>{coverage.coverage_pct === null ? '—' : `${coverage.coverage_pct}%`}</dd>
          </div>
          <div>
            <dt>History start</dt>
            <dd className="mono">{coverage.history_start}</dd>
          </div>
          <div>
            <dt>bookTicker</dt>
            <dd>{coverage.book_ticker}</dd>
          </div>
          <div>
            <dt>Liquidation history</dt>
            <dd>{coverage.liquidation_history}</dd>
          </div>
          <div>
            <dt>Latest metric</dt>
            <dd>{valueOrDash(coverage.latest_metric_at)}</dd>
          </div>
        </dl>
        <p className="panel-foot">{coverage.notes.join(' ')}</p>
      </section>
    </section>
  )
}
