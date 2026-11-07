import { useEffect, useState } from 'react'

import { fetchScan, type ScanData, type ScanEnvelope } from '../api/scans'
import { DataQualityBadge } from '../components/data-quality'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface ScanDetailPageProps {
  scanId: string
  fetchScanFn?: (scanId: string) => Promise<ScanEnvelope>
  isCompact?: boolean
}

interface ScanState {
  state: 'loading' | 'ready' | 'error'
  scan?: ScanData
  message?: string
}

type StageState = 'DONE' | 'RUNNING' | 'PENDING' | 'FAILED'

function stages(scan: ScanData): { name: string; state: StageState; detail: string }[] {
  const failed = scan.status === 'FAILED'
  const running = scan.status === 'RUNNING'
  const dataDone = scan.trade_count > 0
  const metricsDone = Boolean(scan.metrics_manifest_json)
  const reportDone = Boolean(scan.report_id)
  return [
    {
      name: 'DATA',
      state: dataDone ? 'DONE' : failed ? 'FAILED' : running ? 'RUNNING' : 'PENDING',
      detail: `${scan.trade_count} trades in ${scan.period} bars`,
    },
    {
      name: 'METRICS',
      state: metricsDone ? 'DONE' : failed ? 'FAILED' : dataDone ? 'RUNNING' : 'PENDING',
      detail: metricsDone ? 'metric manifest recorded' : 'awaiting metric manifest',
    },
    {
      name: 'REPORT',
      state: reportDone ? 'DONE' : failed ? 'FAILED' : metricsDone ? 'RUNNING' : 'PENDING',
      detail: reportDone ? `report ${scan.report_id}` : 'no CALC report yet',
    },
  ]
}

export function ScanDetailPage({
  scanId,
  fetchScanFn = fetchScan,
  isCompact,
}: ScanDetailPageProps) {
  const [load, setLoad] = useState<ScanState>({ state: 'loading' })
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    fetchScanFn(scanId)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', message: 'Scan response was empty.' })
          return
        }
        setLoad({ state: 'ready', scan: envelope.data })
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
  }, [scanId, fetchScanFn])

  if (load.state === 'loading') {
    return (
      <p className="status-message" role="status">
        Loading scan {scanId}…
      </p>
    )
  }

  if (load.state === 'error' || !load.scan) {
    return (
      <section className="panel" role="alert">
        <h1>Scan unavailable</h1>
        <p>{load.message ?? 'Scan not found.'}</p>
        <a className="btn" href="/scans">
          Back to scans
        </a>
      </section>
    )
  }

  const scan = load.scan
  return (
    <section
      className="system-page scan-page"
      aria-labelledby="scan-heading"
      data-readonly={compact}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/scans">
              Scans
            </a>{' '}
            / detail
          </p>
          <h1 id="scan-heading" className="mono">
            {scan.scan_id}
          </h1>
          <p className="page-sub">
            {scan.symbol} · {scan.period} · trigger {scan.trigger_source} · source{' '}
            {scan.source_mode}
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="CALC" />
          <DataQualityBadge status={scan.data_quality} />
        </div>
      </div>

      <dl className="scan-grid">
        <div>
          <dt>Status</dt>
          <dd>{scan.status}</dd>
        </div>
        <div>
          <dt>Window</dt>
          <dd className="mono">
            {scan.window_start} → {scan.window_end}
          </dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd className="mono">{scan.started_at}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd className="mono">{scan.finished_at || '—'}</dd>
        </div>
        <div>
          <dt>Trades</dt>
          <dd className="mono">{scan.trade_count}</dd>
        </div>
        <div>
          <dt>Delta</dt>
          <dd className="mono">{scan.delta}</dd>
        </div>
        <div>
          <dt>CVD</dt>
          <dd className="mono">{scan.cvd}</dd>
        </div>
        <div>
          <dt>Report</dt>
          <dd className="mono">
            {scan.report_id ? (
              <a className="rowlink" href={`/reports/${encodeURIComponent(scan.report_id)}`}>
                {scan.report_id}
              </a>
            ) : (
              '—'
            )}
          </dd>
        </div>
      </dl>

      <section className="panel" aria-labelledby="stages-heading">
        <div className="panel-head">
          <h2 id="stages-heading">Stages</h2>
          <span className="mono-muted">DATA → METRICS → REPORT</span>
        </div>
        <ol className="stage-list" aria-label="Named scan stages">
          {stages(scan).map((stage) => (
            <li key={stage.name} className={`stage stage--${stage.state.toLowerCase()}`}>
              <span className="stage-name">{stage.name}</span>
              <span className="chip">{stage.state}</span>
              <span className="panel-foot">{stage.detail}</span>
            </li>
          ))}
        </ol>
        <p className="panel-foot" role="status">
          No LLM stage exists in this metrics-only slice.
        </p>
      </section>
    </section>
  )
}
