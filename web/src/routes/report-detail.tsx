import { useEffect, useState } from 'react'

import {
  fetchReport,
  signedDecimal,
  utcLabel,
  type ReportData,
  type ReportEnvelope,
} from '../api/reports'
import { addDecimals } from '../api/decimal'
import { Chart } from '../components/chart'
import { DataQualityBadge } from '../components/data-quality'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface ReportDetailPageProps {
  reportId: string
  fetchReportFn?: (reportId: string) => Promise<ReportEnvelope>
  isCompact?: boolean
}

interface ReportState {
  state: 'loading' | 'ready' | 'error'
  report?: ReportData
  message?: string
}

function footprintTotal(report: ReportData, barStart: string | undefined): string {
  const bar = report.metrics.footprint.bars.find((candidate) => candidate.bar_start === barStart)
  if (!bar) {
    return '—'
  }
  return addDecimals(bar.cells.map((cell) => cell.total_volume))
}

export function ReportDetailPage({
  reportId,
  fetchReportFn = fetchReport,
  isCompact,
}: ReportDetailPageProps) {
  const [load, setLoad] = useState<ReportState>({ state: 'loading' })
  const [selectedBarStart, setSelectedBarStart] = useState<string | undefined>(undefined)
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    fetchReportFn(reportId)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', message: 'Report response was empty.' })
          return
        }
        setLoad({ state: 'ready', report: envelope.data })
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
  }, [reportId, fetchReportFn])

  if (load.state === 'loading') {
    return (
      <p className="status-message" role="status">
        Loading report {reportId}…
      </p>
    )
  }

  if (load.state === 'error' || !load.report) {
    return (
      <section className="panel" role="alert">
        <h1>Report unavailable</h1>
        <p>{load.message ?? 'Report not found.'}</p>
        <a className="btn" href="/reports">
          Back to reports
        </a>
      </section>
    )
  }

  const report = load.report
  const metrics = report.metrics
  const latestPoint = metrics.cvd.points[metrics.cvd.points.length - 1]
  const bars = report.snapshot.bars
  const selected = selectedBarStart ?? bars[bars.length - 1]?.bar_start
  const selectedLabel = selected ? `${utcLabel(selected)} UTC` : '—'
  const selectedFootprint = metrics.footprint.bars.find((bar) => bar.bar_start === selected)
  const gaps = metrics.gaps
  const openInterestMeasured = metrics.open_interest.points.length > 0
  const fundingMeasured = metrics.funding.history.length > 0

  const rows: { label: string; value: string; note: string }[] = [
    {
      label: 'CVD (window)',
      value: signedDecimal(metrics.cvd.summary.cvd_change),
      note: `buy ${metrics.cvd.summary.buy_volume} / sell ${metrics.cvd.summary.sell_volume}`,
    },
    {
      label: 'Delta (latest)',
      value: latestPoint ? signedDecimal(latestPoint.delta) : '—',
      note: latestPoint ? utcLabel(latestPoint.bar_start) : 'no bars',
    },
    { label: 'POC', value: metrics.volume_profile.poc, note: 'highest-volume bin' },
    {
      label: 'VAH / VAL',
      value: `${metrics.volume_profile.vah} / ${metrics.volume_profile.val}`,
      note: `value area ${metrics.volume_profile.value_area_pct}%`,
    },
    {
      label: 'Volume Profile',
      value: `${metrics.volume_profile.bins.length} bins`,
      note: `${metrics.volume_profile.bin_size} per bin`,
    },
    {
      label: 'Footprint (selected bar)',
      value: `${selectedFootprint?.cells.length ?? 0} cells · ${footprintTotal(report, selected)}`,
      note: selectedLabel,
    },
    {
      label: 'OI Δ',
      value: openInterestMeasured ? metrics.open_interest.delta_over_window : 'not measured',
      note: openInterestMeasured
        ? metrics.open_interest.sampling
        : 'MISSING · no measured open-interest sample',
    },
    {
      label: 'Funding',
      value: fundingMeasured ? metrics.funding.current_rate : 'not measured',
      note: fundingMeasured
        ? `next ${utcLabel(metrics.funding.next_funding_time)} UTC`
        : 'MISSING · no measured funding round',
    },
    {
      label: 'Liquidations',
      value: `${metrics.liquidations.count} events · ${metrics.liquidations.coverage}`,
      note: 'since deployment only; no historical backfill',
    },
    {
      label: 'Gaps',
      value: String(gaps.length),
      note: gaps.length > 0 ? 'PARTIAL window; gaps are never interpolated' : 'none detected',
    },
  ]

  return (
    <section
      className="system-page report-page"
      aria-labelledby="report-heading"
      data-readonly={compact}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/reports">
              Reports
            </a>{' '}
            / detail
          </p>
          <h1 id="report-heading" className="mono">
            {report.report_id}
          </h1>
          <p className="page-sub">
            {report.symbol} · {report.timeframe} · generated {report.generated_at} · scan{' '}
            <a className="rowlink mono" href={`/scans/${encodeURIComponent(report.scan_id)}`}>
              {report.scan_id}
            </a>
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="CALC" />
          <DataQualityBadge status={report.data_quality} reasons={metrics.quality_reasons} />
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          Read-only at this width. The chart and table use the same stored values.
        </p>
      )}

      <section className="panel" aria-labelledby="calc-heading">
        <div className="panel-head">
          <h2 id="calc-heading">CALC · deterministic metrics</h2>
          <span className="mono-muted">{metrics.metric_version}</span>
        </div>
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
        <div className="table-wrap" role="region" aria-label="CALC metrics table" tabIndex={0}>
          <table className="data-table calc-table">
            <caption className="visually-hidden">Deterministic metric summary</caption>
            <thead>
              <tr>
                <th scope="col">Metric</th>
                <th scope="col">Value</th>
                <th scope="col">Note</th>
                <th scope="col">Quality</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label}>
                  <th scope="row">{row.label}</th>
                  <td className="mono">{row.value}</td>
                  <td className="calc-note">{row.note}</td>
                  <td className="mono">{report.data_quality}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="panel-foot">
          {report.metrics_manifest.length} immutable parts · input hash{' '}
          <code>{report.snapshot.orderflow.input_manifest_hash.slice(0, 16)}…</code>
        </p>
      </section>

      <Chart
        symbol={report.symbol}
        timeframe={report.timeframe}
        bars={bars}
        metrics={metrics}
        quality={report.data_quality}
        qualityReasons={metrics.quality_reasons}
        selectedBarStart={selected}
        onSelectBar={setSelectedBarStart}
      />

      <section className="band band--llm-disabled" aria-labelledby="llm-heading">
        <div className="band-head">
          <ProvenanceStamp kind="LLM" />
          <h2 id="llm-heading">LLM analysis</h2>
          <span className="chip chip--warn">disabled</span>
        </div>
        <p>{report.llm.reason}</p>
        <p className="panel-foot">
          Decision <code>{report.llm_decision_id ?? 'null'}</code> · narrative{' '}
          <code>{report.narrative_ref ?? 'null'}</code>. No raw HTML, no fabricated narrative.
        </p>
      </section>

      <section className="panel" aria-labelledby="provenance-heading">
        <div className="panel-head">
          <h2 id="provenance-heading">Provenance</h2>
        </div>
        <p className="panel-foot">
          CALC blocks are computed by code from market data and are reproducible. The LLM band is
          disabled because no provider is configured in this build. Missing data is stamped
          MISSING/PARTIAL instead of being estimated into the chart.
        </p>
      </section>
    </section>
  )
}
