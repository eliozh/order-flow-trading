import { useId, useState } from 'react'
import type { KeyboardEvent } from 'react'

import type { MetricsDocument, SnapshotBar } from '../api/reports'
import { signedDecimal, utcLabel } from '../api/reports'
import { DataQualityBadge } from './data-quality'
import { FootprintDock } from './footprint-dock'
import { ProvenanceStamp } from './provenance'

interface ChartProps {
  symbol: string
  timeframe: string
  bars: SnapshotBar[]
  metrics: MetricsDocument
  quality: string
  qualityReasons?: string[]
  selectedBarStart?: string
  onSelectBar?: (barStart: string) => void
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

function priceRange(bars: SnapshotBar[]): { min: number; max: number } {
  const lows = bars.map((bar) => numeric(bar.low))
  const highs = bars.map((bar) => numeric(bar.high))
  const min = Math.min(...lows)
  const max = Math.max(...highs)
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

export function Chart({
  symbol,
  timeframe,
  bars,
  metrics,
  quality,
  qualityReasons = [],
  selectedBarStart,
  onSelectBar,
}: ChartProps) {
  const plotId = useId()
  const tableId = `${plotId}-table`
  const [internalSelection, setInternalSelection] = useState<string | undefined>(undefined)
  const [showTable, setShowTable] = useState(false)
  const selected = selectedBarStart ?? internalSelection ?? bars[bars.length - 1]?.bar_start

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

  const range = priceRange(bars)
  const slot = (WIDTH - PADDING * 2) / bars.length
  const candleWidth = Math.max(slot * 0.55, 3)
  const selectedBar = bars.find((bar) => bar.bar_start === selected) ?? bars[bars.length - 1]
  const footprint = metrics.footprint.bars.find((bar) => bar.bar_start === selectedBar.bar_start)
  const cvdValues = bars.map((bar) => numeric(bar.cvd))
  const cvdMin = Math.min(0, ...cvdValues)
  const cvdMax = Math.max(0, ...cvdValues)
  const cvdSpan = cvdMax - cvdMin || 1
  const maxVolume = Math.max(...bars.map((bar) => numeric(bar.volume)), 0.000001)
  const maxProfileVolume = Math.max(
    ...metrics.volume_profile.bins.map((bin) => numeric(bin.volume)),
    0.000001,
  )

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
    <figure className="chart-frame" aria-label={`${symbol} ${timeframe} chart`}>
      <div className="chart-head">
        <span className="chart-symbol">{symbol}</span>
        <span className="mono-muted">{timeframe} · UTC</span>
        <ProvenanceStamp kind="CALC" />
        <DataQualityBadge status={quality} reasons={qualityReasons} />
        <span className="grow" />
        <span className="mono-muted">
          last {selectedBar.close} · {signedText(selectedBar.delta)}
        </span>
      </div>

      <p className="visually-hidden" aria-live="polite">
        {symbol} {timeframe} chart with {bars.length} bars. Selected{' '}
        {utcLabel(selectedBar.bar_start)} UTC.
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
          tabIndex={0}
          role="img"
          aria-label={`${symbol} ${timeframe} price chart`}
          aria-describedby={showTable ? tableId : undefined}
        >
          <svg
            className="chart-svg"
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            role="presentation"
            focusable="false"
            aria-hidden="true"
            data-testid="chart-plot"
          >
            <g className="chart-grid">
              <line x1={PADDING} y1={PRICE_TOP} x2={WIDTH - PADDING} y2={PRICE_TOP} />
              <line
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
                    <rect x={x - candleWidth / 2} y={top} width={candleWidth} height={bodyHeight} />
                  </g>
                )
              })}
            </g>
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
              <text className="chart-label" x={PADDING + 116} y={PRICE_TOP + 10}>
                POC {metrics.volume_profile.poc}
              </text>
            </g>
            <g data-testid="chart-volume">
              {bars.map((bar, index) => {
                const height = (numeric(bar.volume) / maxVolume) * VOLUME_HEIGHT
                const x = PADDING + slot * index + slot / 2
                return (
                  <rect
                    key={`vol-${bar.bar_start}`}
                    className={numeric(bar.delta) >= 0 ? 'vol-up' : 'vol-down'}
                    data-direction={numeric(bar.delta) >= 0 ? 'up' : 'down'}
                    x={x - candleWidth / 2}
                    y={VOLUME_TOP + VOLUME_HEIGHT - height}
                    width={candleWidth}
                    height={Math.max(height, 1)}
                  />
                )
              })}
              <text className="chart-label" x={PADDING} y={VOLUME_TOP - 4}>
                Volume
              </text>
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
              <text className="chart-label" x={PADDING} y={CVD_TOP - 4}>
                CVD
              </text>
            </g>
          </svg>
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

      <div className="chart-legend" aria-hidden="true">
        <span>
          <span className="swatch swatch--up" /> up
        </span>
        <span>
          <span className="swatch swatch--down" /> down
        </span>
        <span>POC</span>
        <span>POC solid · VAH/VAL value area</span>
      </div>

      {showTable && (
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
              <tr key={`row-${bar.bar_start}`}>
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
                <td>
                  {numeric(bar.close) >= numeric(bar.open) ? 'up' : 'down'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </figure>
  )
}
