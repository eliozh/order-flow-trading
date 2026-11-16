import type { CSSProperties } from 'react'

import type { DataGap, SnapshotBar } from '../api/reports'
import { signedDecimal, utcLabel } from '../api/reports'
import { barQualityStamp } from './chart-overlays'

/**
 * Presentational crosshair lines, linked tooltip, and the shared aria-live
 * summary text (slice 40).
 *
 * The same `crosshairSummary` string feeds both the tooltip values and the
 * polite live region, so the drawn tooltip and the announcement can never
 * disagree. The tooltip is capped at 320px and clamped inside the plot.
 */

/** Max tooltip width per DESIGN §13.2. */
export const TOOLTIP_MAX_WIDTH = 320

/**
 * Keep the ±half-width offset inside the plot. At the 736px canvas a 320px
 * tooltip is ~43% wide, so 22% of each edge is the safe clamp; the mobile media
 * query pins the tooltip full-width instead.
 */
const TOOLTIP_MARGIN_PERCENT = 22

interface CrosshairLinesProps {
  x: number
  y: number
  top: number
  bottom: number
  width: number
  padding: number
}

/** The vertical/horizontal crosshair lines and the focal dot. */
export function ChartCrosshair({ x, y, top, bottom, width, padding }: CrosshairLinesProps) {
  return (
    <g className="chart-crosshair" data-testid="chart-crosshair" aria-hidden="true">
      <line className="chart-crosshair-line" x1={x} y1={top} x2={x} y2={bottom} />
      <line className="chart-crosshair-line" x1={padding} y1={y} x2={width - padding} y2={y} />
      <circle className="chart-crosshair-dot" cx={x} cy={y} r={2.5} />
    </g>
  )
}

/** The one summary string shared by the tooltip and the aria-live region. */
export function crosshairSummary(
  bar: SnapshotBar,
  gaps: DataGap[],
  quality: string,
  qualityReasons: string[] = [],
): string {
  const stamp = barQualityStamp(bar, gaps, quality, qualityReasons)
  return (
    `Selected ${utcLabel(bar.bar_start)} UTC. ` +
    `O ${bar.open} H ${bar.high} L ${bar.low} C ${bar.close}. ` +
    `Vol ${bar.volume}. Delta ${signedDecimal(bar.delta)}. ` +
    `CVD ${signedDecimal(bar.cvd)}. Quality ${stamp}.`
  )
}

interface CrosshairTooltipProps {
  bar: SnapshotBar
  gaps: DataGap[]
  quality: string
  qualityReasons?: string[]
  /** Crosshair x in SVG units, used to keep the tooltip inside the plot. */
  x: number
  width: number
}

/** The linked tooltip: UTC time, O/H/L/C, volume, delta, CVD, and quality. */
export function ChartTooltip({
  bar,
  gaps,
  quality,
  qualityReasons = [],
  x,
  width,
}: CrosshairTooltipProps) {
  const stamp = barQualityStamp(bar, gaps, quality, qualityReasons)
  const ratio = width > 0 ? (x / width) * 100 : 50
  const left = Math.min(Math.max(ratio, TOOLTIP_MARGIN_PERCENT), 100 - TOOLTIP_MARGIN_PERCENT)
  return (
    <div
      className="chart-tooltip"
      data-testid="chart-tooltip"
      style={{ '--crosshair-left': `${left}%` } as CSSProperties}
    >
      <div className="chart-tooltip-head">{utcLabel(bar.bar_start)} UTC</div>
      {/* Provenance travels with the values exactly as in DESIGN §13.2/S04.3. */}
      <div className="chart-tooltip-stamp" data-testid="chart-tooltip-stamp">
        CALC
      </div>
      <dl className="chart-tooltip-grid">
        <dt>O</dt>
        <dd>{bar.open}</dd>
        <dt>H</dt>
        <dd>{bar.high}</dd>
        <dt>L</dt>
        <dd>{bar.low}</dd>
        <dt>C</dt>
        <dd>{bar.close}</dd>
        <dt>Vol</dt>
        <dd>{bar.volume}</dd>
        <dt>Delta</dt>
        <dd>{signedDecimal(bar.delta)}</dd>
        <dt>CVD</dt>
        <dd>{signedDecimal(bar.cvd)}</dd>
        <dt>Quality</dt>
        <dd>{stamp}</dd>
      </dl>
    </div>
  )
}
