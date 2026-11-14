import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Chart } from '../chart'
import type { MetricsDocument, SnapshotBar } from '../../api/reports'

const bars: SnapshotBar[] = [
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
  {
    bar_start: '2026-10-02T00:00:00.000Z',
    bar_end: '2026-10-02T04:00:00.000Z',
    open: '70000.00',
    high: '70003.00',
    low: '69998.00',
    close: '70001.00',
    volume: '0.70',
    quote_volume: '49000.00',
    trade_count: 4,
    taker_buy_volume: '0.30',
    delta: '-0.10',
    cvd: '0.10',
    open_interest: null,
    funding_rate: null,
  },
]

const metrics: MetricsDocument = {
  schema_version: '1.0.0',
  symbol: 'BTCUSDT',
  period: '4h',
  window: {
    start: '2026-10-01T00:00:00.000Z',
    end: '2026-10-03T00:00:00.000Z',
  },
  computed_at: '2026-11-06T12:00:00.000Z',
  metric_version: 'orderflow.v1.0123456789abcdef',
  input_manifest_hash: 'a'.repeat(64),
  data_quality: 'PARTIAL',
  quality_reasons: [],
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
      {
        bar_start: '2026-10-02T00:00:00.000Z',
        bar_end: '2026-10-02T04:00:00.000Z',
        buy_volume: '0.30',
        sell_volume: '0.40',
        delta: '-0.10',
        cvd: '0.10',
        taker_buy_ratio: 0.428571,
      },
    ],
    summary: {
      delta: '0.10',
      cvd_change: '0.10',
      buy_volume: '0.70',
      sell_volume: '0.60',
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
      {
        bar_start: '2026-10-02T00:00:00.000Z',
        bar_end: '2026-10-02T04:00:00.000Z',
        cells: [
          {
            price: '70002.00',
            bid_volume: '0',
            ask_volume: '0.30',
            delta: '0.30',
            total_volume: '0.30',
            imbalance_ratio: null,
            is_stacked_imbalance: false,
          },
        ],
      },
    ],
    top_imbalances: [],
  },
  volume_profile: {
    window: {
      start: '2026-10-01T00:00:00.000Z',
      end: '2026-10-03T00:00:00.000Z',
    },
    bin_size: '0.10',
    value_area_pct: 70,
    poc: '70002.00',
    vah: '70002.00',
    val: '70000.00',
    bins: [
      {
        price_low: '70000.00',
        price_high: '70000.10',
        volume: '0.10',
        buy_volume: '0.10',
        sell_volume: '0',
        is_poc: false,
        in_value_area: true,
      },
      {
        price_low: '70002.00',
        price_high: '70002.10',
        volume: '0.30',
        buy_volume: '0.30',
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
    points: [],
    delta_over_window: '0',
    last: { ts: '2026-10-03T00:00:00.000Z', open_interest: '0', open_interest_value: '0' },
  },
  funding: {
    current_rate: '0',
    next_funding_time: '2026-10-03T00:00:00.000Z',
    interval_hours: 8,
    mark_price: '0',
    index_price: '0',
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

function renderChart(quality = 'PARTIAL') {
  return render(
    <Chart
      symbol="BTCUSDT"
      timeframe="4h"
      bars={bars}
      metrics={metrics}
      quality={quality}
      qualityReasons={[]}
    />,
  )
}

describe('Chart', () => {
  it('renders candles, volume, CVD, volume profile, and the footprint dock', () => {
    renderChart()

    expect(screen.getByRole('figure', { name: /BTCUSDT 4h/i })).toBeInTheDocument()
    expect(screen.getByTestId('chart-plot')).toBeInTheDocument()
    expect(screen.getByTestId('chart-volume')).toBeInTheDocument()
    expect(screen.getByTestId('chart-cvd')).toBeInTheDocument()
    expect(screen.getByTestId('chart-volume-profile')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /Footprint/i })).toBeInTheDocument()
    expect(screen.getByText('POC')).toBeInTheDocument()
  })

  it('renders a data-table alternative with exactly the chart values', () => {
    renderChart()
    fireEvent.click(screen.getByRole('button', { name: /view data table/i }))

    const table = screen.getByRole('table', { name: /chart data table/i })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    const first = within(rows[0]).getAllByRole('cell')
    expect(first[first.length - 1]).toHaveTextContent('up')
    expect(first[0]).toHaveTextContent('2026-10-01 00:00')
    expect(first[1]).toHaveTextContent('70000.00')
    expect(first[2]).toHaveTextContent('70002.00')
    expect(first[3]).toHaveTextContent('69999.00')
    expect(first[4]).toHaveTextContent('70002.00')
    expect(first[5]).toHaveTextContent('0.60')
    expect(first[6]).toHaveTextContent('+0.20')
    expect(first[7]).toHaveTextContent('+0.20')
  })

  it('moves bar selection with the keyboard and keeps the footprint in sync', () => {
    const onSelectBar = vi.fn()
    render(
      <Chart
        symbol="BTCUSDT"
        timeframe="4h"
        bars={bars}
        metrics={metrics}
        quality="PARTIAL"
        qualityReasons={[]}
        onSelectBar={onSelectBar}
      />,
    )

    const firstBar = screen.getByRole('button', { name: /2026-10-01 00:00/i })
    fireEvent.focus(firstBar)
    fireEvent.keyDown(firstBar, { key: 'ArrowRight' })

    expect(onSelectBar).toHaveBeenCalledWith('2026-10-02T00:00:00.000Z')
    const dock = screen.getByRole('region', { name: /Footprint/i })
    expect(within(dock).getByText('70002.00')).toBeInTheDocument()
    expect(within(dock).getByText('0.30')).toBeInTheDocument()
  })

  it('shows an explanation instead of a chart when the data is MISSING', () => {
    renderChart('MISSING')

    expect(screen.getByRole('note')).toHaveTextContent(/no chart/i)
    expect(screen.queryByTestId('chart-plot')).not.toBeInTheDocument()
    expect(screen.queryByRole('table', { name: /chart data table/i })).not.toBeInTheDocument()
  })

  it('renders hostile symbol metadata as text, never as markup', () => {
    const hostile = '<img src=x onerror="alert(1)">'
    const { container } = render(
      <Chart
        symbol={hostile}
        timeframe="4h"
        bars={bars}
        metrics={metrics}
        quality="PARTIAL"
        qualityReasons={[]}
      />,
    )

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getAllByText(hostile).length).toBeGreaterThan(0)
  })

  it('draws the entry marker when the entry is outside the bar price range', () => {
    render(
      <Chart
        symbol="BTCUSDT"
        timeframe="4h"
        bars={bars}
        metrics={metrics}
        quality="PARTIAL"
        qualityReasons={[]}
        entryPrice="70016.0004"
      />,
    )

    expect(screen.getByTestId('chart-entry-marker')).toBeInTheDocument()
    expect(screen.getByText(/ENTRY 70016\.0004/)).toBeInTheDocument()
    expect(screen.getByRole('figure')).toHaveAccessibleDescription(/Entry marker at 70016\.0004/)
  })
})

describe('Chart accessibility and alignment', () => {
  const thirdBar: SnapshotBar = {
    ...bars[1],
    bar_start: '2026-10-03T00:00:00.000Z',
    bar_end: '2026-10-03T04:00:00.000Z',
    close: '69999.00',
    low: '69998.00',
    delta: '-0.05',
    cvd: '0.15',
  }
  const threeBars = [...bars, thirdBar]

  function renderThreeBars() {
    const onSelectBar = vi.fn()
    render(
      <Chart
        symbol="BTCUSDT"
        timeframe="4h"
        bars={threeBars}
        metrics={metrics}
        quality="PARTIAL"
        qualityReasons={[]}
        onSelectBar={onSelectBar}
      />,
    )
    return onSelectBar
  }

  it('moves roving focus continuously across bars with arrows, Home, and End', () => {
    const onSelectBar = renderThreeBars()
    const first = screen.getByRole('button', { name: /2026-10-01 00:00/i })
    const second = screen.getByRole('button', { name: /2026-10-02 00:00/i })
    const third = screen.getByRole('button', { name: /2026-10-03 00:00/i })

    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    expect(second).toHaveFocus()

    fireEvent.keyDown(second, { key: 'ArrowRight' })
    expect(third).toHaveFocus()
    expect(onSelectBar).toHaveBeenLastCalledWith('2026-10-03T00:00:00.000Z')

    fireEvent.keyDown(third, { key: 'Home' })
    expect(first).toHaveFocus()
    fireEvent.keyDown(first, { key: 'End' })
    expect(third).toHaveFocus()
    fireEvent.keyDown(third, { key: 'ArrowLeft' })
    expect(second).toHaveFocus()
  })

  it('exposes the plot as a labelled image and the table as its controlled alternative', () => {
    renderChart()

    const plot = screen.getByRole('img', { name: /BTCUSDT 4h price chart/i })
    expect(plot).toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: /view data table/i })
    const tableId = toggle.getAttribute('aria-controls')
    expect(tableId).toBeTruthy()
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(toggle)
    expect(screen.getByRole('table', { name: /chart data table/i })).toHaveAttribute('id', tableId)
  })

  it('labels panes in fixed CSS px, exposes the zero line, and tab-stops only the table', () => {
    renderChart()
    const plot = screen.getByRole('img', { name: /price chart/i })
    expect(plot).not.toHaveAttribute('tabindex')
    expect(screen.getByTestId('chart-axis-labels')).toBeInTheDocument()
    expect(screen.getByTestId('cvd-zero-label')).toHaveTextContent('0')
    expect(screen.getByRole('figure')).toHaveAccessibleDescription(/Candlestick chart with 2 bars/i)
  })

  it('puts the chart table and VP bins behind a labelled scrollable region', () => {
    renderChart()
    fireEvent.click(screen.getByRole('button', { name: /view data table/i }))

    const region = screen.getByRole('region', { name: /chart data table/i })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(within(region).getByRole('table', { name: /chart data table/i })).toBeInTheDocument()
    const vpTable = within(region).getByRole('table', { name: /volume profile data table/i })
    expect(within(vpTable).getAllByText('POC').length).toBeGreaterThan(0)
    expect(within(vpTable).getByText('70000.00')).toBeInTheDocument()
  })

  it('debounces the polite live-region announcement by 500ms', () => {
    vi.useFakeTimers()
    try {
      renderThreeBars()
      const live = document.querySelector('[aria-live="polite"]') as HTMLElement
      act(() => vi.advanceTimersByTime(500))
      expect(live).toHaveTextContent(/Selected 2026-10-03 00:00 UTC/)

      const third = screen.getByRole('button', { name: /2026-10-03 00:00/i })
      fireEvent.keyDown(third, { key: 'ArrowLeft' })
      expect(live).not.toHaveTextContent(/Selected 2026-10-02 00:00 UTC/)
      act(() => vi.advanceTimersByTime(500))
      expect(live).toHaveTextContent(/Selected 2026-10-02 00:00 UTC/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('aligns volume-profile bins to the price axis instead of row order', () => {
    renderChart()
    const container = screen.getByTestId('chart-volume-profile')
    const bins = container.querySelectorAll('rect')
    expect(bins.length).toBeGreaterThan(1)
    const lowY = Number(bins[0].getAttribute('y'))
    const highY = Number(bins[1].getAttribute('y'))
    expect(lowY).toBeGreaterThan(highY)
  })

  it('gives every bar a non-colour direction cue in text and aria-label', () => {
    renderThreeBars()
    const first = screen.getByRole('button', { name: /2026-10-01 00:00/i })
    const third = screen.getByRole('button', { name: /2026-10-03 00:00/i })

    expect(first).toHaveTextContent('▲')
    expect(third).toHaveTextContent('▼')
    expect(first).toHaveAccessibleName(/up/i)
    expect(third).toHaveAccessibleName(/down/i)
    expect(
      screen.getByTestId('chart-candles').querySelectorAll('[data-direction="up"]'),
    ).toHaveLength(2)
    expect(
      screen.getByTestId('chart-candles').querySelectorAll('[data-direction="down"]'),
    ).toHaveLength(1)
  })

  it('distinguishes direction by text and sign, not hue alone', () => {
    renderChart()
    fireEvent.click(screen.getByRole('button', { name: /view data table/i }))
    expect(screen.getAllByText(/^up$/i).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/^down$/i).length).toBeGreaterThan(0)
    expect(screen.getAllByText('+0.20').length).toBeGreaterThan(0)
    expect(screen.getAllByText('-0.10').length).toBeGreaterThan(0)
  })
})
