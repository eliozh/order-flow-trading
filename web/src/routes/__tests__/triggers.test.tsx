import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import type { OpportunityData, OpportunityEnvelope } from '../../api/opportunities'
import {
  formatAge,
  type DecisionCard,
  type DecisionCardEnvelope,
  type RiskVerdict,
  type TriggerAcceptEnvelope,
  type TriggerDetailEnvelope,
  type TriggerListEnvelope,
  type TriggerPayload,
  type TriggerRejectEnvelope,
  type TriggerSummary,
} from '../../api/triggers'
import { TriggersPage } from '../triggers'
import { TriggerDetailPage } from '../trigger-detail'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'MIXED',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

export function riskVerdict(overrides: Partial<RiskVerdict> = {}): RiskVerdict {
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
    computed_at: '2026-11-06T04:14:31.000Z',
    expires_at: '2026-11-06T04:15:31.000Z',
    stale_inputs: ['spread_bps', 'daily_loss_usd'],
    ...overrides,
  }
}

function summary(overrides: Partial<TriggerSummary> = {}): TriggerSummary {
  return {
    trigger_id: 'TRG-1042',
    opportunity_id: 'OPP-1',
    symbol: 'BTCUSDT',
    side: 'LONG',
    status: 'RECHECKED',
    triggered_at: '2026-11-06T04:14:12.000Z',
    trigger_source: 'CODE',
    idempotency_key: 'TRG-OPP-1-AAAA',
    created_order_id: null,
    ...overrides,
  }
}

function payload(overrides: Partial<TriggerPayload> = {}): TriggerPayload {
  const base = summary()
  return {
    ...base,
    schema_version: '1.0.0',
    environment: 'PAPER',
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
        note: 'price 96,842.10 >= 96,850.00',
      },
    ],
    snapshot: {
      market_snapshot_id: 'SNAP-1',
      snapshot_hash: 'a'.repeat(64),
      as_of: '2026-11-06T04:14:12.000Z',
      data_quality: 'COMPLETE',
    },
    entry_decision_id: 'DEC-1',
    risk_verdict: riskVerdict(),
    human_action: null,
    rejection_reason: null,
    ...overrides,
  }
}

function card(overrides: Partial<DecisionCard> = {}): DecisionCard {
  return {
    decision_id: 'DEC-1',
    trigger_id: 'TRG-1042',
    status: 'RECHECKED',
    rejection_reason: null,
    data_quality: 'PARTIAL',
    error_code: null,
    stage_order: ['TRIGGER', 'LLM', 'RISK', 'HUMAN'],
    trigger: {
      trigger_id: 'TRG-1042',
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
      condition_results: payload().condition_results,
      provenance: 'CALC',
    },
    llm: {
      decision_id: 'DEC-1',
      decision: 'ENTER_LONG',
      validation_status: 'VALID',
      confidence: 0.72,
      rationale: 'POC absorption held; CVD stayed positive.',
      reasons: [],
      provenance: 'LLM',
    },
    risk: riskVerdict(),
    human_action: null,
    ...overrides,
  }
}

function opportunity(overrides: Partial<OpportunityData> = {}): OpportunityData {
  return {
    schema_version: '1.0.0',
    opportunity_id: 'OPP-1',
    symbol: 'BTCUSDT',
    side: 'LONG',
    state: 'TRIGGERED',
    created_at: '2026-11-06T04:00:00.000Z',
    updated_at: '2026-11-06T04:14:12.000Z',
    zone: { low: '96000.00', high: '97000.00' },
    entry_preference: 'LIMIT',
    trigger_conditions: [],
    invalidation_conditions: [],
    expires_at: '2026-11-07T04:00:00.000Z',
    thesis: 'Thesis.',
    confidence: 0.72,
    risk_request: {
      entry_price: '96710.00',
      stop_price: '95500.00',
      quantity: null,
      risk_pct: 0.8,
      leverage: 3,
      notional_usd: '1760.99',
    },
    source: { scan_id: 'SCAN-1', report_id: 'RPT-1', decision_id: 'DEC-SCAN' },
    provenance: {
      llm: {
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        prompt_id: 'analysis',
        prompt_version: 1,
        cost: { amount_usd: '0.021', currency: 'USD', unpriced: false },
        usage: { total_tokens: 1200 },
      },
      calc_snapshot_id: 'SNAP-1',
      calc_snapshot_hash: 'b'.repeat(64),
      calc_metric_version: 'orderflow.v1',
    },
    data_quality: {
      status: 'COMPLETE',
      reasons: [],
      gaps: [],
      last_event_at: '2026-11-06T04:14:12.000Z',
      coverage_start: null,
    },
    state_history: [],
    ...overrides,
  }
}

function listEnvelope(items: TriggerSummary[]): TriggerListEnvelope {
  return { ok: true, data: { items, total: items.length }, error: null, meta }
}

function triggerEnvelope(item: TriggerPayload): TriggerDetailEnvelope {
  return { ok: true, data: item, error: null, meta }
}

function cardEnvelope(value: DecisionCard): DecisionCardEnvelope {
  return { ok: true, data: value, error: null, meta }
}

function opportunityEnvelope(item: OpportunityData): OpportunityEnvelope {
  return { ok: true, data: item, error: null, meta }
}

function detailFetcher(items: TriggerPayload[]) {
  return async (id: string): Promise<TriggerDetailEnvelope> => {
    const found = items.find((item) => item.trigger_id === id)
    if (!found) {
      throw new Error(`missing ${id}`)
    }
    return triggerEnvelope(found)
  }
}

function opportunityFetcher(item: OpportunityData = opportunity()) {
  return async (): Promise<OpportunityEnvelope> => opportunityEnvelope(item)
}

describe('formatAge', () => {
  it('renders minute, hour, and day ages in the design format', () => {
    const now = Date.parse('2026-11-06T05:00:00.000Z')

    expect(formatAge('2026-11-06T04:42:00.000Z', now)).toBe('18m')
    expect(formatAge('2026-11-06T03:52:00.000Z', now)).toBe('1h 08m')
    expect(formatAge('2026-11-05T03:58:00.000Z', now)).toBe('1d 01h')
  })

  it('never fabricates an age for a missing or invalid timestamp', () => {
    expect(formatAge('', Date.now())).toBe('—')
    expect(formatAge('not-a-date', Date.now())).toBe('—')
  })
})

describe('TriggersPage queue', () => {
  const pending = payload({
    trigger_id: 'TRG-1042',
    status: 'RECHECKED',
    triggered_at: '2026-11-06T04:42:00.000Z',
    risk_verdict: riskVerdict(),
  })
  const accepted = payload({
    trigger_id: 'TRG-1039',
    status: 'ACCEPTED',
    symbol: 'ETHUSDT',
    side: 'SHORT',
    triggered_at: '2026-11-06T02:49:00.000Z',
    risk_verdict: riskVerdict({ verdict: 'REDUCE' }),
    human_action: { action: 'ACCEPT', reason: null },
  })

  async function renderQueue(
    items: TriggerPayload[] = [accepted, pending],
    options: { nowMs?: number } = {},
  ) {
    render(
      <TriggersPage
        nowMs={options.nowMs ?? Date.parse('2026-11-06T05:00:00.000Z')}
        fetchTriggersFn={async () => listEnvelope(items)}
        fetchTriggerDetailFn={detailFetcher(items)}
        fetchOpportunityFn={opportunityFetcher()}
      />,
    )
    return screen.findByRole('table', { name: /入场复核队列/i })
  }

  it('shows a loading status before the queue arrives', () => {
    render(
      <TriggersPage fetchTriggersFn={() => new Promise<TriggerListEnvelope>(() => undefined)} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/加载入场复核队列/)
  })

  it('shows an alert and keeps the shell usable when the queue fails', async () => {
    render(
      <TriggersPage
        fetchTriggersFn={async () => {
          throw new Error('queue down')
        }}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/queue down/)
  })

  it('sorts pending first and renders symbol, side, age, confidence, risk, env, and state', async () => {
    const table = await renderQueue()

    expect(screen.getByText(/待处理 1/)).toBeInTheDocument()
    expect(screen.getByText(/已处理 1/)).toBeInTheDocument()

    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows[0]).toHaveTextContent('TRG-1042')
    expect(rows[0]).toHaveTextContent('BTCUSDT')
    expect(rows[0]).toHaveTextContent('▲ LONG')
    expect(rows[0]).toHaveTextContent('18m')
    expect(rows[0]).toHaveTextContent('0.72')
    expect(rows[0]).toHaveTextContent('high')
    expect(rows[0]).toHaveTextContent('PASS')
    expect(rows[0]).toHaveTextContent('PAPER')
    expect(rows[0]).toHaveTextContent('PENDING')

    expect(rows[1]).toHaveTextContent('TRG-1039')
    expect(rows[1]).toHaveTextContent('▼ SHORT')
    expect(rows[1]).toHaveTextContent('REDUCE')
    expect(rows[1]).toHaveTextContent('ACCEPTED')
  })

  it('distinguishes an empty queue from a failed one', async () => {
    render(
      <TriggersPage
        fetchTriggersFn={async () => listEnvelope([])}
        fetchTriggerDetailFn={detailFetcher([])}
        fetchOpportunityFn={opportunityFetcher()}
      />,
    )

    expect(await screen.findByText(/没有待处理的入场复核/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('is read-only at 375px and exposes no accept/reject action in the queue', async () => {
    render(
      <TriggersPage
        isCompact
        nowMs={Date.parse('2026-11-06T05:00:00.000Z')}
        fetchTriggersFn={async () => listEnvelope([pending])}
        fetchTriggerDetailFn={detailFetcher([pending])}
        fetchOpportunityFn={opportunityFetcher()}
      />,
    )
    await screen.findByRole('table', { name: /入场复核队列/i })

    expect(screen.getByText(/移动端只读/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /接受|拒绝/ })).not.toBeInTheDocument()
  })
})

describe('TriggerDetailPage decision card', () => {
  function renderDetail(
    options: {
      trigger?: TriggerPayload
      card?: DecisionCard
      recheck?: (id: string, key: string, signal?: AbortSignal) => Promise<DecisionCardEnvelope>
      accept?: (id: string, key: string) => Promise<TriggerAcceptEnvelope>
      reject?: (id: string, reason: string, key: string) => Promise<TriggerRejectEnvelope>
      compact?: boolean
    } = {},
  ) {
    const trigger = options.trigger ?? payload()
    const decision = options.card ?? card()
    const getTriggerFn = vi.fn(async () => triggerEnvelope(trigger))
    const recheckFn = vi.fn(options.recheck ?? (async () => cardEnvelope(decision)))
    const acceptFn = vi.fn(
      options.accept ??
        (async (): Promise<TriggerAcceptEnvelope> => ({
          ok: true,
          data: {
            status: 'ACCEPTED',
            decision_id: 'DEC-1',
            risk_verdict: riskVerdict(),
            order: { order_id: 'PO-1', status: 'FILLED', client_order_id: 'oft-paper-1' },
            fill: { fill_id: 'PF-1', price: '96714.20', quantity: '0.018', fee: '1.74' },
            position: { position_id: 'POS-1', state: 'OPEN', size: '0.018' },
            account: { balance: '10000.00', currency: 'USDT' },
          },
          error: null,
          meta,
        })),
    )
    const rejectFn = vi.fn(
      options.reject ??
        (async (): Promise<TriggerRejectEnvelope> => ({
          ok: true,
          data: {
            trigger_id: trigger.trigger_id,
            opportunity_id: trigger.opportunity_id,
            status: 'REJECTED',
            rejection_reason: 'structure broken',
            human_action: { action: 'REJECT', reason: 'structure broken' },
          },
          error: null,
          meta,
        })),
    )

    render(
      <TriggerDetailPage
        triggerId={trigger.trigger_id}
        isCompact={options.compact}
        getTriggerFn={getTriggerFn}
        recheckFn={recheckFn}
        acceptFn={acceptFn}
        rejectFn={rejectFn}
        fetchOpportunityFn={opportunityFetcher()}
      />,
    )
    return { getTriggerFn, recheckFn, acceptFn, rejectFn }
  }

  it('renders the mandatory Trigger -> LLM -> Risk -> Human order', async () => {
    renderDetail()

    const headings = await screen.findAllByRole('heading', { level: 2 })
    const titles = headings.map((heading) => heading.textContent ?? '')
    const triggerIndex = titles.findIndex((title) => /1 · Trigger/.test(title))
    const llmIndex = titles.findIndex((title) => /2 · LLM/.test(title))
    const riskIndex = titles.findIndex((title) => /3 · Risk gate/.test(title))
    const humanIndex = titles.findIndex((title) => /4 · Human action/.test(title))

    expect(triggerIndex).toBeGreaterThanOrEqual(0)
    expect(triggerIndex).toBeLessThan(llmIndex)
    expect(llmIndex).toBeLessThan(riskIndex)
    expect(riskIndex).toBeLessThan(humanIndex)
  })

  it('shows confidence with a band, requested vs allowed caps, and the binding cap', async () => {
    renderDetail()
    await screen.findByRole('heading', { name: /3 · Risk gate/ })

    expect(screen.getByText('0.72 · high')).toBeInTheDocument()
    expect(screen.getByText(/请求 0.80%/)).toBeInTheDocument()
    expect(screen.getByText(/allowed 0.018/)).toBeInTheDocument()
    expect(screen.getByTestId('binding-cap')).toHaveTextContent('PER_TRADE_RISK')
    expect(screen.getByText(/更严者/)).toBeInTheDocument()
  })

  it('shows model, prompt, and cost metadata without rendering the prompt body', async () => {
    renderDetail()
    await screen.findByRole('heading', { name: /2 · LLM/ })

    expect(screen.getByText('Provider')).toBeInTheDocument()
    expect(screen.getByText('anthropic')).toBeInTheDocument()
    expect(screen.getByText('claude-sonnet-4-5')).toBeInTheDocument()
    expect(screen.getByText(/analysis v1/)).toBeInTheDocument()
    expect(screen.getByText(/\$0.021/)).toBeInTheDocument()
  })

  it('renders narrative as text and never as HTML', async () => {
    renderDetail({
      card: card({
        llm: {
          ...card().llm,
          rationale: '<img src=x onerror="alert(1)">POC held',
        },
      }),
    })
    await screen.findByRole('heading', { name: /2 · LLM/ })

    expect(screen.getByText(/POC held/)).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
  })

  it('disables accept with the blocking reason when the risk gate blocks', async () => {
    renderDetail({
      card: card({
        llm: { ...card().llm, decision: 'NO_ENTRY' },
        risk: riskVerdict({
          verdict: 'BLOCK',
          binding_cap: 'STALE_MARK',
          reasons: ['mark is stale'],
          stale_inputs: ['mark'],
          allowed: { quantity: null, notional_usd: null, leverage: null, risk_amount_usd: null },
        }),
      }),
    })
    await screen.findByRole('heading', { name: /4 · Human action/ })

    const accept = screen.getByRole('button', { name: /接受（Paper）/ })
    expect(accept).toBeDisabled()
    expect(screen.getAllByText(/BLOCK/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/STALE_MARK/).length).toBeGreaterThan(0)
    expect(screen.getByText(/接受不可用/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /拒绝/ })).toBeEnabled()
  })

  it('disables accept when the final size is zero', async () => {
    renderDetail({
      card: card({
        risk: riskVerdict({
          verdict: 'BLOCK',
          binding_cap: 'MIN_NOTIONAL',
          allowed: { quantity: '0', notional_usd: '0', leverage: null, risk_amount_usd: '0' },
        }),
      }),
    })
    await screen.findByRole('heading', { name: /4 · Human action/ })

    expect(screen.getByRole('button', { name: /接受（Paper）/ })).toBeDisabled()
    expect(screen.getByText(/最终规模为 0/)).toBeInTheDocument()
  })

  it('opens a confirmation that shows the final size, caps, and that risk caps cannot be bypassed', async () => {
    const { acceptFn } = renderDetail()
    await screen.findByRole('heading', { name: /4 · Human action/ })

    fireEvent.click(screen.getByRole('button', { name: /接受（Paper）/ }))

    const dialog = screen.getByRole('dialog', { name: /接受（Paper）确认/ })
    expect(dialog).toHaveTextContent('0.018')
    expect(dialog).toHaveTextContent(/PER_TRADE_RISK/)
    expect(dialog).toHaveTextContent(/LLM 不能绕过风险上限/)
    expect(acceptFn).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: /确认接受（Paper）/ }))

    await waitFor(() => expect(acceptFn).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/PO-1/)).toBeInTheDocument()
    expect(acceptFn.mock.calls[0][0]).toBe('TRG-1042')
    expect(acceptFn.mock.calls[0][1]).toMatch(/^web-accept-TRG-1042/)
  })

  it('reuses one idempotency key across duplicate accepts', async () => {
    const acceptFn = vi
      .fn<(id: string, key: string) => Promise<TriggerAcceptEnvelope>>()
      .mockRejectedValueOnce(new Error('timeout'))
      .mockResolvedValueOnce({
        ok: true,
        data: {
          status: 'ACCEPTED',
          decision_id: 'DEC-1',
          risk_verdict: riskVerdict(),
          order: { order_id: 'PO-1', status: 'FILLED', client_order_id: 'oft-paper-1' },
          fill: { fill_id: 'PF-1' },
          position: { position_id: 'POS-1' },
          account: { balance: '10000.00', currency: 'USDT' },
        },
        error: null,
        meta,
      })
    renderDetail({ accept: acceptFn })
    await screen.findByRole('heading', { name: /4 · Human action/ })

    fireEvent.click(screen.getByRole('button', { name: /接受（Paper）/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认接受（Paper）/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/timeout/)

    fireEvent.click(screen.getByRole('button', { name: /确认接受（Paper）/ }))
    await waitFor(() => expect(acceptFn).toHaveBeenCalledTimes(2))

    expect(acceptFn.mock.calls[0][1]).toBe(acceptFn.mock.calls[1][1])
  })

  it('requires a reason before rejecting and keeps the decision visible after', async () => {
    const { rejectFn } = renderDetail()
    await screen.findByRole('heading', { name: /4 · Human action/ })

    fireEvent.click(screen.getByRole('button', { name: /^拒绝$/ }))
    const dialog = screen.getByRole('dialog', { name: /拒绝入场复核/ })
    fireEvent.click(within(dialog).getByRole('button', { name: /拒绝并标记 invalidated/ }))

    expect(rejectFn).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('alert')).toHaveTextContent(/请输入拒绝理由/)

    fireEvent.change(within(dialog).getByLabelText(/拒绝理由（必填）/), {
      target: { value: 'structure broken' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /拒绝并标记 invalidated/ }))

    await waitFor(() => expect(rejectFn).toHaveBeenCalledTimes(1))
    expect(rejectFn.mock.calls[0][1]).toBe('structure broken')
    expect(await screen.findByText(/structure broken/)).toBeInTheDocument()
    expect(screen.getAllByText(/INVALIDATED/).length).toBeGreaterThan(0)
    expect(screen.getByRole('heading', { name: /2 · LLM/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /接受（Paper）/ })).not.toBeInTheDocument()
  })

  it('is read-only at 375px except for the global kill switch', async () => {
    renderDetail({ compact: true })
    await screen.findByRole('heading', { name: /4 · Human action/ })

    expect(screen.getByText(/请在桌面端或 CLI 处理/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /接受（Paper）/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^拒绝$/ })).not.toBeInTheDocument()
  })

  it('does not run a recheck on an already resolved trigger and keeps its decision reference', async () => {
    const resolved = payload({
      status: 'REJECTED',
      rejection_reason: 'risk blocked',
      human_action: { action: 'REJECT', reason: 'risk blocked' },
    })
    const { recheckFn } = renderDetail({ trigger: resolved, card: card({ status: 'REJECTED' }) })
    await screen.findByRole('heading', { name: /4 · Human action/ })

    expect(recheckFn).not.toHaveBeenCalled()
    expect(screen.getByText(/DEC-1/)).toBeInTheDocument()
    expect(screen.getByText(/risk blocked/)).toBeInTheDocument()
    expect(screen.getByText(/REJECTED/)).toBeInTheDocument()
  })
})
