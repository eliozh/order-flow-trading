import { memo, useEffect, useId, useState } from 'react'
import type { KeyboardEvent } from 'react'

import type { FootprintSection, SnapshotBar, VolumeProfileSection } from '../api/reports'
import { signedDecimal, utcLabel } from '../api/reports'
import { DataQualityBadge } from './data-quality'
import { FootprintDock } from './footprint-dock'
import { ProvenanceStamp } from './provenance'

/**
 * Structural subset of a metrics document that the chart actually draws, so a
 * frozen report document and a chart payload both fit without casts.
 */
interface ChartMetrics {
  footprint: Pick<FootprintSection, 'bars'>
  volume_profile: Pick<VolumeProfileSection, 'bins' | 'poc'>
}

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
}

const WIDTH = 760
const PRICE_TOP = 18
const PRICE_HEIGHT = 210
const VOLUME_TOP = 250
const VOLUME_HEIGHT = 58
const CVD_TOP = 324
const CVD_HEIGHT = 64
const HEIGHT = 410
const PADDING = 12

function numeric(value: string | null): number {
  if (value === null) {
    return 0
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

/**
 * The drawn price domain. An entry marker price is folded into the domain so
 * the marker is always inside the plotted range (the report snapshot predates
 * entry, so the entry is often above or below every bar).
 */
function priceRange(
  bars: SnapshotBar[],
  anchor: number | null = null,
): { min: number; max: number } {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const bar of bars) {
    min = Math.min(min, numeric(bar.low))
    max = Math.max(max, numeric(bar.high))
  }
  if (anchor !== null && Number.isFinite(anchor)) {
    min = Math.min(min, anchor)
    max = Math.max(max, anchor)
  }
  const pad = Math.max((max - min) * 0.08, 0.01)
  return { min: min - pad, max: max + pad }
}

function yForPrice(value: number, range: { min: number; max: number }): number {
  const span = range.max - range.min || 1
  return PRICE_TOP + ((range.max - value) / span) * PRICE_HEIGHT
}

function signedText(value: string): string {
  return signedDecimal(value)
}

function ChartComponent({
  symbol,
  timeframe,
  bars,
  metrics,
  quality,
  qualityReasons = [],
  selectedBarStart,
  onSelectBar,
  entryPrice = null,
}: ChartProps) {
  const plotId = useId()
  const tableId = `${plotId}-table`
  const summaryId = `${plotId}-summary`
  const [internalSelection, setInternalSelection] = useState<string | undefined>(undefined)
  const [showTable, setShowTable] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const selected = selectedBarStart ?? internalSelection ?? bars[bars.length - 1]?.bar_start

  useEffect(() => {
    if (!selected) {
      return
    }
    const timer = setTimeout(() => {
      const bar = bars.find((candidate) => candidate.bar_start === selected)
      if (bar) {
        setAnnouncement(
          `${symbol} ${timeframe}. ${bars.length} bars. Selected ${utcLabel(bar.bar_start)} UTC, ` +
            `close ${bar.close}, delta ${signedText(bar.delta)}.`,
        )
      }
    }, 500)
    return () => clearTimeout(timer)
  }, [bars, selected, symbol, timeframe])

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

  const entryValue =
    entryPrice === null || entryPrice === undefined || entryPrice === '' ? null : Number(entryPrice)
  const entryText = entryValue === null ? null : String(entryPrice)
  const anchoredEntry = entryValue !== null && Number.isFinite(entryValue) ? entryValue : null
  const range = priceRange(bars, anchoredEntry)
  // The entry is part of the price domain above, so a finite entry always has a
  // Y position and an off-range entry renders as an in-scale marker.
  const entryY = anchoredEntry === null ? null : yForPrice(anchoredEntry, range)
  const slot = (WIDTH - PADDING * 2) / bars.length
  const candleWidth = Math.max(slot * 0.55, 3)
  const selectedBar = bars.find((bar) => bar.bar_start === selected) ?? bars[bars.length - 1]
  const footprint = metrics.footprint.bars.find((bar) => bar.bar_start === selectedBar.bar_start)
  let cvdMin = 0
  let cvdMax = 0
  let maxVolume = 0.000001
  for (const bar of bars) {
    const cvd = numeric(bar.cvd)
    cvdMin = Math.min(cvdMin, cvd)
    cvdMax = Math.max(cvdMax, cvd)
    maxVolume = Math.max(maxVolume, numeric(bar.volume))
  }
  const cvdSpan = cvdMax - cvdMin || 1
  let maxProfileVolume = 0.000001
  for (const bin of metrics.volume_profile.bins) {
    maxProfileVolume = Math.max(maxProfileVolume, numeric(bin.volume))
  }

  function selectBar(barStart: string) {
    setInternalSelection(barStart)
    onSelectBar?.(barStart)
  }

  function handleBarKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const index = Number(event.currentTarget.dataset.index)
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
      aria-describedby={summaryId}
    >
      <figcaption id={summaryId} className="visually-hidden">
        Candlestick chart with {bars.length} bars. Price range {range.min.toFixed(2)} to{' '}
        {range.max.toFixed(2)}. Volume, CVD, volume profile, and footprint are shown; the data table
        carries the same values.
        {entryY !== null && entryText !== null ? ` Entry marker at ${entryText}.` : ''}
      </figcaption>
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
          <div className="chart-canvas">
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
              <g className="chart-grid">
                <line
                  data-testid="grid-price-top"
                  x1={PADDING}
                  y1={PRICE_TOP}
                  x2={WIDTH - PADDING}
                  y2={PRICE_TOP}
                />
                <line
                  data-testid="grid-price-mid"
                  x1={PADDING}
                  y1={PRICE_TOP + PRICE_HEIGHT / 2}
                  x2={WIDTH - PADDING}
                  y2={PRICE_TOP + PRICE_HEIGHT / 2}
                />
                <line
                  data-testid="grid-price-bottom"
                  x1={PADDING}
                  y1={PRICE_TOP + PRICE_HEIGHT}
                  x2={WIDTH - PADDING}
                  y2={PRICE_TOP + PRICE_HEIGHT}
                />
              </g>
              <g data-testid="chart-candles">
                {bars.map((bar, index) => {
                  const x = PADDING + slot * index + slot / 2
                  const openY = yForPrice(numeric(bar.open), range)
                  const closeY = yForPrice(numeric(bar.close), range)
                  const up = numeric(bar.close) >= numeric(bar.open)
                  const top = Math.min(openY, closeY)
                  const bodyHeight = Math.max(Math.abs(closeY - openY), 1)
                  return (
                    <g
                      key={bar.bar_start}
                      className={up ? 'candle-up' : 'candle-down'}
                      data-direction={up ? 'up' : 'down'}
                    >
                      <line
                        x1={x}
                        y1={yForPrice(numeric(bar.high), range)}
                        x2={x}
                        y2={yForPrice(numeric(bar.low), range)}
                      />
                      <rect
                        x={x - candleWidth / 2}
                        y={top}
                        width={candleWidth}
                        height={bodyHeight}
                      />
                    </g>
                  )
                })}
              </g>
              {entryY !== null && entryText !== null && (
                <g className="chart-entry-marker" data-testid="chart-entry-marker">
                  <line
                    x1={PADDING}
                    y1={entryY}
                    x2={WIDTH - PADDING}
                    y2={entryY}
                    data-testid="chart-entry-line"
                  />
                  <text
                    x={WIDTH - PADDING - 4}
                    y={entryY - 4}
                    textAnchor="end"
                    className="chart-label"
                    data-testid="chart-entry-label"
                  >
                    ENTRY {entryText}
                  </text>
                </g>
              )}
              <g data-testid="chart-volume-profile">
                {metrics.volume_profile.bins.map((bin) => {
                  const width = (numeric(bin.volume) / maxProfileVolume) * 110
                  const midPrice = (numeric(bin.price_low) + numeric(bin.price_high)) / 2
                  const y = Math.min(
                    Math.max(yForPrice(midPrice, range) - 5, PRICE_TOP),
                    PRICE_TOP + PRICE_HEIGHT - 10,
                  )
                  return (
                    <rect
                      key={`${bin.price_low}-${bin.price_high}`}
                      className={bin.is_poc ? 'vp-bin vp-bin--poc' : 'vp-bin'}
                      x={PADDING}
                      y={y}
                      width={Math.max(width, 1)}
                      height={10}
                    />
                  )
                })}
              </g>
              <g data-testid="chart-volume">
                {bars.map((bar, index) => {
                  const height = (numeric(bar.volume) / maxVolume) * VOLUME_HEIGHT
                  const x = PADDING + slot * index + slot / 2
                  const up = numeric(bar.delta) >= 0
                  const barTop = VOLUME_TOP + VOLUME_HEIGHT - height
                  const barWidth = candleWidth
                  return (
                    <g key={`vol-${bar.bar_start}`} data-direction={up ? 'up' : 'down'}>
                      <rect
                        className={up ? 'vol-up' : 'vol-down'}
                        x={x - barWidth / 2}
                        y={barTop}
                        width={barWidth}
                        height={Math.max(height, 1)}
                      />
                      {up && (
                        // Non-colour direction channel (DESIGN §13.2): a 1px
                        // top cap on up bars; down bars are outlined only.
                        <line
                          className="vol-cap"
                          x1={x - barWidth / 2}
                          x2={x + barWidth / 2}
                          y1={barTop}
                          y2={barTop}
                        />
                      )}
                    </g>
                  )
                })}
              </g>
              <g data-testid="chart-cvd">
                <line
                  className="cvd-zero"
                  x1={PADDING}
                  y1={CVD_TOP + ((cvdMax - 0) / cvdSpan) * CVD_HEIGHT}
                  x2={WIDTH - PADDING}
                  y2={CVD_TOP + ((cvdMax - 0) / cvdSpan) * CVD_HEIGHT}
                />
                <polyline
                  className="cvd-line"
                  points={bars
                    .map((bar, index) => {
                      const x = PADDING + slot * index + slot / 2
                      const y = CVD_TOP + ((cvdMax - numeric(bar.cvd)) / cvdSpan) * CVD_HEIGHT
                      return `${x},${y}`
                    })
                    .join(' ')}
                />
              </g>
            </svg>
          </div>
        </div>

        <FootprintDock
          bar={footprint ?? null}
          timeframe={timeframe}
          selectedLabel={`${utcLabel(selectedBar.bar_start)} UTC`}
        />
      </div>

      <div className="chart-bars" role="group" aria-label="Bar selection">
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
