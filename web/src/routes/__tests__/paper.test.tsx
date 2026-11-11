import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ApiMeta } from '../../api/status'
import type {
  PaperAccountEnvelope,
  PaperFill,
  PaperFillsEnvelope,
  PaperOrder,
  PaperOrdersEnvelope,
  PaperPosition,
} from '../../api/paper'
import { PaperPage } from '../paper'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

export function position(overrides: Partial<PaperPosition> = {}): PaperPosition {
  return {
    position_id: 'POS-1',
    symbol: 'BTCUSDT',
    side: 'LONG',
    state: 'OPEN',
    size: '0.018',
    entry_price: '96714.20',
    avg_entry_price: '96714.20',
    mark_price: '96842.10',
    notional_usd: '1743.16',
    unrealized_pnl_usd: null,
    stop_price: '95500.00',
    leverage: 3,
    margin_usd: '581.05',
    risk_pct: 0.8,
    opened_at: '2026-11-06T04:19:02.000Z',
    ...overrides,
  }
}

export function order(overrides: Partial<PaperOrder> = {}): PaperOrder {
  return {
    order_id: 'PO-8890',
    client_order_id: 'oft-paper-BTCUSDT-entry-abc',
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
    ...overrides,
  }
}

export function fill(overrides: Partial<PaperFill> = {}): PaperFill {
  return {
    fill_id: 'PF-1',
    order_id: 'PO-8890',
    symbol: 'BTCUSDT',
    side: 'BUY',
    price: '96714.20',
    quantity: '0.018',
    fee: '1.74',
    fee_asset: 'USDT',
    maker: false,
    filled_at: '2026-11-06T04:19:02.000Z',
    ...overrides,
  }
}

function accountEnvelope(overrides: Partial<PaperAccountEnvelope['data'] & object> = {}) {
  return {
    ok: true,
    data: {
      account_id: 'ACC-PAPER',
      environment: 'PAPER',
      currency: 'USDT',
      initial_balance: '10000.00',
      cash_balance: '10000.00',
      balance: '10000.00',
      positions: [position()],
      open_positions: 1,
      unrealized_at_last_mark: '1743.16',
      equity: '11743.16',
      stale_inputs: [],
      updated_at: '2026-11-06T04:19:02.000Z',
      ...overrides,
    },
    error: null,
    meta,
  } as PaperAccountEnvelope
}

function ordersEnvelope(items: PaperOrder[]): PaperOrdersEnvelope {
  return { ok: true, data: { items, total: items.length }, error: null, meta }
}

function fillsEnvelope(items: PaperFill[]): PaperFillsEnvelope {
  return { ok: true, data: { items, total: items.length }, error: null, meta }
}

async function renderPaper(
  options: {
    account?: PaperAccountEnvelope
    orders?: PaperOrder[]
    fills?: PaperFill[]
  } = {},
) {
  render(
    <PaperPage
      fetchAccountFn={async () => options.account ?? accountEnvelope()}
      fetchOrdersFn={async () => ordersEnvelope(options.orders ?? [order()])}
      fetchFillsFn={async () => fillsEnvelope(options.fills ?? [fill()])}
    />,
  )
  return screen.findByRole('heading', { name: /Paper 交易账户/ })
}

describe('PaperPage', () => {
  it('shows a loading status while the account snapshot loads', () => {
    render(
      <PaperPage
        fetchAccountFn={() => new Promise<PaperAccountEnvelope>(() => undefined)}
        fetchOrdersFn={async () => ordersEnvelope([])}
        fetchFillsFn={async () => fillsEnvelope([])}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/加载 Paper 账户/)
  })

  it('shows an alert when the account snapshot fails', async () => {
    render(
      <PaperPage
        fetchAccountFn={async () => {
          throw new Error('paper engine down')
        }}
        fetchOrdersFn={async () => ordersEnvelope([])}
        fetchFillsFn={async () => fillsEnvelope([])}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/paper engine down/)
  })

  it('renders equity, cash, open positions, orders, fills, and positions from the read models', async () => {
    await renderPaper()

    expect(screen.getByText('11743.16')).toBeInTheDocument()
    expect(screen.getByText('USDT')).toBeInTheDocument()
    expect(screen.getByText(/1 个未平仓/)).toBeInTheDocument()

    const orders = screen.getByRole('table', { name: /模拟订单/ })
    expect(within(orders).getByText('PO-8890')).toBeInTheDocument()
    expect(within(orders).getByText('FILLED')).toBeInTheDocument()

    const fills = screen.getByRole('table', { name: /模拟成交/ })
    expect(within(fills).getByText('PF-1')).toBeInTheDocument()
    expect(within(fills).getByText('1.74')).toBeInTheDocument()

    const positions = screen.getByRole('table', { name: /未平仓持仓/ })
    expect(within(positions).getByText('BTCUSDT')).toBeInTheDocument()
    expect(within(positions).getByText('96714.20')).toBeInTheDocument()
  })

  it('labels a missing mark as STALE instead of fabricating a value', async () => {
    await renderPaper({
      account: accountEnvelope({
        positions: [position({ mark_price: null })],
        stale_inputs: ['mark'],
      }),
    })

    const positions = screen.getByRole('table', { name: /未平仓持仓/ })
    expect(within(positions).getByText(/STALE/)).toBeInTheDocument()
    expect(screen.getByText(/mark 不可用/)).toBeInTheDocument()
  })

  it('shows no fake fills and explains why the account is empty', async () => {
    await renderPaper({
      account: accountEnvelope({
        positions: [],
        open_positions: 0,
        unrealized_at_last_mark: '0',
        equity: '10000.00',
        cash_balance: '10000.00',
      }),
      orders: [],
      fills: [],
    })

    expect(screen.getByText(/模拟账户尚未产生交易/)).toBeInTheDocument()
    expect(screen.getByText(/不显示虚构/)).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: /模拟成交/ })).not.toBeInTheDocument()
  })

  it('never offers a paper reset or any live/management action', async () => {
    await renderPaper()

    expect(screen.queryByRole('button', { name: /重置/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Live|实盘/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/paper reset/i)).not.toBeInTheDocument()
  })

  it('is read-only at 375px', async () => {
    render(
      <PaperPage
        isCompact
        fetchAccountFn={async () => accountEnvelope()}
        fetchOrdersFn={async () => ordersEnvelope([order()])}
        fetchFillsFn={async () => fillsEnvelope([fill()])}
      />,
    )
    await screen.findByRole('heading', { name: /Paper 交易账户/ })

    expect(screen.getByText(/移动端只读/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重置/ })).not.toBeInTheDocument()
  })
})

describe('PaperPage source safety', () => {
  it('uses a caption and no raw HTML on each table', async () => {
    await renderPaper()
    for (const name of [/模拟订单/, /模拟成交/, /未平仓持仓/]) {
      const table = screen.getByRole('table', { name })
      expect(table.querySelector('caption')).not.toBeNull()
    }
    expect(document.querySelector('script')).toBeNull()
  })
})
