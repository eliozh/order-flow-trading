import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import type {
  OpportunityCondition,
  OpportunityData,
  OpportunityEnvelope,
  OpportunityListEnvelope,
  OpportunitySummary,
} from '../../api/opportunities'
import { OpportunityDetailPage } from '../opportunity-detail'
import { OpportunitiesPage } from '../opportunities'

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

function condition(overrides: Partial<OpportunityCondition> = {}): OpportunityCondition {
  return {
    condition_id: 'C-1',
    type: 'PRICE_TOUCH_ZONE',
    params: { price_low: '69900.00', price_high: '70100.00' },
    window: { start: '2026-11-01T00:00:00.000Z', end: '2026-12-01T00:00:00.000Z' },
    required_data: ['MARK_PRICE'],
    evaluable: true,
    unavailable_reason: null,
    last_value: null,
    last_evaluated_at: null,
    last_result: null,
    ...overrides,
  }
}

function opportunity(overrides: Partial<OpportunityData> = {}): OpportunityData {
  return {
    schema_version: '1.0.0',
    opportunity_id: 'OPP-1',
    symbol: 'BTCUSDT',
    side: 'LONG',
    state: 'OPEN',
    created_at: '2026-11-06T12:00:00.000Z',
    updated_at: '2026-11-06T12:00:00.000Z',
    zone: { low: '69000.00', high: '71000.00' },
    entry_preference: 'MARKET',
    trigger_conditions: [condition()],
    invalidation_conditions: [
      condition({
        condition_id: 'I-1',
        type: 'PRICE_CROSS_BELOW',
        params: { price: '69000.00' },
      }),
    ],
    expires_at: '2027-01-01T00:00:00.000Z',
    thesis: 'Range high retest with positive delta and a protected stop.',
    confidence: 0.72,
    risk_request: {
      entry_price: '70000.00',
      stop_price: '69000.00',
      quantity: null,
      risk_pct: 0.8,
      leverage: 2,
      notional_usd: '1000.00',
    },
    source: { scan_id: 'SCAN-1', report_id: 'RPT-1', decision_id: 'DEC-1' },
    provenance: {
      llm: {
        provider: 'stub',
        model: 'fixture',
        prompt_id: 'analysis',
        prompt_version: 1,
        knowledge_artifact_hash: 'KNW-HASH-1',
      },
      calc_snapshot_id: 'SNAP-1',
      calc_snapshot_hash: 'd'.repeat(64),
      calc_metric_version: 'orderflow.v1',
    },
    data_quality: {
      status: 'PARTIAL',
      reasons: ['open_interest not measured in this slice'],
      gaps: [],
      last_event_at: '2026-11-06T12:00:00.000Z',
      coverage_start: null,
    },
    state_history: [
      {
        from_state: null,
        to_state: 'OPEN',
        at: '2026-11-06T12:00:00.000Z',
        reason_code: 'CREATED',
        note: 'created from a validated LLM draft',
      },
    ],
    ...overrides,
  }
}

function summary(item: OpportunityData): OpportunitySummary {
  return {
    opportunity_id: item.opportunity_id,
    symbol: item.symbol,
    side: item.side,
    state: item.state,
    confidence: item.confidence,
    expires_at: item.expires_at,
    created_at: item.created_at,
    updated_at: item.updated_at,
    source: item.source,
  }
}

function listEnvelope(items: OpportunityData[]): OpportunityListEnvelope {
  return { ok: true, data: { items: items.map(summary), total: items.length }, error: null, meta }
}

function detailEnvelope(item: OpportunityData): OpportunityEnvelope {
  return { ok: true, data: item, error: null, meta }
}

const STATES = ['OPEN', 'TRIGGERED', 'EXPIRED', 'INVALIDATED'] as const
const LEDGER: OpportunityData[] = [
  opportunity({ opportunity_id: 'OPP-OPEN', state: 'OPEN' }),
  opportunity({ opportunity_id: 'OPP-TRIGGERED', state: 'TRIGGERED', symbol: 'ETHUSDT' }),
  opportunity({ opportunity_id: 'OPP-EXPIRED', state: 'EXPIRED', symbol: 'SOLUSDT' }),
  opportunity({ opportunity_id: 'OPP-INVALIDATED', state: 'INVALIDATED', symbol: 'XAUUSDT' }),
]

function detailById(items: OpportunityData[]) {
  return async (id: string): Promise<OpportunityEnvelope> => {
    const found = items.find((item) => item.opportunity_id === id)
    if (!found) {
      throw new Error(`missing ${id}`)
    }
    return detailEnvelope(found)
  }
}

async function renderLedger(items: OpportunityData[] = LEDGER) {
  render(
    <OpportunitiesPage
      fetchOpportunitiesFn={async () => listEnvelope(items)}
      fetchOpportunityFn={detailById(items)}
    />,
  )
  await screen.findByRole('tab', { name: /Open/ })
}

describe('OpportunitiesPage filters and counts', () => {
  it('shows loading while the ledger request is pending', () => {
    render(
      <OpportunitiesPage
        fetchOpportunitiesFn={() => new Promise<OpportunityListEnvelope>(() => undefined)}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/Loading opportunities/)
  })

  it('shows an alert when the ledger request fails', async () => {
    render(
      <OpportunitiesPage
        fetchOpportunitiesFn={async () => {
          throw new Error('ledger down')
        }}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/ledger down/)
  })

  it('renders state counts with neutral, accent, muted, and danger classes', async () => {
    await renderLedger()

    const expected: Record<(typeof STATES)[number], string> = {
      OPEN: 'count count--neutral',
      TRIGGERED: 'count count--accent',
      EXPIRED: 'count count--muted',
      INVALIDATED: 'count count--danger',
    }
    for (const state of STATES) {
      const tab = screen.getByRole('tab', { name: new RegExp(state, 'i') })
      const badge = within(tab).getByText('1')
      expect(badge).toHaveClass(...expected[state].split(' '))
    }
  })

  it('filters rows by state and keeps other states out of the table', async () => {
    await renderLedger()

    expect(screen.getByRole('link', { name: 'OPP-OPEN' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'OPP-TRIGGERED' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Triggered/i }))

    expect(await screen.findByRole('link', { name: 'OPP-TRIGGERED' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'OPP-OPEN' })).not.toBeInTheDocument()
  })

  it('shows enriched zone and trigger detail for each row', async () => {
    await renderLedger([LEDGER[0]])

    expect(await screen.findByText(/69000\.00 – 71000\.00/)).toBeInTheDocument()
    expect(screen.getByText(/price touch zone/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'OPP-OPEN' })).toHaveAttribute(
      'href',
      '/opportunities/OPP-OPEN',
    )
  })

  it('distinguishes no-scan from an empty filter', async () => {
    const { unmount } = render(
      <OpportunitiesPage
        fetchOpportunitiesFn={async () => listEnvelope([])}
        fetchOpportunityFn={detailById([])}
      />,
    )
    expect(
      await screen.findByRole('heading', { name: /No opportunities yet/i }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Run a scan/i })).toHaveAttribute('href', '/scans')
    expect(screen.queryByText(/in this filter/i)).not.toBeInTheDocument()
    unmount()

    await renderLedger([opportunity({ opportunity_id: 'OPP-T', state: 'TRIGGERED' })])
    expect(
      await screen.findByRole('heading', { name: /No open opportunities/i }),
    ).toBeInTheDocument()
    expect(screen.getByText(/in this filter/i)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /No opportunities yet/i })).not.toBeInTheDocument()
  })
})

describe('OpportunityDetailPage', () => {
  it('shows direction, zone, confidence, expiry, source, model, prompt, thesis, and timeline', async () => {
    const item = opportunity()
    render(<OpportunityDetailPage opportunityId="OPP-1" fetchOpportunityFn={detailById([item])} />)
    expect(await screen.findByRole('heading', { name: 'OPP-1' })).toBeInTheDocument()

    expect(screen.getByText(/▲ LONG/)).toBeInTheDocument()
    expect(screen.getByText(/69000\.00 – 71000\.00/)).toBeInTheDocument()
    expect(screen.getAllByText('0.72 · high').length).toBeGreaterThan(0)
    expect(screen.getByText(/2027-01-01 00:00 UTC/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'RPT-1' })).toHaveAttribute('href', '/reports/RPT-1')
    expect(screen.getByText(/SCAN-1/)).toBeInTheDocument()
    expect(screen.getByText('stub')).toBeInTheDocument()
    expect(screen.getByText('fixture')).toBeInTheDocument()
    expect(screen.getByText(/analysis v1/)).toBeInTheDocument()
    expect(screen.getByText(item.thesis)).toBeInTheDocument()

    const timeline = screen.getByRole('list', { name: /state timeline/i })
    expect(within(timeline).getByText(/CREATED/)).toBeInTheDocument()
    expect(within(timeline).getByText(/created from a validated LLM draft/)).toBeInTheDocument()
  })

  it('labels an unevaluable condition UNAVAILABLE and never renders it as met', async () => {
    const item = opportunity({
      trigger_conditions: [
        condition({
          condition_id: 'C-2',
          type: 'LIQUIDATION_NOTIONAL_ABOVE',
          params: { threshold: '2' },
          required_data: ['LIQUIDATIONS'],
          evaluable: false,
          unavailable_reason: 'missing required data: LIQUIDATIONS',
          last_result: true,
        }),
      ],
    })
    render(<OpportunityDetailPage opportunityId="OPP-1" fetchOpportunityFn={detailById([item])} />)
    await screen.findByRole('heading', { name: 'OPP-1' })

    const table = screen.getByRole('table', { name: /Trigger conditions/i })
    const row = within(table)
      .getByText(/liquidation notional above/i)
      .closest('tr')
    if (!row) {
      throw new Error('missing condition row')
    }
    expect(within(row).getByText('UNAVAILABLE')).toBeInTheDocument()
    expect(within(row).queryByText('MET')).not.toBeInTheDocument()
    expect(within(row).getByText(/missing required data: LIQUIDATIONS/)).toBeInTheDocument()
    expect(within(row).getByText(/LIQUIDATIONS/)).toBeInTheDocument()
  })

  it('renders MET and NOT MET from code results for evaluable conditions', async () => {
    const item = opportunity({
      trigger_conditions: [
        condition({ condition_id: 'C-1', last_result: true, last_value: '70000.00' }),
        condition({
          condition_id: 'C-2',
          type: 'DELTA_ABOVE',
          params: { threshold: '12.0' },
          last_result: false,
        }),
      ],
    })
    render(<OpportunityDetailPage opportunityId="OPP-1" fetchOpportunityFn={detailById([item])} />)
    await screen.findByRole('heading', { name: 'OPP-1' })

    const table = screen.getByRole('table', { name: /Trigger conditions/i })
    expect(within(table).getByText('MET')).toBeInTheDocument()
    expect(within(table).getByText('NOT MET')).toBeInTheDocument()
  })

  it('shows N/A with a reports link when the source report is missing', async () => {
    const item = opportunity({ source: { scan_id: 'SCAN-1', report_id: '', decision_id: 'DEC-1' } })
    render(<OpportunityDetailPage opportunityId="OPP-1" fetchOpportunityFn={detailById([item])} />)
    await screen.findByRole('heading', { name: 'OPP-1' })

    const source = screen.getByRole('region', { name: /Source/i })
    expect(within(source).getByText('N/A')).toBeInTheDocument()
    expect(within(source).getByRole('link', { name: /reports/i })).toHaveAttribute(
      'href',
      '/reports',
    )
  })

  it('renders a 404-style panel for an unknown id', async () => {
    render(
      <OpportunityDetailPage
        opportunityId="OPP-MISSING"
        fetchOpportunityFn={async () => {
          throw new Error('opportunity not found')
        }}
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(/Opportunity not found/)
    expect(screen.getByRole('link', { name: /Back to opportunities/i })).toHaveAttribute(
      'href',
      '/opportunities',
    )
  })

  it('renders only links and read-only text, with no accept or reject action', async () => {
    render(
      <OpportunityDetailPage
        opportunityId="OPP-1"
        fetchOpportunityFn={detailById([opportunity()])}
      />,
    )
    await screen.findByRole('heading', { name: 'OPP-1' })

    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: /accept|reject|invalidate/i }),
    ).not.toBeInTheDocument()
  })

  it('renders thesis markup as inert text', async () => {
    const hostile = '<img src=x onerror="alert(1)">'
    const item = opportunity({ thesis: hostile })
    const { container } = render(
      <OpportunityDetailPage opportunityId="OPP-1" fetchOpportunityFn={detailById([item])} />,
    )
    await screen.findByRole('heading', { name: 'OPP-1' })

    expect(screen.getByText(hostile)).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
  })

  it('does not fetch when unmounted before the request resolves', async () => {
    const never = vi.fn(() => new Promise<OpportunityEnvelope>(() => undefined))
    const { unmount } = render(
      <OpportunityDetailPage opportunityId="OPP-1" fetchOpportunityFn={never} />,
    )
    unmount()
    expect(never).toHaveBeenCalledWith('OPP-1')
  })
})
