import { useCallback, useEffect, useRef, useState } from 'react'

import {
  fetchScans,
  formatScanCost,
  telegramDeliveryLabel,
  triggerScan,
  type ScanData,
  type ScanEnvelope,
  type ScanListEnvelope,
} from '../api/scans'
import { fetchStatus, type ScheduleData, type StatusEnvelope } from '../api/status'
import { DataQualityBadge } from '../components/data-quality'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface ScansPageProps {
  fetchScansFn?: () => Promise<ScanListEnvelope>
  fetchStatusFn?: () => Promise<StatusEnvelope>
  triggerScanFn?: (period: string) => Promise<ScanEnvelope>
  isCompact?: boolean
}

interface ScansState {
  state: 'loading' | 'ready' | 'error'
  items: ScanData[]
  total: number
  message?: string
}

interface TriggerState {
  state: 'idle' | 'running' | 'done' | 'error'
  message?: string
}

const STATUS_CHIP: Record<string, string> = {
  DONE: 'chip chip--ok',
  COMPLETE: 'chip chip--ok',
  PARTIAL: 'chip chip--warn',
  DEGRADED: 'chip chip--warn',
  RUNNING: 'chip',
  QUEUED: 'chip',
  FAILED: 'chip chip--danger',
}

const LLM_CHIP: Record<string, string> = {
  VALID: 'chip chip--ok',
  REPAIRED: 'chip chip--ok',
  REJECTED: 'chip chip--danger',
}

const PERIOD_HOURS: Record<string, number> = {
  '1m': 1 / 60,
  '5m': 5 / 60,
  '15m': 15 / 60,
  '1h': 1,
  '4h': 4,
  '8h': 8,
  '1d': 24,
}

const BOUNDARY_HOURS: Record<string, string> = {
  '4h': '00/04/08/12/16/20 UTC',
  '8h': '00/08/16 UTC',
}

function boundaryLabel(period: string): string {
  return BOUNDARY_HOURS[period] ?? 'UTC boundaries'
}

function displayStage(scan: ScanData): string {
  if (scan.symbol_stage) {
    return scan.symbol_stage
  }
  if (scan.llm_status === 'REJECTED') {
    return 'DEGRADED'
  }
  // Batch payloads report COMPLETE; S03.1 shows the terminal DONE state.
  return scan.status === 'COMPLETE' ? 'DONE' : scan.status
}

function costLabel(scan: ScanData): string {
  return formatScanCost(scan.total_cost_usd ?? scan.cost_usd)
}

function symbolRows(scan: ScanData) {
  return scan.symbols ?? []
}

function symbolCounts(scan: ScanData): { completed: number; total: number } {
  const rows = symbolRows(scan)
  const total = scan.symbols_total ?? (rows.length || 1)
  const completed = scan.symbols_completed ?? rows.filter((row) => row.stage === 'DONE').length
  return { completed, total }
}

function symbolsLabel(scan: ScanData): string {
  const { completed, total } = symbolCounts(scan)
  return `${completed}/${total}`
}

function symbolNames(scan: ScanData): string[] {
  const rows = symbolRows(scan).map((row) => row.symbol)
  if (rows.length > 0) {
    return rows
  }
  return scan.symbol ? [scan.symbol] : []
}

function durationLabel(scan: ScanData): string {
  if (!scan.started_at) {
    return '—'
  }
  if (!scan.finished_at) {
    return 'running'
  }
  const started = Date.parse(scan.started_at)
  const finished = Date.parse(scan.finished_at)
  if (Number.isNaN(started) || Number.isNaN(finished)) {
    return '—'
  }
  const seconds = Math.max(0, Math.round((finished - started) / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

function nextRunLabel(period: string): string {
  const hours = PERIOD_HOURS[period] ?? 4
  const step = hours * 60 * 60 * 1000
  const now = Date.now()
  const next = Math.ceil(now / step) * step
  return new Date(next).toISOString()
}

function budgetTotal(items: ScanData[]): string {
  const today = new Date().toISOString().slice(0, 10)
  const total = items.reduce((sum, scan) => {
    if (!scan.started_at || scan.started_at.slice(0, 10) !== today) {
      return sum
    }
    const value = Number(scan.total_cost_usd ?? scan.cost_usd ?? '0')
    return Number.isFinite(value) ? sum + value : sum
  }, 0)
  return total.toFixed(2)
}

export function ScansPage({
  fetchScansFn = fetchScans,
  fetchStatusFn = fetchStatus,
  triggerScanFn = triggerScan,
  isCompact,
}: ScansPageProps) {
  const [load, setLoad] = useState<ScansState>({ state: 'loading', items: [], total: 0 })
  const [schedule, setSchedule] = useState<ScheduleData | null>(null)
  const [trigger, setTrigger] = useState<TriggerState>({ state: 'idle' })
  const compact = useCompact(isCompact)
  const mounted = useRef(true)
  const triggerInFlight = useRef(false)

  const loadScans = useCallback(async () => {
    try {
      const envelope = await fetchScansFn()
      if (!mounted.current) {
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
    } catch (error: unknown) {
      if (!mounted.current) {
        return
      }
      setLoad({
        state: 'error',
        items: [],
        total: 0,
        message: error instanceof Error ? error.message : 'Unknown error',
      })
    }
  }, [fetchScansFn])

  const loadSchedule = useCallback(async () => {
    try {
      const envelope = await fetchStatusFn()
      if (mounted.current && envelope.data?.schedule) {
        setSchedule(envelope.data.schedule)
      }
    } catch {
      // Keep the last known cadence; the history table still renders.
    }
  }, [fetchStatusFn])

  useEffect(() => {
    mounted.current = true
    setLoad({ state: 'loading', items: [], total: 0 })
    void loadScans()
    void loadSchedule()
    return () => {
      mounted.current = false
    }
  }, [loadScans, loadSchedule])

  async function onRun() {
    if (triggerInFlight.current) {
      return
    }
    triggerInFlight.current = true
    setTrigger({ state: 'running' })
    try {
      const envelope = await triggerScanFn(cadence)
      // POST /api/v1/scans runs the batch synchronously; refresh the history
      // and schedule card so the finished run appears without a reload.
      await Promise.all([loadScans(), loadSchedule()])
      const scanId = envelope.data?.scan_id ?? 'scan'
      if (mounted.current) {
        setTrigger({ state: 'done', message: `${scanId} scan finished` })
      }
    } catch (error: unknown) {
      if (mounted.current) {
        setTrigger({
          state: 'error',
          message: error instanceof Error ? error.message : 'Manual scan failed.',
        })
      }
    } finally {
      triggerInFlight.current = false
    }
  }

  const latest = load.items[0]
  const cadence = schedule?.period ?? latest?.period ?? '4h'
  const nextRun = schedule?.next_run_at ?? nextRunLabel(cadence)
  const lastRun = latest
    ? `${symbolsLabel(latest)} · ${latest.symbols_degraded ?? 0} degraded · ${
        latest.symbols_failed ?? 0
      } failed`
    : 'never'

  return (
    <section className="system-page" aria-labelledby="scans-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">Scans</p>
          <h1 id="scans-heading">Scan history</h1>
          <p className="page-sub">
            Starred-symbol scans run QUEUED → DATA → METRICS → LLM → REPORT → DONE. A failed symbol
            never aborts the batch; a rejected narrative is marked DEGRADED with a CALC-only report.
          </p>
        </div>
        <ProvenanceStamp kind="CALC" />
      </div>

      <section className="panel" aria-labelledby="schedule-heading">
        <div className="panel-head">
          <h2 id="schedule-heading">Schedule state</h2>
          <span className="mono-muted">{cadence} · UTC boundaries</span>
        </div>
        <dl className="scan-grid">
          <div>
            <dt>Next run</dt>
            <dd className="mono">{nextRun}</dd>
          </div>
          <div>
            <dt>Cadence</dt>
            <dd className="mono">
              {cadence} · {boundaryLabel(cadence)}
              {schedule?.pending_period ? ` · next ${schedule.pending_period}` : ''}
            </dd>
          </div>
          <div>
            <dt>Last run</dt>
            <dd className="mono">{lastRun}</dd>
          </div>
          <div>
            <dt>Budget today</dt>
            <dd className="mono">
              ${budgetTotal(load.items)} <span className="mono-muted">no daily cap configured</span>
            </dd>
          </div>
        </dl>
        {!compact && (
          <div className="panel-head">
            <button
              type="button"
              className="btn"
              onClick={onRun}
              aria-disabled={trigger.state === 'running'}
              aria-busy={trigger.state === 'running'}
            >
              Run scan now
            </button>
            <span className="mono-muted" role="status">
              {trigger.state === 'running' && 'Triggering a manual scan…'}
              {trigger.state === 'done' && trigger.message}
              {trigger.state === 'idle' && 'Manual runs are allowed; a scan never sends orders.'}
            </span>
            {trigger.state === 'error' && (
              <span className="mono-muted" role="alert">
                {trigger.message}
              </span>
            )}
          </div>
        )}
        {compact && (
          <p className="panel-foot" role="note">
            Read-only summary on small screens; run scans from desktop or the CLI (
            <code>oft scan run</code>).
          </p>
        )}
      </section>

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
            Run <code>oft scan run</code> to scan every starred symbol, or{' '}
            <code>oft scan run --metrics-only --symbol BTCUSDT</code> for one CALC-only scan.
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
                  {!compact && <th scope="col">Time</th>}
                  <th scope="col">Duration</th>
                  <th scope="col">Symbols</th>
                  <th scope="col">Status</th>
                  {!compact && <th scope="col">LLM</th>}
                  {!compact && <th scope="col">Quality</th>}
                  {!compact && <th scope="col">Cost</th>}
                  <th scope="col">Telegram</th>
                  {!compact && <th scope="col">Report</th>}
                </tr>
              </thead>
              <tbody>
                {load.items.map((scan) => {
                  const counts = symbolCounts(scan)
                  return (
                    <tr key={scan.scan_id}>
                      <th scope="row">
                        <a
                          className="rowlink mono"
                          href={`/scans/${encodeURIComponent(scan.scan_id)}`}
                        >
                          {scan.scan_id}
                        </a>
                      </th>
                      {!compact && <td className="mono">{scan.started_at}</td>}
                      <td className="mono">{durationLabel(scan)}</td>
                      <td className="mono">
                        <span aria-hidden="true">{symbolsLabel(scan)}</span>
                        <span className="visually-hidden">
                          {counts.completed} of {counts.total} symbols completed
                        </span>
                        {symbolNames(scan).length > 0 && (
                          <span className="mono-muted"> · {symbolNames(scan).join(', ')}</span>
                        )}
                      </td>
                      <td>
                        <span className={STATUS_CHIP[displayStage(scan)] ?? 'chip'}>
                          {displayStage(scan)}
                        </span>
                      </td>
                      {!compact && (
                        <td>
                          {scan.llm_status ? (
                            <span className={LLM_CHIP[scan.llm_status] ?? 'chip'}>
                              {scan.llm_status}
                            </span>
                          ) : (
                            <span className="mono-muted">not configured</span>
                          )}
                        </td>
                      )}
                      {!compact && (
                        <td>
                          {scan.data_quality ? (
                            <DataQualityBadge status={scan.data_quality} />
                          ) : (
                            '—'
                          )}
                        </td>
                      )}
                      {!compact && (
                        <td className="mono">
                          {costLabel(scan)}
                          {scan.cost_unpriced && <span className="mono-muted"> unpriced</span>}
                        </td>
                      )}
                      <td>
                        {scan.delivery?.configured ? (
                          telegramDeliveryLabel(scan.delivery)
                        ) : (
                          <span className="mono-muted">not configured</span>
                        )}
                      </td>
                      {!compact && (
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
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  )
}
