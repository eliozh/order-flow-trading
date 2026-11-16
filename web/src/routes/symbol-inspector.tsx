import { useEffect, useRef, useState } from 'react'

import {
  fetchSymbol,
  type SymbolContract,
  type SymbolCoverage,
  type SymbolDetailEnvelope,
} from '../api/symbols'
import { fetchLatestReportForSymbol } from '../api/charts'
import type { ReportSummary } from '../api/reports'
import { useCompact } from '../hooks/use-compact'

interface SymbolInspectorPageProps {
  symbol: string
  fetchSymbolFn?: (symbol: string) => Promise<SymbolDetailEnvelope>
  fetchLatestReportFn?: (symbol: string, signal?: AbortSignal) => Promise<ReportSummary | null>
  isCompact?: boolean
}

interface InspectorState {
  state: 'loading' | 'ready' | 'error'
  contract?: SymbolContract
  coverage?: SymbolCoverage
  message?: string
}

function valueOrDash(value: string | null): string {
  return value === null || value === '' ? '—' : value
}

export function SymbolInspectorPage({
  symbol,
  fetchSymbolFn = fetchSymbol,
  fetchLatestReportFn = fetchLatestReportForSymbol,
  isCompact,
}: SymbolInspectorPageProps) {
  const [load, setLoad] = useState<InspectorState>({ state: 'loading' })
  const [latestReport, setLatestReport] = useState<ReportSummary | null>(null)
  const [attempt, setAttempt] = useState(0)
  const compact = useCompact(isCompact)
  const alertRef = useRef<HTMLParagraphElement>(null)
  const fetcherRef = useRef(fetchSymbolFn)

  useEffect(() => {
    fetcherRef.current = fetchSymbolFn
  }, [fetchSymbolFn])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setLatestReport(null)
    fetchLatestReportFn(symbol, controller.signal)
      .then((report) => {
        if (active) {
          setLatestReport(report)
        }
      })
      .catch(() => {
        // A missing or failed report read never breaks the inspector.
        if (active) {
          setLatestReport(null)
        }
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [symbol, fetchLatestReportFn])

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    fetcherRef
      .current(symbol)
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
  }, [symbol, attempt])

  useEffect(() => {
    if (load.state === 'error') {
      alertRef.current?.focus()
    }
  }, [load.state])

  const contract = load.contract
  const coverage = load.coverage

  return (
    <section className="system-page" aria-labelledby="symbol-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">Watchlist / symbol inspector</p>
          <h1 id="symbol-heading">Symbol {symbol}</h1>
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

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          Loading symbol…
        </p>
      )}

      {load.state === 'error' && (
        <div>
          <p
            ref={alertRef}
            tabIndex={-1}
            className="status-message status-message--error"
            role="alert"
          >
            Symbol unavailable: {load.message}
          </p>
          <div className="action-row">
            <button
              type="button"
              className="control-btn"
              onClick={() => setAttempt((current) => current + 1)}
            >
              Retry
            </button>
            <a className="control-link" href="/watchlist">
              Back to watchlist
            </a>
          </div>
        </div>
      )}

      {load.state === 'ready' && (!contract || !coverage) && (
        <p
          ref={alertRef}
          tabIndex={-1}
          className="status-message status-message--error"
          role="alert"
        >
          Symbol unavailable: empty response
        </p>
      )}

      {load.state === 'ready' && contract && coverage && (
        <>
          {latestReport !== null && (
            <p className="action-row">
              <a
                className="control-link"
                href={`/reports/${encodeURIComponent(latestReport.report_id)}/chart`}
              >
                Open latest report chart
              </a>
            </p>
          )}
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
        </>
      )}
    </section>
  )
}
