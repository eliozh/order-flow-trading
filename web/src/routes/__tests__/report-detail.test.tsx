import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ApiMeta, MetricsDocument, ReportData, ReportEnvelope } from '../../api/reports'
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
      last: { ts: '2026-10-01T00:00:00.000Z', open_interest: '100', open_interest_value: '7000000' },
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

function report(measured: boolean): ReportData {
  const document = metrics(measured)
  return {
    report_id: 'RPT-BTCUSDT-20261106-120000-ABCD1234',
    scan_id: 'SCAN-20261106-ABCD1234',
    symbol: 'BTCUSDT',
    timeframe: '4h',
    generated_at: '2026-11-06T12:00:00.000Z',
    data_quality: 'MISSING',
    llm_decision_id: null,
    narrative_ref: null,
    llm: { configured: false, reason: 'LLM not configured.' },
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

function envelope(measured: boolean): ReportEnvelope {
  return { ok: true, data: report(measured), error: null, meta }
}

async function renderReport(measured: boolean) {
  render(
    <ReportDetailPage
      reportId="RPT-BTCUSDT-20261106-120000-ABCD1234"
      fetchReportFn={async () => envelope(measured)}
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
    await renderReport(false)

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
    await renderReport(true)

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
        fetchReportFn={async () => ({ ...envelope(false), data: null })}
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
