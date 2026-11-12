import type { PaperFill, PaperOrder, PaperPosition } from '../api/paper'
import { utcLabel } from '../api/reports'

function dash(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return String(value)
}

function Side({ side }: { side: string }) {
  const upper = side.toUpperCase()
  if (upper === 'SHORT' || upper === 'SELL') {
    return <span className="value-down">▼ {upper}</span>
  }
  if (upper === 'LONG' || upper === 'BUY') {
    return <span className="value-up">▲ {upper}</span>
  }
  return <span className="mono">{upper || '—'}</span>
}

function EmptyState({ message }: { message: string }) {
  return (
    <p className="status-message" role="status">
      {message}
    </p>
  )
}

/** Open positions from the paper ledger. A missing mark is labelled, never filled in. */
export function PaperPositionsTable({ positions }: { positions: PaperPosition[] }) {
  if (positions.length === 0) {
    return (
      <EmptyState message="没有未平仓持仓。接受触发的入场后才会产生模拟持仓；不生成占位持仓。" />
    )
  }
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
    <div className="table-wrap" role="region" tabIndex={0} aria-label="未平仓持仓表">
      <table className="data-table">
        <caption className="visually-hidden">未平仓持仓，{positions.length} 个，Paper</caption>
        <thead lang="en">
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col">Size</th>
            <th scope="col">Entry</th>
            <th scope="col">Mark</th>
            <th scope="col">uPnL</th>
            <th scope="col">Notional</th>
            <th scope="col">Lev</th>
            <th scope="col">Risk</th>
            <th scope="col">Opened</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.position_id}>
              <th scope="row" className="mono">
                {position.symbol}
              </th>
              <td>
                <Side side={position.side} />
              </td>
              <td className="mono">{dash(position.size)}</td>
              <td className="mono">{dash(position.entry_price)}</td>
              <td className="mono">
                {position.mark_price === null ? (
                  <span className="value-down">STALE</span>
                ) : (
                  position.mark_price
                )}
              </td>
              <td className="mono">{dash(position.unrealized_pnl_usd)}</td>
              <td className="mono">{dash(position.notional_usd)}</td>
              <td className="mono">
                {position.leverage === undefined ? '—' : `${position.leverage}x`}
              </td>
              <td className="mono">
                {position.risk_pct === undefined ? '—' : `${position.risk_pct.toFixed(2)}%`}
              </td>
              <td className="mono">
                {position.opened_at ? `${utcLabel(position.opened_at)} UTC` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Simulated orders exactly as persisted. No synthetic filled rows. */
export function PaperOrdersTable({ orders, total }: { orders: PaperOrder[]; total?: number }) {
  if (orders.length === 0) {
    return <EmptyState message="暂无模拟订单。运行扫描并接受一次触发的入场后才会出现订单。" />
  }
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
    <div className="table-wrap" role="region" tabIndex={0} aria-label="模拟订单表">
      <table className="data-table">
        <caption className="visually-hidden">
          模拟订单{total !== undefined ? `，共 ${total} 条` : ''} · 最近 {orders.length} 条
        </caption>
        <thead lang="en">
          <tr>
            <th scope="col">Order ID</th>
            <th scope="col">Time</th>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col">Type</th>
            <th scope="col">Qty</th>
            <th scope="col">Price</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={order.order_id}>
              <th scope="row" className="mono">
                {order.order_id}
              </th>
              <td className="mono">{utcLabel(order.created_at)} UTC</td>
              <td className="mono">{order.symbol}</td>
              <td>
                <Side side={order.side} />
              </td>
              <td className="mono">{order.type}</td>
              <td className="mono">{order.quantity}</td>
              <td className="mono">{order.avg_fill_price ?? order.price ?? 'MARKET'}</td>
              <td>
                <span
                  className={
                    order.status === 'FILLED'
                      ? 'chip chip--ok'
                      : order.status === 'REJECTED'
                        ? 'chip chip--danger'
                        : 'chip'
                  }
                >
                  {order.status}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Simulated fills. A missing fee is `N/A`; it is never counted as zero. */
export function PaperFillsTable({ fills, total }: { fills: PaperFill[]; total?: number }) {
  if (fills.length === 0) {
    return <EmptyState message="暂无模拟成交。不显示虚构成交；接受入场并成交后才会有 fill。" />
  }
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
    <div className="table-wrap" role="region" tabIndex={0} aria-label="模拟成交表">
      <table className="data-table">
        <caption className="visually-hidden">
          模拟成交{total !== undefined ? `，共 ${total} 条` : ''} · 最近 {fills.length} 条
        </caption>
        <thead lang="en">
          <tr>
            <th scope="col">Fill ID</th>
            <th scope="col">Time</th>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col">Qty</th>
            <th scope="col">Price</th>
            <th scope="col">Fee</th>
            <th scope="col">Order ID</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((fill) => (
            <tr key={fill.fill_id}>
              <th scope="row" className="mono">
                {fill.fill_id}
              </th>
              <td className="mono">{utcLabel(fill.filled_at)} UTC</td>
              <td className="mono">{fill.symbol}</td>
              <td>
                <Side side={fill.side} />
              </td>
              <td className="mono">{fill.quantity}</td>
              <td className="mono">{fill.price}</td>
              <td className="mono">{fill.fee === null ? 'N/A' : fill.fee}</td>
              <td className="mono">{fill.order_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
