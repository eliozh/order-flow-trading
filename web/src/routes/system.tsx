import { useEffect, useState } from 'react'

import {
  fetchHealth,
  fetchReadiness,
  type HealthData,
  type HealthEnvelope,
  type ReadinessData,
  type ReadinessEnvelope,
} from '../api/status'
import { useCompact } from '../hooks/use-compact'

type SystemFetcher<T> = () => Promise<T>

interface SystemPageProps {
  fetchHealthFn?: SystemFetcher<HealthEnvelope>
  fetchReadinessFn?: SystemFetcher<ReadinessEnvelope>
  isCompact?: boolean
}

interface ReadyState {
  state: 'loading' | 'ready' | 'error'
  health?: HealthData
  readiness?: ReadinessData
  message?: string
}

function statusClass(status: string): string {
  if (status === 'ok' || status === 'ready') {
    return 'chip chip--ok'
  }
  if (status === 'degraded' || status === 'not_ready') {
    return 'chip chip--warn'
  }
  // UNKNOWN is not a failure; keep it neutral rather than danger.
  if (status === 'unknown') {
    return 'chip chip--muted'
  }
  return 'chip chip--danger'
}

interface DeltaPresentation {
  text: string
  className: string
  accessible: string
}

function presentDelta(raw: string): DeltaPresentation {
  const value = Number(raw)
  if (Number.isFinite(value) && value > 0) {
    return { text: `▲ +${raw}`, className: 'value-up', accessible: `positive ${raw}` }
  }
  if (Number.isFinite(value) && value < 0) {
    return { text: `▼ ${raw}`, className: 'value-down', accessible: `negative ${raw}` }
  }
  return { text: raw, className: 'value-flat', accessible: `zero ${raw}` }
}

export function SystemPage({
  fetchHealthFn = fetchHealth,
  fetchReadinessFn = fetchReadiness,
  isCompact,
}: SystemPageProps) {
  const [load, setLoad] = useState<ReadyState>({ state: 'loading' })
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    Promise.all([fetchHealthFn(), fetchReadinessFn()])
      .then(([healthEnvelope, readinessEnvelope]) => {
        if (!active) {
          return
        }
        const health = healthEnvelope.data
        const readiness = readinessEnvelope.data
        if (!health || !readiness) {
          setLoad({ state: 'error', message: 'Empty system response' })
          return
        }
        setLoad({ state: 'ready', health, readiness })
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
  }, [fetchHealthFn, fetchReadinessFn])

  if (load.state === 'loading') {
    return (
      <p className="status-message" role="status">
        Loading system status…
      </p>
    )
  }

  if (load.state === 'error') {
    return (
      <p className="status-message status-message--error" role="alert">
        System status unavailable: {load.message}
      </p>
    )
  }

  const health = load.health
  const readiness = load.readiness
  if (!health || !readiness) {
    return (
      <p className="status-message status-message--error" role="alert">
        System status unavailable: empty response
      </p>
    )
  }

  const scan = readiness.latest_scan
  const delta = scan ? presentDelta(scan.delta) : null

  return (
    <section className="system-page" aria-labelledby="system-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">System</p>
          <h1 id="system-heading">System status</h1>
          <p className="page-sub">
            daemon, storage checks, version, data directory, and the latest fixture scan.
          </p>
        </div>
        {compact && (
          <p className="readonly-note" role="note">
            Read-only at this width. Use a desktop viewport or the CLI for actions.
          </p>
        )}
      </div>

      <dl className="stat-grid">
        <div className="stat">
          <dt>Health</dt>
          <dd>
            <span className={statusClass(health.status)}>{health.status.toUpperCase()}</span>
          </dd>
          <p className="stat-note">GET /health</p>
        </div>
        <div className="stat">
          <dt>Readiness</dt>
          <dd>
            <span className={statusClass(readiness.status)}>{readiness.status.toUpperCase()}</span>
          </dd>
          <p className="stat-note">{readiness.checks.length} named checks</p>
        </div>
        <div className="stat">
          <dt>Version</dt>
          <dd>{readiness.version}</dd>
          <p className="stat-note">walking skeleton</p>
        </div>
        <div className="stat">
          <dt>Data directory</dt>
          <dd>
            <code>{readiness.data_dir}</code>
          </dd>
          <p className="stat-note">checked {readiness.checked_at}</p>
        </div>
      </dl>

      <section className="panel" aria-labelledby="checks-heading">
        <div className="panel-head">
          <h2 id="checks-heading">Readiness checks</h2>
          <span className="mono-muted">{readiness.checked_at}</span>
        </div>
        <table className="data-table">
          <caption className="visually-hidden">Named readiness checks</caption>
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">State</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {readiness.checks.map((check) => (
              <tr key={check.name}>
                <td className="mono">{check.name}</td>
                <td>
                  <span className={statusClass(check.status)}>{check.status.toUpperCase()}</span>
                </td>
                <td>{check.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel" aria-labelledby="scan-heading">
        <div className="panel-head">
          <h2 id="scan-heading">Latest fixture scan</h2>
          <span className="chip chip--calc">CALC</span>
        </div>
        {scan ? (
          <>
            <dl className="scan-grid">
              <div>
                <dt>Scan ID</dt>
                <dd className="mono">{scan.scan_id}</dd>
              </div>
              <div>
                <dt>Symbol · period</dt>
                <dd>
                  {scan.symbol} · {scan.period}
                </dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>
                  <span className={statusClass(scan.status === 'DONE' ? 'ok' : 'degraded')}>
                    {scan.status}
                  </span>
                </dd>
              </div>
              <div>
                <dt>Delta</dt>
                <dd className={delta?.className} aria-label={`Delta ${delta?.accessible}`}>
                  {delta?.text}
                </dd>
              </div>
              <div>
                <dt>CVD</dt>
                <dd>{scan.cvd}</dd>
              </div>
              <div>
                <dt>Trades</dt>
                <dd>{scan.trade_count}</dd>
              </div>
              <div>
                <dt>Buy / sell volume</dt>
                <dd>
                  {scan.buy_volume} / {scan.sell_volume}
                </dd>
              </div>
              <div>
                <dt>Quality</dt>
                <dd>{scan.data_quality}</dd>
              </div>
            </dl>
            <p className="panel-foot">
              Parquet part <code>{scan.manifest_path}</code> · {scan.parquet_rows} rows
            </p>
          </>
        ) : (
          <p className="status-message" role="status">
            No fixture scan yet. Run{' '}
            <code>oft scan run --fixture tests/fixtures/market/btcusdt_agg-trades-sample.csv</code>.
          </p>
        )}
      </section>

      <section className="panel" aria-labelledby="diagnostics-heading">
        <div className="panel-head">
          <h2 id="diagnostics-heading">Diagnostics</h2>
        </div>
        <p className="panel-foot">
          CLI equivalents: <code>oft doctor</code>, <code>oft status --json</code>,{' '}
          <code>oft scan status &lt;scan-id&gt; --json</code>. Logs redact bearer tokens.
        </p>
      </section>
    </section>
  )
}
