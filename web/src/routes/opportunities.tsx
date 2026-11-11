import { useEffect, useState, type KeyboardEvent } from 'react'

import {
  describeCondition,
  fetchOpportunities,
  fetchOpportunity,
  type OpportunityData,
  type OpportunityListEnvelope,
  type OpportunityState,
  type OpportunitySummary,
  type OpportunityEnvelope,
} from '../api/opportunities'
import { confidenceBand, utcLabel } from '../api/reports'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface OpportunitiesPageProps {
  fetchOpportunitiesFn?: () => Promise<OpportunityListEnvelope>
  fetchOpportunityFn?: (opportunityId: string) => Promise<OpportunityEnvelope>
  isCompact?: boolean
}

interface LedgerState {
  state: 'loading' | 'ready' | 'error'
  items: OpportunitySummary[]
  total: number
  message?: string
}

const STATE_ORDER: OpportunityState[] = ['OPEN', 'TRIGGERED', 'EXPIRED', 'INVALIDATED']

const STATE_LABEL: Record<OpportunityState, string> = {
  OPEN: 'Open',
  TRIGGERED: 'Triggered',
  EXPIRED: 'Expired',
  INVALIDATED: 'Invalidated',
}

const STATE_CHIP: Record<OpportunityState, string> = {
  OPEN: 'chip',
  TRIGGERED: 'chip chip--calc',
  EXPIRED: 'chip chip--warn',
  INVALIDATED: 'chip chip--danger',
}

/** Design counts: neutral uploads, cyan triggers, gray expiry, red invalidation. */
const COUNT_CLASS: Record<OpportunityState, string> = {
  OPEN: 'count count--neutral',
  TRIGGERED: 'count count--accent',
  EXPIRED: 'count count--muted',
  INVALIDATED: 'count count--danger',
}

function sideLabel(side: string): string {
  return side === 'SHORT' ? `▼ ${side}` : `▲ ${side}`
}

function sideClass(side: string): string {
  return side === 'SHORT' ? 'value-down' : 'value-up'
}

function emptyCounts(): Record<OpportunityState, number> {
  return { OPEN: 0, TRIGGERED: 0, EXPIRED: 0, INVALIDATED: 0 }
}

export function OpportunitiesPage({
  fetchOpportunitiesFn = fetchOpportunities,
  fetchOpportunityFn = fetchOpportunity,
  isCompact,
}: OpportunitiesPageProps) {
  const [load, setLoad] = useState<LedgerState>({ state: 'loading', items: [], total: 0 })
  const [details, setDetails] = useState<Record<string, OpportunityData>>({})
  const [filter, setFilter] = useState<OpportunityState>('OPEN')
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading', items: [], total: 0 })
    setDetails({})
    fetchOpportunitiesFn()
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({
            state: 'error',
            items: [],
            total: 0,
            message: 'Opportunity response was empty.',
          })
          return
        }
        setLoad({ state: 'ready', items: data.items, total: data.total })
        void Promise.all(
          data.items.map(async (item) => {
            try {
              const detail = await fetchOpportunityFn(item.opportunity_id)
              return detail.data ? ([item.opportunity_id, detail.data] as const) : null
            } catch {
              return null
            }
          }),
        ).then((rows) => {
          if (!active) {
            return
          }
          const next: Record<string, OpportunityData> = {}
          for (const row of rows) {
            if (row !== null) {
              next[row[0]] = row[1]
            }
          }
          setDetails(next)
        })
      })
      .catch((error: unknown) => {
        if (active) {
          setLoad({
            state: 'error',
            items: [],
            total: 0,
            message: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      })
    return () => {
      active = false
    }
  }, [fetchOpportunitiesFn, fetchOpportunityFn])

  const counts = emptyCounts()
  for (const item of load.items) {
    counts[item.state] += 1
  }
  const filtered = load.items.filter((item) => item.state === filter)
  const filterLabel = STATE_LABEL[filter].toLowerCase()

  function selectState(state: OpportunityState): void {
    setFilter(state)
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    const key = event.key
    let nextIndex: number | null = null
    if (key === 'ArrowRight') {
      nextIndex = (index + 1) % STATE_ORDER.length
    } else if (key === 'ArrowLeft') {
      nextIndex = (index - 1 + STATE_ORDER.length) % STATE_ORDER.length
    } else if (key === 'Home') {
      nextIndex = 0
    } else if (key === 'End') {
      nextIndex = STATE_ORDER.length - 1
    }
    if (nextIndex === null) {
      return
    }
    event.preventDefault()
    const next = STATE_ORDER[nextIndex]
    setFilter(next)
    document.getElementById(`opp-tab-${next}`)?.focus()
  }

  return (
    <section
      className="system-page"
      aria-labelledby="opportunities-heading"
      data-readonly={compact}
    >
      <div className="page-head">
        <div>
          <p className="kicker">Opportunity ledger</p>
          <h1 id="opportunities-heading">Opportunities</h1>
          <p className="page-sub">
            Code owns every state transition. The LLM proposes; the trigger evaluator decides. There
            is no manual trigger, accept, or reject action in the ledger.
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="LLM" />
          <ProvenanceStamp kind="CALC" />
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          Read-only at this width. State transitions are executed by code.
        </p>
      )}

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          Loading opportunities…
        </p>
      )}

      {load.state === 'error' && (
        <p className="status-message status-message--error" role="alert">
          Opportunity ledger unavailable: {load.message}
        </p>
      )}

      {load.state === 'ready' && load.total === 0 && (
        <section className="panel" role="status">
          <h2>No opportunities yet</h2>
          <p>
            Opportunities are proposed by scans. Run a scan to produce a report and its ledger
            entries.
          </p>
          <a className="btn primary" href="/scans">
            Run a scan
          </a>
        </section>
      )}

      {load.state === 'ready' && load.total > 0 && (
        <>
          <div className="tabs" role="tablist" aria-label="Opportunity state">
            {STATE_ORDER.map((state, index) => (
              <button
                key={state}
                type="button"
                role="tab"
                className="tab"
                id={`opp-tab-${state}`}
                aria-controls={`opp-panel-${state}`}
                aria-selected={filter === state}
                tabIndex={filter === state ? 0 : -1}
                onClick={() => selectState(state)}
                onKeyDown={(event) => handleTabKey(event, index)}
              >
                {STATE_LABEL[state]} <span className={COUNT_CLASS[state]}>{counts[state]}</span>
              </button>
            ))}
          </div>

          <div
            role="tabpanel"
            id={`opp-panel-${filter}`}
            aria-labelledby={`opp-tab-${filter}`}
            className="tabpanel"
            tabIndex={0}
          >
            {filtered.length === 0 ? (
              <section className="panel empty-panel" role="status">
                <h2>No {filterLabel} opportunities</h2>
                <p>
                  There are {load.total} opportunities in other states; none in this filter. Switch
                  states above to review them. {filterLabel} is empty, not missing.
                </p>
              </section>
            ) : (
              <section className="panel" aria-labelledby="ledger-table-heading">
                <div className="panel-head">
                  <h2 id="ledger-table-heading">
                    {STATE_LABEL[filter]} opportunities ({filtered.length})
                  </h2>
                  <span className="mono-muted">expiry in UTC</span>
                </div>
                <div
                  className="table-wrap"
                  role="region"
                  aria-label="Opportunity ledger table"
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
                  tabIndex={0}
                >
                  <table className="data-table">
                    <caption className="visually-hidden">
                      Opportunity ledger · {filterLabel}
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">ID</th>
                        <th scope="col">Symbol</th>
                        <th scope="col">Side</th>
                        <th scope="col">Zone</th>
                        <th scope="col">Trigger</th>
                        <th scope="col">Confidence</th>
                        <th scope="col">Expiry</th>
                        <th scope="col">State</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((item) => {
                        const detail = details[item.opportunity_id]
                        const trigger = detail?.trigger_conditions[0]
                        const band = confidenceBand(item.confidence)
                        return (
                          <tr key={item.opportunity_id}>
                            <td>
                              <a
                                className="rowlink mono"
                                href={`/opportunities/${encodeURIComponent(item.opportunity_id)}`}
                              >
                                {item.opportunity_id}
                              </a>
                            </td>
                            <td className="mono">{item.symbol}</td>
                            <td className={sideClass(item.side)}>{sideLabel(item.side)}</td>
                            <td className="mono">
                              {detail ? `${detail.zone.low} – ${detail.zone.high}` : '—'}
                            </td>
                            <td className="calc-note">
                              {trigger ? describeCondition(trigger) : '—'}
                            </td>
                            <td>
                              <span className="mono">{item.confidence}</span>{' '}
                              {band !== null && <span className="chip">{band}</span>}
                            </td>
                            <td className="mono">{utcLabel(item.expires_at)} UTC</td>
                            <td>
                              <span className={STATE_CHIP[item.state]}>{item.state}</span>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="panel-foot">
                  Triggered states are decided by code. A condition marked UNAVAILABLE is never
                  treated as met. No accept/reject action exists here.
                </p>
              </section>
            )}
          </div>
        </>
      )}
    </section>
  )
}
