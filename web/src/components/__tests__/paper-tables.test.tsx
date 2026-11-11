import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PaperFillsTable, PaperOrdersTable, PaperPositionsTable } from '../paper-tables'
import type { PaperFill, PaperOrder, PaperPosition } from '../../api/paper'

const position: PaperPosition = {
  position_id: 'POS-1',
  symbol: 'BTCUSDT',
  side: 'LONG',
  size: '0.018',
  entry_price: '96714.20',
  mark_price: null,
  notional_usd: '1743.16',
  unrealized_pnl_usd: null,
  leverage: 3,
  risk_pct: 0.8,
  opened_at: '2026-11-06T04:19:02.000Z',
}

const order: PaperOrder = {
  order_id: 'PO-1',
  client_order_id: 'oft-paper-1',
  symbol: 'BTCUSDT',
  side: 'BUY',
  type: 'MARKET',
  quantity: '0.018',
  price: null,
  status: 'FILLED',
  filled_quantity: '0.018',
  avg_fill_price: '96714.20',
  created_at: '2026-11-06T04:19:02.000Z',
  environment: 'PAPER',
}

const fill: PaperFill = {
  fill_id: 'PF-1',
  order_id: 'PO-1',
  symbol: 'BTCUSDT',
  side: 'BUY',
  price: '96714.20',
  quantity: '0.018',
  fee: null,
  filled_at: '2026-11-06T04:19:02.000Z',
}

describe('PaperTables', () => {
  it('labels a missing mark and a missing fee as STALE/N/A, never zero', () => {
    render(
      <>
        <PaperPositionsTable positions={[position]} />
        <PaperFillsTable fills={[fill]} />
      </>,
    )

    const positions = screen.getByRole('table', { name: /未平仓持仓/ })
    expect(within(positions).getByText(/STALE/)).toBeInTheDocument()
    // A missing PnL is rendered as a dash, not a fabricated zero.
    expect(within(positions).getByText('—')).toBeInTheDocument()

    const fills = screen.getByRole('table', { name: /模拟成交/ })
    expect(within(fills).getByText('N/A')).toBeInTheDocument()
  })

  it('renders an explanatory empty row instead of a fake order or fill', () => {
    render(
      <>
        <PaperOrdersTable orders={[]} />
        <PaperFillsTable fills={[]} />
      </>,
    )

    expect(screen.getByText(/暂无模拟订单/)).toBeInTheDocument()
    expect(screen.getByText(/不显示虚构成交/)).toBeInTheDocument()
  })

  it('keeps the order and fill history read-only with captions', () => {
    render(
      <>
        <PaperOrdersTable orders={[order]} />
        <PaperFillsTable fills={[fill]} />
      </>,
    )

    for (const name of [/模拟订单/, /模拟成交/]) {
      const table = screen.getByRole('table', { name })
      expect(table.querySelector('caption')).not.toBeNull()
    }
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
