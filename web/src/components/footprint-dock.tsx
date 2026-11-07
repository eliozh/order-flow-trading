import type { FootprintBar } from '../api/reports'
import { signedDecimal, utcLabel } from '../api/reports'

interface FootprintDockProps {
  bar: FootprintBar | null
  timeframe: string
  selectedLabel?: string
}

function heatClass(value: string, max: number): string {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || max <= 0) {
    return 'fp-cell'
  }
  const ratio = numeric / max
  if (ratio >= 0.75) {
    return 'fp-cell fp-cell--heat-3'
  }
  if (ratio >= 0.4) {
    return 'fp-cell fp-cell--heat-2'
  }
  if (ratio > 0) {
    return 'fp-cell fp-cell--heat-1'
  }
  return 'fp-cell'
}

export function FootprintDock({ bar, timeframe, selectedLabel }: FootprintDockProps) {
  if (!bar) {
    return (
      <section className="footprint" aria-label="Footprint for selected bar">
        <h3>Footprint</h3>
        <p className="panel-foot">Select a bar to inspect its footprint.</p>
      </section>
    )
  }
  const label = selectedLabel ?? `${utcLabel(bar.bar_start)} UTC`
  const cells = [...bar.cells].sort((left, right) => Number(right.price) - Number(left.price))
  const maxBid = Math.max(...cells.map((cell) => Number(cell.bid_volume)), 0)
  const maxAsk = Math.max(...cells.map((cell) => Number(cell.ask_volume)), 0)
  const total = cells.reduce((sum, cell) => sum + Number(cell.total_volume), 0)

  return (
    <section
      className="footprint"
     
      aria-label={`Footprint for selected bar, ${label}`}
    >
      <div className="footprint-head">
        <h3>Footprint</h3>
        <span className="mono-muted">
          {label} · {timeframe}
        </span>
      </div>
      <table className="fp-table">
        <caption className="visually-hidden">
          Footprint cells for {label}; bid is sell-aggressor, ask is buy-aggressor.
        </caption>
        <thead>
          <tr>
            <th scope="col">Price</th>
            <th scope="col">Bid</th>
            <th scope="col">Ask</th>
            <th scope="col">Δ</th>
          </tr>
        </thead>
        <tbody>
          {cells.map((cell) => (
            <tr key={`${bar.bar_start}-${cell.price}`}>
              <th scope="row" className="mono">
                {cell.price}
              </th>
              <td className={heatClass(cell.bid_volume, maxBid)}>{cell.bid_volume}</td>
              <td className={heatClass(cell.ask_volume, maxAsk)}>{cell.ask_volume}</td>
              <td className={cell.delta.startsWith('-') ? 'value-down' : 'value-up'}>
                {signedDecimal(cell.delta)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="panel-foot">
        {cells.length} price levels · total volume {total.toFixed(2)}
      </p>
    </section>
  )
}
