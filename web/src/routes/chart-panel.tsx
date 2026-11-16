import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchChart, type ChartData, type ChartEnvelope, type ChartPeriod } from '../api/charts'
import { isAbortError } from '../api/abort'
import { Chart } from '../components/chart'
import { DataQualityBadge } from '../components/data-quality'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

/** Options the switch offers beyond the report's native period, in display order. */
const SWITCH_PERIODS: readonly ChartPeriod[] = ['15m', '1h', '4h']

interface ChartPanelPageProps {
  reportId: string
  fetchChartFn?: (reportId: string, period?: string, signal?: AbortSignal) => Promise<ChartEnvelope>
  isCompact?: boolean
}

interface ChartPanelState {
  state: 'loading' | 'ready' | 'error'
  chart?: ChartData
  message?: string
}

/** The requested period from the URL, only when it is a real switch period. */
function initialPeriod(): ChartPeriod | undefined {
  if (typeof window === 'undefined') {
    return undefined
  }
  const requested = new URLSearchParams(window.location.search).get('period')
  return requested === '15m' || requested === '1h' || requested === '4h' ? requested : undefined
}

function periodOptions(native: string): string[] {
  return Array.from(new Set<string>([native, ...SWITCH_PERIODS]))
}

function disabledReason(symbol: string, period: string): string {
  return (
    `No published ${period} bars or metrics exist for ${symbol}; ` +
    'this control is disabled rather than upsampled.'
  )
}

export function ChartPanelPage({
  reportId,
  fetchChartFn = fetchChart,
  isCompact,
}: ChartPanelPageProps) {
  const [period, setPeriod] = useState<ChartPeriod | undefined>(initialPeriod)
  const [load, setLoad] = useState<ChartPanelState>({ state: 'loading' })
  const [reloadKey, setReloadKey] = useState(0)
  const [selectedBarStart, setSelectedBarStart] = useState<string | undefined>(undefined)
  const compact = useCompact(isCompact)
  // The report whose chart the retained data belongs to, so a report change
  // can never keep showing another report's series while the next one loads.
  const loadedReportRef = useRef<string | undefined>(undefined)
  const periodRefs = useRef(new Map<string, HTMLButtonElement>())
  const restoreFocusRef = useRef(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoad((previous) => ({
      state: 'loading',
      chart: loadedReportRef.current === reportId ? previous.chart : undefined,
    }))
    fetchChartFn(reportId, period, controller.signal)
      .then((envelope) => {
        if (controller.signal.aborted) {
          return
        }
        if (!envelope.data) {
          setLoad((previous) => ({
            state: 'error',
            message: 'Chart response was empty.',
            chart: previous.chart,
          }))
          return
        }
        loadedReportRef.current = reportId
        setLoad({ state: 'ready', chart: envelope.data })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) {
          return
        }
        setLoad((previous) => ({
          state: 'error',
          message: error instanceof Error ? error.message : 'Unknown error',
          chart: previous.chart,
        }))
      })
    return () => {
      controller.abort()
    }
  }, [reportId, period, fetchChartFn, reloadKey])

  // The switch stays mounted across a fetch, so focus normally survives on its
  // own; restore it explicitly once the resolved period lands so an assistive
  // announcement and the pressed state agree with the focused control.
  useEffect(() => {
    if (load.state !== 'ready' || load.chart === undefined || !restoreFocusRef.current) {
      return
    }
    restoreFocusRef.current = false
    periodRefs.current.get(load.chart.requested_period)?.focus()
  }, [load.state, load.chart])

  // A failed load remounts the alert after the busy render. A stable callback
  // ref focuses it on every mount, so a repeated retry failure can never drop
  // focus onto <body> even when the load state returns to an Object.is-equal
  // `{state:'error', chart}` value.
  const focusAlert = useCallback((node: HTMLElement | null) => {
    if (node) {
      node.focus()
    }
  }, [])

  function switchPeriod(next: ChartPeriod) {
    setSelectedBarStart(undefined)
    restoreFocusRef.current = true
    setPeriod(next)
    // Keep the selected period shareable without leaving the route.
    try {
      if (typeof window !== 'undefined' && typeof window.history?.replaceState === 'function') {
        const url = new URL(window.location.href)
        url.searchParams.set('period', next)
        window.history.replaceState(null, '', `${url.pathname}${url.search}`)
      }
    } catch {
      // A history-less environment (jsdom, embedded webview) still switches.
    }
  }

  function retryChart() {
    restoreFocusRef.current = true
    setReloadKey((value) => value + 1)
  }

  const crumb = (
    <p className="kicker">
      <a className="rowlink" href="/reports">
        Reports
      </a>{' '}
      /{' '}
      <a className="rowlink mono" href={`/reports/${encodeURIComponent(reportId)}`}>
        {reportId}
      </a>{' '}
      / chart
    </p>
  )

  const chart = load.chart
  const busy = load.state === 'loading'

  if (chart === undefined) {
    if (load.state === 'error') {
      return (
        <section
          className="system-page chart-panel"
          aria-labelledby="chart-heading"
          data-readonly={compact}
          lang="en"
        >
          <div className="page-head">
            <div>
              {crumb}
              <h1 id="chart-heading">Chart unavailable</h1>
            </div>
          </div>
          <section className="panel" role="alert" tabIndex={-1} ref={focusAlert}>
            <p>{load.message ?? 'Chart not found.'}</p>
            <p className="action-row">
              <a className="control-link" href={`/reports/${encodeURIComponent(reportId)}`}>
                Back to report
              </a>
              <a className="control-link" href="/reports">
                Back to reports
              </a>
            </p>
          </section>
        </section>
      )
    }
    return (
      <section
        className="system-page chart-panel"
        aria-labelledby="chart-heading"
        data-readonly={compact}
        lang="en"
      >
        <div className="page-head">
          <div>
            {crumb}
            <h1 id="chart-heading">Chart panel</h1>
          </div>
        </div>
        <p className="status-message" role="status">
          Loading chart for {reportId}…
        </p>
      </section>
    )
  }

  const activePeriod = period ?? chart.native_period
  const options = periodOptions(chart.native_period)
  const available = chart.available_periods
  // One predicate gates both the drawn chart and the announcement, so the page
  // never says "Showing" for a payload it refuses to draw (or vice versa).
  const canDraw =
    chart.status === 'AVAILABLE' &&
    chart.metrics !== null &&
    chart.bars.length > 0 &&
    chart.data_quality !== 'MISSING'
  const unavailableReason =
    chart.reason ??
    (chart.status === 'AVAILABLE'
      ? `The published ${chart.requested_period} report has no bars or drawable metrics.`
      : 'No published bars or metrics exist for this period.')
  const resolvedPeriod = chart.requested_period === activePeriod
  let announcement = ''
  if (busy) {
    announcement = `Loading ${activePeriod} chart.`
  } else if (load.state === 'error') {
    announcement = `Chart for ${activePeriod} could not be loaded.`
  } else if (resolvedPeriod) {
    // Never claim a chart is shown while a stale response is still mounted or
    // when the resolved payload is an explicit MISSING/unavailable series.
    announcement = canDraw
      ? `Showing ${chart.requested_period} chart for ${chart.symbol}.`
      : `No ${chart.requested_period} chart available; published periods: ${available.join(', ')}.`
  }

  return (
    <section
      className="system-page chart-panel"
      aria-labelledby="chart-heading"
      data-readonly={compact}
      lang="en"
    >
      <div className="page-head">
        <div>
          {crumb}
          <h1 id="chart-heading">{chart.symbol} chart</h1>
          <p className="page-sub">
            {chart.native_period} native · source{' '}
            {chart.source_report_id !== null ? (
              <a
                className="rowlink mono"
                href={`/reports/${encodeURIComponent(chart.source_report_id)}`}
              >
                {chart.source_report_id}
              </a>
            ) : (
              <span className="mono">none</span>
            )}{' '}
            · generated {chart.generated_at ?? '—'}
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="CALC" />
          <DataQualityBadge status={chart.data_quality} reasons={chart.quality_reasons} />
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          Read-only at this width. The chart and table use the same stored values.
        </p>
      )}

      <p className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>

      <div className="chart-period" role="group" aria-label="Timeframe">
        {options.map((option) => {
          const enabled = available.includes(option)
          const active = option === activePeriod
          const reasonId = `chart-period-reason-${option}`
          const reason = enabled ? undefined : disabledReason(chart.symbol, option)
          return (
            <span key={option} className="chart-period-item">
              <button
                type="button"
                className={active ? 'control-btn control-btn--active' : 'control-btn'}
                aria-pressed={active}
                aria-disabled={enabled ? undefined : true}
                aria-describedby={reason ? reasonId : undefined}
                title={reason}
                ref={(node) => {
                  if (node) {
                    periodRefs.current.set(option, node)
                  } else {
                    periodRefs.current.delete(option)
                  }
                }}
                onClick={() => {
                  if (enabled) {
                    switchPeriod(option as ChartPeriod)
                  }
                }}
              >
                {option}
              </button>
              {reason && (
                <span id={reasonId} className="visually-hidden">
                  {reason}
                </span>
              )}
            </span>
          )
        })}
      </div>

      <div className="chart-region" aria-busy={busy ? 'true' : undefined}>
        {busy ? (
          <p className="status-message">Loading {activePeriod} chart…</p>
        ) : load.state === 'error' ? (
          <section className="panel" role="alert" tabIndex={-1} ref={focusAlert}>
            <p>{load.message ?? 'Chart not found.'}</p>
            <p className="action-row">
              <button type="button" className="control-btn" onClick={retryChart}>
                Retry {activePeriod}
              </button>
              <a className="control-link" href={`/reports/${encodeURIComponent(reportId)}`}>
                Back to report
              </a>
              <a className="control-link" href="/reports">
                Back to reports
              </a>
            </p>
          </section>
        ) : canDraw && chart.metrics !== null ? (
          <>
            {/* The chart section heading keeps the route's h1 -> h2 -> h3
                (Footprint) order; it is visually hidden because the drawing
                itself carries no title. */}
            <h2 className="visually-hidden">Chart</h2>
            <Chart
              key={chart.requested_period}
              symbol={chart.symbol}
              timeframe={chart.requested_period}
              bars={chart.bars}
              metrics={chart.metrics}
              quality={chart.data_quality}
              qualityReasons={chart.quality_reasons}
              selectedBarStart={selectedBarStart}
              onSelectBar={setSelectedBarStart}
              zones={chart.zones}
              markers={chart.markers}
              gaps={chart.gaps}
            />
            <p className="chart-gaps explain">
              Gaps: {chart.gaps.length}. gaps are never interpolated.
            </p>
          </>
        ) : (
          <section
            className="panel chart-unavailable"
            role="note"
            aria-label={`Chart unavailable for ${chart.requested_period}`}
          >
            <h2>No {chart.requested_period} chart</h2>
            <p>{unavailableReason}</p>
            {chart.data_quality === 'MISSING' && (
              <>
                <p>
                  The market data required for a chart is MISSING. No candles, volume, CVD, volume
                  profile, or footprint are drawn from invented values.
                </p>
                <p>
                  Data quality: {chart.data_quality}. {chart.quality_reasons.join(' ')}
                </p>
              </>
            )}
            <p className="mono-muted">Available periods: {available.join(', ')}</p>
            <p className="panel-foot">
              The chart never draws an invented, upsampled, or interpolated series.
            </p>
          </section>
        )}
      </div>
    </section>
  )
}
