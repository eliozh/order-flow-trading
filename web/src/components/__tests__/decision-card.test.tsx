import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DecisionCard } from '../decision-card'
import type { DecisionCard as DecisionCardData } from '../../api/triggers'

export function card(overrides: Partial<DecisionCardData> = {}): DecisionCardData {
  return {
    decision_id: 'DEC-1',
    trigger_id: 'TRG-1',
    status: 'RECHECKED',
    rejection_reason: null,
    data_quality: 'PARTIAL',
    error_code: null,
    stage_order: ['TRIGGER', 'LLM', 'RISK', 'HUMAN'],
    trigger: {
      trigger_id: 'TRG-1',
      opportunity_id: 'OPP-1',
      symbol: 'BTCUSDT',
      side: 'LONG',
      status: 'RECHECKED',
      trigger_source: 'CODE',
      triggered_at: '2026-11-06T04:14:12.000Z',
      snapshot: {
        market_snapshot_id: 'SNAP-1',
        snapshot_hash: 'a'.repeat(64),
        as_of: '2026-11-06T04:14:12.000Z',
        data_quality: 'COMPLETE',
      },
      condition_results: [
        {
          condition_id: 'C-1',
          type: 'PRICE_TOUCH_ZONE',
          met: true,
          observed_value: '96842.10',
          threshold: '96850.00',
          evaluated_at: '2026-11-06T04:14:12.000Z',
          data_quality: 'COMPLETE',
          evaluable: true,
          note: '',
        },
      ],
      provenance: 'CALC',
    },
    llm: {
      decision_id: 'DEC-1',
      decision: 'ENTER_LONG',
      validation_status: 'VALID',
      confidence: 0.72,
      rationale: 'POC absorption held.',
      reasons: [],
      provenance: 'LLM',
    },
    risk: {
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
    },
    human_action: null,
    ...overrides,
  }
}

describe('DecisionCard', () => {
  it('renders the four stages in the mandatory order', () => {
    render(<DecisionCard card={card()} />)

    const headings = screen.getAllByRole('heading', { level: 2 }).map((item) => item.textContent)
    expect(headings[0]).toMatch(/1 · Trigger/)
    expect(headings[1]).toMatch(/2 · LLM/)
    expect(headings[2]).toMatch(/3 · Risk gate/)
    expect(headings[3]).toMatch(/4 · Human action/)
  })

  it('labels CALC and LLM provenance and treats the rationale as untrusted text', () => {
    render(<DecisionCard card={card()} />)

    expect(screen.getAllByText('CALC').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('LLM')).toBeInTheDocument()
    expect(screen.getByText(/Untrusted model text/)).toBeInTheDocument()
    expect(screen.getByText('POC absorption held.')).toBeInTheDocument()
  })

  it('shows accept and reject callbacks only when actionable', () => {
    const onAccept = vi.fn()
    const onReject = vi.fn()
    render(<DecisionCard card={card()} onAccept={onAccept} onReject={onReject} />)

    fireEvent.click(screen.getByRole('button', { name: /接受（Paper）/ }))
    fireEvent.click(screen.getByRole('button', { name: /^拒绝$/ }))

    expect(onAccept).toHaveBeenCalledTimes(1)
    expect(onReject).toHaveBeenCalledTimes(1)
  })

  it('shows the recorded human action instead of action buttons', () => {
    render(
      <DecisionCard
        card={card({
          status: 'REJECTED',
          human_action: {
            action: 'REJECT',
            reason: 'structure broken',
            at: '2026-11-06T04:20:00Z',
          },
        })}
      />,
    )

    expect(screen.getByText('REJECT')).toBeInTheDocument()
    expect(screen.getByText('structure broken')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /接受（Paper）/ })).not.toBeInTheDocument()
  })

  it('is read-only at 375px and explains where to act', () => {
    render(<DecisionCard card={card()} compact />)

    expect(screen.getByText(/请在桌面端或 CLI 处理/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /接受（Paper）/ })).not.toBeInTheDocument()
  })
})
