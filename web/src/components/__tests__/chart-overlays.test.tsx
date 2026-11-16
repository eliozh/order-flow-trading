import { readFileSync } from 'node:fs'

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChartMarker, ChartZone } from '../../api/charts'
import type { DataGap, MetricsDocument, SnapshotBar } from '../../api/reports'
import { Chart } from '../chart'
import {
  barQualityStamp,
  gapBarIndices,
  gapLabelText,
  numericPrice,
  zoneDash,
} from '../chart-overlays'

function bar(
  index: number,
  close: string,
  extra: Partial<SnapshotBar> = {},
): SnapshotBar {
  const startHour = index * 4
  const start = `2026-10-01T${String(startHour).padStart(2, '0')}:00:00.000Z`
  const end = `2026-10-01T${String(startHour + 4).padStart(2, '0')}:00:00.000Z`
  return {
    bar_start: start,
    bar_end: end,
    open: '70000.00',
    high: '70010.00',
    low: '69990.00',
    close,
    volume: '1.00',
    quote_volume: '70000.00',
    trade_count: 2,
    taker_buy_volume: '0.60',
    delta: index === 1 ? '-0.10' : '0.20',
    cvd: index === 1 ? '0.10' : '0.30',
    open_interest: null,
    funding_rate: null,
    ...extra,
  }
}

const BARS: SnapshotBar[] = [
  bar(0, '70002.00'),
  bar(1, '69998.00'),
  bar(2, '70005.00'),
]

const GAP: DataGap = {
  gap_id: 'GAP-1',
  data_type: 'aggTrades',
  start: '2026-10-01T04:00:00.000Z',
  end: '2026-10-01T08:00:00.000Z',
  reason: 'no recorded trades',
  severity: 'WARN',
  resolved: false,
}

const METRICS: MetricsDocument = {
  schema_version: '1.0.0',
  symbol: 'BTCUSDT',
  period: '4h',
  window: { start: '2026-10-01T00:00:00.000Z', end: '2026-10-01T12:00:00.000Z' },
  computed_at: '2026-10-01T12:00:00.000Z',
  metric_version: 'orderflow.v1',
  input_manifest_hash: 'a'.repeat(64),
  data_quality: 'COMPLETE',
  quality_reasons: [],
  cvd: {
    unit: 'BASE',
    points: [],
    summary: { delta: '0.30', cvd_change: '0.30', buy_volume: '1.80', sell_volume: '1.20' },
    divergence_flags: [],
  },
  footprint: {
    price_bucket: '0.10',
    imbalance_threshold: 3,
    bars: BARS.map((item) => ({
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
    window: { start: '2026-10-01T00:00:00.000Z', end: '2026-10-01T12:00:00.000Z' },
    bin_size: '0.10',
    value_area_pct: 70,
    poc: '70000.00',
    vah: '70010.00',
    val: '69990.00',
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
    last: { ts: '2026-10-01T12:00:00.000Z', open_interest: '0', open_interest_value: '0' },
  },
  funding: {
    current_rate: '0',
    next_funding_time: '2026-10-01T16:00:00.000Z',
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

const LONG_ZONE: ChartZone = {
  opportunity_id: 'OPP-1',
  side: 'LONG',
  state: 'TRIGGERED',
  low: '69400.00',
  high: '70400.00',
  trigger_price: '70400.00',
  label: 'OPP-1 · LONG · 0.72',
}

const MARKERS: ChartMarker[] = [
  { kind: 'entry', price: '69900.00', label: 'ENTRY 69900.00', position_id: 'POS-1' },
  { kind: 'stop', price: '69500.00', label: 'STOP 69500.00', position_id: 'POS-1' },
  { kind: 'target', price: '70500.00', label: 'TARGET 70500.00', position_id: 'POS-1' },
]

function renderChart(overrides: {
  zones?: ChartZone[]
  markers?: ChartMarker[]
  gaps?: DataGap[]
  quality?: string
  qualityReasons?: string[]
} = {}) {
  return render(
    <Chart
      symbol="BTCUSDT"
      timeframe="4h"
      bars={BARS}
      metrics={METRICS}
      quality={overrides.quality ?? 'COMPLETE'}
      qualityReasons={overrides.qualityReasons ?? []}
      zones={overrides.zones ?? []}
      markers={overrides.markers ?? []}
      gaps={overrides.gaps ?? []}
    />,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('opportunity zones and trigger lines', () => {
  it('draws a triggered LONG zone band and trigger line at the exact stored price', () => {
    const { container } = renderChart({ zones: [LONG_ZONE] })

    const zone = screen.getByTestId('chart-zone-OPP-1')
    expect(zone).toHaveAttribute('data-state', 'TRIGGERED')
    expect(within(zone).getByText('TRIGGERED')).toBeInTheDocument()
    expect(within(zone).getByText('OPP-1 · LONG · 0.72')).toBeInTheDocument()
    expect(within(zone).getByText('TRIGGER 70400.00')).toBeInTheDocument()

    const band = zone.querySelector('.chart-zone-band') as SVGRectElement
    expect(band).not.toBeNull()
    expect(Number(band.getAttribute('height'))).toBeGreaterThan(0)

    const edges = zone.querySelectorAll('.chart-zone-edge')
    expect(edges).toHaveLength(2)
    const highEdgeY = Number(edges[0].getAttribute('y1'))
    const triggerY = Number(
      (zone.querySelector('.chart-trigger-line') as SVGLineElement).getAttribute('y1'),
    )
    // LONG triggers at zone.high, so the trigger line sits exactly on the top edge.
    expect(triggerY).toBe(highEdgeY)

    const trigger = container.querySelector('[data-testid="chart-trigger-line"]')
    expect(trigger).not.toBeNull()
  })

  it('uses the visible state word and a distinct dash pattern, never color alone', () => {
    const { container, rerender } = renderChart({
      zones: [{ ...LONG_ZONE, state: 'EXPIRED' }],
    })

    const expired = screen.getByTestId('chart-zone-OPP-1')
    expect(within(expired).getByText('EXPIRED')).toBeInTheDocument()
    const expiredDash = expired.querySelector('.chart-zone-edge')?.getAttribute('stroke-dasharray')
    expect(expiredDash).toBe(zoneDash('EXPIRED'))
    expect(expiredDash).not.toBe(zoneDash('TRIGGERED'))

    rerender(
      <Chart
        symbol="BTCUSDT"
        timeframe="4h"
        bars={BARS}
        metrics={METRICS}
        quality="COMPLETE"
        zones={[{ ...LONG_ZONE, state: 'INVALIDATED' }]}
      />,
    )
    const invalidated = screen.getByTestId('chart-zone-OPP-1')
    expect(within(invalidated).getByText('INVALIDATED')).toBeInTheDocument()
    expect(invalidated.querySelector('.chart-zone-edge')?.getAttribute('stroke-dasharray')).toBe(
      zoneDash('INVALIDATED'),
    )
    expect(container.querySelector('img')).toBeNull()
  })

  it('renders a hostile zone label as text, never as markup', () => {
    const hostile = '<img src=x onerror="alert(1)">'
    const { container } = renderChart({
      zones: [{ ...LONG_ZONE, label: hostile }],
    })

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText(hostile)).toBeInTheDocument()
  })
})

describe('entry, exit, stop, and target markers', () => {
  it('places every marker at its exact stored price', () => {
    renderChart({ markers: MARKERS })

    const entry = screen.getByTestId('chart-entry-marker')
    const stop = screen.getByTestId('chart-marker-stop')
    const target = screen.getByTestId('chart-marker-target')
    expect(entry).toHaveAttribute('data-kind', 'entry')
    expect(entry).toHaveAttribute('data-price', '69900.00')
    expect(stop).toHaveAttribute('data-kind', 'stop')
    expect(stop).toHaveAttribute('data-price', '69500.00')
    expect(target).toHaveAttribute('data-kind', 'target')
    expect(target).toHaveAttribute('data-price', '70500.00')

    // Lower prices sit lower on the price axis (larger y).
    expect(Number(stop.getAttribute('data-y'))).toBeGreaterThan(
      Number(target.getAttribute('data-y')),
    )
    expect(screen.getByText('STOP 69500.00')).toBeInTheDocument()
    expect(screen.getByText('TARGET 70500.00')).toBeInTheDocument()
  })

  it('omits a missing or zero marker instead of placing it at zero', () => {
    renderChart({
      markers: [
        { kind: 'stop', price: '0', label: 'STOP 0', position_id: 'POS-1' },
        { kind: 'target', price: 'not-a-price', label: 'TARGET', position_id: 'POS-1' },
        ...MARKERS,
      ],
    })

    expect(screen.queryByTestId('chart-marker-exit')).toBeNull()
    const stops = screen.getAllByTestId('chart-marker-stop')
    expect(stops).toHaveLength(1)
    expect(stops[0]).toHaveAttribute('data-price', '69500.00')
    expect(document.querySelector('[data-kind="stop"][data-price="0"]')).toBeNull()
  })

  it('offsets near-identical labels apart while keeping the marker y at its price', () => {
    renderChart({
      markers: [
        { kind: 'stop', price: '69999.00', label: 'STOP 69999.00', position_id: 'POS-1' },
        { kind: 'exit', price: '69998.00', label: 'EXIT 69998.00', position_id: 'POS-1' },
      ],
    })

    const stop = screen.getByTestId('chart-marker-stop')
    const exit = screen.getByTestId('chart-marker-exit')
    const stopY = Number(stop.getAttribute('data-y'))
    const exitY = Number(exit.getAttribute('data-y'))
    const stopLabelY = Number(stop.querySelector('.chart-marker-label')?.getAttribute('y'))
    const exitLabelY = Number(exit.querySelector('.chart-marker-label')?.getAttribute('y'))
    // Prices stay exact and close; the text baselines are pushed apart.
    expect(Math.abs(stopY - exitY)).toBeLessThan(11)
    expect(Math.abs(stopLabelY - exitLabelY)).toBeGreaterThanOrEqual(11)
  })

  it('truncates a long zone and marker label to exactly 80 characters', () => {
    const long = 'X'.repeat(120)
    const { container } = renderChart({
      zones: [{ ...LONG_ZONE, label: long }],
      markers: [{ kind: 'stop', price: '69500.00', label: long, position_id: 'POS-1' }],
    })

    expect(container.querySelector('.chart-zone-label')?.textContent).toHaveLength(80)
    expect(container.querySelector('.chart-marker-label')?.textContent).toHaveLength(80)
  })
})

describe('gap breaks and CVD segments', () => {
  it('renders a hatched gap band and splits CVD into non-interpolated runs', () => {
    renderChart({ gaps: [GAP] })

    expect(screen.getByTestId('chart-gaps')).toBeInTheDocument()
    expect(screen.getByTestId('chart-gap-break')).toBeInTheDocument()
    expect(screen.getByText(gapLabelText(GAP))).toBeInTheDocument()

    // The middle bar is missing, so the CVD line is two disjoint polylines.
    expect(screen.getByTestId('chart-cvd-segment-0')).toBeInTheDocument()
    expect(screen.getByTestId('chart-cvd-segment-1')).toBeInTheDocument()
    expect(screen.queryByTestId('chart-cvd-segment-2')).toBeNull()
  })

  it('maps gaps to overlapping bar indices and stamps missing bars', () => {
    expect(gapBarIndices(BARS, [GAP])).toEqual([1])
    expect(barQualityStamp(BARS[1], [GAP], 'COMPLETE')).toBe('MISSING')
    expect(barQualityStamp(BARS[0], [], 'COMPLETE')).toBe('COMPLETE')
    expect(barQualityStamp(BARS[0], [], 'COMPLETE', ['CVD is stale'])).toBe('STALE')
    expect(barQualityStamp(BARS[0], [], 'ESTIMATED')).toBe('ESTIMATED')
  })

  it('matches reasons by exact token, so negated or derived words do not mislabel', () => {
    expect(barQualityStamp(BARS[0], [], 'COMPLETE', ['not stale'])).toBe('COMPLETE')
    expect(barQualityStamp(BARS[0], [], 'COMPLETE', ['staleness check skipped'])).toBe('COMPLETE')
    expect(barQualityStamp(BARS[0], [], 'COMPLETE', ['CVD estimated'])).toBe('ESTIMATED')
    expect(barQualityStamp(BARS[0], [], 'STALE', ['not stale'])).toBe('STALE')
  })

  it('does not mark a bar for a gap that only touches its boundary', () => {
    const touching: DataGap = {
      ...GAP,
      gap_id: 'GAP-TOUCH',
      start: '2026-10-01T12:00:00.000Z',
      end: '2026-10-01T16:00:00.000Z',
    }

    expect(gapBarIndices(BARS, [touching])).toEqual([])
  })

  it('renders one CVD segment and no gap band when the series has no gap', () => {
    renderChart()

    expect(screen.getByTestId('chart-cvd-segment-0')).toBeInTheDocument()
    expect(screen.queryByTestId('chart-cvd-segment-1')).toBeNull()
    expect(screen.queryByTestId('chart-gaps')).toBeNull()
  })

  it('renders distinct gap bands for equal windows with distinct ids', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    renderChart({ gaps: [GAP, { ...GAP, gap_id: 'GAP-2' }] })

    expect(screen.getAllByTestId('chart-gap-break')).toHaveLength(2)
    expect(error.mock.calls.flat().join(' ')).not.toMatch(/same key/i)
  })

  it('rejects non-positive or unparseable prices', () => {
    expect(numericPrice('70000.00')).toBe(70000)
    expect(numericPrice('0')).toBeNull()
    expect(numericPrice('-1')).toBeNull()
    expect(numericPrice('NaN')).toBeNull()
    expect(numericPrice('nope')).toBeNull()
    expect(numericPrice(null)).toBeNull()
  })
})

describe('keyboard and pointer crosshair', () => {
  function barButton(label: RegExp) {
    return screen.getByRole('button', { name: label })
  }

  it('shows the crosshair and tooltip on selection and matches the table and aria-live', async () => {
    renderChart()
    fireEvent.click(screen.getByRole('button', { name: /view data table/i }))

    // Hidden initially.
    expect(screen.queryByTestId('chart-crosshair')).toBeNull()
    expect(screen.queryByTestId('chart-tooltip')).toBeNull()

    const first = barButton(/2026-10-01 00:00/i)
    const second = barButton(/2026-10-01 04:00/i)
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowRight' })

    expect(screen.getByTestId('chart-crosshair')).toBeInTheDocument()
    const tooltip = screen.getByTestId('chart-tooltip')
    expect(tooltip).toHaveTextContent('2026-10-01 04:00 UTC')
    expect(tooltip).toHaveTextContent('69998.00')
    expect(tooltip).toHaveTextContent('COMPLETE')
    expect(screen.getByTestId('chart-tooltip-stamp')).toHaveTextContent('CALC')

    const table = screen.getByRole('table', { name: /chart data table/i })
    const current = within(table)
      .getAllByRole('row')
      .find((row) => row.getAttribute('aria-current') === 'true')
    expect(current).toHaveTextContent('2026-10-01 04:00')
    expect(current).toHaveTextContent('COMPLETE')

    const live = document.querySelector('[aria-live="polite"]') as HTMLElement
    await waitFor(() => expect(live).toHaveTextContent(/Selected 2026-10-01 04:00 UTC/))
    expect(live).toHaveTextContent(/O 70000\.00 H 70010\.00 L 69990\.00 C 69998\.00/)
    expect(live).toHaveTextContent(/Vol 1\.00/)
    expect(live).toHaveTextContent(/Delta -0\.10/)
    expect(live).toHaveTextContent(/CVD \+0\.10/)
    expect(live).toHaveTextContent(/Quality COMPLETE/)

    // Right/End/Left move the same crosshair selection.
    second.focus()
    fireEvent.keyDown(second, { key: 'End' })
    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('70005.00')
    fireEvent.keyDown(barButton(/2026-10-01 08:00/i), { key: 'Home' })
    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('70002.00')
    fireEvent.keyDown(barButton(/2026-10-01 00:00/i), { key: 'ArrowRight' })
    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('69998.00')
  })

  it('hides the crosshair on Escape without changing the selection or focus', () => {
    renderChart()
    const first = barButton(/2026-10-01 00:00/i)
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    expect(screen.getByTestId('chart-crosshair')).toBeInTheDocument()

    const second = barButton(/2026-10-01 04:00/i)
    fireEvent.keyDown(second, { key: 'Escape' })

    expect(screen.queryByTestId('chart-crosshair')).toBeNull()
    expect(screen.queryByTestId('chart-tooltip')).toBeNull()
    expect(second).toHaveAttribute('aria-pressed', 'true')
    expect(document.activeElement).toBe(second)
  })

  it('shows the tooltip on pointer hover without moving keyboard focus', () => {
    const { container } = renderChart()
    const canvas = screen.getByTestId('chart-canvas')
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
      width: 760,
      height: 410,
      left: 0,
      top: 0,
      right: 760,
      bottom: 410,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect)

    const first = barButton(/2026-10-01 00:00/i)
    first.focus()
    expect(document.activeElement).toBe(first)

    // x in SVG units maps to the second bar for slot width (760 - 24) / 3.
    // A MouseEvent carries clientX in jsdom (PointerEvent may not).
    fireEvent(canvas, new MouseEvent('pointermove', { clientX: 300, bubbles: true }))

    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('69998.00')
    expect(document.activeElement).toBe(first)
    expect(container.querySelector('.chart-tooltip')).not.toBeNull()
  })

  it('introduces no motion and keeps the overlay styles inert under reduced motion', () => {
    renderChart({ zones: [LONG_ZONE], markers: MARKERS, gaps: [GAP] })

    const tooltipRules = readFileSync('src/styles/app.css', 'utf8')
    expect(tooltipRules).toContain('@media (prefers-reduced-motion: reduce)')
    const tooltipBlock = tooltipRules.slice(tooltipRules.indexOf('.chart-tooltip {'))
    expect(tooltipBlock.slice(0, tooltipBlock.indexOf('}'))).not.toMatch(
      /(animation|transition):/,
    )
  })
})

function mockCanvasRect(canvas: HTMLElement) {
  return vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
    width: 760,
    height: 410,
    left: 0,
    top: 0,
    right: 760,
    bottom: 410,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect)
}

function hoverCanvas(canvas: HTMLElement, clientX: number) {
  fireEvent(canvas, new MouseEvent('pointermove', { clientX, bubbles: true }))
}

describe('overlay text alternative', () => {
  it('describes every zone bound, marker price, and gap from the server props', () => {
    renderChart({ zones: [LONG_ZONE], markers: MARKERS, gaps: [GAP] })

    const figure = screen.getByRole('figure', { name: /BTCUSDT 4h chart/i })
    const summary = screen.getByTestId('chart-overlay-summary')
    expect(figure.getAttribute('aria-describedby')?.split(' ')).toContain(summary.id)
    expect(summary).toHaveTextContent(/state TRIGGERED/)
    expect(summary).toHaveTextContent(/low 69400.00/)
    expect(summary).toHaveTextContent(/high 70400.00/)
    expect(summary).toHaveTextContent(/trigger 70400.00/)
    expect(summary).toHaveTextContent(/entry marker at 69900.00/)
    expect(summary).toHaveTextContent(/stop marker at 69500.00/)
    expect(summary).toHaveTextContent(/target marker at 70500.00/)
    expect(summary).toHaveTextContent(/Gap from 2026-10-01T04:00:00.000Z/)
    // It is a static alternative, never a second live region.
    expect(summary).not.toHaveAttribute('aria-live')
  })

  it('states the empty overlay case instead of leaving a dangling description', () => {
    renderChart()

    expect(screen.getByTestId('chart-overlay-summary')).toHaveTextContent(
      /No server-derived opportunity zones, position markers, or gaps/i,
    )
  })
})

describe('pointer hover stays independent from selection', () => {
  it('moves only the crosshair/tooltip, never aria-pressed, tab stop, or onSelectBar', () => {
    const onSelectBar = vi.fn()
    render(
      <Chart
        symbol="BTCUSDT"
        timeframe="4h"
        bars={BARS}
        metrics={METRICS}
        quality="COMPLETE"
        onSelectBar={onSelectBar}
      />,
    )
    const canvas = screen.getByTestId('chart-canvas')
    mockCanvasRect(canvas)
    const first = screen.getByRole('button', { name: /2026-10-01 00:00/i })
    const last = screen.getByRole('button', { name: /2026-10-01 08:00/i })
    first.focus()
    expect(last).toHaveAttribute('aria-pressed', 'true')

    hoverCanvas(canvas, 300)

    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('69998.00')
    expect(onSelectBar).not.toHaveBeenCalled()
    expect(last).toHaveAttribute('aria-pressed', 'true')
    expect(last).toHaveAttribute('tabindex', '0')
    expect(first).toHaveAttribute('aria-pressed', 'false')
    expect(document.activeElement).toBe(first)

    // A keyboard commit still selects and calls the parent callback.
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    expect(onSelectBar).toHaveBeenCalledWith(BARS[1].bar_start)
    expect(screen.getByRole('button', { name: /2026-10-01 04:00/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('reads the canvas rect once across pointer moves', () => {
    renderChart()
    const canvas = screen.getByTestId('chart-canvas')
    const rect = mockCanvasRect(canvas)

    hoverCanvas(canvas, 300)
    hoverCanvas(canvas, 400)

    expect(rect).toHaveBeenCalledTimes(1)
  })

  it('keeps the static plate nodes across a crosshair move', () => {
    renderChart()
    const canvas = screen.getByTestId('chart-canvas')
    mockCanvasRect(canvas)
    const candles = screen.getByTestId('chart-candles')

    hoverCanvas(canvas, 300)

    expect(screen.getByTestId('chart-candles')).toBe(candles)
  })
})

describe('crosshair dismissal', () => {
  it('dismisses a pointer-opened crosshair with Escape from anywhere', () => {
    renderChart()
    const canvas = screen.getByTestId('chart-canvas')
    mockCanvasRect(canvas)
    hoverCanvas(canvas, 300)
    expect(screen.getByTestId('chart-tooltip')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByTestId('chart-tooltip')).toBeNull()
    expect(screen.queryByTestId('chart-crosshair')).toBeNull()
  })

  it('clears the crosshair when the pointer leaves the canvas', () => {
    renderChart()
    const canvas = screen.getByTestId('chart-canvas')
    mockCanvasRect(canvas)
    hoverCanvas(canvas, 300)
    expect(screen.getByTestId('chart-tooltip')).toBeInTheDocument()

    fireEvent.pointerLeave(canvas)

    expect(screen.queryByTestId('chart-tooltip')).toBeNull()
  })
})

describe('rendered quality stamps', () => {
  function selectSecondBar() {
    const first = screen.getByRole('button', { name: /2026-10-01 00:00/i })
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowRight' })
  }

  it('stamps a gap bar MISSING in the tooltip, table, and live region', async () => {
    renderChart({ gaps: [GAP] })
    fireEvent.click(screen.getByRole('button', { name: /view data table/i }))
    selectSecondBar()

    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('MISSING')
    const table = screen.getByRole('table', { name: /chart data table/i })
    const current = within(table)
      .getAllByRole('row')
      .find((row) => row.getAttribute('aria-current') === 'true')
    expect(current).toHaveTextContent('MISSING')
    const live = document.querySelector('[aria-live="polite"]') as HTMLElement
    await waitFor(() => expect(live).toHaveTextContent(/Quality MISSING/))
  })

  it('stamps STALE from the chart-level series quality', () => {
    renderChart({ quality: 'STALE' })
    selectSecondBar()

    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('STALE')
  })

  it('stamps ESTIMATED from an exact server reason token', () => {
    renderChart({ quality: 'COMPLETE', qualityReasons: ['CVD estimated'] })
    selectSecondBar()

    expect(screen.getByTestId('chart-tooltip')).toHaveTextContent('ESTIMATED')
  })
})

describe('announcement scope', () => {
  it('does not announce the default bar on mount', () => {
    vi.useFakeTimers()
    try {
      renderChart()
      const live = document.querySelector('.chart-frame [aria-live="polite"]') as HTMLElement
      act(() => {
        vi.advanceTimersByTime(600)
      })
      expect(live.textContent).toBe('')
    } finally {
      vi.useRealTimers()
    }
  })
})
