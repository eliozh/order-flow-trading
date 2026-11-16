import { memo, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'

import type { ChartMarker, ChartZone } from '../api/charts'
import type { DataGap, SnapshotBar } from '../api/reports'
import { signedDecimal, utcLabel } from '../api/reports'
import { ChartCrosshair, ChartTooltip, crosshairSummary } from './chart-crosshair'
import {
  CVD_HEIGHT,
  CVD_TOP,
  ChartPlate,
  HEIGHT,
  PADDING,
  PRICE_HEIGHT,
  PRICE_TOP,
  WIDTH,
  numeric,
  yForPrice,
  type ChartMetrics,
} from './chart-plate'
import { barQualityStamp, clampLabel, numericPrice } from './chart-overlays'
import { DataQualityBadge } from './data-quality'
import { FootprintDock } from './footprint-dock'
import { ProvenanceStamp } from './provenance'

interface ChartProps {
  symbol: string
  timeframe: string
  bars: SnapshotBar[]
  metrics: ChartMetrics
  quality: string
  qualityReasons?: string[]
  selectedBarStart?: string
  onSelectBar?: (barStart: string) => void
  /**
   * Additive, optional entry marker: one horizontal rule at this price and a
   * text label. It adds no indicator, pane, or series.
   */
  entryPrice?: string | number | null
  /** Server-derived opportunity zones and trigger lines (never inferred). */
  zones?: ChartZone[]
  /** Server-derived entry/exit/stop/target markers (never inferred). */
  markers?: ChartMarker[]
  /** Server-declared data gaps, drawn as breaks instead of interpolated data. */
  gaps?: DataGap[]
  /**
   * True when a linked read or the marker cap clipped the overlay list; the
   * hidden overlay summary then says so instead of implying a complete set.
   */
  overlaysTruncated?: boolean
  /**
   * Test-only render instrumentation forwarded to the memoized plate. Production
   * callers leave it undefined; tests use it to prove the plate does not rebuild
   * on an equal-data poll.
   */
  onPlateRender?: () => void
}

// Stable empty defaults: a `[]` literal in the parameter list would get a new
// identity on every render and invalidate the range memo (and, for gaps and
// reasons, the announcement effect).
const EMPTY_ZONES: ChartZone[] = []
const EMPTY_MARKERS: ChartMarker[] = []
const EMPTY_GAPS: DataGap[] = []
const EMPTY_REASONS: string[] = []

/**
 * The drawn price domain. Entry, zone, and marker prices are folded in so an
 * overlay is always inside the plotted range (the report snapshot predates the
 * entry, so the entry is often above or below every bar).
 */
function priceRange(
  bars: SnapshotBar[],
  anchors: Array<number | null> = [],
): { min: number; max: number } {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const bar of bars) {
    min = Math.min(min, numeric(bar.low))
    max = Math.max(max, numeric(bar.high))
  }
  for (const anchor of anchors) {
    if (anchor !== null && Number.isFinite(anchor)) {
      min = Math.min(min, anchor)
      max = Math.max(max, anchor)
    }
  }
  const pad = Math.max((max - min) * 0.08, 0.01)
  return { min: min - pad, max: max + pad }
}

function signedText(value: string): string {
  return signedDecimal(value)
}

interface ChartCanvasProps {
  bars: SnapshotBar[]
  metrics: ChartMetrics
  zones: ChartZone[]
  markers: ChartMarker[]
  gaps: DataGap[]
  quality: string
  qualityReasons: string[]
  range: { min: number; max: number }
  slot: number
  candleWidth: number
  hatchId: string
  anchoredEntry: number | null
  entryText: string | null
  /** Keyboard/click crosshair. Pointer hover takes precedence while active. */
  crosshairBarStart?: string
  /** Incremented by a keyboard/click selection; clears a stale pointer hover. */
  selectionToken: number
  onPlateRender?: () => void
}

/**
 * The interactive canvas layer: axis labels, the memoized plate, the
 * crosshair/tooltip, and the pointer hover state. Kept separate and memoized so
 * a pointer move re-renders only this layer; the chart frame, legend, and data
 * table in the parent stay identity-stable.
 */
const ChartCanvas = memo(function ChartCanvas({
  bars,
  metrics,
  zones,
  markers,
  gaps,
  quality,
  qualityReasons,
  range,
  slot,
  candleWidth,
  hatchId,
  anchoredEntry,
  entryText,
  crosshairBarStart,
  selectionToken,
  onPlateRender,
}: ChartCanvasProps) {
  const [hoverBarStart, setHoverBarStart] = useState<string | undefined>(undefined)
  // Cache the canvas rect across pointermove events; scroll/resize invalidate it
  // so pointer-to-bar mapping cannot drift while the page moves underneath.
  const canvasRectRef = useRef<DOMRect | null>(null)

  // A keyboard/click selection wins over a stale hover, so the tooltip never
  // shows a different bar than the one the live region just announced.
  useEffect(() => {
    setHoverBarStart(undefined)
  }, [selectionToken])

  useEffect(() => {
    function invalidate() {
      canvasRectRef.current = null
    }
    window.addEventListener('scroll', invalidate, true)
    window.addEventListener('resize', invalidate)
    return () => {
      window.removeEventListener('scroll', invalidate, true)
      window.removeEventListener('resize', invalidate)
    }
  }, [])

  // Escape dismisses a pointer-opened crosshair even when no bar button has
  // focus; the bar-level Escape handler covers keyboard semantics.
  useEffect(() => {
    if (hoverBarStart === undefined) {
      return
    }
    function handleDocumentKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === 'Escape') {
        setHoverBarStart(undefined)
      }
    }
    document.addEventListener('keydown', handleDocumentKeyDown)
    return () => document.removeEventListener('keydown', handleDocumentKeyDown)
  }, [hoverBarStart])

  function handlePointerEnter(event: PointerEvent<HTMLDivElement>) {
    canvasRectRef.current = event.currentTarget.getBoundingClientRect()
  }

  function handlePointerLeave() {
    canvasRectRef.current = null
    setHoverBarStart(undefined)
  }

  /**
   * Pointer hover moves only the crosshair/tooltip and never commits the
   * selection, so `aria-pressed` and the roving tab stop stay with the
   * keyboard/click selection and focus survives a mouse pass.
   */
  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    const rect = canvasRectRef.current ?? event.currentTarget.getBoundingClientRect()
    canvasRectRef.current = rect
    const clientX = event.clientX
    if (rect.width <= 0 || bars.length === 0 || !Number.isFinite(clientX)) {
      return
    }
    const svgX = ((clientX - rect.left) / rect.width) * WIDTH
    const rawIndex = Math.floor((svgX - PADDING) / slot)
    const index = Math.min(Math.max(rawIndex, 0), bars.length - 1)
    const barStart = bars[index].bar_start
    if (barStart !== hoverBarStart) {
      setHoverBarStart(barStart)
    }
  }

  const effectiveBarStart = hoverBarStart ?? crosshairBarStart
  const crosshairBar =
    effectiveBarStart === undefined
      ? undefined
      : (bars.find((bar) => bar.bar_start === effectiveBarStart) ?? undefined)
  const crosshairIndex =
    crosshairBar === undefined
      ? -1
      : bars.findIndex((bar) => bar.bar_start === crosshairBar.bar_start)
  const crosshairX = crosshairIndex < 0 ? null : PADDING + slot * crosshairIndex + slot / 2

  return (
    <div
      className="chart-canvas"
      onPointerEnter={handlePointerEnter}
      onPointerMove={handlePointerMove}
      onPointerLeave={handlePointerLeave}
      data-testid="chart-canvas"
    >
      <div className="chart-axis-labels" data-testid="chart-axis-labels" aria-hidden="true">
        <span
          data-testid="price-label-max"
          style={{ top: `${(PRICE_TOP / HEIGHT) * 100}%`, transform: 'translateY(-50%)' }}
        >
          {range.max.toFixed(2)}
        </span>
        <span
          data-testid="price-label-mid"
          style={{
            top: `${((PRICE_TOP + PRICE_HEIGHT / 2) / HEIGHT) * 100}%`,
            transform: 'translateY(-50%)',
          }}
        >
          {((range.max + range.min) / 2).toFixed(2)}
        </span>
        <span
          data-testid="price-label-min"
          style={{
            top: `${((PRICE_TOP + PRICE_HEIGHT) / HEIGHT) * 100}%`,
            transform: 'translateY(-50%)',
          }}
        >
          {range.min.toFixed(2)}
        </span>
      </div>
      <svg
        className="chart-svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="presentation"
        focusable="false"
        aria-hidden="true"
        data-testid="chart-plot"
      >
        <ChartPlate
          bars={bars}
          metrics={metrics}
          zones={zones}
          markers={markers}
          gaps={gaps}
          anchoredEntry={anchoredEntry}
          entryText={entryText}
          range={range}
          slot={slot}
          candleWidth={candleWidth}
          hatchId={hatchId}
          onPlateRender={onPlateRender}
        />
        {crosshairBar !== undefined && crosshairX !== null && (
          <ChartCrosshair
            x={crosshairX}
            y={yForPrice(numeric(crosshairBar.close), range)}
            top={PRICE_TOP}
            bottom={CVD_TOP + CVD_HEIGHT}
            width={WIDTH}
            padding={PADDING}
          />
        )}
      </svg>
      {crosshairBar !== undefined && crosshairX !== null && (
        <ChartTooltip
          bar={crosshairBar}
          gaps={gaps}
          quality={quality}
          qualityReasons={qualityReasons}
          x={crosshairX}
          width={WIDTH}
        />
      )}
    </div>
  )
})

function ChartComponent({
  symbol,
  timeframe,
  bars,
  metrics,
  quality,
  qualityReasons = EMPTY_REASONS,
  selectedBarStart,
  onSelectBar,
  entryPrice = null,
  zones = EMPTY_ZONES,
  markers = EMPTY_MARKERS,
  gaps = EMPTY_GAPS,
  overlaysTruncated = false,
  onPlateRender,
}: ChartProps) {
  const plotId = useId()
  const tableId = `${plotId}-table`
  const summaryId = `${plotId}-summary`
  const overlaySummaryId = `${plotId}-overlays`
  const barHelpId = `${plotId}-bar-help`
  const hatchId = `${plotId}-hatch`
  const [internalSelection, setInternalSelection] = useState<string | undefined>(undefined)
  const [crosshairBarStart, setCrosshairBarStart] = useState<string | undefined>(undefined)
  // Bumped on every keyboard/click selection so the pointer layer can drop a
  // stale hover before the live region announces the new selection.
  const [selectionToken, setSelectionToken] = useState(0)
  const [showTable, setShowTable] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  // Only a user-initiated selection warrants a bar announcement. The initial
  // mount and a parent-driven period switch are announced once by the panel's
  // own polite region, so two polite regions never queue back-to-back.
  const hasUserSelectionRef = useRef(false)
  const selected = selectedBarStart ?? internalSelection ?? bars[bars.length - 1]?.bar_start

  useEffect(() => {
    if (!selected || !hasUserSelectionRef.current) {
      return
    }
    const timer = setTimeout(() => {
      const bar = bars.find((candidate) => candidate.bar_start === selected)
      if (bar) {
        setAnnouncement(
          `${symbol} ${timeframe}. ${bars.length} bars. ` +
            crosshairSummary(bar, gaps, quality, qualityReasons),
        )
      }
    }, 500)
    return () => clearTimeout(timer)
  }, [bars, selected, symbol, timeframe, gaps, quality, qualityReasons])

  // Escape must dismiss a keyboard/click crosshair even when focus has moved
  // away from the bar buttons (the descriptions promise this); the bar-level
  // handler still covers the focused-button case.
  useEffect(() => {
    if (crosshairBarStart === undefined) {
      return
    }
    function handleDocumentKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === 'Escape') {
        setCrosshairBarStart(undefined)
      }
    }
    document.addEventListener('keydown', handleDocumentKeyDown)
    return () => document.removeEventListener('keydown', handleDocumentKeyDown)
  }, [crosshairBarStart])

  const entryValue =
    entryPrice === null || entryPrice === undefined || entryPrice === '' ? null : Number(entryPrice)
  const entryText = entryValue === null ? null : String(entryPrice)
  const anchoredEntry = entryValue !== null && Number.isFinite(entryValue) ? entryValue : null
  // The drawn price domain. Entry, zone, and marker prices are folded in so an
  // overlay is always inside the plotted range. Memoized so the crosshair layer
  // can re-render without giving the memoized plate a new range identity.
  const range = useMemo(() => {
    const zoneAnchors = zones.flatMap((zone) => [
      numericPrice(zone.low),
      numericPrice(zone.high),
      numericPrice(zone.trigger_price),
    ])
    const markerAnchors = markers.map((marker) => numericPrice(marker.price))
    return priceRange(bars, [anchoredEntry, ...zoneAnchors, ...markerAnchors])
  }, [bars, zones, markers, anchoredEntry])

  if (quality === 'MISSING' || bars.length === 0) {
    return (
      <section className="panel chart-missing" role="note" aria-label="Chart unavailable">
        <h2>No chart for this report</h2>
        <p>
          The market data required for a chart is MISSING. No candles, volume, CVD, volume profile,
          or footprint are drawn from invented values.
        </p>
        <p className="panel-foot">
          Run a completed backfill first, then re-run the metrics-only scan.
        </p>
        <p>
          Data quality: {quality}. {qualityReasons.join(' ')}
        </p>
      </section>
    )
  }

  const slot = (WIDTH - PADDING * 2) / bars.length
  const candleWidth = Math.max(slot * 0.55, 3)
  const selectedBar = bars.find((bar) => bar.bar_start === selected) ?? bars[bars.length - 1]
  const footprint = metrics.footprint.bars.find((bar) => bar.bar_start === selectedBar.bar_start)

  function selectBar(barStart: string) {
    hasUserSelectionRef.current = true
    setInternalSelection(barStart)
    // The crosshair is hidden initially and appears on the first selection or
    // pointer hover; Escape hides it again.
    setCrosshairBarStart(barStart)
    setSelectionToken((value) => value + 1)
    onSelectBar?.(barStart)
  }

  function handleBarKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const index = Number(event.currentTarget.dataset.index)
    if (event.key === 'Escape') {
      event.preventDefault()
      setCrosshairBarStart(undefined)
      return
    }
    let next = index
    if (event.key === 'ArrowRight') {
      next = Math.min(index + 1, bars.length - 1)
    } else if (event.key === 'ArrowLeft') {
      next = Math.max(index - 1, 0)
    } else if (event.key === 'Home') {
      next = 0
    } else if (event.key === 'End') {
      next = bars.length - 1
    } else {
      return
    }
    event.preventDefault()
    selectBar(bars[next].bar_start)
    const controls =
      event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('.bar-select')
    controls?.[next]?.focus()
  }

  return (
    <figure
      className="chart-frame"
      lang="en"
      aria-label={`${symbol} ${timeframe} chart`}
      aria-describedby={`${summaryId} ${overlaySummaryId}`}
    >
      <figcaption id={summaryId} className="visually-hidden">
        Candlestick chart with {bars.length} bars. Price range {range.min.toFixed(2)} to{' '}
        {range.max.toFixed(2)}. Volume, CVD, volume profile, and footprint are shown; the data table
        carries the same values.
        {anchoredEntry !== null && entryText !== null ? ` Entry marker at ${entryText}.` : ''}{' '}
        Keyboard: Left and Right move the crosshair, Home and End jump to the first and last bar,
        Escape clears the crosshair.
      </figcaption>
      {/*
        Bounded text alternative for the overlay values that exist only inside
        the aria-hidden SVG. It is static (server props), so it never competes
        with the debounced polite region that announces the selected bar.
      */}
      <div
        id={overlaySummaryId}
        className="visually-hidden"
        data-testid="chart-overlay-summary"
      >
        {zones.length === 0 && markers.length === 0 && gaps.length === 0 && !overlaysTruncated ? (
          <p>No server-derived opportunity zones, position markers, or gaps.</p>
        ) : (
          <>
            <p>Chart overlays from the server payload.</p>
            {overlaysTruncated && (
              <p>Overlay lists were truncated to the server bound.</p>
            )}
            {zones.length > 0 && (
              <ul>
                {zones.map((zone) => (
                  <li key={`overlay-zone-${zone.opportunity_id}`}>
                    Zone {clampLabel(zone.label)}, state {zone.state}, low {zone.low}, high{' '}
                    {zone.high}, trigger {zone.trigger_price}.
                  </li>
                ))}
              </ul>
            )}
            {markers.length > 0 && (
              <ul>
                {markers.map((marker, index) => (
                  <li key={`overlay-marker-${marker.kind}-${marker.price}-${index}`}>
                    {marker.kind} marker at {marker.price}.
                  </li>
                ))}
              </ul>
            )}
            {gaps.length > 0 && (
              <ul>
                {gaps.map((gap, index) => (
                  <li key={`overlay-gap-${gap.gap_id ?? index}`}>
                    Gap from {gap.start} to {gap.end}.
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <div className="chart-head">
        <span className="chart-symbol">{symbol}</span>
        <span className="mono-muted">{timeframe} · UTC</span>
        <ProvenanceStamp kind="CALC" />
        <DataQualityBadge status={quality} reasons={qualityReasons} />
        <span className="grow" />
        <span className="mono-muted">
          last {selectedBar.close} · Δ {signedText(selectedBar.delta)}
        </span>
      </div>

      <p className="visually-hidden" aria-live="polite">
        {announcement}
      </p>

      <div className="chart-toolbar">
        <span className="mono-muted">Candles + volume + CVD + VP + footprint</span>
        <button
          type="button"
          className="control-btn"
          aria-expanded={showTable}
          aria-controls={tableId}
          onClick={() => setShowTable((value) => !value)}
        >
          View data table
        </button>
      </div>

      <div className="chart-body">
        <div
          className="chart-plot"
          role="img"
          aria-label={`${symbol} ${timeframe} price chart`}
          aria-describedby={showTable ? tableId : undefined}
        >
          <div className="chart-pane-labels" aria-hidden="true">
            <span>Price</span>
            <span>Volume</span>
            <span>CVD</span>
            <span data-testid="cvd-zero-label">0</span>
            <span>POC {metrics.volume_profile.poc}</span>
          </div>
          <ChartCanvas
            bars={bars}
            metrics={metrics}
            zones={zones}
            markers={markers}
            gaps={gaps}
            quality={quality}
            qualityReasons={qualityReasons}
            range={range}
            slot={slot}
            candleWidth={candleWidth}
            hatchId={hatchId}
            anchoredEntry={anchoredEntry}
            entryText={entryText}
            crosshairBarStart={crosshairBarStart}
            selectionToken={selectionToken}
            onPlateRender={onPlateRender}
          />
        </div>

        <FootprintDock
          bar={footprint ?? null}
          timeframe={timeframe}
          selectedLabel={`${utcLabel(selectedBar.bar_start)} UTC`}
        />
      </div>

      <p id={barHelpId} className="visually-hidden">
        Use Left and Right to move the crosshair, Home and End to jump to the first and last bar,
        and Escape to clear the crosshair.
      </p>
      <div
        className="chart-bars"
        role="group"
        aria-label="Bar selection"
        aria-describedby={barHelpId}
      >
        {bars.map((bar, index) => {
          const label = `${utcLabel(bar.bar_start)} UTC`
          const isSelected = bar.bar_start === selectedBar.bar_start
          const up = numeric(bar.close) >= numeric(bar.open)
          const direction = up ? 'up' : 'down'
          return (
            <button
              key={bar.bar_start}
              type="button"
              className={isSelected ? 'bar-select bar-select--selected' : 'bar-select'}
              data-index={index}
              tabIndex={isSelected ? 0 : -1}
              aria-pressed={isSelected}
              aria-label={`${direction} ${label} open ${bar.open} high ${bar.high} low ${bar.low} close ${bar.close}`}
              onClick={() => selectBar(bar.bar_start)}
              onKeyDown={handleBarKeyDown}
            >
              {up ? '▲ ' : '▼ '}
              {label.slice(5)}
            </button>
          )
        })}
      </div>

      <div className="chart-legend">
        <span>
          <span className="swatch swatch--up" aria-hidden="true" /> up
        </span>
        <span>
          <span className="swatch swatch--down" aria-hidden="true" /> down
        </span>
        <span>up bars: filled bar with top cap · down bars: outline bar</span>
        <span>zone state: OPEN · TRIGGERED · EXPIRED · INVALIDATED (distinct dash + word)</span>
        <span>markers: triangle entry · square exit · × stop · ○ target</span>
        <span>gaps: hatched break, never interpolated</span>
        <span>POC</span>
        <span>POC highlighted bin · value-area bins in table</span>
      </div>

      {showTable && (
        <div
          className="table-wrap chart-table-wrap"
          role="region"
          aria-label="Chart data table"
          // Scrollable regions need a focus stop so keyboard users can pan them.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          {' '}
          <table className="data-table chart-table" id={tableId} aria-label="Chart data table">
            <caption className="visually-hidden">
              Chart data table with the same values drawn in the chart ({plotId}).
            </caption>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">O</th>
                <th scope="col">H</th>
                <th scope="col">L</th>
                <th scope="col">C</th>
                <th scope="col">Vol</th>
                <th scope="col">Delta</th>
                <th scope="col">CVD</th>
                <th scope="col">Dir</th>
                <th scope="col">Quality</th>
              </tr>
            </thead>
            <tbody>
              {bars.map((bar) => (
                <tr
                  key={`row-${bar.bar_start}`}
                  aria-current={bar.bar_start === selectedBar.bar_start ? 'true' : undefined}
                  data-selected={bar.bar_start === selectedBar.bar_start ? 'true' : undefined}
                  className={
                    bar.bar_start === selectedBar.bar_start ? 'chart-row--selected' : undefined
                  }
                >
                  <td className="mono">{utcLabel(bar.bar_start)}</td>
                  <td className="mono">{bar.open}</td>
                  <td className="mono">{bar.high}</td>
                  <td className="mono">{bar.low}</td>
                  <td className="mono">{bar.close}</td>
                  <td className="mono">{bar.volume}</td>
                  <td className={bar.delta.startsWith('-') ? 'value-down' : 'value-up'}>
                    {signedText(bar.delta)}
                  </td>
                  <td className={bar.cvd.startsWith('-') ? 'value-down' : 'value-up'}>
                    {signedText(bar.cvd)}
                  </td>
                  <td>{numeric(bar.close) >= numeric(bar.open) ? 'up' : 'down'}</td>
                  <td className="mono">
                    {barQualityStamp(bar, gaps, quality, qualityReasons)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="data-table chart-vp-table" aria-label="Volume profile data table">
            <caption className="visually-hidden">
              Volume profile bins with price range, volume, POC, and value-area membership.
            </caption>
            <thead>
              <tr>
                <th scope="col">Price low</th>
                <th scope="col">Price high</th>
                <th scope="col">Volume</th>
                <th scope="col">POC</th>
                <th scope="col">Value area</th>
              </tr>
            </thead>
            <tbody>
              {metrics.volume_profile.bins.map((bin) => (
                <tr key={`vp-row-${bin.price_low}-${bin.price_high}`}>
                  <td className="mono">{bin.price_low}</td>
                  <td className="mono">{bin.price_high}</td>
                  <td className="mono">{bin.volume}</td>
                  <td>{bin.is_poc ? 'POC' : '—'}</td>
                  <td>{bin.in_value_area ? 'yes' : 'no'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </figure>
  )
}

/**
 * Memoized so the 5s position revalidation does not rebuild the whole SVG
 * tree; the report/snapshot props are not replaced by a poll.
 */
export const Chart = memo(ChartComponent)
