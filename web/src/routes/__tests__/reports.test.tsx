import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ApiMeta, ReportListEnvelope, ReportSummary } from '../../api/reports'
import { ReportsPage } from '../reports'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function summary(overrides: Partial<ReportSummary> = {}): ReportSummary {
  return {
    report_id: 'RPT-BTCUSDT-20261106-120000-ABCD1234',
    scan_id: 'SCAN-20261106-ABCD1234',
    symbol: 'BTCUSDT',
    timeframe: '4h',
    generated_at: '2026-11-06T12:00:00.000Z',
    data_quality: 'COMPLETE',
    llm_decision_id: null,
    narrative_ref: null,
    llm_stage: 'SKIPPED',
    llm_tokens: 0,
    cost_usd: '0',
    cost_unpriced: false,
    llm: { configured: false, validation_status: null, opportunity_ids: [] },
    ...overrides,
  }
}

function envelope(items: ReportSummary[]): ReportListEnvelope {
  return {
    ok: true,
    data: { items, total: items.length, scan_id: null },
    error: null,
    meta,
  }
}

function row(reportId: string): HTMLElement {
  const element = screen.getByRole('link', { name: reportId }).closest('tr')
  if (!element) {
    throw new Error(`missing row ${reportId}`)
  }
  return element
}

describe('ReportsPage states', () => {
  it('shows loading while the list request is pending', () => {
    render(<ReportsPage fetchReportsFn={() => new Promise<ReportListEnvelope>(() => undefined)} />)
    expect(screen.getByRole('status')).toHaveTextContent(/Loading reports/)
  })

  it('shows an empty state when no reports exist', async () => {
    render(<ReportsPage fetchReportsFn={async () => envelope([])} />)
    expect(await screen.findByRole('heading', { name: /No reports yet/i })).toBeInTheDocument()
  })

  it('shows an alert when the list request fails', async () => {
    render(
      <ReportsPage
        fetchReportsFn={async () => {
          throw new Error('api down')
        }}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/api down/)
  })

  it('lists reports with links when data is present', async () => {
    render(<ReportsPage fetchReportsFn={async () => envelope([summary()])} />)
    expect(await screen.findByRole('table', { name: /CALC report list/i })).toBeInTheDocument()
    const first = summary()
    expect(screen.getByRole('link', { name: first.report_id })).toHaveAttribute(
      'href',
      `/reports/${first.report_id}`,
    )
  })
})

describe('ReportsPage LLM stage chips', () => {
  it('marks a metrics-only report CALC only', async () => {
    render(<ReportsPage fetchReportsFn={async () => envelope([summary()])} />)
    await screen.findByRole('table', { name: /CALC report list/i })

    const chip = within(row('RPT-BTCUSDT-20261106-120000-ABCD1234')).getByText('CALC only')
    expect(chip).toHaveClass('chip', 'chip--warn')
  })

  it('marks a schema-valid narrative as LLM', async () => {
    const valid = summary({
      report_id: 'RPT-LLM-1',
      llm_stage: 'DONE',
      llm_tokens: 1200,
      cost_usd: '0.012',
      llm: { configured: true, validation_status: 'VALID', opportunity_ids: ['OPP-1'] },
    })
    render(<ReportsPage fetchReportsFn={async () => envelope([valid])} />)
    await screen.findByRole('table', { name: /CALC report list/i })

    const chip = within(row('RPT-LLM-1')).getByText('LLM')
    expect(chip).toHaveClass('chip', 'chip--calc')
  })

  it('marks a rejected narrative as REJECTED while the CALC report remains listed', async () => {
    const rejected = summary({
      report_id: 'RPT-REJECTED-1',
      llm_stage: 'FAILED',
      llm: { configured: true, validation_status: 'REJECTED', opportunity_ids: [] },
    })
    render(<ReportsPage fetchReportsFn={async () => envelope([rejected])} />)
    await screen.findByRole('table', { name: /CALC report list/i })

    expect(within(row('RPT-REJECTED-1')).getByText('REJECTED')).toHaveClass('chip', 'chip--danger')
    expect(screen.getByRole('link', { name: 'RPT-REJECTED-1' })).toBeInTheDocument()
  })
})
