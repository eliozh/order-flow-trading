import { useId } from 'react'

import type { PositionData } from '../api/positions'
import { formatSignedPercent } from '../api/positions'
import {
  durationLabel,
  numberOrNull,
  outcomeOf,
  rLabel,
  type JournalOutcome,
  type TradeLogSummary,
} from '../api/journal'

interface JournalTableProps {
  trades: TradeLogSummary[]
  openPositions?: PositionData[]
  total?: number
  captionNote?: string
}

function Side({ side }: { side: string }) {
  const upper = side.toUpperCase()
  if (upper === 'SHORT') {
    return <span className="value-down">▼ SHORT</span>
  }
  if (upper === 'LONG') {
    return <span className="value-up">▲ LONG</span>
  }
  return <span className="mono">{upper || '—'}</span>
}

function OutcomeChip({ outcome }: { outcome: JournalOutcome }) {
  const className =
    outcome === 'WIN'
      ? 'chip chip--ok'
      : outcome === 'LOSS'
        ? 'chip chip--danger'
        : outcome === 'PARTIAL'
          ? 'chip chip--warn'
          : 'chip'
  return <span className={className}>{outcome}</span>
}

function QualityChip({ status, reasons = [] }: { status: string; reasons?: string[] }) {
  const describedBy = useId()
  const className =
    status === 'COMPLETE'
      ? 'chip chip--ok'
      : status === 'MISSING'
        ? 'chip chip--danger'
        : 'chip chip--warn'
  return (
    <span
      className={className}
      title={reasons.length > 0 ? reasons.join('; ') : undefined}
      aria-describedby={reasons.length > 0 ? describedBy : undefined}
    >
      {status}
      {reasons.length > 0 && (
        <span id={describedBy} className="visually-hidden">
          {`data quality reasons: ${reasons.join('; ')}`}
        </span>
      )}
    </span>
  )
}

function netClass(value: string): string {
  const parsed = numberOrNull(value)
  if (parsed === null) {
    return 'mono'
  }
  if (parsed > 0) {
    return 'mono value-up'
  }
  if (parsed < 0) {
    return 'mono value-down'
  }
  return 'mono value-flat'
}

/**
 * The closed-trade journal table plus the OPEN rows that are explicitly
 * excluded from the statistics. The list contract carries no entry/exit/signal
 * columns, so none are invented here; open rows show `—` for every PnL term.
 */
export function JournalTable({
  trades,
  openPositions = [],
  total = trades.length,
  captionNote,
}: JournalTableProps) {
  if (trades.length === 0 && openPositions.length === 0) {
    return null
  }

  const captionParts = [`已平仓交易，${trades.length} 条`]
  if (total > trades.length) {
    captionParts.push(`共 ${total} 条`)
  }
  if (captionNote) {
    captionParts.push(captionNote)
  }
  if (openPositions.length > 0) {
    captionParts.push(`${openPositions.length} 个开放持仓标记为 OPEN excluded`)
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table
    <div className="table-wrap" role="region" tabIndex={0} aria-label="交易日志表">
      <table className="data-table journal-table">
        <caption className="visually-hidden">{captionParts.join(' · ')}</caption>
        <thead lang="en">
          <tr>
            <th scope="col">TRADE ID</th>
            <th scope="col">SYMBOL</th>
            <th scope="col">SIDE</th>
            <th scope="col">NET PNL</th>
            <th scope="col">R</th>
            <th scope="col">RETURN %</th>
            <th scope="col">DURATION</th>
            <th scope="col">OUTCOME</th>
            <th scope="col">DATA QUALITY</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade) => {
            const quality = trade.data_quality?.status ?? 'MISSING'
            return (
              <tr key={trade.trade_id}>
                <th scope="row" className="mono">
                  <a
                    className="rowlink mono"
                    href={`/journal/${encodeURIComponent(trade.trade_id)}`}
                  >
                    {trade.trade_id}
                  </a>
                </th>
                <td className="mono">{trade.symbol}</td>
                <td>
                  <Side side={trade.side} />
                </td>
                <td className={netClass(trade.net_pnl_usd)}>{trade.net_pnl_usd}</td>
                <td className="mono">{rLabel(trade)}</td>
                <td className="mono">{formatSignedPercent(trade.return_pct)}</td>
                <td className="mono">{durationLabel(trade.opened_at, trade.closed_at)}</td>
                <td>
                  <OutcomeChip outcome={outcomeOf(trade)} />
                </td>
                <td>
                  <QualityChip status={quality} reasons={trade.data_quality?.reasons} />
                </td>
              </tr>
            )
          })}
          {openPositions.map((position) => (
            <tr key={position.position_id} data-testid={`journal-open-row-${position.position_id}`}>
              <th scope="row" className="mono">
                <a
                  className="rowlink mono"
                  href={`/positions/${encodeURIComponent(position.position_id)}`}
                >
                  {position.position_id}
                </a>
              </th>
              <td className="mono">{position.symbol}</td>
              <td>
                <Side side={position.side} />
              </td>
              <td className="mono">—</td>
              <td className="mono">—</td>
              <td className="mono">—</td>
              <td className="mono">—</td>
              <td>
                <span className="chip">OPEN excluded</span>
              </td>
              <td>
                <QualityChip
                  status={position.data_quality?.status ?? 'MISSING'}
                  reasons={position.data_quality?.reasons}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
