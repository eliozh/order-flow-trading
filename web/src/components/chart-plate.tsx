import { memo } from 'react'

import type { ChartMarker, ChartZone } from '../api/charts'
import type {
  DataGap,
  FootprintSection,
  SnapshotBar,
  VolumeProfileSection,
} from '../api/reports'
import { ChartGapBands, ChartMarkers, ChartZones, gapBarIndices } from './chart-overlays'

/**
 * Structural subset of a metrics document that the chart actually draws, so a
 * frozen report document and a chart payload both fit without casts.
 */
export interface ChartMetrics {
  footprint: Pick<FootprintSection, 'bars'>
  volume_profile: Pick<VolumeProfileSection, 'bins' | 'poc'>
}

// Shared SVG geometry. The frame draws the crosshair/tooltip and axis labels
// against the same coordinate space as the plate, so the values live here.
export const WIDTH = 760
export const PRICE_TOP = 18
export const PRICE_HEIGHT = 210
const VOLUME_TOP = 250
const VOLUME_HEIGHT = 58
export const CVD_TOP = 324
export const CVD_HEIGHT = 64
export const HEIGHT = 410
export const PADDING = 12

export function numeric(value: string | null): number {
  if (value === null) {
    return 0
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function yForPrice(value: number, range: { min: number; max: number }): number {
  const span = range.max - range.min || 1
  return PRICE_TOP + ((range.max - value) / span) * PRICE_HEIGHT
}

/** Contiguous non-gap runs of bar indices; a gap is a break, never interpolated. */
function cvdSegments(bars: SnapshotBar[], gapIndices: ReadonlySet<number>): number[][] {
  const segments: number[][] = []
  let run: number[] = []
  bars.forEach((_bar, index) => {
    if (gapIndices.has(index)) {
      if (run.length > 0) {
        segments.push(run)
        run = []
      }
    } else {
      run.push(index)
    }
  })
  if (run.length > 0) {
    segments.push(run)
  }
  return segments
}

interface ChartPlateProps {
  bars: SnapshotBar[]
  metrics: ChartMetrics
  zones: ChartZone[]
  markers: ChartMarker[]
  gaps: DataGap[]
  anchoredEntry: number | null
  entryText: string | null
  range: { min: number; max: number }
  slot: number
  candleWidth: number
  hatchId: string
  /**
   * Test-only render instrumentation. Production callers leave it undefined, so
   * it is a no-op; tests use it to prove the memoized plate does not rebuild.
   */
  onPlateRender?: () => void
}

/**
 * The static plate: candles, volume, VP, CVD runs, zones, markers, and gap
 * breaks. Memoized so a crosshair move (local state) updates only the
 * crosshair/tooltip layer instead of rebuilding every SVG layer; every prop is
 * a stable server-derived array or a primitive.
 */
export const ChartPlate = memo(function ChartPlate({
  bars,
  metrics,
  zones,
  markers,
  gaps,
  anchoredEntry,
  entryText,
  range,
  slot,
  candleWidth,
  hatchId,
  onPlateRender,
}: ChartPlateProps) {
  onPlateRender?.()
  const entryY = anchoredEntry === null ? null : yForPrice(anchoredEntry, range)
  const markerOverlays =
    anchoredEntry === null ? markers : markers.filter((marker) => marker.kind !== 'entry')
  const gapColumns = new Set(gapBarIndices(bars, gaps))
  const cvdRuns = cvdSegments(bars, gapColumns)
  function xForIndex(index: number): number {
    return PADDING + slot * index + slot / 2
  }
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
  const priceY = (price: number) => yForPrice(price, range)

  return (
    <>
      <defs>
        <pattern
          id={hatchId}
          width="6"
          height="6"
          patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)"
        >
          <line x1="0" y1="0" x2="0" y2="6" className="chart-gap-hatch-line" strokeWidth="1" />
        </pattern>
      </defs>
      <g className="chart-grid">
        <line data-testid="grid-price-top" x1={PADDING} y1={PRICE_TOP} x2={WIDTH - PADDING} y2={PRICE_TOP} />
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
      <ChartZones zones={zones} yForPrice={priceY} width={WIDTH} padding={PADDING} />
      <ChartGapBands
        bars={bars}
        gaps={gaps}
        xForIndex={xForIndex}
        slot={slot}
        top={PRICE_TOP}
        height={PRICE_HEIGHT}
        patternId={hatchId}
      />
      <g data-testid="chart-candles">
        {bars.map((bar, index) => {
          const x = PADDING + slot * index + slot / 2
          const openY = priceY(numeric(bar.open))
          const closeY = priceY(numeric(bar.close))
          const up = numeric(bar.close) >= numeric(bar.open)
          const top = Math.min(openY, closeY)
          const bodyHeight = Math.max(Math.abs(closeY - openY), 1)
          return (
            <g
              key={bar.bar_start}
              className={up ? 'candle-up' : 'candle-down'}
              data-direction={up ? 'up' : 'down'}
            >
              <line x1={x} y1={priceY(numeric(bar.high))} x2={x} y2={priceY(numeric(bar.low))} />
              <rect x={x - candleWidth / 2} y={top} width={candleWidth} height={bodyHeight} />
            </g>
          )
        })}
      </g>
      <ChartMarkers
        markers={markerOverlays}
        yForPrice={priceY}
        width={WIDTH}
        padding={PADDING}
        top={PRICE_TOP}
        bottom={PRICE_TOP + PRICE_HEIGHT}
      />
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
            Math.max(priceY(midPrice) - 5, PRICE_TOP),
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
                // Non-colour direction channel (DESIGN §13.2): a 1px top cap
                // on up bars; down bars are outlined only.
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
        {cvdRuns.map((run, runIndex) => (
          <polyline
            key={`cvd-run-${runIndex}`}
            className="cvd-line"
            data-testid={`chart-cvd-segment-${runIndex}`}
            points={run
              .map((index) => {
                const y = CVD_TOP + ((cvdMax - numeric(bars[index].cvd)) / cvdSpan) * CVD_HEIGHT
                return `${xForIndex(index)},${y}`
              })
              .join(' ')}
          />
        ))}
      </g>
    </>
  )
})
