import { useId } from 'react'

import type { OpportunityData, OpportunityState, Side } from '../api/opportunities'
import { confidenceBand } from '../api/reports'
import { utcLabel } from '../api/reports'

interface OpportunityCardProps {
  opportunityId: string
  opportunity?: OpportunityData | null
}

const STATE_CHIP: Record<OpportunityState, string> = {
  OPEN: 'chip',
  TRIGGERED: 'chip chip--calc',
  EXPIRED: 'chip chip--warn',
  INVALIDATED: 'chip chip--danger',
}

function sideClass(side: Side): string {
  return side === 'SHORT' ? 'value-down' : 'value-up'
}

function sideLabel(side: Side): string {
  return side === 'SHORT' ? `▼ ${side}` : `▲ ${side}`
}

/** A read-only report card: it links to the ledger and offers no action. */
export function OpportunityCard({ opportunityId, opportunity }: OpportunityCardProps) {
  // A per-instance id keeps `aria-labelledby` valid even if the same
  // opportunity id appears twice in the report's proposal list.
  const headingId = useId()
  const band = opportunity ? confidenceBand(opportunity.confidence) : null

  return (
    <article className="opp-card" aria-labelledby={headingId}>
      <div className="oc-top">
        {opportunity ? (
          <span className={STATE_CHIP[opportunity.state]}>{opportunity.state}</span>
        ) : (
          <span className="chip">UNKNOWN</span>
        )}
        <span className="oc-id mono">{opportunityId}</span>
      </div>

      {/* The heading always exists, so `aria-labelledby` never dangles before
          (or without) the detail fetch. */}
      <h3 id={headingId}>
        {opportunity ? (
          <>
            {opportunity.symbol}{' '}
            <span className={sideClass(opportunity.side)}>{sideLabel(opportunity.side)}</span>
          </>
        ) : (
          <span className="mono">{opportunityId}</span>
        )}
      </h3>

      {opportunity ? (
        <>
          <div className={`zone-band zone-band--${opportunity.state.toLowerCase()} mono`}>
            {opportunity.zone.low} – {opportunity.zone.high}
          </div>
          <div className="oc-meta">
            <span>conf</span>
            <span className="mono">{opportunity.confidence}</span>
            {band !== null && <span className="chip">{band}</span>}
            <span>expires {utcLabel(opportunity.expires_at)} UTC</span>
            <span>scan {opportunity.source.scan_id}</span>
          </div>
        </>
      ) : (
        <p className="explain">Details unavailable; open the ledger entry.</p>
      )}

      <div className="oc-foot">
        <a className="btn sm" href={`/opportunities/${encodeURIComponent(opportunityId)}`}>
          View {opportunityId}
        </a>
      </div>
    </article>
  )
}
