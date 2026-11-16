import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChartData, ChartEnvelope } from '../../api/charts'
import type {
  ApiMeta,
  MetricsDocument,
  ReportData,
  ReportEnvelope,
  SnapshotBar,
} from '../../api/reports'
import { ChartPanelPage } from '../chart-panel'
import { ReportDetailPage } from '../report-detail'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'PARTIAL',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function bar(start: string, close: string, delta: string): SnapshotBar {
  return {
    bar_start: start,
    bar_end: start,
    open: '70000.00',
    high: '70010.00',
    low: '69990.00',
    close,
    volume: '1.00',
    quote_volume: '70000.00',
    trade_count: 2,
    taker_buy_volume: '0.60',
    delta,
    cvd: delta,
    open_interest: null,
    funding_rate: null,
  }
}

const FOUR_HOUR_BARS = [
  bar('2026-10-01T00:00:00.000Z', '70002.00', '0.20'),
  bar('2026-10-01T04:00:00.000Z', '70004.00', '-0.10'),
  bar('2026-10-01T08:00:00.000Z', '70006.00', '0.30'),
]

const ONE_HOUR_BARS = [
  bar('2026-10-01T00:00:00.000Z', '70001.00', '0.10'),
  bar('2026-10-01T01:00:00.000Z', '70003.00', '-0.05'),
]

function metricsFor(period: string, bars: SnapshotBar[]): MetricsDocument {
  return {
    schema_version: '1.0.0',
    symbol: 'BTCUSDT',
    period,
    window: { start: bars[0].bar_start, end: '2026-10-02T00:00:00.000Z' },
    computed_at: '2026-11-06T12:00:00.000Z',
    metric_version: 'orderflow.v1.0123456789abcdef',
    input_manifest_hash: 'a'.repeat(64),
    data_quality: 'PARTIAL',
    quality_reasons: ['open_interest not measured in this slice'],
    cvd: {
      unit: 'BASE',
      points: bars.map((item) => ({
        bar_start: item.bar_start,
        bar_end: item.bar_end,
        buy_volume: '0.60',
        sell_volume: '0.40',
        delta: item.delta,
        cvd: item.cvd,
        taker_buy_ratio: 0.6,
      })),
      summary: { delta: '0.30', cvd_change: '0.30', buy_volume: '1.20', sell_volume: '0.80' },
      divergence_flags: [],
    },
    footprint: {
      price_bucket: '0.10',
      imbalance_threshold: 3,
      bars: bars.map((item) => ({
        bar_start: item.bar_start,
        bar_end: item.bar_end,
        cells: [
          {
            price: '70000.00',
            bid_volume: '0.20',
            ask_volume: '0.10',
            delta: '0.10',
            total_volume: '0.30',
            imbalance_ratio: null,
            is_stacked_imbalance: false,
          },
        ],
      })),
      top_imbalances: [],
    },
    volume_profile: {
      window: { start: bars[0].bar_start, end: '2026-10-02T00:00:00.000Z' },
      bin_size: '0.10',
      value_area_pct: 70,
      poc: '70000.00',
      vah: '70001.00',
      val: '69999.00',
      bins: [
        {
          price_low: '70000.00',
          price_high: '70000.10',
          volume: '0.30',
          buy_volume: '0.20',
          sell_volume: '0.10',
          is_poc: true,
          in_value_area: true,
        },
      ],
      notes: [],
    },
    open_interest: {
      unit: 'BASE',
      sampling: 'REGULAR',
      points: [],
      delta_over_window: '0',
      last: {
        ts: '2026-10-01T00:00:00.000Z',
        open_interest: '100',
        open_interest_value: '7000000',
      },
    },
    funding: {
      current_rate: '0',
      next_funding_time: '2026-10-01T08:00:00.000Z',
      interval_hours: 8,
      mark_price: '70000.00',
      index_price: '70000.00',
      history: [],
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

function availableChart(period: string, bars: SnapshotBar[]): ChartData {
  return {
    report_id: 'RPT-BTCUSDT-1',
    symbol: 'BTCUSDT',
    native_period: '4h',
    requested_period: period,
    status: 'AVAILABLE',
    available_periods: ['4h', '1h'],
    source_report_id: `RPT-${period}`,
    generated_at: '2026-11-06T12:00:00.000Z',
    bars,
    metrics: metricsFor(period, bars),
    gaps: [],
    data_quality: 'PARTIAL',
    quality_reasons: ['open_interest not measured in this slice'],
    reason: null,
  }
}

const REASON =
  'No published 1h bars or metrics exist for BTCUSDT. ' +
  'The chart never upsamples or interpolates from another period.'

const MISSING_CHART: ChartData = {
  report_id: 'RPT-BTCUSDT-1',
  symbol: 'BTCUSDT',
  native_period: '4h',
  requested_period: '1h',
  status: 'MISSING',
  available_periods: ['4h'],
  source_report_id: null,
  generated_at: null,
  bars: [],
  metrics: null,
  gaps: [],
  data_quality: 'MISSING',
  quality_reasons: [],
  reason: REASON,
}

function chartEnvelope(data: ChartData): ChartEnvelope {
  return { ok: true, data, error: null, meta }
}

function useChart(period?: string): ChartEnvelope {
  return period === '1h'
    ? chartEnvelope(availableChart('1h', ONE_HOUR_BARS))
    : chartEnvelope(availableChart('4h', FOUR_HOUR_BARS))
}

function selectedRowTime(container: HTMLElement): string | null {
  const row = Array.from(container.querySelectorAll('tbody tr')).find(
    (candidate) => candidate.getAttribute('aria-current') === 'true',
  )
  return row?.querySelector('td')?.textContent ?? null
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
  vi.restoreAllMocks()
})

describe('ChartPanelPage available period', () => {
  it('renders the chart, stamps, bar selection, and a table that follows the selection', async () => {
    const fetchChartFn = vi.fn(async (_id: string, period?: string) => useChart(period))
    const { container } = render(
      <ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />,
    )

    const figure = await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    expect(figure).toBeInTheDocument()
    expect(screen.getAllByRole('note', { name: /Provenance CALC/i }).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('note', { name: /Data quality PARTIAL/i }).length).toBeGreaterThan(0)
    expect(screen.getByText(/gaps are never interpolated/i)).toBeInTheDocument()

    const barsGroup = screen.getByRole('group', { name: 'Bar selection' })
    const barButtons = within(barsGroup).getAllByRole('button')
    expect(barButtons).toHaveLength(FOUR_HOUR_BARS.length)

    fireEvent.click(screen.getByRole('button', { name: /View data table/i }))
    const table = screen.getByRole('table', { name: 'Chart data table' })
    expect(table.querySelectorAll('tbody tr')).toHaveLength(FOUR_HOUR_BARS.length)
    const currentRows = within(table)
      .getAllByRole('row')
      .filter((row) => row.getAttribute('aria-current') === 'true')
    expect(currentRows).toHaveLength(1)
    expect(currentRows[0]).toHaveAttribute('data-selected', 'true')
    expect(selectedRowTime(container)).toBe('2026-10-01 08:00')

    fireEvent.keyDown(barButtons[2], { key: 'ArrowLeft' })
    await waitFor(() => expect(selectedRowTime(container)).toBe('2026-10-01 04:00'))
    expect(barButtons[1]).toHaveAttribute('aria-pressed', 'true')

    fireEvent.keyDown(barButtons[1], { key: 'Home' })
    await waitFor(() => expect(selectedRowTime(container)).toBe('2026-10-01 00:00'))
    expect(barButtons[0]).toHaveAttribute('aria-pressed', 'true')

    fireEvent.keyDown(barButtons[0], { key: 'End' })
    await waitFor(() => expect(selectedRowTime(container)).toBe('2026-10-01 08:00'))
    expect(barButtons[2]).toHaveAttribute('aria-pressed', 'true')
  })

  it('switches periods through the accessible group and replaces the series', async () => {
    const calls: Array<string | undefined> = []
    const fetchChartFn = vi.fn(async (_id: string, period?: string) => {
      calls.push(period)
      return useChart(period)
    })
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    const group = screen.getByRole('group', { name: 'Timeframe' })
    const replaceState = vi.spyOn(window.history, 'replaceState')

    fireEvent.click(within(group).getByRole('button', { name: '1h' }))

    expect(await screen.findByRole('figure', { name: /BTCUSDT 1h chart/i })).toBeInTheDocument()
    expect(calls).toContain('1h')
    expect(replaceState).toHaveBeenCalled()
    expect(String(replaceState.mock.calls.at(-1)?.[2])).toContain('period=1h')
  })

  it('keeps the timeframe group mounted and restores focus and the announcement after a switch', async () => {
    let resolveHourly: ((value: ChartEnvelope) => void) | undefined
    const fetchChartFn = vi.fn((_id: string, requested?: string): Promise<ChartEnvelope> => {
      if (requested === '1h' && !resolveHourly) {
        return new Promise<ChartEnvelope>((resolve) => {
          resolveHourly = resolve
        })
      }
      return Promise.resolve(useChart(requested))
    })
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    const group = screen.getByRole('group', { name: 'Timeframe' })
    const hourly = within(group).getByRole('button', { name: '1h' })
    hourly.focus()
    expect(document.activeElement).toBe(hourly)

    fireEvent.click(hourly)

    // The head and switch stay mounted; only the chart region is busy.
    expect(screen.getByRole('group', { name: 'Timeframe' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(document.querySelector('.chart-region')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('status')).toHaveTextContent(/Loading 1h chart/i)
    expect(screen.getByRole('status')).not.toHaveTextContent(/Showing/i)

    await act(async () => {
      resolveHourly?.(useChart('1h'))
    })

    const active = within(screen.getByRole('group', { name: 'Timeframe' })).getByRole('button', {
      name: '1h',
    })
    expect(active).toHaveAttribute('aria-pressed', 'true')
    expect(active).not.toHaveAttribute('aria-disabled')
    expect(document.activeElement).toBe(active)
    expect(screen.getByRole('status')).toHaveTextContent(/Showing 1h chart/i)
    expect(document.querySelector('.chart-region')).not.toHaveAttribute('aria-busy')
    expect(await screen.findByRole('figure', { name: /BTCUSDT 1h chart/i })).toBeInTheDocument()
  })

  it('keeps the panel mounted and offers a retry when a period switch fails', async () => {
    let rejectHourly: ((reason: Error) => void) | undefined
    let hourlyAttempts = 0
    const fetchChartFn = vi.fn((_id: string, requested?: string): Promise<ChartEnvelope> => {
      if (requested === '1h') {
        hourlyAttempts += 1
        if (hourlyAttempts === 1) {
          return new Promise<ChartEnvelope>((_resolve, reject) => {
            rejectHourly = reject
          })
        }
      }
      return Promise.resolve(useChart(requested))
    })
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    const hourly = within(screen.getByRole('group', { name: 'Timeframe' })).getByRole('button', {
      name: '1h',
    })
    hourly.focus()
    fireEvent.click(hourly)

    await act(async () => {
      rejectHourly?.(new Error('refetch failed'))
    })

    // The failed switch keeps the head and switch mounted and moves focus to
    // the in-region alert instead of dropping it to the document body.
    expect(screen.getByRole('group', { name: 'Timeframe' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/BTCUSDT chart/i)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/refetch failed/i)
    expect(alert).toHaveFocus()

    fireEvent.click(within(alert).getByRole('button', { name: /Retry 1h/i }))

    expect(await screen.findByRole('figure', { name: /BTCUSDT 1h chart/i })).toBeInTheDocument()
    expect(fetchChartFn.mock.calls.filter(([, requested]) => requested === '1h')).toHaveLength(2)
  })

  it('encodes volume direction with a shape channel, not only hue, and says so in the legend', async () => {
    const { container } = render(
      <ChartPanelPage
        reportId="RPT-BTCUSDT-1"
        fetchChartFn={async (_id, period) => useChart(period)}
      />,
    )

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })

    const volume = container.querySelector('[data-testid="chart-volume"]')
    expect(volume?.querySelectorAll('.vol-up')).toHaveLength(2)
    expect(volume?.querySelectorAll('.vol-down')).toHaveLength(1)
    // Up bars carry a non-colour top cap; down bars are outlined, not filled.
    expect(volume?.querySelectorAll('.vol-cap')).toHaveLength(2)
    expect(screen.getByText(/filled bar with top cap/i)).toBeInTheDocument()
    expect(screen.getByText(/outline bar/i)).toBeInTheDocument()
    // The non-visual alternative keeps the direction column.
    fireEvent.click(screen.getByRole('button', { name: /View data table/i }))
    const headers = within(screen.getByRole('table', { name: 'Chart data table' })).getAllByRole(
      'columnheader',
    )
    expect(headers.map((header) => header.textContent)).toContain('Dir')
  })
})

describe('ChartPanelPage missing period', () => {
  it('shows an honest unavailable panel without drawing a series', async () => {
    window.history.replaceState(null, '', '/reports/RPT-BTCUSDT-1/chart?period=1h')
    const fetchChartFn = vi.fn(async (_id: string, period?: string) =>
      period === '1h' ? chartEnvelope(MISSING_CHART) : useChart(period),
    )
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    const note = await screen.findByRole('note', { name: /Chart unavailable for 1h/i })
    expect(note).toHaveTextContent('No published 1h bars')
    expect(note).toHaveTextContent('never upsamples or interpolates')
    expect(note).toHaveTextContent(/Available periods/i)
    expect(note).toHaveTextContent('4h')
    expect(screen.queryByTestId('chart-plot')).toBeNull()
    expect(screen.queryByRole('figure')).toBeNull()
    expect(screen.queryByRole('table', { name: 'Chart data table' })).toBeNull()

    const group = screen.getByRole('group', { name: 'Timeframe' })
    const hourly = within(group).getByRole('button', { name: '1h' })
    expect(hourly).not.toBeDisabled()
    expect(hourly).toHaveAttribute('aria-disabled', 'true')
    expect(hourly).toHaveAttribute('aria-pressed', 'true')
    hourly.focus()
    expect(document.activeElement).toBe(hourly)
    const describedBy = hourly.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy as string)?.textContent).toMatch(/No published 1h/)
    expect(hourly.getAttribute('title')).toMatch(/No published 1h/)
    expect(within(group).getByRole('button', { name: '4h' })).not.toHaveAttribute('aria-disabled')

    const status = screen.getByRole('status')
    expect(status).toHaveTextContent(/No 1h chart available/i)
    expect(status).toHaveTextContent(/published periods: 4h/i)
    expect(status).not.toHaveTextContent(/Showing/i)
  })

  it('does not switch when an unavailable period is activated', async () => {
    window.history.replaceState(null, '', '/reports/RPT-BTCUSDT-1/chart?period=1h')
    const fetchChartFn = vi.fn(async (_id: string, period?: string) =>
      period === '1h' ? chartEnvelope(MISSING_CHART) : useChart(period),
    )
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    await screen.findByRole('note', { name: /Chart unavailable for 1h/i })
    expect(fetchChartFn).toHaveBeenCalledTimes(1)

    const group = screen.getByRole('group', { name: 'Timeframe' })
    const unavailable = within(group).getByRole('button', { name: '15m' })
    expect(unavailable).toHaveAttribute('aria-disabled', 'true')
    unavailable.focus()
    expect(document.activeElement).toBe(unavailable)

    fireEvent.click(unavailable)

    expect(fetchChartFn).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('group', { name: 'Timeframe' })).toBeInTheDocument()
  })
})

describe('ChartPanelPage states', () => {
  it('shows an alert with links back when the request fails', async () => {
    render(
      <ChartPanelPage
        reportId="RPT-BAD"
        fetchChartFn={async () => {
          throw new Error('network down')
        }}
      />,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/network down/)
    expect(within(alert).getByRole('link', { name: /^Back to report$/i })).toHaveAttribute(
      'href',
      '/reports/RPT-BAD',
    )
    expect(within(alert).getByRole('link', { name: /^Back to reports$/i })).toHaveAttribute(
      'href',
      '/reports',
    )
  })

  it('marks the 375px surface read-only', async () => {
    render(
      <ChartPanelPage
        reportId="RPT-BTCUSDT-1"
        fetchChartFn={async (_id, period) => useChart(period)}
        isCompact
      />,
    )

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    expect(screen.getByText(/Read-only at this width/)).toBeInTheDocument()
    expect(screen.getByRole('figure').closest('section')).toHaveAttribute('data-readonly', 'true')
  })
})

describe('ChartPanelPage review regressions', () => {
  it('focuses the full-page alert when the initial load fails', async () => {
    const { container } = render(
      <ChartPanelPage
        reportId="RPT-BAD"
        fetchChartFn={async () => {
          throw new Error('initial down')
        }}
      />,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/initial down/)
    await waitFor(() => expect(alert).toHaveFocus())
    expect(document.activeElement).not.toBe(document.body)
    expect(container.querySelector('section.system-page.chart-panel')).toHaveAttribute('lang', 'en')
  })

  it('refocuses the in-region alert after every failed retry', async () => {
    let hourlyAttempts = 0
    const fetchChartFn = vi.fn(async (_id: string, requested?: string) => {
      if (requested === '1h') {
        hourlyAttempts += 1
        throw new Error(`period failed ${hourlyAttempts}`)
      }
      return useChart(requested)
    })
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    const hourly = within(screen.getByRole('group', { name: 'Timeframe' })).getByRole('button', {
      name: '1h',
    })
    hourly.focus()
    fireEvent.click(hourly)

    const firstAlert = await screen.findByRole('alert')
    expect(firstAlert).toHaveTextContent(/period failed 1/i)
    await waitFor(() => expect(firstAlert).toHaveFocus())

    fireEvent.click(within(firstAlert).getByRole('button', { name: /Retry 1h/i }))

    await waitFor(() =>
      expect(fetchChartFn.mock.calls.filter(([, requested]) => requested === '1h')).toHaveLength(2),
    )
    const secondAlert = await screen.findByRole('alert')
    expect(secondAlert).toHaveTextContent(/period failed 2/i)
    await waitFor(() => expect(secondAlert).toHaveFocus())
    expect(document.activeElement).not.toBe(document.body)
  })

  it('resets bar selection, the table toggle, and the announcement across a period switch', async () => {
    const fetchChartFn = vi.fn(async (_id: string, requested?: string) => useChart(requested))
    render(<ChartPanelPage reportId="RPT-BTCUSDT-1" fetchChartFn={fetchChartFn} />)

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    const sharedFourHourBar = within(
      screen.getByRole('group', { name: 'Bar selection' }),
    ).getByRole('button', { name: /2026-10-01 00:00/i })
    fireEvent.click(sharedFourHourBar)
    expect(sharedFourHourBar).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('button', { name: /View data table/i }))
    expect(screen.getByRole('button', { name: /View data table/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    )

    fireEvent.click(
      within(screen.getByRole('group', { name: 'Timeframe' })).getByRole('button', { name: '1h' }),
    )

    await screen.findByRole('figure', { name: /BTCUSDT 1h chart/i })
    const oneHourBars = screen.getByRole('group', { name: 'Bar selection' })
    // The 1h default is its own last bar; the shared 00:00 bar is not selected.
    expect(within(oneHourBars).getByRole('button', { name: /2026-10-01 01:00/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(within(oneHourBars).getByRole('button', { name: /2026-10-01 00:00/i })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(screen.getByRole('button', { name: /View data table/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
    expect(screen.getByRole('status')).toHaveTextContent(/Showing 1h chart/i)
  })

  it('shows an explicit unavailable note when AVAILABLE has no drawable bars', async () => {
    const emptyBars: ChartData = { ...availableChart('4h', FOUR_HOUR_BARS), bars: [] }
    render(
      <ChartPanelPage
        reportId="RPT-BTCUSDT-1"
        fetchChartFn={async () => chartEnvelope(emptyBars)}
      />,
    )

    const note = await screen.findByRole('note', { name: /Chart unavailable for 4h/i })
    expect(note).toHaveTextContent(/no bars or drawable metrics/i)
    expect(note).not.toHaveTextContent(/Data quality: MISSING/i)
    expect(screen.queryByRole('figure')).toBeNull()
    expect(screen.queryByTestId('chart-plot')).toBeNull()
    expect(screen.queryByRole('table', { name: 'Chart data table' })).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent(/No 4h chart available/i)
    expect(screen.getByRole('status')).not.toHaveTextContent(/Showing/i)
  })

  it('keeps the MISSING-quality explanation when an AVAILABLE report has no bars', async () => {
    const missingMarketData: ChartData = {
      ...availableChart('4h', FOUR_HOUR_BARS),
      bars: [],
      data_quality: 'MISSING',
      quality_reasons: ['The stored snapshot has no recorded market bars.'],
    }
    render(
      <ChartPanelPage
        reportId="RPT-BTCUSDT-1"
        fetchChartFn={async () => chartEnvelope(missingMarketData)}
      />,
    )

    const note = await screen.findByRole('note', { name: /Chart unavailable for 4h/i })
    expect(note).toHaveTextContent(
      'The market data required for a chart is MISSING. No candles, volume, CVD, volume profile, ' +
        'or footprint are drawn from invented values.',
    )
    expect(note).toHaveTextContent(
      'Data quality: MISSING. The stored snapshot has no recorded market bars.',
    )
    expect(note).toHaveTextContent(/No 4h chart/i)
    expect(note).toHaveTextContent(/no bars or drawable metrics/i)
    expect(note).toHaveTextContent(/Available periods: 4h, 1h/i)
    expect(note).toHaveTextContent(/never draws an invented, upsampled, or interpolated series/i)
    expect(screen.queryByRole('figure')).toBeNull()
    expect(screen.queryByTestId('chart-plot')).toBeNull()
    expect(screen.queryByRole('table', { name: 'Chart data table' })).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent(/No 4h chart available/i)
    expect(screen.getByRole('status')).not.toHaveTextContent(/Showing/i)
  })

  it('uses one h1 and steps through h2 then h3 without skipping a level', async () => {
    render(
      <ChartPanelPage
        reportId="RPT-BTCUSDT-1"
        fetchChartFn={async (_id, period) => useChart(period)}
      />,
    )

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    const levels = screen.getAllByRole('heading').map((heading) => Number(heading.tagName.slice(1)))

    expect(levels.filter((level) => level === 1)).toHaveLength(1)
    expect(levels[0]).toBe(1)
    expect(levels).toContain(2)
    expect(levels).toContain(3)
    for (let index = 1; index < levels.length; index += 1) {
      expect(levels[index] - levels[index - 1]).toBeLessThanOrEqual(1)
    }
  })

  it('marks the English route and chart subtree as English in a zh-CN document', async () => {
    const { container } = render(
      <ChartPanelPage
        reportId="RPT-BTCUSDT-1"
        fetchChartFn={async (_id, period) => useChart(period)}
      />,
    )

    await screen.findByRole('figure', { name: /BTCUSDT 4h chart/i })
    expect(container.querySelector('section.system-page.chart-panel')).toHaveAttribute('lang', 'en')
    expect(screen.getByRole('figure')).toHaveAttribute('lang', 'en')
    expect(screen.getByRole('group', { name: 'Timeframe' }).closest('[lang="en"]')).not.toBeNull()
    expect(screen.getByRole('status').closest('[lang="en"]')).not.toBeNull()
  })
})

describe('report detail chart link', () => {
  it('links to the encoded chart route', async () => {
    const data = reportData()
    render(
      <ReportDetailPage
        reportId="RPT-BTCUSDT-1"
        fetchReportFn={async () => reportEnvelope(data)}
        fetchOpportunityFn={async () => {
          throw new Error('unused')
        }}
      />,
    )

    const link = await screen.findByRole('link', { name: /Open chart panel/i })
    expect(link).toHaveAttribute('href', '/reports/RPT-BTCUSDT-1/chart')
  })
})

function reportEnvelope(data: ReportData): ReportEnvelope {
  return { ok: true, data, error: null, meta }
}

function reportData(): ReportData {
  const metrics = metricsFor('4h', FOUR_HOUR_BARS)
  return {
    report_id: 'RPT-BTCUSDT-1',
    scan_id: 'SCAN-1',
    symbol: 'BTCUSDT',
    timeframe: '4h',
    generated_at: '2026-11-06T12:00:00.000Z',
    data_quality: 'PARTIAL',
    llm_decision_id: null,
    narrative_ref: null,
    llm: { configured: false, reason: 'LLM not configured.' },
    llm_stage: 'SKIPPED',
    llm_tokens: 0,
    cost_usd: '0',
    cost_unpriced: false,
    metrics_manifest: [],
    snapshot: {
      schema_version: '1.0.0',
      snapshot_id: 'SNAP-BTCUSDT',
      symbol: 'BTCUSDT',
      timeframe: '4h',
      as_of: '2026-11-06T12:00:00.000Z',
      window: { start: '2026-10-01T00:00:00.000Z', end: '2026-10-02T00:00:00.000Z' },
      source_mode: 'BACKFILL',
      data_quality: 'PARTIAL',
      quality_reasons: metrics.quality_reasons,
      last_price: '70006.00',
      mark_price: '70006.00',
      bars: FOUR_HOUR_BARS,
      orderflow: metrics,
      session: { underlying_session: 'CRYPTO', is_no_trading: false, note: '' },
      contract_specs: {},
      sources: [],
      gaps: [],
      snapshot_hash: 'b'.repeat(64),
    },
    metrics,
  }
}
