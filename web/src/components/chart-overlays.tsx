import type { ChartMarker, ChartZone } from '../api/charts'
import type { DataGap, SnapshotBar } from '../api/reports'
import { utcLabel } from '../api/reports'

/**
 * Presentational overlay groups for the report/position chart (slice 40).
 *
 * Everything drawn here comes from the server payload: zone bounds, trigger
 * prices, states, and marker prices are stored strings copied verbatim. Labels
 * are untrusted text rendered as SVG `<text>` nodes (React escapes them) and
 * bounded to 80 characters on the client as well as on the server.
 */

/** Bound on one overlay label (matches the server projection). */
export const MAX_OVERLAY_LABEL = 80

const DEFAULT_DASH = '4 3'

/**
 * A non-color state channel: each opportunity state has a distinct dash
 * pattern, and the state word is always rendered next to the band.
 */
const STATE_DASH: Record<string, string> = {
  OPEN: '2 3',
  TRIGGERED: '6 3',
  EXPIRED: '1 4',
  INVALIDATED: '8 2',
}

export function zoneDash(state: string): string {
  return STATE_DASH[state] ?? DEFAULT_DASH
}

/** Bound one label to the client cap. */
export function clampLabel(label: string): string {
  return label.slice(0, MAX_OVERLAY_LABEL)
}

/** A finite, positive price from a server string, else `null` (never zero). */
export function numericPrice(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null
  }
  return parsed
}

function overlapsGap(bar: SnapshotBar, gap: DataGap): boolean {
  const barStart = Date.parse(bar.bar_start)
  const barEnd = Date.parse(bar.bar_end)
  const gapStart = Date.parse(gap.start)
  const gapEnd = Date.parse(gap.end)
  if (![barStart, barEnd, gapStart, gapEnd].every(Number.isFinite)) {
    return false
  }
  return barStart < gapEnd && barEnd > gapStart
}

/** The bar indices that overlap any server gap window (missing data). */
export function gapBarIndices(bars: SnapshotBar[], gaps: DataGap[]): number[] {
  const indices: number[] = []
  bars.forEach((bar, index) => {
    if (gaps.some((gap) => overlapsGap(bar, gap))) {
      indices.push(index)
    }
  })
  return indices
}

/**
 * True when the gap window sits strictly between two consecutive bars, e.g.
 * 00:00-04:00 and 08:00-12:00 with a 04:00-08:00 gap. No present bar overlaps
 * such a gap, so `overlapsGap` alone cannot see the missing window.
 */
function bracketsGap(before: SnapshotBar, after: SnapshotBar, gap: DataGap): boolean {
  const beforeEnd = Date.parse(before.bar_end)
  const afterStart = Date.parse(after.bar_start)
  const gapStart = Date.parse(gap.start)
  const gapEnd = Date.parse(gap.end)
  if (![beforeEnd, afterStart, gapStart, gapEnd].every(Number.isFinite)) {
    return false
  }
  return beforeEnd <= gapStart && gapEnd <= afterStart
}

/**
 * Indices where a gap bracketed by consecutive bars breaks the series *before*
 * that index. The two neighbouring bars are present, so they are never stamped
 * MISSING; only the absent window between them is a break.
 */
export function gapBoundaryIndices(bars: SnapshotBar[], gaps: DataGap[]): number[] {
  const indices: number[] = []
  for (let index = 1; index < bars.length; index += 1) {
    if (gaps.some((gap) => bracketsGap(bars[index - 1], bars[index], gap))) {
      indices.push(index)
    }
  }
  return indices
}

/**
 * The first break-before index for a gap bracketed by two consecutive bars, or
 * `null` when no such pair exists (for example, when a bar overlaps the gap).
 */
function boundaryIndexFor(bars: SnapshotBar[], gap: DataGap): number | null {
  for (let index = 1; index < bars.length; index += 1) {
    if (bracketsGap(bars[index - 1], bars[index], gap)) {
      return index
    }
  }
  return null
}

/**
 * Exact-token match over the server's free-text quality reasons. Substring
 * matching would let "not stale" or "staleness check skipped" mislabel every
 * bar as STALE; tokens are split on non-alphanumerics, uppercased, and a token
 * immediately preceded by NOT does not count.
 */
function hasReasonToken(reasons: string[], target: string): boolean {
  const tokens = reasons
    .flatMap((reason) => reason.split(/[^A-Za-z0-9]+/))
    .filter(Boolean)
    .map((token) => token.toUpperCase())
  return tokens.some(
    (token, index) => token === target && (index === 0 || tokens[index - 1] !== 'NOT'),
  )
}

/**
 * The stamp for one bar: `MISSING` when it overlaps a server gap, otherwise
 * `STALE`/`ESTIMATED` when the server quality or an exact reason token says so,
 * else the server quality itself (the series-level fallback).
 */
export function barQualityStamp(
  bar: SnapshotBar,
  gaps: DataGap[],
  quality: string,
  qualityReasons: string[] = [],
): string {
  if (gaps.some((gap) => overlapsGap(bar, gap))) {
    return 'MISSING'
  }
  if (quality === 'STALE' || quality === 'ESTIMATED') {
    return quality
  }
  if (hasReasonToken(qualityReasons, 'STALE')) {
    return 'STALE'
  }
  if (hasReasonToken(qualityReasons, 'ESTIMATED')) {
    return 'ESTIMATED'
  }
  return quality
}

/** A bounded gap label: `GAP <start>–<end>` in compact UTC. */
export function gapLabelText(gap: DataGap): string {
  return clampLabel(`GAP ${utcLabel(gap.start)}–${utcLabel(gap.end)}`)
}

interface ZoneProps {
  zones: ChartZone[]
  yForPrice: (price: number) => number
  width: number
  padding: number
}

/** Opportunity zone bands and trigger lines at their exact stored prices. */
export function ChartZones({ zones, yForPrice, width, padding }: ZoneProps) {
  if (zones.length === 0) {
    return null
  }
  const bandWidth = width - padding * 2
  return (
    <g data-testid="chart-zones">
      {zones.map((zone) => {
        const high = numericPrice(zone.high)
        const low = numericPrice(zone.low)
        if (high === null || low === null) {
          return null
        }
        const top = yForPrice(high)
        const bottom = yForPrice(low)
        const bandTop = Math.min(top, bottom)
        const bandHeight = Math.max(Math.abs(bottom - top), 1)
        const dash = zoneDash(zone.state)
        const trigger = numericPrice(zone.trigger_price)
        return (
          <g
            key={zone.opportunity_id}
            className={`chart-zone chart-zone--${zone.state.toLowerCase()}`}
            data-state={zone.state}
            data-testid={`chart-zone-${zone.opportunity_id}`}
          >
            <rect
              className="chart-zone-band"
              x={padding}
              y={bandTop}
              width={bandWidth}
              height={bandHeight}
            />
            <line
              className="chart-zone-edge"
              x1={padding}
              y1={top}
              x2={width - padding}
              y2={top}
              strokeDasharray={dash}
            />
            <line
              className="chart-zone-edge"
              x1={padding}
              y1={bottom}
              x2={width - padding}
              y2={bottom}
              strokeDasharray={dash}
            />
            <text className="chart-zone-state" x={padding + 4} y={bandTop + 12}>
              {zone.state}
            </text>
            <text className="chart-zone-label" x={padding + 4} y={bandTop + 26}>
              {clampLabel(zone.label)}
            </text>
            {trigger !== null && (
              <g data-testid="chart-trigger-line">
                <line
                  className="chart-trigger-line"
                  x1={padding}
                  y1={yForPrice(trigger)}
                  x2={width - padding}
                  y2={yForPrice(trigger)}
                  strokeDasharray={dash}
                />
                <text
                  className="chart-trigger-label"
                  x={padding + 4}
                  y={yForPrice(trigger) - 4}
                >
                  TRIGGER {zone.trigger_price}
                </text>
              </g>
            )}
          </g>
        )
      })}
    </g>
  )
}

interface MarkerProps {
  markers: ChartMarker[]
  yForPrice: (price: number) => number
  width: number
  padding: number
  /** Optional label clamp bounds (SVG units); marker y stays at its price. */
  top?: number
  bottom?: number
}

/** Minimum vertical distance between two marker label baselines. */
const MARKER_LABEL_GAP = 11

/**
 * The label baseline for every drawable marker. Markers keep their exact price
 * y; only the text boxes are pushed apart (and clamped inside the price pane)
 * so near-identical STOP/EXIT prices cannot overlap.
 */
function markerLabelYs(
  entries: Array<{ index: number; y: number }>,
  top: number | undefined,
  bottom: number | undefined,
): Map<number, number> {
  const labelYs = new Map<number, number>()
  const minY = (top ?? Number.NEGATIVE_INFINITY) + 8
  const maxY = (bottom ?? Number.POSITIVE_INFINITY) - 2
  const ordered = [...entries].sort((left, right) => left.y - right.y)
  let floor = Number.NEGATIVE_INFINITY
  for (const entry of ordered) {
    const proposed = Math.max(entry.y + 3, floor + MARKER_LABEL_GAP)
    const clamped = Math.min(Math.max(proposed, minY), maxY)
    labelYs.set(entry.index, clamped)
    floor = clamped
  }
  return labelYs
}

function MarkerShape({ kind, x, y }: { kind: ChartMarker['kind']; x: number; y: number }) {
  if (kind === 'entry') {
    return (
      <polygon
        className="chart-marker-shape"
        points={`${x},${y - 6} ${x - 5},${y + 3} ${x + 5},${y + 3}`}
      />
    )
  }
  if (kind === 'exit') {
    return <rect className="chart-marker-shape" x={x - 4} y={y - 4} width={8} height={8} />
  }
  if (kind === 'stop') {
    return (
      <path
        className="chart-marker-shape"
        d={`M${x - 5},${y - 5} l10,10 M${x + 5},${y - 5} l-10,10`}
      />
    )
  }
  return <circle className="chart-marker-shape" cx={x} cy={y} r={4} />
}

/** Entry/exit/stop/target markers at their exact stored prices. */
export function ChartMarkers({ markers, yForPrice, width, padding, top, bottom }: MarkerProps) {
  if (markers.length === 0) {
    return null
  }
  const x = width - padding - 16
  const placed = markers.flatMap((marker, index) => {
    const price = numericPrice(marker.price)
    return price === null ? [] : [{ index, y: yForPrice(price) }]
  })
  const labelYs = markerLabelYs(placed, top, bottom)
  return (
    <g data-testid="chart-markers">
      {markers.map((marker, index) => {
        const price = numericPrice(marker.price)
        if (price === null) {
          return null
        }
        const y = yForPrice(price)
        const labelY = labelYs.get(index) ?? y + 3
        return (
          <g
            key={`${marker.kind}-${marker.price}-${index}`}
            className={`chart-marker chart-marker--${marker.kind}`}
            data-kind={marker.kind}
            data-price={marker.price}
            data-y={y}
            data-testid={marker.kind === 'entry' ? 'chart-entry-marker' : `chart-marker-${marker.kind}`}
          >
            <MarkerShape kind={marker.kind} x={x} y={y} />
            <text
              className="chart-marker-label"
              x={x - 8}
              y={labelY}
              data-label-y={labelY}
              textAnchor="end"
            >
              {clampLabel(marker.label)}
            </text>
          </g>
        )
      })}
    </g>
  )
}

interface GapProps {
  bars: SnapshotBar[]
  gaps: DataGap[]
  xForIndex: (index: number) => number
  slot: number
  top: number
  height: number
  patternId: string
}

/**
 * Hatched break marks for server gaps. A gap that covers present bars draws a
 * band over those bars (`chart-gap-break`); a gap bracketed by consecutive bars
 * draws a narrow hatched break at the boundary (`chart-gap-boundary`). Neither
 * form is ever bridged by a CVD polyline.
 */
export function ChartGapBands({ bars, gaps, xForIndex, slot, top, height, patternId }: GapProps) {
  if (gaps.length === 0 || bars.length === 0) {
    return null
  }
  const overlapGroups = gaps
    .map((gap) => ({
      gap,
      indices: bars
        .map((bar, index) => (overlapsGap(bar, gap) ? index : -1))
        .filter((index) => index >= 0),
    }))
    .filter((group) => group.indices.length > 0)
  const boundaryGroups = gaps.flatMap((gap) => {
    const index = boundaryIndexFor(bars, gap)
    return index === null ? [] : [{ gap, index }]
  })
  if (overlapGroups.length === 0 && boundaryGroups.length === 0) {
    return null
  }
  const leftEdge = xForIndex(0) - slot / 2
  const rightEdge = xForIndex(bars.length - 1) + slot / 2
  return (
    <g data-testid="chart-gaps">
      {overlapGroups.map(({ gap, indices }) => {
        const first = indices[0]
        const last = indices[indices.length - 1]
        const x = xForIndex(first) - slot / 2
        const width = (last - first + 1) * slot
        return (
          <g
            key={gap.gap_id ?? `${gap.data_type}-${gap.start}-${gap.end}`}
            data-testid="chart-gap-break"
          >
            <rect
              className="chart-gap-band"
              x={x}
              y={top}
              width={width}
              height={height}
              fill={`url(#${patternId})`}
            />
            <text className="chart-gap-label" x={x + 4} y={top + 12}>
              {gapLabelText(gap)}
            </text>
          </g>
        )
      })}
      {boundaryGroups.map(({ gap, index }) => {
        const boundaryX = (xForIndex(index - 1) + xForIndex(index)) / 2
        const markWidth = Math.max(slot * 0.18, 3)
        const nearRight = boundaryX > (leftEdge + rightEdge) / 2
        return (
          <g
            key={`boundary-${gap.gap_id ?? `${gap.data_type}-${gap.start}-${gap.end}`}`}
            data-testid="chart-gap-boundary"
          >
            <rect
              className="chart-gap-boundary-mark"
              x={boundaryX - markWidth / 2}
              y={top}
              width={markWidth}
              height={height}
              fill={`url(#${patternId})`}
            />
            <text
              className="chart-gap-label"
              x={boundaryX + (nearRight ? -5 : 5)}
              y={top + 12}
              textAnchor={nearRight ? 'end' : 'start'}
            >
              {gapLabelText(gap)}
            </text>
          </g>
        )
      })}
    </g>
  )
}
