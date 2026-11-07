import { useEffect, useState } from 'react'

import { fetchScans, type ScanData, type ScanListEnvelope } from '../api/scans'
import { DataQualityBadge } from '../components/data-quality'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface ScansPageProps {
  fetchScansFn?: () => Promise<ScanListEnvelope>
  isCompact?: boolean
}

interface ScansState {
  state: 'loading' | 'ready' | 'error'
  items: ScanData[]
  total: number
  message?: string
}

export function ScansPage({ fetchScansFn = fetchScans, isCompact }: ScansPageProps) {
  const [load, setLoad] = useState<ScansState>({ state: 'loading', items: [], total: 0 })
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    fetchScansFn()
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({
            state: 'error',
            items: [],
            total: 0,
            message: 'Scan list response was empty.',
          })
          return
        }
        setLoad({ state: 'ready', items: envelope.data.items, total: envelope.data.total })
      })
      .catch((error: unknown) => {
        if (active) {
          setLoad({
            state: 'error',
            items: [],
            total: 0,
            message: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      })
    return () => {
      active = false
    }
  }, [fetchScansFn])

  return (
    <section className="system-page" aria-labelledby="scans-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">Scans</p>
          <h1 id="scans-heading">Scan history</h1>
          <p className="page-sub">
            Manual metrics-only scans run DATA → METRICS → REPORT. No LLM step exists in this build.
          </p>
        </div>
        <ProvenanceStamp kind="CALC" />
      </div>

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          Loading scans…
        </p>
      )}

      {load.state === 'error' && (
        <p className="status-message status-message--error" role="alert">
          Scan history unavailable: {load.message}
        </p>
      )}

      {load.state === 'ready' && load.items.length === 0 && (
        <section className="panel" role="status">
          <h2>No scans yet</h2>
          <p>
            Run{' '}
            <code>oft scan run --metrics-only --symbol BTCUSDT --period 4h --source backfill</code>.
          </p>
        </section>
      )}

      {load.state === 'ready' && load.items.length > 0 && (
        <section className="panel" aria-labelledby="scan-table-heading">
          <div className="panel-head">
            <h2 id="scan-table-heading">Scans ({load.total})</h2>
            <span className="mono-muted">newest first</span>
          </div>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
          <div className="table-wrap" role="region" aria-label="Scans table" tabIndex={0}>
            <table className="data-table">
              <caption className="visually-hidden">Scan history, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Scan</th>
                  <th scope="col">Symbol</th>
                  <th scope="col">Period</th>
                  <th scope="col">Status</th>
                  <th scope="col">Quality</th>
                  <th scope="col">Report</th>
                </tr>
              </thead>
              <tbody>
                {load.items.map((scan) => (
                  <tr key={scan.scan_id}>
                    <td>
                      <a
                        className="rowlink mono"
                        href={`/scans/${encodeURIComponent(scan.scan_id)}`}
                      >
                        {scan.scan_id}
                      </a>
                    </td>
                    <td className="mono">{scan.symbol}</td>
                    <td>{scan.period}</td>
                    <td>{scan.status}</td>
                    <td>
                      <DataQualityBadge status={scan.data_quality} />
                    </td>
                    <td className="mono">
                      {scan.report_id ? (
                        <a
                          className="rowlink"
                          href={`/reports/${encodeURIComponent(scan.report_id)}`}
                        >
                          {scan.report_id}
                        </a>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  )
}
