import type { PositionData, PositionPnlData } from '../api/positions'
import {
  dash,
  formatSignedMoney,
  netPnl,
  pricePnl,
  recordedFees,
  recordedFunding,
} from '../api/positions'

interface PnlBreakdownProps {
  position: PositionData
  /** The history projection, when loaded: names the basis and its quality. */
  pnl?: PositionPnlData | null
}

function signed(value: number | null): string {
  return value === null ? '—' : formatSignedMoney(value)
}

/**
 * The OPEN-position PnL terms, kept explicit. Price PnL is CALC from the mark;
 * fees and funding are the recorded values. When the mark or a recorded term is
 * unmeasured the term (and the net) is a dash, never a fabricated zero.
 */
export function PnlBreakdown({ position, pnl = null }: PnlBreakdownProps) {
  const price = pricePnl(position)
  const fees = recordedFees(position)
  const funding = recordedFunding(position)
  const net = netPnl(position)
  const feeTerm = fees === null ? null : -fees
  const markMissing = price === null
  const basis = pnl?.basis ?? 'CALC'
  const quality = pnl?.data_quality
  const reasons = quality?.reasons ?? []

  return (
    <div>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table */}
      <div className="table-wrap" role="region" tabIndex={0} aria-label="PnL 分解">
        <table className="data-table pnl-table">
          <caption className="visually-hidden">
            PnL 分解：价格、费用、资金费与净 uPnL。basis {basis}。
          </caption>
          <thead lang="en">
            <tr>
              <th scope="col">Component</th>
              <th scope="col">USDT</th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Price PnL</th>
              <td className={price !== null && price < 0 ? 'mono value-down' : 'mono value-up'}>
                {signed(price)}
              </td>
              <td className="calc-note">
                mark {dash(position.mark_price)} · entry {dash(position.avg_entry_price)}
              </td>
            </tr>
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
              <th scope="row">Net uPnL</th>
              <td className={net !== null && net < 0 ? 'mono value-down' : 'mono value-up'}>
                {signed(net)}
              </td>
              <td className="calc-note">price − fees + funding · basis {basis}</td>
            </tr>
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
      <p className="panel-foot">
        Fees 与 Funding 是已记录的账务项；Price PnL 由 mark 与 entry 计算（CALC）。OPEN 持仓的 basis
        为 {basis}。
      </p>
    </div>
  )
}
