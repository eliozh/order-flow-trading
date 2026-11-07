import { render, screen } from '@testing-library/react'
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

const summary: ReportSummary = {
  report_id: 'RPT-BTCUSDT-20261106-120000-ABCD1234',
  scan_id: 'SCAN-20261106-ABCD1234',
  symbol: 'BTCUSDT',
  timeframe: '4h',
  generated_at: '2026-11-06T12:00:00.000Z',
  data_quality: 'COMPLETE',
  llm_decision_id: null,
}

function envelope(items: ReportSummary[]): ReportListEnvelope {
  return {
    ok: true,
    data: { items, total: items.length, scan_id: null },
    error: null,
    meta,
  }
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
    render(<ReportsPage fetchReportsFn={async () => envelope([summary])} />)
    expect(await screen.findByRole('table', { name: /CALC report list/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: summary.report_id })).toHaveAttribute(
      'href',
      `/reports/${summary.report_id}`,
    )
  })
})
