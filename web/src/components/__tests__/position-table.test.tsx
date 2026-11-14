import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { PositionData } from '../../api/positions'
import { PositionTable } from '../position-table'

const NOW = Date.parse('2026-11-06T12:00:00.000Z')

function position(overrides: Partial<PositionData> = {}): PositionData {
  return {
    position_id: 'POS-1',
    account_id: 'ACC-PAPER',
    environment: 'PAPER',
    symbol: 'BTCUSDT',
    side: 'SHORT',
    state: 'OPEN',
    size: '0.042',
    entry_price: '97340.00',
    avg_entry_price: '97340.00',
    mark_price: '96842.10',
    notional_usd: '4088.28',
    unrealized_pnl_usd: null,
    unrealized_pnl_pct: null,
    stop_price: '98210.00',
    target_prices: ['95120.00', '94500.00'],
    leverage: 3,
    margin_mode: 'ISOLATED',
    margin_usd: '1362.76',
    risk_amount_usd: '136.28',
    risk_pct: 0.62,
    fees_usd: '2.04',
    funding_usd: '1.00',
    realized_pnl_usd: null,
    opened_at: '2026-11-05T06:00:00.000Z',
    updated_at: '2026-11-06T11:59:50.000Z',
    closed_at: null,
    exit_price: null,
    source_trigger_id: null,
    source_decision_id: null,
    management_history: [],
    data_quality: { status: 'COMPLETE', reasons: [] },
    ...overrides,
  }
}

describe('PositionTable', () => {
  it('renders all documented columns inside a labelled scrollable region with a caption', () => {
    render(<PositionTable positions={[position()]} now={NOW} />)

    const region = screen.getByRole('region', { name: /未平仓持仓/ })
    expect(region).toHaveAttribute('tabindex', '0')

    const table = screen.getByRole('table', { name: /未平仓持仓/ })
    expect(table.querySelector('caption')).not.toBeNull()
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((header) => header.textContent)
    expect(headers).toEqual([
      'Symbol',
      'Side',
      'Size',
      'Entry',
      'Mark',
      'uPnL',
      'R',
      'Stop',
      'Target',
      'Lev',
      'Margin',
      'Risk',
      'Age',
      'Env',
    ])

    const row = within(table).getByRole('row', { name: /BTCUSDT/ })
    expect(within(row).getByRole('link', { name: 'BTCUSDT' })).toHaveAttribute(
      'href',
      '/positions/POS-1',
    )
    expect(within(row).getByText('POS-1')).toBeInTheDocument()
    expect(within(row).getByText('▼ SHORT')).toBeInTheDocument()
    expect(within(row).getByText('95120.00 / 94500.00')).toBeInTheDocument()
    expect(within(row).getByText('3x')).toBeInTheDocument()
    expect(within(row).getByText('0.62%')).toBeInTheDocument()
    expect(within(row).getByText('1d 06h')).toBeInTheDocument()
    expect(within(row).getByText('PAPER')).toBeInTheDocument()
  })

  it('labels a stale mark as STALE and keeps the last measured values', () => {
    render(
      <PositionTable
        positions={[position({ updated_at: '2026-11-06T10:00:00.000Z' })]}
        now={NOW}
      />,
    )

    const row = screen.getByRole('row', { name: /BTCUSDT/ })
    expect(within(row).getByText(/STALE/)).toBeInTheDocument()
    expect(within(row).getByText('96842.10')).toBeInTheDocument()
    expect(within(row).getByText('+19.87')).toBeInTheDocument()
  })

  it('renders a dash and no fabricated PnL when the mark is missing', () => {
    render(
      <PositionTable
        positions={[
          position({
            symbol: 'XAUUSDT',
            mark_price: null,
            data_quality: { status: 'PARTIAL', reasons: ['mark missing'] },
          }),
        ]}
        now={NOW}
      />,
    )

    const row = screen.getByRole('row', { name: /XAUUSDT/ })
    expect(within(row).getByText(/STALE/)).toBeInTheDocument()
    expect(within(row).getByText(/PARTIAL/)).toBeInTheDocument()
    expect(within(row).queryByText(/^[+-]\d/)).toBeNull()
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0)
  })

  it('explains the empty state and links only to opportunities and paper', () => {
    render(<PositionTable positions={[]} now={NOW} />)

    expect(screen.getByText(/没有未平仓持仓/)).toBeInTheDocument()
    expect(screen.getByText(/Paper 与 Live 范围不同/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看机会/ })).toHaveAttribute('href', '/opportunities')
    expect(screen.getByRole('link', { name: /Paper 账户/ })).toHaveAttribute('href', '/paper')
    expect(document.querySelector('a[href="/journal"]')).toBeNull()
  })
})
