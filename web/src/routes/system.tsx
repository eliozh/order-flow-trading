import { useCallback, useEffect, useState } from 'react'

import {
  ApiShapeError,
  fetchHealth,
  fetchReadiness,
  fetchStatus,
  type HealthData,
  type HealthEnvelope,
  type ReadinessData,
  type ReadinessEnvelope,
  type StatusEnvelope,
  type TelegramStatusData,
} from '../api/status'
import { useCompact } from '../hooks/use-compact'
import { usePolling } from '../hooks/use-polling'

/** Revalidation cadence for the additive Telegram receipt card. */
const TELEGRAM_STATUS_REFRESH_MS = 30_000

type SystemFetcher<T> = (signal?: AbortSignal) => Promise<T>

interface SystemPageProps {
  fetchHealthFn?: SystemFetcher<HealthEnvelope>
  fetchReadinessFn?: SystemFetcher<ReadinessEnvelope>
  fetchStatusFn?: SystemFetcher<StatusEnvelope>
  isCompact?: boolean
}

interface ReadyState {
  state: 'loading' | 'ready' | 'error'
  health?: HealthData
  readiness?: ReadinessData
  message?: string
}

interface TelegramLoadState {
  state: 'loading' | 'ready' | 'error'
  data: TelegramStatusData | null
  error?: unknown
}

/** A bounded failure summary that keeps the error class distinguishable. */
function telegramStatusFailure(error: unknown): string {
  if (error instanceof ApiShapeError) {
    return `Telegram status contract mismatch (ApiShapeError): ${error.message}`
  }
  if (error instanceof Error) {
    return `Telegram status unavailable (${error.name}): ${error.message}`
  }
  return 'Telegram status unavailable (unknown error).'
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

function receiptStateClass(state: string): string {
  if (state === 'sent') {
    return 'chip chip--ok'
  }
  if (state === 'failed') {
    return 'chip chip--danger'
  }
  return 'chip chip--warn'
}

/** Keep the stable code but drop the shared prefix so the chip cannot overflow. */
function pollerStateLabel(state: string): string {
  return state.replace(/^TELEGRAM_/, '')
}

/** Tone for the poller enum; UNKNOWN stays neutral, failures are emphasized. */
function pollerStateClass(state: string): string {
  if (state === 'RUNNING') {
    return statusClass('ok')
  }
  if (state === 'DEGRADED') {
    return statusClass('degraded')
  }
  if (
    state === 'CONFLICT' ||
    state === 'TELEGRAM_TOKEN_INVALID' ||
    state === 'TELEGRAM_UNAUTHORIZED'
  ) {
    return statusClass('degraded')
  }
  return statusClass('unknown')
}

function pollerIssue(telegram: TelegramStatusData): boolean {
  return (
    telegram.configured &&
    (telegram.poller_state === 'DEGRADED' || telegram.poller_state === 'CONFLICT')
  )
}

/** A short human summary of the Telegram delivery state for a live region. */
function telegramSummary(telegram: TelegramStatusData): string {
  const sent = telegram.receipts.sent ?? 0
  const retrying = (telegram.receipts.queued ?? 0) + (telegram.receipts.sending ?? 0)
  return `${sent} sent, ${telegram.failing_receipts} failed, ${retrying} retrying`
}

interface TelegramDeliveryPanelProps {
  telegram: TelegramStatusData
  compact: boolean
}

function TelegramDeliveryPanel({ telegram, compact }: TelegramDeliveryPanelProps) {
  const sent = telegram.receipts.sent ?? 0
  const failed = telegram.failing_receipts
  const retrying = (telegram.receipts.queued ?? 0) + (telegram.receipts.sending ?? 0)
  const lastError = telegram.last_error_code ?? telegram.overflow_code
  const degraded = pollerIssue(telegram)

  return (
    <>
      {failed > 0 && (
        <p className="status-message status-message--error" role="alert" lang="en">
          Telegram pushes failing: {failed} failed receipt(s).
          {lastError ? (
            <>
              {' '}
              Last error <code>{lastError}</code>.
            </>
          ) : null}{' '}
          Other chats continue; run <code>oft telegram receipts --limit</code> for details.
        </p>
      )}
      {failed === 0 && degraded && (
        <p className="status-message status-message--error" role="alert" lang="en">
          {telegram.poller_state === 'CONFLICT'
            ? 'Telegram poller CONFLICT: another poller owns this token.'
            : 'Telegram poller DEGRADED.'}
          {telegram.last_error_code ? (
            <>
              {' '}
              Reason <code>{telegram.last_error_code}</code>.
            </>
          ) : null}{' '}
          Other schedulers continue; run <code>oft doctor</code> for details.
        </p>
      )}
      <section className="panel" aria-labelledby="telegram-heading" lang="en">
        <div className="panel-head">
          <h2 id="telegram-heading">Telegram delivery receipts</h2>
          <span className={pollerStateClass(telegram.poller_state)}>
            {pollerStateLabel(telegram.poller_state)}
          </span>
        </div>
        <p className="panel-foot">
          {telegram.configured ? 'Configured' : 'Not configured'} · {telegram.chat_count} chat(s) ·{' '}
          {telegram.allowed_user_count} allowed user(s)
        </p>
        <dl className="stat-grid">
          <div className="stat">
            <dt>Sent</dt>
            <dd>{sent}</dd>
          </div>
          <div className="stat">
            <dt>Failed</dt>
            <dd>{failed}</dd>
          </div>
          <div className="stat">
            <dt>Retrying</dt>
            <dd>{retrying}</dd>
          </div>
        </dl>
        <p className="panel-foot">
          last successful poll {telegram.last_poll_at ?? 'never'} · last successful send{' '}
          {telegram.last_send_at ?? 'never'}
        </p>
        {telegram.overflow_code && (
          <p className="panel-foot">
            Queue overflow: <code>{telegram.overflow_code}</code> — excess messages were rejected,
            not queued.
          </p>
        )}
        {compact ? (
          <>
            <div role="group" aria-label="Telegram delivery receipts by chat">
              <dl className="scan-grid scan-grid--telegram">
                {telegram.per_chat.length === 0 ? (
                  <div>
                    <dt>Receipts</dt>
                    <dd>No Telegram receipts yet.</dd>
                  </div>
                ) : (
                  telegram.per_chat.map((chat) => (
                    <div key={chat.chat_id}>
                      <dt>Chat {chat.chat_id}</dt>
                      <dd>
                        sent {chat.sent} · failed {chat.failed} · retrying{' '}
                        {chat.queued + chat.sending} · last error {chat.last_error_code ?? '-'}
                      </dd>
                    </div>
                  ))
                )}
              </dl>
            </div>
            {telegram.recent_receipts.length > 0 && (
              <div role="group" aria-label="Recent Telegram receipts">
                <dl className="scan-grid scan-grid--telegram">
                  {telegram.recent_receipts.map((receipt) => (
                    <div key={receipt.event_key}>
                      <dt>
                        {receipt.state.toUpperCase()} · {receipt.event_key} · chat {receipt.chat_id}
                      </dt>
                      <dd>
                        message {receipt.message_id ?? '-'} · error {receipt.last_error_code ?? '-'}
                      </dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}
          </>
        ) : (
          <>
            <div
              className="table-wrap"
              role="region"
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
              tabIndex={0}
              aria-label="Telegram delivery receipts by chat"
            >
              <table className="data-table">
                <caption className="visually-hidden">Telegram delivery receipts by chat</caption>
                <thead>
                  <tr>
                    <th scope="col">Chat</th>
                    <th scope="col">Sent</th>
                    <th scope="col">Failed</th>
                    <th scope="col">Retrying</th>
                    <th scope="col">Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {telegram.per_chat.length === 0 ? (
                    <tr>
                      <td colSpan={5}>No Telegram receipts yet.</td>
                    </tr>
                  ) : (
                    telegram.per_chat.map((chat) => (
                      <tr key={chat.chat_id}>
                        <td className="mono">{chat.chat_id}</td>
                        <td>{chat.sent}</td>
                        <td>{chat.failed}</td>
                        <td>{chat.queued + chat.sending}</td>
                        <td className="mono">{chat.last_error_code ?? '-'}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            {telegram.recent_receipts.length > 0 && (
              <div
                className="table-wrap"
                role="region"
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
                tabIndex={0}
                aria-label="Recent Telegram receipts"
              >
                <table className="data-table">
                  <caption className="visually-hidden">Recent Telegram receipts</caption>
                  <thead>
                    <tr>
                      <th scope="col">Event</th>
                      <th scope="col">Chat</th>
                      <th scope="col">State</th>
                      <th scope="col">Message ID</th>
                      <th scope="col">Last error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {telegram.recent_receipts.map((receipt) => (
                      <tr key={receipt.event_key}>
                        <td className="mono">{receipt.event_key}</td>
                        <td className="mono">{receipt.chat_id}</td>
                        <td>
                          <span className={receiptStateClass(receipt.state)}>
                            {receipt.state.toUpperCase()}
                          </span>
                        </td>
                        <td className="mono">{receipt.message_id ?? '-'}</td>
                        <td className="mono">{receipt.last_error_code ?? '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </>
  )
}

export function SystemPage({
  fetchHealthFn = fetchHealth,
  fetchReadinessFn = fetchReadiness,
  fetchStatusFn = fetchStatus,
  isCompact,
}: SystemPageProps) {
  const [load, setLoad] = useState<ReadyState>({ state: 'loading' })
  const [telegramLoad, setTelegramLoad] = useState<TelegramLoadState>({
    state: 'loading',
    data: null,
  })
  const compact = useCompact(isCompact)

  const loadTelegramStatus = useCallback(
    async (signal: AbortSignal): Promise<void> => {
      const envelope = await fetchStatusFn(signal)
      setTelegramLoad({ state: 'ready', data: envelope.data?.telegram ?? null })
    },
    [fetchStatusFn],
  )

  // Revalidate the additive Telegram card once the diagnostics settle. The
  // pause doubles as the "wait for health/readiness" gate: before it clears,
  // no request, interval, or focus listener exists. A failed revalidation keeps
  // the last-good data instead of flickering back to a loading state.
  usePolling(loadTelegramStatus, {
    refreshMs: TELEGRAM_STATUS_REFRESH_MS,
    paused: load.state !== 'ready',
    onError: (error) => {
      setTelegramLoad((previous) => ({ state: 'error', data: previous.data, error }))
    },
  })

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
  const telegram = telegramLoad.data
  const telegramAnnouncement =
    telegramLoad.state === 'loading'
      ? 'Loading Telegram delivery receipts…'
      : telegramLoad.state === 'error'
        ? telegramStatusFailure(telegramLoad.error)
        : telegram === null
          ? 'Telegram is not configured.'
          : telegram.failing_receipts > 0
            ? 'Telegram delivery receipts loaded.'
            : `Telegram delivery receipts loaded: ${telegramSummary(telegram)}.`

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
        <div
          className="table-wrap"
          role="region"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
          tabIndex={0}
          aria-label="Named readiness checks"
        >
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
        </div>
      </section>

      <p
        className={
          telegramLoad.state === 'error'
            ? 'status-message status-message--error'
            : 'visually-hidden'
        }
        role="status"
        data-testid="telegram-status"
      >
        {telegramAnnouncement}
      </p>

      {telegram && <TelegramDeliveryPanel telegram={telegram} compact={compact} />}

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
