import type { ReactElement } from 'react'

import type { JournalTradeData } from '../api/journal'
import { dash, type PositionHistoryEvent } from '../api/positions'
import type { TriggerPayload } from '../api/triggers'

/** A same-origin link for a referenced id, or a plain N/A when there is none. */
export function JournalRefLink({
  id,
  base,
}: {
  id: string | null | undefined
  base: string
}): ReactElement {
  if (!id) {
    return <span>N/A</span>
  }
  return (
    <a className="rowlink mono" href={`${base}/${encodeURIComponent(id)}`}>
      {id}
    </a>
  )
}

interface JournalTimelineProps {
  trade: JournalTradeData
  /** `opportunity.created_at`; never a fabricated entry time. */
  signalTime: string | null
  signalReason: string
  trigger: TriggerPayload | null
  triggerReason: string
  decisionText: string | null
  decisionConfidence: number | null
  decisionId: string | null
  decisionProvenance: Record<string, unknown> | null
  /** `decision.persisted_at`; never the trigger time. */
  decisionTime: string | null
  decisionReason: string
  managementEvents: PositionHistoryEvent[]
  managementReason: string | null
  netValue: number | null
}

/**
 * The signal → trigger → decision → entry → management → exit timeline. Every
 * timestamp comes from the stage that produced it; a missing stage renders
 * `N/A — <reason>` and never reuses another stage's time.
 */
export function JournalTimeline({
  trade,
  signalTime,
  signalReason,
  trigger,
  triggerReason,
  decisionText,
  decisionConfidence,
  decisionId,
  decisionProvenance,
  decisionTime,
  decisionReason,
  managementEvents,
  managementReason,
  netValue,
}: JournalTimelineProps) {
  const refs = trade.signal
  return (
    <section className="panel" aria-labelledby="journal-timeline-heading">
      <div className="panel-head">
        <h2 id="journal-timeline-heading">时间线</h2>
        <span className="mono-muted">
          signal → trigger → decision → entry → management → exit
        </span>
      </div>
      <ol className="timeline timeline--stages" aria-label="交易时间线">
        <li className={signalTime ? 'acc' : undefined}>
          <div className="tl-time">{signalTime ? `${signalTime} UTC` : 'N/A'}</div>
          <div className="tl-title">signal created</div>
          <div className="tl-desc">
            {refs?.opportunity_id ?? 'N/A'} · {trade.symbol} {trade.side} · report{' '}
            {refs?.report_id ?? 'N/A'}
          </div>
          {signalTime === null && <div className="tl-desc">N/A — {signalReason}</div>}
        </li>
        <li className={trigger ? 'acc' : undefined}>
          <div className="tl-time">{trigger ? `${trigger.triggered_at} UTC` : 'N/A'}</div>
          <div className="tl-title">triggered</div>
          <div className="tl-desc">
            {trigger
              ? `snapshot ${trigger.snapshot.as_of} · ${trigger.snapshot.data_quality} · ${trigger.trigger_id}`
              : `N/A — ${triggerReason}`}
          </div>
        </li>
        <li className={decisionText ? 'acc' : undefined}>
          <div className="tl-time">
            {decisionTime
              ? `${decisionTime} UTC`
              : decisionText
                ? 'N/A — no decision timestamp'
                : 'N/A'}
          </div>
          <div className="tl-title">
            {decisionText ? `LLM decision ${decisionText}` : 'LLM decision N/A'}
          </div>
          <div className="tl-desc">
            {decisionText
              ? `confidence ${decisionConfidence === null ? 'N/A' : decisionConfidence.toFixed(2)} · ${decisionId ?? 'N/A'} · prompt ${String(
                  (decisionProvenance?.prompt_id as string | undefined) ?? 'N/A',
                )}${typeof decisionProvenance?.prompt_version === 'number' ? ` v${decisionProvenance.prompt_version}` : ''}`
              : `N/A — ${decisionReason}`}
          </div>
        </li>
        <li className="ok">
          <div className="tl-time">{trade.opened_at} UTC</div>
          <div className="tl-title">entry</div>
          <div className="tl-desc">
            entry {dash(trade.entry_avg_price)} · size {dash(trade.size)} · fees{' '}
            {dash(trade.fees_usd)}
          </div>
        </li>
        <li>
          <div className="tl-time">
            {managementEvents.length > 0
              ? `${managementEvents[managementEvents.length - 1].at} UTC`
              : 'N/A'}
          </div>
          <div className="tl-title">management</div>
          {managementEvents.length > 0 ? (
            <ul className="journal-timeline-events">
              {managementEvents.map((event) => (
                <li key={event.seq}>
                  {event.action} · {event.at} · {event.order_id ?? '—'} · {event.note ?? '—'}
                </li>
              ))}
            </ul>
          ) : (
            <div className="tl-desc">N/A — {managementReason}</div>
          )}
        </li>
        <li className={netValue !== null && netValue < 0 ? 'failed' : undefined}>
          <div className="tl-time">{trade.closed_at} UTC</div>
          <div className="tl-title">exit</div>
          <div className="tl-desc">
            exit {dash(trade.exit_avg_price)} · net {trade.net_pnl_usd} ·{' '}
            {trade.outcome_tags.join(' / ') || '—'}
          </div>
        </li>
      </ol>
    </section>
  )
}

interface SignalSnapshotProps {
  opportunityId: string | null
  reportId: string | null
  zoneText: string
  entryAvgPrice: string
  slippageText: string
  invalidationText: string
}

/** The signal snapshot, with named N/A fallbacks for missing fields. */
export function SignalSnapshot({
  opportunityId,
  reportId,
  zoneText,
  entryAvgPrice,
  slippageText,
  invalidationText,
}: SignalSnapshotProps) {
  return (
    <section className="panel" aria-labelledby="journal-snapshot-heading">
      <div className="panel-head">
        <h2 id="journal-snapshot-heading">Signal snapshot</h2>
        <span className="mono-muted">CALC · source report</span>
      </div>
      <dl className="kv">
        <dt>Signal / Opportunity</dt>
        <dd className="mono">{opportunityId ?? 'N/A'}</dd>
        <dt>Proposed zone</dt>
        <dd className="mono">{zoneText}</dd>
        <dt>Actual entry</dt>
        <dd className="mono">{dash(entryAvgPrice)}</dd>
        <dt>Trigger→fill slippage</dt>
        <dd className="mono">{slippageText}</dd>
        <dt>Invalidation</dt>
        <dd>{invalidationText}</dd>
        <dt>Source report</dt>
        <dd className="mono">
          <JournalRefLink id={reportId} base="/reports" />
        </dd>
      </dl>
      <p className="panel-foot">缺失字段显示 N/A；不伪造 signal snapshot。</p>
    </section>
  )
}

interface JournalFillsProps {
  trade: JournalTradeData
  partial: boolean
  qualityReasons: string[]
}

/** The fills panel; missing fields render N/A and never a fabricated zero. */
export function JournalFills({ trade, partial, qualityReasons }: JournalFillsProps) {
  return (
    <section className="panel" aria-labelledby="journal-fills-heading">
      <div className="panel-head">
        <h2 id="journal-fills-heading">Fills</h2>
        {partial && <span className="chip chip--warn">PARTIAL</span>}
      </div>
      <dl className="kv">
        <dt>Entry fill</dt>
        <dd className="mono">
          {dash(trade.entry_avg_price)} · size {dash(trade.size)}
        </dd>
        <dt>Exit fill</dt>
        <dd className="mono">
          {dash(trade.exit_avg_price)} · size {dash(trade.size)}
        </dd>
        <dt>Gross / Fees / Funding</dt>
        <dd className="mono">
          {dash(trade.gross_pnl_usd)} / {dash(trade.fees_usd)} / {dash(trade.funding_usd)}
        </dd>
        <dt>Net PnL</dt>
        <dd className="mono">{trade.net_pnl_usd}</dd>
        <dt>Opened / Closed</dt>
        <dd className="mono">
          {trade.opened_at} / {trade.closed_at}
        </dd>
      </dl>
      {partial && qualityReasons.length > 0 && (
        <ul className="journal-fills-reasons">
          {qualityReasons.map((reason, index) => (
            <li key={`${index}-${reason.slice(0, 16)}`}>{reason}</li>
          ))}
        </ul>
      )}
      <p className="panel-foot">缺失字段显示 N/A，不写 0。</p>
    </section>
  )
}

interface JournalLinksProps {
  triggerId: string | null
  opportunityId: string | null
  positionId: string | null
  reportId: string | null
}

/** The related-record links (trigger, opportunity, position, report). */
export function JournalLinks({ triggerId, opportunityId, positionId, reportId }: JournalLinksProps) {
  return (
    <section className="panel" aria-labelledby="journal-links-heading">
      <div className="panel-head">
        <h2 id="journal-links-heading">关联</h2>
      </div>
      <div className="journal-links">
        <div>
          <span className="mono-muted">Trigger</span>{' '}
          <JournalRefLink id={triggerId} base="/triggers" />
        </div>
        <div>
          <span className="mono-muted">Opportunity</span>{' '}
          <JournalRefLink id={opportunityId} base="/opportunities" />
        </div>
        <div>
          <span className="mono-muted">Position</span>{' '}
          <JournalRefLink id={positionId} base="/positions" />
        </div>
        <div>
          <span className="mono-muted">Report</span> <JournalRefLink id={reportId} base="/reports" />
        </div>
      </div>
    </section>
  )
}
