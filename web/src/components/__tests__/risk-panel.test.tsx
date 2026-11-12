import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { RiskPanel } from '../risk-panel'
import { acceptBlockedReason, type RiskVerdict } from '../../api/triggers'

export function verdict(overrides: Partial<RiskVerdict> = {}): RiskVerdict {
  return {
    verdict: 'ALLOW',
    requested: {
      entry_price: '96710.00',
      stop_price: '95500.00',
      quantity: null,
      risk_pct: 0.8,
      leverage: 3,
      notional_usd: '1760.99',
    },
    allowed: {
      quantity: '0.018',
      notional_usd: '1740.78',
      leverage: 3,
      risk_amount_usd: '78.40',
    },
    binding_cap: 'PER_TRADE_RISK',
    reasons: [],
    stale_inputs: [],
    ...overrides,
  }
}

describe('RiskPanel', () => {
  it('shows requested vs allowed caps, the final size, and the binding cap', () => {
    render(<RiskPanel verdict={verdict()} symbol="BTCUSDT" />)

    expect(screen.getByText(/请求 0.80%/)).toBeInTheDocument()
    expect(screen.getByText(/78.40 USDT/)).toBeInTheDocument()
    expect(screen.getByText(/0.018 BTCUSDT/)).toBeInTheDocument()
    expect(screen.getByTestId('binding-cap')).toHaveTextContent('PER_TRADE_RISK')
    expect(screen.getByText('PASS')).toBeInTheDocument()
    expect(screen.getByText(/更严格的一方决定/)).toBeInTheDocument()
  })

  it('labels a partial gate and names the stale inputs', () => {
    render(
      <RiskPanel
        verdict={verdict({
          stale_inputs: ['spread_bps', 'daily_loss_usd'],
          reasons: ['spread not measured'],
        })}
      />,
    )

    expect(screen.getByText('PARTIAL')).toBeInTheDocument()
    expect(screen.getByText(/spread_bps/)).toBeInTheDocument()
    expect(screen.getByText(/daily_loss_usd/)).toBeInTheDocument()
  })

  it('names the binding cap and disables acceptance for a block', () => {
    const blocked = verdict({
      verdict: 'BLOCK',
      binding_cap: 'STALE_MARK',
      allowed: { quantity: '0', notional_usd: '0', leverage: null, risk_amount_usd: '0' },
      stale_inputs: ['mark'],
    })
    render(<RiskPanel verdict={blocked} />)

    expect(screen.getByText('BLOCKED')).toBeInTheDocument()
    expect(acceptBlockedReason(blocked)).toMatch(/STALE_MARK/)
    expect(acceptBlockedReason(blocked)).toMatch(/mark/)
  })

  it('allows an unmeasured non-mark input but blocks a stale mark', () => {
    expect(acceptBlockedReason(verdict({ stale_inputs: ['spread_bps'] }))).toBeNull()
    expect(acceptBlockedReason(verdict({ stale_inputs: ['mark'] }))).not.toBeNull()
    expect(acceptBlockedReason(null)).toMatch(/尚未评估/)
  })

  it('shows requested and allowed values in their own columns', () => {
    render(<RiskPanel verdict={verdict()} symbol="BTCUSDT" />)

    const finalRow = screen.getByTestId('cap-final-size')
    const cells = within(finalRow).getAllByRole('cell')
    expect(cells[0]).toHaveTextContent('请求 —')
    expect(cells[0]).not.toHaveTextContent(/allowed/i)
    expect(cells[1]).toHaveTextContent('0.018 BTCUSDT')

    const entryRow = within(screen.getByTestId('cap-entry')).getAllByRole('cell')
    expect(entryRow[0]).toHaveTextContent('请求 96710.00')
    expect(entryRow[1]).toHaveTextContent('—')

    const stopRow = within(screen.getByTestId('cap-stop')).getAllByRole('cell')
    expect(stopRow[0]).toHaveTextContent('请求 95500.00')
    expect(stopRow[1]).toHaveTextContent('—')
  })

  it('shows a requested quantity when the LLM proposed one', () => {
    render(
      <RiskPanel
        verdict={verdict({ requested: { ...verdict().requested, quantity: '0.050' } })}
        symbol="BTCUSDT"
      />,
    )

    const cells = within(screen.getByTestId('cap-final-size')).getAllByRole('cell')
    expect(cells[0]).toHaveTextContent('请求 0.050 BTCUSDT')
    expect(cells[1]).toHaveTextContent('0.018 BTCUSDT')
  })

  it('exposes the caps table as a keyboard-scrollable region', () => {
    render(<RiskPanel verdict={verdict()} />)

    const region = screen.getByRole('region', { name: /Risk gate caps/i })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(region.querySelector('table')).not.toBeNull()
  })

  it('shows no cap values when the gate has not produced a verdict', () => {
    render(<RiskPanel verdict={null} />)

    expect(screen.getByText(/风险门尚未评估/)).toBeInTheDocument()
    expect(screen.queryByTestId('binding-cap')).not.toBeInTheDocument()
  })
})
