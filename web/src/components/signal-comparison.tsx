import type { JournalCompareData, JournalComparisonSide } from '../api/journal'
import { DataQualityBadge } from './data-quality'

interface SignalComparisonProps {
  compare: JournalCompareData | null
  error?: string | null
}

function text(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return String(value)
}

function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) {
    return 'N/A'
  }
  return value ? 'YES' : 'NO'
}

function statusClass(status: string): string {
  if (status === 'N/A') {
    return 'chip chip--danger'
  }
  if (status === 'PARTIAL' || status === 'ABOVE_ZONE' || status === 'BELOW_ZONE') {
    return 'chip chip--warn'
  }
  return 'chip chip--ok'
}

function Reasons({ side }: { side: JournalComparisonSide }) {
  return <>{side.reasons.length > 0 ? side.reasons.join('; ') : '—'}</>
}

function Row({
  label,
  side,
  detail,
}: {
  label: string
  side: JournalComparisonSide
  detail: string
}) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>
        <span className={statusClass(side.status)}>{side.status}</span>
      </td>
      <td className="mono">{detail}</td>
      <td className="journal-compare-reasons">
        <Reasons side={side} />
      </td>
    </tr>
  )
}

/**
 * The signal-vs-outcome panel. Every side renders its status and named reasons;
 * a PARTIAL or N/A side never hides the rest of the comparison, and an
 * AMBIGUOUS_BAR is stated in text rather than implied by color.
 */
export function SignalComparison({ compare, error = null }: SignalComparisonProps) {
  return (
    <section
      className="panel"
      aria-labelledby="signal-comparison-heading"
      data-testid="signal-comparison"
    >
      <div className="panel-head">
        <h2 id="signal-comparison-heading">Signal vs Outcome 对照</h2>
        {compare ? (
          <DataQualityBadge status={compare.data_quality} />
        ) : (
          <span className="chip chip--danger">N/A</span>
        )}
      </div>
      {compare ? (
        <>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table */}
          <div className="table-wrap" role="region" tabIndex={0} aria-label="信号与结果对照">
            <table className="data-table journal-compare-table">
              <caption className="visually-hidden">
                信号与结果对照：entry zone、slippage、first touch 与 decision
              </caption>
              <thead lang="en">
                <tr>
                  <th scope="col">Side</th>
                  <th scope="col">Status</th>
                  <th scope="col">Detail</th>
                  <th scope="col">Reasons</th>
                </tr>
              </thead>
              <tbody>
                <Row
                  label="Entry zone"
                  side={compare.zone}
                  detail={`${text(compare.zone.entry)} · zone ${text(
                    compare.zone.zone_low,
                  )}–${text(compare.zone.zone_high)}${
                    compare.zone.distance_bps === null ? '' : ` · ${compare.zone.distance_bps} bps`
                  }`}
                />
                <Row
                  label="Slippage"
                  side={compare.slippage}
                  detail={`${text(compare.slippage.entry_avg_price)} vs ${text(
                    compare.slippage.reference_price,
                  )} · ${compare.slippage.bps === null ? '—' : `${compare.slippage.bps} bps`}`}
                />
                <Row
                  label="First touch"
                  side={compare.first_touch}
                  detail={`${text(compare.first_touch.kind)} at ${text(
                    compare.first_touch.at,
                  )}${compare.first_touch.ambiguous ? ' · AMBIGUOUS_BAR' : ''}`}
                />
                <Row
                  label="Decision"
                  side={compare.decision}
                  detail={`${text(compare.decision.decision)} · matched ${yesNo(
                    compare.decision.matched,
                  )} · direction agrees ${yesNo(
                    compare.decision.direction_agrees,
                  )} · ${text(compare.decision.decision_id)} · net ${text(
                    compare.decision.net_pnl_usd,
                  )}`}
                />
              </tbody>
            </table>
          </div>
          {compare.reasons.length > 0 && (
            <p className="panel-foot">原因：{compare.reasons.join('; ')}</p>
          )}
        </>
      ) : (
        <p className="panel-foot" role="alert">
          N/A — {error ?? 'comparison 数据不可用'}
        </p>
      )}
    </section>
  )
}
