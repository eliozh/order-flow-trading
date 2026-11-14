import type { PositionData } from '../api/positions'
import {
  dash,
  formatAge,
  formatSignedMoney,
  formatSignedPercent,
  markAsOf,
  markState,
  netPnl,
  pnlPct,
  rMultiple,
  resolvedMark,
} from '../api/positions'
import { utcLabel } from '../api/reports'

interface PositionTableProps {
  positions: PositionData[]
  /** Deterministic clock for age and mark freshness (defaults to Date.now()). */
  now?: number
  /** The unfiltered ledger total; when larger than the page the view is PARTIAL. */
  total?: number
  /** True when the server reports a next page for this query. */
  truncated?: boolean
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

function Environment({ environment }: { environment: string }) {
  const upper = environment.toUpperCase()
  const className =
    upper === 'PAPER'
      ? 'env-chip env-chip--paper'
      : upper === 'LIVE'
        ? 'env-chip env-chip--live'
        : 'env-chip'
  return <span className={className}>{upper || '—'}</span>
}

function targets(position: PositionData): string {
  if (position.target_prices.length === 0) {
    return '—'
  }
  return position.target_prices.join(' / ')
}

function EmptyState() {
  return (
    <div className="panel empty-panel" role="status">
      <p className="kicker" lang="en">
        0 POSITIONS
      </p>
      <h2>没有未平仓持仓</h2>
      <p className="panel-foot" style={{ borderTop: 0, padding: '8px 0' }}>
        Paper 与 Live 范围不同：状态栏显示当前环境。触发的机会在接受后才会成为持仓；本列表只显示
        OPEN 持仓，已平仓交易不在本切片展示。不生成占位持仓。
      </p>
      <div className="action-row">
        <a className="btn" href="/opportunities">
          查看机会
        </a>
        <a className="btn" href="/paper">
          Paper 账户
        </a>
      </div>
    </div>
  )
}

/**
 * The open-position ledger: every documented column, with a mark that is
 * explicitly STALE/PARTIAL instead of a fabricated PnL. Rows link to detail.
 */
export function PositionTable({
  positions,
  now = Date.now(),
  total = positions.length,
  truncated = false,
}: PositionTableProps) {
  if (positions.length === 0) {
    // A page with no rows is never "no open positions" when the ledger total is
    // non-zero: say that this page is empty and the aggregate is PARTIAL.
    if (total > 0) {
      return (
        <div className="panel empty-panel" role="status" data-testid="position-table-page-empty">
          <p className="kicker" lang="en">
            PAGE EMPTY · TOTAL {total}
          </p>
          <h2>本页没有持仓行</h2>
          <p className="panel-foot" style={{ borderTop: 0, padding: '8px 0' }}>
            台账共有 {total} 个未平仓持仓，但当前页未返回任何行；不把该页当作“没有未平仓持仓”。
            汇总标记为 PARTIAL。
          </p>
        </div>
      )
    }
    return <EmptyState />
  }

  const captionTotal =
    total > positions.length ? `，共 ${total} 个（本页 ${positions.length} 个）` : ''

  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table
    <div className="table-wrap" role="region" tabIndex={0} aria-label="未平仓持仓表">
      <table className="data-table position-table">
        <caption className="visually-hidden">
          未平仓持仓，{positions.length} 个，Paper{captionTotal}
          {truncated ? '，存在下一页：汇总为 PARTIAL' : ''}
        </caption>
        <thead lang="en">
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col">Size</th>
            <th scope="col">Entry</th>
            <th scope="col">Mark</th>
            <th scope="col">uPnL</th>
            <th scope="col">R</th>
            <th scope="col">Stop</th>
            <th scope="col">Target</th>
            <th scope="col">Lev</th>
            <th scope="col">Margin</th>
            <th scope="col">Risk</th>
            <th scope="col">Age</th>
            <th scope="col">Env</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const state = markState(position, now)
            const mark = resolvedMark(position)
            const net = netPnl(position)
            const pct = pnlPct(position)
            const r = rMultiple(position)
            const partial = position.data_quality.status !== 'COMPLETE'
            return (
              <tr key={position.position_id}>
                <th scope="row" className="mono">
                  <a
                    className="rowlink mono"
                    href={`/positions/${encodeURIComponent(position.position_id)}`}
                  >
                    {position.symbol}
                  </a>
                  <div className="cell-sub">{position.position_id}</div>
                  {partial && <span className="chip chip--warn">PARTIAL</span>}
                </th>
                <td>
                  <Side side={position.side} />
                </td>
                <td className="mono">{dash(position.size)}</td>
                <td className="mono">{dash(position.avg_entry_price)}</td>
                <td className="mono">
                  {state === 'MISSING' ? (
                    <>
                      <span className="chip chip--warn">STALE</span> <span>—</span>
                    </>
                  ) : (
                    <>
                      {mark}
                      {state === 'STALE' && (
                        <>
                          {' '}
                          <span className="chip chip--warn">STALE</span>
                        </>
                      )}
                      <div className="cell-sub">as of {utcLabel(markAsOf(position))} UTC</div>
                    </>
                  )}
                </td>
                <td className="mono">
                  {net === null ? (
                    <span>—</span>
                  ) : (
                    <>
                      <span
                        className={net > 0 ? 'value-up' : net < 0 ? 'value-down' : 'value-flat'}
                      >
                        {formatSignedMoney(net)}
                      </span>{' '}
                      <span className="cell-sub">
                        ({pct === null ? '—' : formatSignedPercent(pct)})
                      </span>
                    </>
                  )}
                </td>
                <td className="mono">{r === null ? '—' : r.toFixed(2)}</td>
                <td className="mono">{dash(position.stop_price)}</td>
                <td className="mono">{targets(position)}</td>
                <td className="mono">{`${position.leverage}x`}</td>
                <td className="mono">{dash(position.margin_usd)}</td>
                <td className="mono">{`${position.risk_pct.toFixed(2)}%`}</td>
                <td className="mono">{formatAge(position.opened_at, now)}</td>
                <td>
                  <Environment environment={position.environment} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {truncated && (
        <p className="status-message" role="status" data-testid="position-table-truncated">
          列表已分页：仅显示本页 {positions.length} / {total} 个持仓；汇总为 PARTIAL，不把本页当作
          完整台账。
        </p>
      )}
    </div>
  )
}
