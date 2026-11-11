import { useEffect, useState } from 'react'

import { fetchReports, type ReportListEnvelope, type ReportSummary } from '../api/reports'
import { DataQualityBadge } from '../components/data-quality'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface ReportsPageProps {
  fetchReportsFn?: () => Promise<ReportListEnvelope>
  isCompact?: boolean
}

interface ReportsState {
  state: 'loading' | 'ready' | 'error'
  items: ReportSummary[]
  total: number
  message?: string
}

/** The report's narrative stage, kept visually separate from the CALC report. */
function NarrativeChip({ report }: { report: ReportSummary }) {
  if (!report.llm.configured) {
    return <span className="chip chip--warn">CALC only</span>
  }
  if (report.llm.validation_status === 'REJECTED' || report.llm_stage === 'FAILED') {
    return <span className="chip chip--danger">REJECTED</span>
  }
  if (report.llm_stage === 'DONE') {
    return <span className="chip chip--calc">LLM</span>
  }
  return <span className="chip">unvalidated</span>
}

export function ReportsPage({ fetchReportsFn = fetchReports, isCompact }: ReportsPageProps) {
  const [load, setLoad] = useState<ReportsState>({
    state: 'loading',
    items: [],
    total: 0,
  })
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    fetchReportsFn()
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
            message: 'Report list response was empty.',
          })
          return
        }
        setLoad({ state: 'ready', items: data.items, total: data.total })
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
  }, [fetchReportsFn])

  return (
    <section className="system-page" aria-labelledby="reports-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">Reports</p>
          <h1 id="reports-heading">CALC reports</h1>
          <p className="page-sub">
            Metrics-only reports produced by manual scans. Narrative lives in a separate LLM band
            and is never fabricated here.
          </p>
        </div>
        <ProvenanceStamp kind="CALC" />
      </div>

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          Loading reports…
        </p>
      )}

      {load.state === 'error' && (
        <p className="status-message status-message--error" role="alert">
          Report index unavailable: {load.message}
        </p>
      )}

      {load.state === 'ready' && load.items.length === 0 && (
        <section className="panel" role="status">
          <h2>No reports yet</h2>
          <p>
            Run{' '}
            <code>oft scan run --metrics-only --symbol BTCUSDT --period 4h --source backfill</code>.
          </p>
        </section>
      )}

      {load.state === 'ready' && load.items.length > 0 && (
        <section className="panel" aria-labelledby="report-table-heading">
          <div className="panel-head">
            <h2 id="report-table-heading">Reports ({load.total})</h2>
            <span className="mono-muted">newest first</span>
          </div>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
          <div className="table-wrap" role="region" aria-label="Reports table" tabIndex={0}>
            <table className="data-table">
              <caption className="visually-hidden">CALC report list, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Report</th>
                  <th scope="col">Symbol</th>
                  <th scope="col">Period</th>
                  <th scope="col">Generated</th>
                  <th scope="col">Quality</th>
                  <th scope="col">Narrative</th>
                </tr>
              </thead>
              <tbody>
                {load.items.map((report) => (
                  <tr key={report.report_id}>
                    <td>
                      <a
                        className="rowlink mono"
                        href={`/reports/${encodeURIComponent(report.report_id)}`}
                      >
                        {report.report_id}
                      </a>
                    </td>
                    <td className="mono">{report.symbol}</td>
                    <td>{report.timeframe}</td>
                    <td className="mono">{report.generated_at}</td>
                    <td>
                      <DataQualityBadge status={report.data_quality} />
                    </td>
                    <td>
                      <NarrativeChip report={report} />
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
