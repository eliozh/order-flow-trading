import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { PositionData, PositionPnlData } from '../../api/positions'
import { PnlBreakdown } from '../pnl-breakdown'

function position(overrides: Partial<PositionData> = {}): PositionData {
  return {
    position_id: 'POS-1',
    account_id: 'ACC-PAPER',
    environment: 'PAPER',
    symbol: 'ETHUSDT',
    side: 'LONG',
    state: 'OPEN',
    size: '8.42',
    entry_price: '3214.60',
    avg_entry_price: '3214.60',
    mark_price: '3284.55',
    notional_usd: '27066.93',
    unrealized_pnl_usd: null,
    unrealized_pnl_pct: null,
    stop_price: '3118.40',
    target_prices: ['3420.00'],
    leverage: 5,
    margin_mode: 'ISOLATED',
    margin_usd: '5413.39',
    risk_amount_usd: '796.80',
    risk_pct: 0.8,
    fees_usd: '12.44',
    funding_usd: '-10.78',
    realized_pnl_usd: null,
    opened_at: '2026-11-04T06:00:00.000Z',
    updated_at: '2026-11-06T11:59:50.000Z',
    closed_at: null,
    exit_price: null,
    source_trigger_id: 'TRG-1',
    source_decision_id: 'DEC-1',
    management_history: [],
    data_quality: { status: 'COMPLETE', reasons: [] },
    ...overrides,
  }
}

function pnl(overrides: Partial<PositionPnlData> = {}): PositionPnlData {
  return {
    basis: 'ACCUMULATED_GROSS',
    gross_pnl_usd: '0.00',
    realized_gross_pnl_usd: '0.00',
    fees_usd: '12.44',
    funding_usd: '-10.78',
    net_pnl_usd: null,
    r_multiple: null,
    return_pct: null,
    mae_usd: null,
    mfe_usd: null,
    outcome_tags: [],
    data_quality: { status: 'COMPLETE', reasons: [] },
    ...overrides,
  }
}

describe('PnlBreakdown', () => {
  it('shows price, recorded fees, recorded funding, and the net term with an explicit basis', () => {
    render(<PnlBreakdown position={position()} pnl={pnl()} />)

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    expect(within(table).getByText('Price PnL')).toBeInTheDocument()
    expect(within(table).getByText('+588.98')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    expect(within(table).getByText('+565.76')).toBeInTheDocument()
    expect(screen.getAllByText(/ACCUMULATED_GROSS/).length).toBeGreaterThan(0)
    expect(screen.getByText(/recorded fees/)).toBeInTheDocument()
  })

  it('shows recorded fees and funding while marking an unmeasured price/net term as a dash', () => {
    render(<PnlBreakdown position={position({ mark_price: null })} pnl={pnl({ basis: 'NET' })} />)

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    const priceRow = within(table).getByRole('row', { name: /Price PnL/ })
    const netRow = within(table).getByRole('row', { name: /Net uPnL/ })
    expect(within(priceRow).getByText('—')).toBeInTheDocument()
    expect(within(netRow).getByText('—')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    expect(screen.getByText(/mark 缺失/)).toBeInTheDocument()
    expect(screen.getByText(/不把未测量项当零/)).toBeInTheDocument()
  })

  it('surfaces the PnL data-quality status and reasons from the history projection', () => {
    render(
      <PnlBreakdown
        position={position()}
        pnl={pnl({
          data_quality: {
            status: 'PARTIAL',
            reasons: ['funding settlement missing a recorded rate'],
          },
        })}
      />,
    )

    expect(screen.getByText(/PnL data quality: PARTIAL/)).toBeInTheDocument()
    expect(screen.getByText(/funding settlement missing a recorded rate/)).toBeInTheDocument()
  })

  it('never presents an unmeasured fee or funding term as zero', () => {
    render(
      <PnlBreakdown
        position={position({ fees_usd: 'not-a-number', funding_usd: '' })}
        pnl={pnl()}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    const netRow = within(table).getByRole('row', { name: /Net uPnL/ })
    expect(within(netRow).getByText('—')).toBeInTheDocument()
  })
})
