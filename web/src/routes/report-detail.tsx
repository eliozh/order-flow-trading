import { useEffect, useState } from 'react'

import {
  fetchReport,
  signedDecimal,
  utcLabel,
  type ReportData,
  type ReportEnvelope,
} from '../api/reports'
import {
  fetchOpportunity,
  type OpportunityData,
  type OpportunityEnvelope,
} from '../api/opportunities'
import { isAbortError } from '../api/abort'
import { addDecimals } from '../api/decimal'
import { Chart } from '../components/chart'
import { DataQualityBadge } from '../components/data-quality'
import { LlmBand } from '../components/llm-band'
import { OpportunityCard } from '../components/opportunity-card'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface ReportDetailPageProps {
  reportId: string
  fetchReportFn?: (reportId: string, signal?: AbortSignal) => Promise<ReportEnvelope>
  fetchOpportunityFn?: (opportunityId: string, signal?: AbortSignal) => Promise<OpportunityEnvelope>
  isCompact?: boolean
}

interface ReportState {
  state: 'loading' | 'ready' | 'error'
  report?: ReportData
  message?: string
}

interface OpportunityCardState {
  opportunity_id: string
  opportunity?: OpportunityData
  error?: string
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
  fetchOpportunityFn = fetchOpportunity,
  isCompact,
}: ReportDetailPageProps) {
  const [load, setLoad] = useState<ReportState>({ state: 'loading' })
  const [selectedBarStart, setSelectedBarStart] = useState<string | undefined>(undefined)
  const [cards, setCards] = useState<OpportunityCardState[]>([])
  const compact = useCompact(isCompact)

  useEffect(() => {
    const controller = new AbortController()
    setLoad({ state: 'loading' })
    setCards([])
    fetchReportFn(reportId, controller.signal)
      .then((envelope) => {
        if (controller.signal.aborted) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', message: 'Report response was empty.' })
          return
        }
        setLoad({ state: 'ready', report: envelope.data })
        // Duplicate ids would render duplicate cards; keep the first.
        const ids = Array.from(new Set(envelope.data.llm.opportunity_ids ?? []))
        setCards(ids.map((opportunityId) => ({ opportunity_id: opportunityId })))
        void Promise.all(
          ids.map(async (opportunityId): Promise<OpportunityCardState> => {
            try {
              const detail = await fetchOpportunityFn(opportunityId, controller.signal)
              return detail.data
                ? { opportunity_id: opportunityId, opportunity: detail.data }
                : { opportunity_id: opportunityId, error: 'Details unavailable.' }
            } catch {
              return { opportunity_id: opportunityId, error: 'Details unavailable.' }
            }
          }),
        ).then((loaded) => {
          if (!controller.signal.aborted) {
            setCards(loaded)
          }
        })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) {
          return
        }
        setLoad({
          state: 'error',
          message: error instanceof Error ? error.message : 'Unknown error',
        })
      })
    return () => {
      controller.abort()
    }
  }, [reportId, fetchReportFn, fetchOpportunityFn])

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
          <a className="btn sm" href={`/reports/${encodeURIComponent(report.report_id)}/chart`}>
            Open chart panel
          </a>
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
        gaps={gaps}
      />

      <LlmBand
        llm={report.llm}
        decisionId={report.llm_decision_id}
        narrativeRef={report.narrative_ref}
      />

      {cards.length > 0 && (
        <section className="panel" aria-labelledby="report-opps-heading">
          <div className="panel-head">
            <h2 id="report-opps-heading">Opportunities</h2>
            <a className="btn sm" href="/opportunities">
              Open ledger
            </a>
          </div>
          <div className="card-grid">
            {cards.map((card) => (
              <OpportunityCard
                key={card.opportunity_id}
                opportunityId={card.opportunity_id}
                opportunity={card.opportunity}
              />
            ))}
          </div>
          <p className="panel-foot">
            Read-only report: there is no accept or reject action here. Acceptance happens in entry
            review after the code-owned trigger.
          </p>
        </section>
      )}

      <section className="panel" aria-labelledby="provenance-heading">
        <div className="panel-head">
          <h2 id="provenance-heading">Provenance</h2>
        </div>
        <p className="panel-foot">
          CALC blocks are computed by code from market data and are reproducible. The LLM band is{' '}
          {report.llm.configured
            ? 'model-generated and not reproducible across models; provider, model, prompt version, and cost are shown above.'
            : 'disabled because no provider is configured in this build.'}{' '}
          Missing data is stamped MISSING/PARTIAL instead of being estimated into the chart.
        </p>
      </section>
    </section>
  )
}
