import type { ReactElement } from 'react'

import { numberOrNull, type EntryDecisionData, type JournalTradeData } from '../api/journal'
import { dash, type PositionHistoryEvent } from '../api/positions'
import { confidenceBand } from '../api/reports'
import type { TriggerPayload } from '../api/triggers'
import { ProvenanceStamp } from './provenance'
import { ReviewProvenance } from './review-band'

const BAND_LABELS = { high: '高', medium: '中', low: '低' } as const
const BAND_CLASSES = {
  high: 'chip chip--ok',
  medium: 'chip chip--warn',
  low: 'chip chip--danger',
} as const

/** A same-origin link for a referenced id, or a plain N/A when there is none. */
export function JournalRefLink({
  id,
  base,
}: {
  id: string | null | undefined
  base: string
}): ReactElement {
  if (!id) {
    return <span lang="en">N/A</span>
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
        <span className="mono-muted" lang="en">
          signal → trigger → decision → entry → management → exit
        </span>
      </div>
      <ol className="timeline timeline--stages" aria-label="交易时间线">
        <li className={signalTime ? 'acc' : undefined}>
          <div className="tl-time">{signalTime ? `${signalTime} UTC` : 'N/A'}</div>
          <div className="tl-title" lang="en">
            signal created
          </div>
          <div className="tl-desc" lang="en">
            {refs?.opportunity_id ?? 'N/A'} · {trade.symbol} {trade.side} · report{' '}
            {refs?.report_id ?? 'N/A'}
          </div>
          {signalTime === null && <div className="tl-desc">N/A — {signalReason}</div>}
        </li>
        <li className={trigger ? 'acc' : undefined}>
          <div className="tl-time">{trigger ? `${trigger.triggered_at} UTC` : 'N/A'}</div>
          <div className="tl-title" lang="en">
            triggered
          </div>
          <div className="tl-desc">
            {trigger ? (
              <span lang="en">{`snapshot ${trigger.snapshot.as_of} · ${trigger.snapshot.data_quality} · ${trigger.trigger_id}`}</span>
            ) : (
              `N/A — ${triggerReason}`
            )}
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
          <div className="tl-title" lang="en">
            {decisionText ? `LLM decision ${decisionText}` : 'LLM decision N/A'}
          </div>
          <div className="tl-desc">
            {decisionText ? (
              <span lang="en">{`confidence ${decisionConfidence === null ? 'N/A' : decisionConfidence.toFixed(2)} · ${decisionId ?? 'N/A'} · prompt ${String(
                (decisionProvenance?.prompt_id as string | undefined) ?? 'N/A',
              )}${typeof decisionProvenance?.prompt_version === 'number' ? ` v${decisionProvenance.prompt_version}` : ''}`}</span>
            ) : (
              `N/A — ${decisionReason}`
            )}
          </div>
        </li>
        <li className="ok">
          <div className="tl-time">{trade.opened_at} UTC</div>
          <div className="tl-title" lang="en">
            entry
          </div>
          <div className="tl-desc" lang="en">
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
          <div className="tl-title" lang="en">
            management
          </div>
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
          <div className="tl-title" lang="en">
            exit
          </div>
          <div className="tl-desc" lang="en">
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
        <h2 id="journal-snapshot-heading" lang="en">
          Signal snapshot
        </h2>
        <span className="mono-muted" lang="en">
          CALC · source report
        </span>
      </div>
      <dl className="kv">
        <dt lang="en">Signal / Opportunity</dt>
        <dd className="mono" lang="en">
          {opportunityId ?? 'N/A'}
        </dd>
        <dt lang="en">Proposed zone</dt>
        <dd className="mono" lang="en">
          {zoneText}
        </dd>
        <dt lang="en">Actual entry</dt>
        <dd className="mono">{dash(entryAvgPrice)}</dd>
        <dt lang="en">Trigger→fill slippage</dt>
        <dd className="mono">{slippageText}</dd>
        <dt lang="en">Invalidation</dt>
        <dd>{invalidationText}</dd>
        <dt lang="en">Source report</dt>
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
        <h2 id="journal-fills-heading" lang="en">
          Fills
        </h2>
        {partial && (
          <span className="chip chip--warn" lang="en">
            PARTIAL
          </span>
        )}
      </div>
      <dl className="kv">
        <dt lang="en">Entry fill</dt>
        <dd className="mono" lang="en">
          {dash(trade.entry_avg_price)} · size {dash(trade.size)}
        </dd>
        <dt lang="en">Exit fill</dt>
        <dd className="mono" lang="en">
          {dash(trade.exit_avg_price)} · size {dash(trade.size)}
        </dd>
        <dt lang="en">Gross / Fees / Funding</dt>
        <dd className="mono">
          {dash(trade.gross_pnl_usd)} / {dash(trade.fees_usd)} / {dash(trade.funding_usd)}
        </dd>
        <dt lang="en">Net PnL</dt>
        <dd className="mono">{trade.net_pnl_usd}</dd>
        <dt lang="en">Opened / Closed</dt>
        <dd className="mono">
          {trade.opened_at} / {trade.closed_at}
        </dd>
      </dl>
      {partial && qualityReasons.length > 0 && (
        <ul className="journal-fills-reasons">
          {qualityReasons.map((reason, index) => (
            <li key={`${index}-${reason.slice(0, 16)}`} lang="en">
              {reason}
            </li>
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
export function JournalLinks({
  triggerId,
  opportunityId,
  positionId,
  reportId,
}: JournalLinksProps) {
  return (
    <section className="panel" aria-labelledby="journal-links-heading">
      <div className="panel-head">
        <h2 id="journal-links-heading">关联</h2>
      </div>
      <div className="journal-links">
        <div>
          <span className="mono-muted" lang="en">
            Trigger
          </span>{' '}
          <JournalRefLink id={triggerId} base="/triggers" />
        </div>
        <div>
          <span className="mono-muted" lang="en">
            Opportunity
          </span>{' '}
          <JournalRefLink id={opportunityId} base="/opportunities" />
        </div>
        <div>
          <span className="mono-muted" lang="en">
            Position
          </span>{' '}
          <JournalRefLink id={positionId} base="/positions" />
        </div>
        <div>
          <span className="mono-muted" lang="en">
            Report
          </span>{' '}
          <JournalRefLink id={reportId} base="/reports" />
        </div>
      </div>
    </section>
  )
}

interface JournalDecisionPanelProps {
  entryDecision: EntryDecisionData | null
  decisionText: string | null
  decisionConfidence: number | null
  decisionId: string | null
  decisionValidation: string | null
  decisionRationale: string | null
  decisionProvenance: Record<string, unknown> | null
  decisionReason: string
}

/** The persisted entry decision, separate from the advisory review band. */
export function JournalDecisionPanel({
  entryDecision,
  decisionText,
  decisionConfidence,
  decisionId,
  decisionValidation,
  decisionRationale,
  decisionProvenance,
  decisionReason,
}: JournalDecisionPanelProps) {
  const confidenceValue = numberOrNull(decisionConfidence)
  const band = confidenceBand(confidenceValue)
  return (
    <section className="panel" aria-labelledby="journal-decision-heading">
      <div className="panel-head">
        <h2 id="journal-decision-heading">LLM 决策记录</h2>
        <ProvenanceStamp kind="LLM" />
      </div>
      {entryDecision?.status === 'FOUND' && entryDecision.decision ? (
        <>
          <dl className="kv">
            <dt lang="en">Decision</dt>
            <dd className="mono">{decisionText ?? 'N/A'}</dd>
            <dt lang="en">Confidence</dt>
            <dd className="mono">
              {confidenceValue === null ? '—' : confidenceValue.toFixed(2)}{' '}
              {band && <span className={BAND_CLASSES[band]}>{BAND_LABELS[band]}</span>}
            </dd>
            <dt lang="en">Validation</dt>
            <dd className="mono">{decisionValidation ?? 'N/A'}</dd>
            <dt lang="en">Decision ID</dt>
            <dd className="mono">{decisionId ?? 'N/A'}</dd>
          </dl>
          {decisionRationale !== null && (
            <p className="prose journal-decision-rationale">{decisionRationale}</p>
          )}
          <ReviewProvenance provenance={decisionProvenance} />
        </>
      ) : (
        <p className="panel-foot" data-testid="journal-decision-unavailable">
          N/A — {decisionReason}
        </p>
      )}
    </section>
  )
}

interface JournalExportPanelProps {
  headingId: string
  csvLabel: string
  jsonLabel: string
  exporting: 'csv' | 'json' | null
  exportError: string | null
  errorTestId: string
  onExport: (format: 'csv' | 'json') => void
}

/** The server-generated export panel shared by the journal list and detail. */
export function JournalExportPanel({
  headingId,
  csvLabel,
  jsonLabel,
  exporting,
  exportError,
  errorTestId,
  onExport,
}: JournalExportPanelProps) {
  return (
    <section className="panel" aria-labelledby={headingId}>
      <div className="panel-head">
        <h2 id={headingId}>导出</h2>
        <span className="mono-muted">服务端生成 · CSV formula 中和在服务端完成</span>
      </div>
      <div className="action-row journal-filter-actions">
        <button
          type="button"
          className="btn"
          disabled={exporting !== null}
          aria-busy={exporting === 'csv'}
          onClick={() => onExport('csv')}
        >
          {exporting === 'csv' ? '正在导出…' : csvLabel}
        </button>
        <button
          type="button"
          className="btn"
          disabled={exporting !== null}
          aria-busy={exporting === 'json'}
          onClick={() => onExport('json')}
        >
          {exporting === 'json' ? '正在导出…' : jsonLabel}
        </button>
      </div>
      {exportError !== null && (
        <p className="mono-muted" role="alert" data-testid={errorTestId}>
          导出失败：{exportError}
        </p>
      )}
      <p className="panel-foot">
        导出文件由 API 生成并下载；客户端不生成文件。CSV 公式前缀在服务端中和。
      </p>
    </section>
  )
}
