import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { JournalCompareData } from '../../api/journal'
import { SignalComparison } from '../signal-comparison'

function compareFixture(overrides: Partial<JournalCompareData> = {}): JournalCompareData {
  return {
    trade_id: 'TRD-1',
    position_id: 'POS-1',
    symbol: 'SOLUSDT',
    side: 'LONG',
    opened_at: '2026-10-28T00:00:00.000Z',
    closed_at: '2026-10-30T08:16:02.000Z',
    net_pnl_usd: '482.16',
    zone: {
      status: 'INSIDE',
      reasons: [],
      entry: '139.82',
      zone_low: '138.20',
      zone_high: '139.80',
      inside: true,
      distance_bps: 1.4,
    },
    slippage: {
      status: 'OK',
      reasons: [],
      entry_avg_price: '139.82',
      reference_price: '139.80',
      reference_as_of: '2026-10-28T04:14:02.000Z',
      bps: 1.4,
      formula: 'trigger_to_fill_bps = (entry - reference)/reference*10000',
    },
    first_touch: {
      status: 'PARTIAL',
      reasons: [
        'AMBIGUOUS_BAR: one recorded bar contains both the stop and the target; the stop is assumed first',
      ],
      kind: 'STOP',
      at: '2026-10-29T12:00:00.000Z',
      ambiguous: true,
    },
    decision: {
      status: 'OK',
      reasons: [],
      decision: 'ENTER_LONG',
      direction_agrees: true,
      matched: false,
      net_pnl_usd: '482.16',
      decision_id: 'DEC-ENTRY-1',
    },
    reasons: ['AMBIGUOUS_BAR'],
    data_quality: 'PARTIAL',
    ...overrides,
  }
}

describe('SignalComparison', () => {
  it('renders the four sides with their statuses and details', () => {
    render(<SignalComparison compare={compareFixture()} />)

    const table = screen.getByRole('table', { name: /信号与结果对照/ })
    const zone = within(table).getByRole('row', { name: /Entry zone/ })
    expect(within(zone).getByText('INSIDE')).toBeInTheDocument()
    expect(within(zone).getByText(/138\.20/)).toBeInTheDocument()

    const slippage = within(table).getByRole('row', { name: /Slippage/ })
    expect(within(slippage).getByText('OK')).toBeInTheDocument()
    expect(within(slippage).getByText(/1\.4 bps/)).toBeInTheDocument()

    const firstTouch = within(table).getByRole('row', { name: /First touch/ })
    expect(within(firstTouch).getByText('PARTIAL')).toBeInTheDocument()
    expect(within(firstTouch).getAllByText(/AMBIGUOUS_BAR/).length).toBeGreaterThan(0)

    const decision = within(table).getByRole('row', { name: /Decision/ })
    expect(within(decision).getByText(/ENTER_LONG/)).toBeInTheDocument()
    expect(within(decision).getByText(/DEC-ENTRY-1/)).toBeInTheDocument()
    expect(within(decision).getByText(/matched/i)).toBeInTheDocument()
    expect(within(decision).getByText(/direction agrees/i)).toBeInTheDocument()
    expect(within(decision).getByText(/482\.16/)).toBeInTheDocument()
  })

  it('keeps the panel visible for a PARTIAL side and shows the overall data quality', () => {
    render(<SignalComparison compare={compareFixture()} />)

    expect(screen.getByTestId('signal-comparison')).toBeInTheDocument()
    expect(screen.getAllByText('PARTIAL').length).toBeGreaterThan(0)
    expect(screen.getByText(/原因：AMBIGUOUS_BAR/)).toBeInTheDocument()
  })

  it('renders an explicit N/A panel instead of hiding when compare is unavailable', () => {
    render(<SignalComparison compare={null} error="comparison down" />)

    expect(screen.getByRole('heading', { name: /Signal vs Outcome/ })).toBeInTheDocument()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(/N\/A/)
    expect(alert).toHaveTextContent(/comparison down/)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
