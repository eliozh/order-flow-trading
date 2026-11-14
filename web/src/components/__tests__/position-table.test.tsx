import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PositionTable } from '../position-table'
import {
  FRESH_MARK_AS_OF,
  NOW,
  positionFixture,
  STALE_MARK_AS_OF,
} from '../../test/positions-fixtures'

function btcShort(overrides: Parameters<typeof positionFixture>[0] = {}) {
  return positionFixture({
    position_id: 'POS-1',
    symbol: 'BTCUSDT',
    side: 'SHORT',
    size: '0.042',
    entry_price: '97340.00',
    avg_entry_price: '97340.00',
    mark_price: '96842.10',
    resolved_mark_price: '96842.10',
    resolved_mark_as_of: FRESH_MARK_AS_OF,
    notional_usd: '4088.28',
    stop_price: '98210.00',
    target_prices: ['95120.00', '94500.00'],
    leverage: 3,
    margin_usd: '1362.76',
    risk_amount_usd: '136.28',
    risk_pct: 0.62,
    fees_usd: '2.04',
    funding_usd: '1.00',
    opened_at: '2026-11-05T06:00:00.000Z',
    source_trigger_id: null,
    source_decision_id: null,
    ...overrides,
  })
}

describe('PositionTable', () => {
  it('renders all documented columns inside a labelled scrollable region with a caption', () => {
    render(<PositionTable positions={[btcShort()]} now={NOW} />)

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

  it('renders the resolved mark and its as-of label', () => {
    render(
      <PositionTable
        positions={[
          btcShort({
            mark_price: '1.00',
            resolved_mark_price: '99000.00',
            resolved_mark_as_of: FRESH_MARK_AS_OF,
            resolved_mark_source: 'BARS',
          }),
        ]}
        now={NOW}
      />,
    )

    const row = screen.getByRole('row', { name: /BTCUSDT/ })
    expect(within(row).getByText('99000.00')).toBeInTheDocument()
    expect(within(row).queryByText('1.00')).not.toBeInTheDocument()
    expect(within(row).getByText(/as of .* UTC/)).toBeInTheDocument()
  })

  it('labels a stale resolved mark as STALE and keeps the last measured values', () => {
    render(
      <PositionTable positions={[btcShort({ resolved_mark_as_of: STALE_MARK_AS_OF })]} now={NOW} />,
    )

    const row = screen.getByRole('row', { name: /BTCUSDT/ })
    expect(within(row).getByText(/STALE/)).toBeInTheDocument()
    expect(within(row).getByText('96842.10')).toBeInTheDocument()
    expect(within(row).getByText('+19.87')).toBeInTheDocument()
  })

  it('falls back to the persisted mark when the resolved fields are absent', () => {
    const legacy = btcShort()
    delete legacy.resolved_mark_price
    delete legacy.resolved_mark_as_of
    render(<PositionTable positions={[legacy]} now={NOW} />)

    const row = screen.getByRole('row', { name: /BTCUSDT/ })
    expect(within(row).getByText('96842.10')).toBeInTheDocument()
    expect(within(row).getByText('+19.87')).toBeInTheDocument()
  })

  it('renders a dash and no fabricated PnL when the mark is missing', () => {
    render(
      <PositionTable
        positions={[
          btcShort({
            symbol: 'XAUUSDT',
            mark_price: null,
            resolved_mark_price: null,
            resolved_mark_as_of: null,
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

  it('never shows the empty state when the ledger total is non-zero', () => {
    render(<PositionTable positions={[]} now={NOW} total={3} truncated />)

    expect(screen.queryByRole('heading', { name: '没有未平仓持仓' })).not.toBeInTheDocument()
    expect(screen.getByTestId('position-table-page-empty')).toHaveTextContent(/台账共有 3/)
    expect(screen.getByTestId('position-table-page-empty')).toHaveTextContent(/PARTIAL/)
  })

  it('marks a truncated page as PARTIAL in the caption and a visible note', () => {
    render(<PositionTable positions={[btcShort()]} now={NOW} total={4} truncated />)

    const table = screen.getByRole('table', { name: /未平仓持仓/ })
    expect(table.querySelector('caption')).toHaveTextContent(/存在下一页/)
    expect(screen.getByTestId('position-table-truncated')).toHaveTextContent(/1 \/ 4/)
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
