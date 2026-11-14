import type { PositionData, PositionPnlData } from '../api/positions'
import {
  accumulatedGross,
  dash,
  formatSignedMoney,
  markAsOf,
  netPnl,
  pricePnl,
  recordedFees,
  recordedFunding,
  resolvedMark,
} from '../api/positions'
import { utcLabel } from '../api/reports'

interface PnlBreakdownProps {
  position: PositionData
  /** The history projection, when loaded: names the basis and its quality. */
  pnl?: PositionPnlData | null
}

function signed(value: number | null): string {
  return value === null ? '—' : formatSignedMoney(value)
}

function termClass(value: number | null, positiveWhenZero = true): string {
  if (value === null) {
    return 'mono'
  }
  if (value < 0) {
    return 'mono value-down'
  }
  return positiveWhenZero ? 'mono value-up' : 'mono'
}

/** Parse one history-projection term; an absent or malformed value is null. */
function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * The position PnL terms, kept explicit and on separate bases:
 *
 * - ``ACCUMULATED_GROSS``/``CALC`` (OPEN): Price PnL (unrealized) is CALC from
 *   the resolved mark for the remaining size; Accumulated gross (realized) is
 *   the recorded ``realized_pnl_usd`` (rendered only when measured); Fees and
 *   funding are the recorded cumulative accounting terms; Net uPnL (if closed)
 *   = price + accumulated gross − fees + funding.
 * - ``NET`` (a closed projection): the history terms are authoritative. The
 *   stored ``realized_pnl_usd`` is NET, not accumulated gross, so it is never
 *   relabelled as gross. The rows show the projection's own
 *   gross/fees/funding/net values and are not recomputed from the stored mark.
 *
 * When a term is unmeasured it is a dash, never a fabricated zero.
 */
export function PnlBreakdown({ position, pnl = null }: PnlBreakdownProps) {
  const basis = pnl?.basis ?? 'CALC'
  const netBasis = pnl?.basis === 'NET'
  // The history projection owns every term in the NET/closed branch.
  const historyGross = toNumber(pnl?.gross_pnl_usd)
  const historyFees = toNumber(pnl?.fees_usd)
  const historyFunding = toNumber(pnl?.funding_usd)
  const historyNet = toNumber(pnl?.net_pnl_usd)
  // The OPEN/CALC branch computes from the position; it never runs for NET.
  const price = netBasis ? null : pricePnl(position)
  const gross = netBasis ? null : accumulatedGross(position)
  const fees = netBasis ? null : recordedFees(position)
  const funding = netBasis ? null : recordedFunding(position)
  const net = netBasis ? null : netPnl(position)
  const feeTerm = netBasis || fees === null ? null : -fees
  const historyFeeTerm = historyFees === null ? null : -historyFees
  const markMissing = !netBasis && price === null
  const quality = pnl?.data_quality
  const reasons = quality?.reasons ?? []

  return (
    <div>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table */}
      <div className="table-wrap" role="region" tabIndex={0} aria-label="PnL 分解">
        <table className="data-table pnl-table">
          <caption className="visually-hidden">
            PnL 分解：未实现价格 PnL、累计已实现 gross、费用、资金费与净 uPnL（若平仓）；basis{' '}
            {basis}。
          </caption>
          <thead lang="en">
            <tr>
              <th scope="col">Component</th>
              <th scope="col">USDT</th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {netBasis ? (
              <>
                <tr>
                  <th scope="row">Price PnL (realized gross)</th>
                  <td className={termClass(historyGross)}>{signed(historyGross)}</td>
                  <td className="calc-note">
                    history gross_pnl_usd · closed projection · basis {basis}
                  </td>
                </tr>
                <tr>
                  <th scope="row">Fees</th>
                  <td
                    className={
                      historyFeeTerm !== null && historyFeeTerm < 0 ? 'mono value-down' : 'mono'
                    }
                  >
                    {signed(historyFeeTerm)}
                  </td>
                  <td className="calc-note">history fees_usd (cost)</td>
                </tr>
                <tr>
                  <th scope="row">Funding</th>
                  <td
                    className={
                      historyFunding !== null && historyFunding < 0 ? 'mono value-down' : 'mono'
                    }
                  >
                    {signed(historyFunding)}
                  </td>
                  <td className="calc-note">history funding_usd</td>
                </tr>
                <tr>
                  <th scope="row">Net PnL</th>
                  <td className={termClass(historyNet)}>{signed(historyNet)}</td>
                  <td className="calc-note">history net_pnl_usd · basis {basis}</td>
                </tr>
              </>
            ) : (
              <>
                <tr>
                  <th scope="row">Price PnL (unrealized)</th>
                  <td className={termClass(price)}>{signed(price)}</td>
                  <td className="calc-note">
                    mark {dash(resolvedMark(position))} as of {utcLabel(markAsOf(position))} UTC ·
                    entry {dash(position.avg_entry_price)} · remaining size
                  </td>
                </tr>
                {gross !== null && (
                  <tr>
                    <th scope="row">Accumulated gross (realized)</th>
                    <td className={termClass(gross)}>{signed(gross)}</td>
                    <td className="calc-note">recorded realized_pnl_usd · accumulated</td>
                  </tr>
                )}
                <tr>
                  <th scope="row">Fees</th>
                  <td className={feeTerm !== null && feeTerm < 0 ? 'mono value-down' : 'mono'}>
                    {signed(feeTerm)}
                  </td>
                  <td className="calc-note">recorded fees (entry + management)</td>
                </tr>
                <tr>
                  <th scope="row">Funding</th>
                  <td className={funding !== null && funding < 0 ? 'mono value-down' : 'mono'}>
                    {signed(funding)}
                  </td>
                  <td className="calc-note">recorded funding settlements</td>
                </tr>
                <tr>
                  <th scope="row">Net uPnL (if closed)</th>
                  <td className={termClass(net)}>{signed(net)}</td>
                  <td className="calc-note">
                    price + accumulated gross − fees + funding · basis {basis}
                  </td>
                </tr>
              </>
            )}
          </tbody>
        </table>
      </div>
      {markMissing && (
        <p className="panel-foot" role="note">
          mark 缺失（STALE）：价格 PnL 与净 uPnL 未测量，保持 —，不把未测量项当零；已记录的费用与
          资金费仍照常显示。
        </p>
      )}
      {quality && (
        <p className="panel-foot">
          PnL data quality: {quality.status}
          {reasons.length > 0 ? ` — ${reasons.join('; ')}` : ''}.{' '}
          {quality.status === 'COMPLETE' ? '' : '未测量项不会被补零。'}
        </p>
      )}
      {netBasis ? (
        <p className="panel-foot">
          已平仓投影（basis {basis}）：Price PnL、Fees、Funding 与 Net PnL
          直接取历史投影的已记录项，不用存储的 mark 重新计算；存储的 realized_pnl_usd 是 NET，绝不
          标成 accumulated gross。未测量项保持 —。
        </p>
      ) : (
        <p className="panel-foot">
          Price PnL 由 resolved mark 与 entry 计算（CALC），只覆盖剩余规模；Accumulated gross
          是已记录的已实现部分。Fees 与 Funding 是累计已记录账务项；只有两者都测量时才给出 Net
          uPnL（若平仓）。OPEN 持仓的 basis 为 {basis}。
        </p>
      )}
    </div>
  )
}
