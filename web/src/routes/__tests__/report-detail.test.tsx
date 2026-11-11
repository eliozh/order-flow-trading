import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type {
  ApiMeta,
  MetricsDocument,
  ReportData,
  ReportEnvelope,
  LlmState,
} from '../../api/reports'
import type { OpportunityData, OpportunityEnvelope } from '../../api/opportunities'
import { ReportDetailPage } from '../report-detail'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'MISSING',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function metrics(measured: boolean): MetricsDocument {
  return {
    schema_version: '1.0.0',
    symbol: 'BTCUSDT',
    period: '4h',
    window: { start: '2026-10-01T00:00:00.000Z', end: '2026-10-02T00:00:00.000Z' },
    computed_at: '2026-11-06T12:00:00.000Z',
    metric_version: 'orderflow.v1.0123456789abcdef',
    input_manifest_hash: 'a'.repeat(64),
    data_quality: 'MISSING',
    quality_reasons: measured ? [] : ['open_interest not measured in this slice'],
    cvd: {
      unit: 'BASE',
      points: [
        {
          bar_start: '2026-10-01T00:00:00.000Z',
          bar_end: '2026-10-01T04:00:00.000Z',
          buy_volume: '0.40',
          sell_volume: '0.20',
          delta: '0.20',
          cvd: '0.20',
          taker_buy_ratio: 0.666667,
        },
      ],
      summary: {
        delta: '0.20',
        cvd_change: '0.20',
        buy_volume: '0.40',
        sell_volume: '0.20',
      },
      divergence_flags: [],
    },
    footprint: {
      price_bucket: '0.10',
      imbalance_threshold: 3,
      bars: [
        {
          bar_start: '2026-10-01T00:00:00.000Z',
          bar_end: '2026-10-01T04:00:00.000Z',
          cells: [
            {
              price: '70000.00',
              bid_volume: '0',
              ask_volume: '0.10',
              delta: '0.10',
              total_volume: '0.10',
              imbalance_ratio: null,
              is_stacked_imbalance: false,
            },
            {
              price: '70001.00',
              bid_volume: '0.20',
              ask_volume: '0',
              delta: '-0.20',
              total_volume: '0.20',
              imbalance_ratio: null,
              is_stacked_imbalance: false,
            },
          ],
        },
      ],
      top_imbalances: [],
    },
    volume_profile: {
      window: { start: '2026-10-01T00:00:00.000Z', end: '2026-10-02T00:00:00.000Z' },
      bin_size: '0.10',
      value_area_pct: 70,
      poc: '70000.00',
      vah: '70001.00',
      val: '69999.00',
      bins: [
        {
          price_low: '70000.00',
          price_high: '70000.10',
          volume: '0.10',
          buy_volume: '0.10',
          sell_volume: '0',
          is_poc: true,
          in_value_area: true,
        },
      ],
      notes: [],
    },
    open_interest: {
      unit: 'BASE',
      sampling: 'REGULAR',
      points: measured
        ? [
            {
              ts: '2026-10-01T00:00:00.000Z',
              open_interest: '100',
              open_interest_value: '7000000',
              long_short_ratio: null,
              taker_ratio: null,
            },
          ]
        : [],
      delta_over_window: measured ? '2' : '0',
      last: {
        ts: '2026-10-01T00:00:00.000Z',
        open_interest: '100',
        open_interest_value: '7000000',
      },
    },
    funding: {
      current_rate: measured ? '0.0001' : '0',
      next_funding_time: '2026-10-01T08:00:00.000Z',
      interval_hours: 8,
      mark_price: '70000.00',
      index_price: '70000.00',
      history: measured
        ? [{ ts: '2026-10-01T00:00:00.000Z', rate: '0.0001', interval_hours: 8 }]
        : [],
    },
    liquidations: {
      coverage: 'NONE',
      coverage_start: null,
      coverage_end: null,
      total_long_notional: '0',
      total_short_notional: '0',
      count: 0,
      buckets: [],
      limitations: [],
    },
    sources: [],
    gaps: [],
  }
}

const VALID_LLM: LlmState = {
  configured: true,
  reason: null,
  provider: 'stub',
  model: 'fixture',
  prompt_id: 'analysis',
  prompt_version: 1,
  prompt_hash: 'c'.repeat(64),
  knowledge: {
    status: 'AVAILABLE',
    mode: 'FULL',
    artifact_id: 'KNW-ARTIFACT-1234',
    selected_section_ids: ['S-1'],
    selected_topics: ['orderflow'],
    token_count: 120,
    budget_tokens: 10000,
    dropped_section_ids: [],
    excluded_sections: [],
    warning: null,
    reason: null,
    exact_replay: true,
  },
  validation_status: 'VALID',
  decision_type: 'OPPORTUNITY_PROPOSAL',
  confidence: 0.72,
  narrative:
    'Price is rotating inside the range with a positive cumulative delta. This is advisory prose, not a measurement.',
  rationale: 'Range high retest.',
  opportunity_ids: ['OPP-1'],
  cost: { amount_usd: '0.012', currency: 'USD', price_version: 'stub/v1', unpriced: false },
  usage: { total_tokens: 1200 },
  reference_price: '70002.00',
}

function report(llm: LlmState = { configured: false, reason: 'LLM not configured.' }): ReportData {
  const document = metrics(false)
  return {
    report_id: 'RPT-BTCUSDT-20261106-120000-ABCD1234',
    scan_id: 'SCAN-20261106-ABCD1234',
    symbol: 'BTCUSDT',
    timeframe: '4h',
    generated_at: '2026-11-06T12:00:00.000Z',
    data_quality: 'MISSING',
    llm_decision_id: llm.configured ? 'DEC-1' : null,
    narrative_ref: llm.configured ? 'NAR-1' : null,
    llm,
    llm_stage: llm.configured ? 'DONE' : 'SKIPPED',
    llm_tokens: llm.usage?.total_tokens ?? 0,
    cost_usd: llm.cost?.amount_usd ?? '0',
    cost_unpriced: llm.cost?.unpriced ?? false,
    metrics_manifest: [],
    snapshot: {
      schema_version: '1.0.0',
      snapshot_id: 'SNAP-BTCUSDT',
      symbol: 'BTCUSDT',
      timeframe: '4h',
      as_of: '2026-11-06T12:00:00.000Z',
      window: document.window,
      source_mode: 'BACKFILL',
      data_quality: 'MISSING',
      quality_reasons: document.quality_reasons,
      last_price: '70000.00',
      mark_price: '70000.00',
      bars: [
        {
          bar_start: '2026-10-01T00:00:00.000Z',
          bar_end: '2026-10-01T04:00:00.000Z',
          open: '70000.00',
          high: '70002.00',
          low: '69999.00',
          close: '70002.00',
          volume: '0.60',
          quote_volume: '42000.00',
          trade_count: 3,
          taker_buy_volume: '0.40',
          delta: '0.20',
          cvd: '0.20',
          open_interest: null,
          funding_rate: null,
        },
      ],
      orderflow: document,
      session: { underlying_session: 'CRYPTO', is_no_trading: false, note: '' },
      contract_specs: {},
      sources: [],
      gaps: [],
      snapshot_hash: 'b'.repeat(64),
    },
    metrics: document,
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
    trigger_conditions: [],
    invalidation_conditions: [],
    expires_at: '2027-01-01T00:00:00.000Z',
    thesis: 'Range high retest with positive delta.',
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
      llm: { provider: 'stub', model: 'fixture', prompt_id: 'analysis', prompt_version: 1 },
      calc_snapshot_id: 'SNAP-1',
      calc_snapshot_hash: 'd'.repeat(64),
      calc_metric_version: 'orderflow.v1',
    },
    data_quality: {
      status: 'PARTIAL',
      reasons: [],
      gaps: [],
      last_event_at: null,
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

function envelope(data: ReportData = report()): ReportEnvelope {
  return { ok: true, data, error: null, meta }
}

function opportunityEnvelope(item: OpportunityData): OpportunityEnvelope {
  return { ok: true, data: item, error: null, meta }
}

async function renderReport(
  data: ReportData = report(),
  fetchOpportunityFn = async (id: string) =>
    opportunityEnvelope(opportunity({ opportunity_id: id })),
) {
  render(
    <ReportDetailPage
      reportId="RPT-BTCUSDT-20261106-120000-ABCD1234"
      fetchReportFn={async () => envelope(data)}
      fetchOpportunityFn={fetchOpportunityFn}
    />,
  )
  await screen.findByRole('heading', { name: /CALC/ })
}

function row(label: string): HTMLElement {
  const rowElement = screen.getByText(label).closest('tr')
  if (!rowElement) {
    throw new Error(`missing row ${label}`)
  }
  return rowElement
}

describe('ReportDetail unmeasured sections', () => {
  it('shows not measured instead of zero for absent OI and funding', async () => {
    await renderReport(report())

    expect(within(row('OI Δ')).getByText('not measured')).toBeInTheDocument()
    expect(within(row('OI Δ')).getByText(/MISSING · no measured open-interest/)).toHaveClass(
      'calc-note',
    )
    const funding = within(row('Funding'))
    expect(funding.getByText('not measured')).toBeInTheDocument()
    expect(funding.queryByText(/next .* UTC/)).not.toBeInTheDocument()
    expect(within(row('OI Δ')).queryByText('0')).not.toBeInTheDocument()
  })

  it('shows measured OI, funding rate, and next funding time when present', async () => {
    const measured = report()
    measured.metrics = metrics(true)
    measured.snapshot.orderflow = measured.metrics
    await renderReport(measured)

    expect(within(row('OI Δ')).getByText('2')).toBeInTheDocument()
    expect(screen.getByRole('table', { name: /deterministic metric summary/i })).toHaveClass(
      'calc-table',
    )
    expect(within(row('Footprint (selected bar)')).getByText(/2 cells · 0.30/)).toBeInTheDocument()
    const funding = within(row('Funding'))
    expect(funding.getByText('0.0001')).toBeInTheDocument()
    expect(funding.getByText(/next 2026-10-01 08:00 UTC/)).toBeInTheDocument()
  })
})

describe('ReportDetail CALC and LLM band order', () => {
  it('renders the CALC band before the LLM band', async () => {
    await renderReport(report(VALID_LLM))

    const calc = screen.getByRole('heading', { name: /CALC/ })
    const llm = screen.getByRole('heading', { name: /LLM/ })
    expect(calc.compareDocumentPosition(llm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('renders the LLM narrative as text with provider, model, prompt, confidence, cost, and knowledge hash', async () => {
    await renderReport(report(VALID_LLM))

    expect(screen.getByText(VALID_LLM.narrative ?? '')).toBeInTheDocument()
    expect(screen.getByText(/untrusted model text, not a measurement/i)).toBeInTheDocument()
    expect(screen.getByText('stub')).toBeInTheDocument()
    expect(screen.getByText('fixture')).toBeInTheDocument()
    expect(screen.getByText(/analysis v1/)).toBeInTheDocument()
    expect(screen.getByText('0.72 · high')).toBeInTheDocument()
    expect(screen.getByText('$0.012')).toBeInTheDocument()
    expect(screen.getByText(/KNW-ARTIFACT-1234/)).toBeInTheDocument()
    expect(screen.getByText(/1,?200 tokens/)).toBeInTheDocument()
    expect(screen.getByRole('note', { name: /Provenance LLM/i })).toBeInTheDocument()
  })

  it('never claims narrative is a measurement', async () => {
    await renderReport(report(VALID_LLM))

    expect(screen.queryByText(/narrative measurement/i)).not.toBeInTheDocument()
    expect(screen.getAllByText(/not a measurement/i).length).toBeGreaterThan(0)
  })

  it('keeps the unpriced marker when the cost amount is absent', async () => {
    const unpriced: LlmState = {
      ...VALID_LLM,
      cost: { amount_usd: null, unpriced: true },
    }
    await renderReport(report(unpriced))

    expect(screen.getByText('unpriced · N/A')).toBeInTheDocument()
  })
})

describe('ReportDetail narrative failure states', () => {
  it('shows the failing schema field and no narrative without blanking the CALC report', async () => {
    const rejected: LlmState = {
      ...VALID_LLM,
      validation_status: 'REJECTED',
      reason: 'SCHEMA_INVALID: opportunity.risk_request.stop_price required',
      narrative: 'This rejected narrative must not be displayed.',
      opportunity_ids: [],
    }
    const data = report(rejected)
    data.llm_stage = 'FAILED'
    await renderReport(data)

    const band = screen.getByRole('region', { name: /LLM/i })
    expect(within(band).getByText('REJECTED')).toHaveClass('chip', 'chip--danger')
    expect(within(band).getByText(/SCHEMA_INVALID/)).toBeInTheDocument()
    expect(within(band).getByText(/No narrative was produced/i)).toBeInTheDocument()
    expect(
      within(band).queryByText('This rejected narrative must not be displayed.'),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /CALC/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /CALC/ })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: /deterministic metric summary/i })).toBeInTheDocument()
  })

  it('shows the disabled reason and keeps the report usable when LLM is off', async () => {
    await renderReport(report())

    const band = screen.getByRole('region', { name: /LLM/i })
    expect(within(band).getByText('LLM not configured.')).toBeInTheDocument()
    expect(within(band).queryByText(/not a measurement/i)).not.toBeInTheDocument()
    expect(screen.getByRole('table', { name: /deterministic metric summary/i })).toBeInTheDocument()
  })
})

describe('ReportDetail opportunity cards', () => {
  it('links each proposed opportunity to its ledger detail and exposes no accept action', async () => {
    const data = report(VALID_LLM)
    data.llm.opportunity_ids = ['OPP-1']
    await renderReport(data)

    const link = await screen.findByRole('link', { name: /View OPP-1/i })
    expect(link).toHaveAttribute('href', '/opportunities/OPP-1')
    const card = link.closest('article')
    if (!card) {
      throw new Error('missing opportunity card')
    }
    expect(within(card).getByRole('heading', { name: /BTCUSDT/ })).toBeInTheDocument()
    expect(within(card).getByText(/69000\.00 – 71000\.00/)).toBeInTheDocument()
    expect(within(card).getByText('0.72')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /accept|reject/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /accept|reject/i })).not.toBeInTheDocument()
  })

  it('keeps a link-only card when the opportunity detail cannot be loaded', async () => {
    const data = report(VALID_LLM)
    data.llm.opportunity_ids = ['OPP-9']
    await renderReport(data, async () => {
      throw new Error('ledger unavailable')
    })

    const link = await screen.findByRole('link', { name: /View OPP-9/i })
    expect(link).toHaveAttribute('href', '/opportunities/OPP-9')
    expect(screen.getByText(/details unavailable/i)).toBeInTheDocument()
    // The heading always exists, so `aria-labelledby` never points at nothing.
    const card = link.closest('article')
    if (!card) {
      throw new Error('missing opportunity card')
    }
    expect(within(card).getByRole('heading', { name: 'OPP-9' })).toBeInTheDocument()
  })

  it('deduplicates a repeated opportunity id into one card', async () => {
    const data = report(VALID_LLM)
    data.llm.opportunity_ids = ['OPP-1', 'OPP-1']
    await renderReport(data)

    expect(await screen.findByRole('link', { name: /View OPP-1/i })).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /View OPP-1/i })).toHaveLength(1)
  })
})

describe('ReportDetail narrative safety', () => {
  it('renders markup-like narrative as inert text, never as HTML', async () => {
    const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    const data = report({ ...VALID_LLM, narrative: hostile, opportunity_ids: [] })

    const { container } = render(
      <ReportDetailPage
        reportId="RPT-XSS"
        fetchReportFn={async () => envelope(data)}
        fetchOpportunityFn={async () => {
          throw new Error('unused')
        }}
      />,
    )
    await screen.findByRole('heading', { name: /CALC/ })

    expect(screen.getByText(hostile)).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
  })
})

describe('ReportDetail loading and error states', () => {
  it('announces loading while the report request is pending', () => {
    render(
      <ReportDetailPage
        reportId="RPT-PENDING"
        fetchReportFn={() => new Promise<ReportEnvelope>(() => undefined)}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/Loading report RPT-PENDING/)
  })

  it('shows an alert when the report is empty or the request fails', async () => {
    const { unmount } = render(
      <ReportDetailPage
        reportId="RPT-EMPTY"
        fetchReportFn={async () => ({ ...envelope(), data: null })}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/Report unavailable/)
    unmount()

    render(
      <ReportDetailPage
        reportId="RPT-ERROR"
        fetchReportFn={async () => {
          throw new Error('network down')
        }}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/network down/)
  })
})
