import { useEffect, useState } from 'react'

import {
  fetchScan,
  telegramDeliveryLabel,
  type ScanData,
  type ScanEnvelope,
  type ScanSymbolRow,
} from '../api/scans'
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

type StageState = 'DONE' | 'RUNNING' | 'PENDING' | 'FAILED' | 'SKIPPED'

/** How often a RUNNING/QUEUED batch refreshes the detail snapshot. */
export const SCAN_POLL_INTERVAL_MS = 5000
const ACTIVE_SCAN_STATUSES = new Set(['RUNNING', 'QUEUED'])

const CHIP_CLASS: Record<StageState, string> = {
  DONE: 'chip chip--ok',
  RUNNING: 'chip',
  PENDING: 'chip',
  FAILED: 'chip chip--danger',
  SKIPPED: 'chip chip--warn',
}

const LLM_CHIP: Record<string, string> = {
  VALID: 'chip chip--ok',
  REPAIRED: 'chip chip--ok',
  REJECTED: 'chip chip--danger',
}

const TERMINAL_STAGES = new Set(['DONE', 'DEGRADED', 'FAILED'])

function symbolRows(scan: ScanData): ScanSymbolRow[] {
  if (scan.symbols && scan.symbols.length > 0) {
    return scan.symbols
  }
  return [
    {
      symbol: scan.symbol ?? scan.scan_id,
      stage: scan.symbol_stage ?? scan.status,
      report_id: scan.report_id,
      data_quality: scan.data_quality,
      error_code: scan.error_code ?? null,
      error_message: scan.error_message ?? null,
      llm_status: scan.llm_status ?? null,
      llm_tokens: scan.llm_tokens ?? 0,
      cost_usd: scan.cost_usd ?? '0',
      cost_unpriced: scan.cost_unpriced ?? true,
    },
  ]
}

function aggregateStage(flags: StageFlags[], key: keyof StageFlags): StageState {
  if (flags.some((flags) => flags[key] === 'FAILED')) {
    return 'FAILED'
  }
  if (flags.some((flags) => flags[key] === 'RUNNING')) {
    return 'RUNNING'
  }
  const states = flags.map((flags) => flags[key])
  if (states.length > 0 && states.every((state) => state === 'DONE' || state === 'SKIPPED')) {
    // SKIPPED means a symbol failed before this stage, so it never ran there.
    // A stage that completed for every reachable symbol is DONE, not PENDING.
    return states.includes('DONE') ? 'DONE' : 'SKIPPED'
  }
  return 'PENDING'
}

function stages(scan: ScanData): { name: string; state: StageState; detail: string }[] {
  const rows = symbolRows(scan)
  const flags = rows.map(symbolStageFlags)
  const running = scan.status === 'RUNNING' || scan.status === 'QUEUED'
  const failed = scan.status === 'FAILED'
  const terminal = rows.length > 0 && rows.every((row) => TERMINAL_STAGES.has(row.stage))
  const started = terminal || rows.some((row) => row.stage !== 'QUEUED')
  const anyRejected = rows.some((row) => row.llm_status === 'REJECTED')
  const dataState = aggregateStage(flags, 'data')
  const metricsState = aggregateStage(flags, 'metrics')
  const llmState = aggregateStage(flags, 'llm')
  let reportState = aggregateStage(flags, 'report')
  if (reportState === 'PENDING' && failed) {
    reportState = 'FAILED'
  } else if (reportState === 'PENDING' && running) {
    reportState = 'RUNNING'
  }
  return [
    {
      name: 'QUEUED',
      state: started ? 'DONE' : running ? 'RUNNING' : 'PENDING',
      detail: `${rows.length} symbol${rows.length === 1 ? '' : 's'} in this run`,
    },
    {
      name: 'DATA',
      state: dataState,
      detail:
        dataState === 'FAILED'
          ? 'one symbol has a data gap'
          : 'persisted aggTrades resampled to UTC bars',
    },
    {
      name: 'METRICS',
      state: metricsState,
      detail:
        metricsState === 'FAILED'
          ? 'one symbol failed while computing the order-flow document'
          : 'versioned order-flow document and snapshot',
    },
    {
      name: 'LLM',
      state: anyRejected ? 'FAILED' : llmState,
      detail: anyRejected
        ? 'narrative rejected; CALC-only report kept'
        : llmState === 'DONE'
          ? 'schema-validated narrative'
          : 'not configured for this run',
    },
    {
      name: 'REPORT',
      state: reportState,
      detail: reportState === 'DONE' ? 'at least one report link available' : 'no report yet',
    },
  ]
}

function symbolStageClass(stage: string): string {
  if (stage === 'DONE') {
    return 'chip chip--ok'
  }
  if (stage === 'DEGRADED' || stage === 'PARTIAL') {
    return 'chip chip--warn'
  }
  if (stage === 'FAILED') {
    return 'chip chip--danger'
  }
  return 'chip'
}

interface StageFlags {
  data: string
  metrics: string
  llm: string
  report: string
}

/** Per-symbol DATA/METRICS/LLM/REPORT cells (payload first, then derived). */
function symbolStageFlags(row: ScanSymbolRow): StageFlags {
  if (row.data_stage && row.metrics_stage && row.llm_stage && row.report_stage) {
    return {
      data: row.data_stage,
      metrics: row.metrics_stage,
      llm: row.llm_stage,
      report: row.report_stage,
    }
  }
  const flags: StageFlags = {
    data: 'PENDING',
    metrics: 'PENDING',
    llm: 'PENDING',
    report: 'PENDING',
  }
  if (row.stage === 'DATA') {
    flags.data = 'RUNNING'
  } else if (row.stage === 'METRICS') {
    flags.data = 'DONE'
    flags.metrics = 'RUNNING'
  } else if (row.stage === 'LLM') {
    flags.data = 'DONE'
    flags.metrics = 'DONE'
    flags.llm = 'RUNNING'
  } else if (row.stage === 'REPORT') {
    flags.data = 'DONE'
    flags.metrics = 'DONE'
    flags.llm = 'DONE'
    flags.report = 'RUNNING'
  } else if (row.stage === 'DONE' || row.stage === 'DEGRADED') {
    flags.data = 'DONE'
    flags.metrics = 'DONE'
    flags.llm =
      row.llm_status === 'VALID' || row.llm_status === 'REPAIRED'
        ? 'DONE'
        : row.llm_status === 'REJECTED'
          ? 'FAILED'
          : 'SKIPPED'
    flags.report = row.report_id ? 'DONE' : 'SKIPPED'
  } else if (row.stage === 'FAILED') {
    const failedStage =
      row.failed_stage ??
      (row.error_code === 'DATA_UNAVAILABLE'
        ? 'DATA'
        : row.error_code === 'METRICS_INCOMPLETE'
          ? 'METRICS'
          : 'REPORT')
    if (failedStage === 'DATA') {
      flags.data = 'FAILED'
      flags.metrics = 'SKIPPED'
      flags.llm = 'SKIPPED'
      flags.report = 'SKIPPED'
    } else if (failedStage === 'METRICS') {
      flags.data = 'DONE'
      flags.metrics = 'FAILED'
      flags.llm = 'SKIPPED'
      flags.report = 'SKIPPED'
    } else if (failedStage === 'LLM') {
      flags.data = 'DONE'
      flags.metrics = 'DONE'
      flags.llm = 'FAILED'
      flags.report = 'SKIPPED'
    } else {
      flags.data = 'DONE'
      flags.metrics = 'DONE'
      flags.llm = row.llm_status ? 'DONE' : 'SKIPPED'
      flags.report = 'FAILED'
    }
  }
  return flags
}

function StageCell({ value }: { value: string }) {
  if (value === 'DONE') {
    return (
      <span className="value-up">
        <span aria-hidden="true">✓</span> done
      </span>
    )
  }
  if (value === 'RUNNING') {
    return (
      <span className="mono-muted">
        <span aria-hidden="true">›</span> running
      </span>
    )
  }
  if (value === 'FAILED') {
    return (
      <span className="value-down">
        <span aria-hidden="true">✕</span> failed
      </span>
    )
  }
  return <span className="mono-muted">—</span>
}

export function ScanDetailPage({
  scanId,
  fetchScanFn = fetchScan,
  isCompact,
}: ScanDetailPageProps) {
  const [load, setLoad] = useState<ScanState>({ state: 'loading' })
  const [pollError, setPollError] = useState<string | null>(null)
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    setPollError(null)
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

  const activeStatus = load.state === 'ready' ? load.scan?.status : undefined
  const polling = activeStatus !== undefined && ACTIVE_SCAN_STATUSES.has(activeStatus)

  useEffect(() => {
    if (!polling) {
      return
    }
    let active = true
    const timer = setInterval(() => {
      fetchScanFn(scanId)
        .then((envelope) => {
          if (!active) {
            return
          }
          if (!envelope.data) {
            setPollError('the daemon returned an empty response')
            return
          }
          setPollError(null)
          setLoad({ state: 'ready', scan: envelope.data })
        })
        .catch((error: unknown) => {
          if (!active) {
            return
          }
          // Keep the last good snapshot, but surface that it is now stale.
          setPollError(error instanceof Error ? error.message : 'refresh failed')
        })
    }, SCAN_POLL_INTERVAL_MS)
    return () => {
      active = false
      clearInterval(timer)
    }
  }, [polling, scanId, fetchScanFn])

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
  const rows = symbolRows(scan)
  const totalTokens = scan.total_tokens ?? scan.llm_tokens ?? 0
  const totalCost = scan.total_cost_usd ?? scan.cost_usd ?? '0'
  const stageRows = scan.symbols ?? []
  const completedCount = scan.symbols_completed ?? rows.filter((row) => row.stage === 'DONE').length
  const totalCount = scan.symbols_total ?? rows.length
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
            {[
              scan.symbol,
              scan.period,
              `trigger ${scan.trigger_source}`,
              `source ${scan.source_mode}`,
            ]
              .filter(Boolean)
              .join(' · ')}
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
          <dt>Symbols</dt>
          <dd className="mono">
            <span aria-hidden="true">
              {completedCount}/{totalCount}
            </span>
            <span className="visually-hidden">
              {completedCount} of {totalCount} symbols completed
            </span>
            {(scan.symbols_degraded ?? 0) > 0 ? ` · ${scan.symbols_degraded} degraded` : ''}
            {(scan.symbols_failed ?? 0) > 0 ? ` · ${scan.symbols_failed} failed` : ''}
          </dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd className="mono">
            ${totalCost}
            {scan.cost_unpriced && <span className="mono-muted"> · unpriced</span>}
          </dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd className="mono">{totalTokens}</dd>
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
          <span className="mono-muted">QUEUED → DATA → METRICS → LLM → REPORT → DONE</span>
        </div>
        <ol className="stage-list" aria-label="Named scan stages">
          {stages(scan).map((stage) => (
            <li key={stage.name} className={`stage stage--${stage.state.toLowerCase()}`}>
              <span className="stage-name">{stage.name}</span>
              <span className={CHIP_CLASS[stage.state]}>{stage.state}</span>
              <span className="panel-foot">{stage.detail}</span>
            </li>
          ))}
        </ol>
        {scan.metrics_only && !scan.llm_configured && (
          <p className="panel-foot" role="status">
            No LLM stage exists in this metrics-only slice.
          </p>
        )}
      </section>

      {stageRows.length > 0 && (
        <section className="panel" aria-labelledby="symbols-heading">
          <div className="panel-head">
            <h2 id="symbols-heading">Per-symbol results</h2>
            <span className="mono-muted">{stageRows.length} symbols</span>
          </div>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
          <div className="table-wrap" role="region" aria-label="Per-symbol results" tabIndex={0}>
            <table className="data-table">
              <caption className="visually-hidden">Per-symbol scan stages</caption>
              <thead>
                <tr>
                  <th scope="col">Symbol</th>
                  <th scope="col">DATA</th>
                  <th scope="col">METRICS</th>
                  <th scope="col">LLM</th>
                  <th scope="col">REPORT</th>
                  <th scope="col">State</th>
                  <th scope="col">Quality</th>
                  {!compact && <th scope="col">Cost</th>}
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {stageRows.map((row) => {
                  const flags = symbolStageFlags(row)
                  return (
                    <tr key={row.symbol}>
                      <th scope="row" className="mono">
                        {row.symbol}
                      </th>
                      <td>
                        <StageCell value={flags.data} />
                      </td>
                      <td>
                        <StageCell value={flags.metrics} />
                      </td>
                      <td>
                        <StageCell value={flags.llm} />
                        {row.llm_status && (
                          <span className={`${LLM_CHIP[row.llm_status] ?? 'chip'} llm-status`}>
                            {row.llm_status}
                          </span>
                        )}
                      </td>
                      <td>
                        <StageCell value={flags.report} />
                      </td>
                      <td>
                        <span className={symbolStageClass(row.stage)}>{row.stage}</span>
                      </td>
                      <td>
                        {row.data_quality ? (
                          <DataQualityBadge status={row.data_quality} />
                        ) : (
                          <span className="mono-muted">—</span>
                        )}
                      </td>
                      {!compact && (
                        <td className="mono">
                          ${row.cost_usd}
                          {row.cost_unpriced && <span className="mono-muted"> unpriced</span>}
                        </td>
                      )}
                      <td>
                        {row.report_id ? (
                          <a
                            className="rowlink mono"
                            href={`/reports/${encodeURIComponent(row.report_id)}`}
                          >
                            {row.report_id}
                          </a>
                        ) : row.error_message ? (
                          <span className="quality-reason">{row.error_message}</span>
                        ) : (
                          <span className="mono-muted">—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="panel-foot">
            Telegram:{' '}
            {scan.delivery?.configured
              ? telegramDeliveryLabel(scan.delivery)
              : (scan.delivery?.reason ?? 'Telegram is not configured in this build.')}
          </p>
          {scan.report_links && scan.report_links.length > 0 && (
            <p className="panel-foot">
              Report links:{' '}
              {scan.report_links.map((link) => (
                <a key={link.report_id} className="rowlink mono" href={link.href}>
                  {link.report_id}
                </a>
              ))}
            </p>
          )}
        </section>
      )}

      {polling && pollError && (
        <p className="panel-foot status-message--error" role="alert">
          Live updates paused ({pollError}); showing the last snapshot.
        </p>
      )}

      {polling && !pollError && (
        <p className="panel-foot" role="status">
          Stages update in place while a scan runs; no fabricated percentage is shown.
        </p>
      )}
    </section>
  )
}
