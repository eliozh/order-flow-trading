import { useEffect, useState, type KeyboardEvent } from 'react'

import { isAbortError } from '../api/abort'
import {
  describeCondition,
  fetchOpportunities,
  formatOpportunityAge,
  type OpportunityListEnvelope,
  type OpportunityState,
  type OpportunitySummary,
} from '../api/opportunities'
import { confidenceBand, utcLabel } from '../api/reports'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface OpportunitiesPageProps {
  fetchOpportunitiesFn?: (
    options: { state: OpportunityState; limit: number },
    signal?: AbortSignal,
  ) => Promise<OpportunityListEnvelope>
  isCompact?: boolean
  /** Injectable clock so the age column is deterministic under test. */
  now?: () => Date
}

interface LedgerState {
  state: 'loading' | 'ready' | 'error'
  items: OpportunitySummary[]
  total: number
  counts: Record<OpportunityState, number>
  message?: string
}

const STATE_ORDER: OpportunityState[] = ['OPEN', 'TRIGGERED', 'EXPIRED', 'INVALIDATED']

/** One page of rows; the server reports `total` and per-state counts. */
const PAGE_SIZE = 50

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

function emptyCounts(): Record<OpportunityState, number> {
  return { OPEN: 0, TRIGGERED: 0, EXPIRED: 0, INVALIDATED: 0 }
}

function overallCount(counts: Record<OpportunityState, number>): number {
  return STATE_ORDER.reduce((sum, state) => sum + counts[state], 0)
}

export function OpportunitiesPage({
  fetchOpportunitiesFn = fetchOpportunities,
  isCompact,
  now = () => new Date(),
}: OpportunitiesPageProps) {
  const [load, setLoad] = useState<LedgerState>({
    state: 'loading',
    items: [],
    total: 0,
    counts: emptyCounts(),
  })
  const [filter, setFilter] = useState<OpportunityState>('OPEN')
  const compact = useCompact(isCompact)
  const renderedAt = now()

  useEffect(() => {
    const controller = new AbortController()
    // Keep the previous counts while the next filter loads so the tabs stay
    // mounted and focused.
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchOpportunitiesFn({ state: filter, limit: PAGE_SIZE }, controller.signal)
      .then((envelope) => {
        if (controller.signal.aborted) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad((previous) => ({
            ...previous,
            state: 'error',
            items: [],
            total: 0,
            message: 'Opportunity response was empty.',
          }))
          return
        }
        setLoad({
          state: 'ready',
          items: data.items,
          total: data.total,
          counts: data.counts ?? emptyCounts(),
        })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) {
          return
        }
        setLoad((previous) => ({
          ...previous,
          state: 'error',
          items: [],
          total: 0,
          message: error instanceof Error ? error.message : 'Unknown error',
        }))
      })
    return () => {
      controller.abort()
    }
  }, [fetchOpportunitiesFn, filter])

  const overall = overallCount(load.counts)
  const filtered = load.items
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

  const loadingAnnouncement =
    load.state === 'ready'
      ? `${STATE_LABEL[filter]} filter: showing ${filtered.length} of ${load.total} opportunities.`
      : ''

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

      {load.state === 'loading' && overall === 0 && (
        <p className="status-message" role="status">
          Loading opportunities…
        </p>
      )}

      {load.state === 'error' && (
        <p className="status-message status-message--error" role="alert">
          Opportunity ledger unavailable: {load.message}
        </p>
      )}

      {load.state === 'ready' && overall === 0 && (
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

      {load.state !== 'error' && overall > 0 && (
        <>
          <p className="visually-hidden" role="status" aria-live="polite">
            {loadingAnnouncement}
          </p>

          <div className="tabs" role="tablist" aria-label="Opportunity state">
            {STATE_ORDER.map((state, index) => (
              <button
                key={state}
                type="button"
                role="tab"
                className="tab"
                id={`opp-tab-${state}`}
                // Only the selected tab owns an existing panel; a reference from
                // any other tab would dangle.
                aria-controls={filter === state ? `opp-panel-${state}` : undefined}
                aria-selected={filter === state}
                tabIndex={filter === state ? 0 : -1}
                onClick={() => selectState(state)}
                onKeyDown={(event) => handleTabKey(event, index)}
              >
                {STATE_LABEL[state]}{' '}
                <span className={COUNT_CLASS[state]}>{load.counts[state]}</span>
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
            {load.state === 'loading' ? (
              <p className="status-message" role="status">
                Loading {filterLabel} opportunities…
              </p>
            ) : filtered.length === 0 ? (
              <section className="panel empty-panel" role="status">
                <h2>No {filterLabel} opportunities</h2>
                <p>
                  There are {overall} opportunities in other states; none in this filter. Switch
                  states above to review them. {filterLabel} is empty, not missing.
                </p>
              </section>
            ) : (
              <section className="panel" aria-labelledby="ledger-table-heading">
                <div className="panel-head">
                  <h2 id="ledger-table-heading">
                    {STATE_LABEL[filter]} opportunities ({load.total})
                  </h2>
                  <span className="mono-muted">expiry in UTC</span>
                </div>
                <div
                  className="table-wrap"
                  role="region"
                  aria-labelledby="ledger-table-heading"
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
                        <th scope="col">Age</th>
                        <th scope="col">Expiry</th>
                        <th scope="col">Source</th>
                        <th scope="col">State</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((item) => {
                        const trigger = item.trigger_conditions[0]
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
                            <td className={item.side === 'SHORT' ? 'value-down' : 'value-up'}>
                              {item.side === 'SHORT' ? `▼ ${item.side}` : `▲ ${item.side}`}
                            </td>
                            <td className="mono">
                              {item.zone.low} – {item.zone.high}{' '}
                              <span className="mono-muted">{item.entry_preference}</span>
                            </td>
                            <td className="calc-note">
                              {trigger ? describeCondition(trigger) : '—'}
                            </td>
                            <td>
                              <span className="mono">{item.confidence}</span>{' '}
                              {band !== null && <span className="chip">{band}</span>}
                            </td>
                            <td className="mono">
                              {formatOpportunityAge(item.created_at, renderedAt)}
                            </td>
                            <td className="mono">{utcLabel(item.expires_at)} UTC</td>
                            <td>
                              {item.source.scan_id ? (
                                <>
                                  <a
                                    className="rowlink mono"
                                    href={`/scans/${encodeURIComponent(item.source.scan_id)}`}
                                  >
                                    {item.source.scan_id}
                                  </a>
                                  <br />
                                  {item.source.report_id ? (
                                    <a
                                      className="rowlink mono-muted"
                                      href={`/reports/${encodeURIComponent(item.source.report_id)}`}
                                    >
                                      {item.source.report_id}
                                    </a>
                                  ) : (
                                    <span className="mono-muted">N/A</span>
                                  )}
                                </>
                              ) : (
                                <span className="mono-muted">N/A</span>
                              )}
                            </td>
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
                {load.total > filtered.length && (
                  <p className="panel-foot">
                    Showing {filtered.length} of {load.total} {filterLabel} opportunities.
                  </p>
                )}
              </section>
            )}
          </div>
        </>
      )}
    </section>
  )
}
