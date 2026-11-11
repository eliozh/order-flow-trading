import { useEffect, useState } from 'react'

import {
  conditionThreshold,
  describeCondition,
  fetchOpportunity,
  type OpportunityCondition,
  type OpportunityData,
  type OpportunityEnvelope,
} from '../api/opportunities'
import { confidenceBand, utcLabel, type LlmState } from '../api/reports'
import { DataQualityBadge } from '../components/data-quality'
import { LlmBand } from '../components/llm-band'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'

interface OpportunityDetailPageProps {
  opportunityId: string
  fetchOpportunityFn?: (opportunityId: string) => Promise<OpportunityEnvelope>
  isCompact?: boolean
}

interface DetailState {
  state: 'loading' | 'ready' | 'error'
  opportunity?: OpportunityData
  message?: string
}

function conditionState(condition: OpportunityCondition): { label: string; className: string } {
  if (!condition.evaluable) {
    return { label: 'UNAVAILABLE', className: 'chip chip--warn' }
  }
  if (condition.last_result === true) {
    return { label: 'MET', className: 'chip chip--ok' }
  }
  if (condition.last_result === false) {
    return { label: 'NOT MET', className: 'chip' }
  }
  return { label: 'UNKNOWN', className: 'chip' }
}

function ConditionTable({
  tableLabel,
  conditions,
}: {
  tableLabel: string
  conditions: OpportunityCondition[]
}) {
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
    <div className="table-wrap" role="region" aria-label={`${tableLabel} table`} tabIndex={0}>
      <table className="data-table condition-table">
        <caption className="visually-hidden">{tableLabel}</caption>
        <thead>
          <tr>
            <th scope="col">Condition</th>
            <th scope="col">Current value</th>
            <th scope="col">Threshold</th>
            <th scope="col">State</th>
            <th scope="col">Availability</th>
          </tr>
        </thead>
        <tbody>
          {conditions.map((condition) => {
            const state = conditionState(condition)
            return (
              <tr key={condition.condition_id}>
                <th scope="row">{describeCondition(condition)}</th>
                <td className="mono">{condition.last_value ?? '—'}</td>
                <td className="mono">{conditionThreshold(condition)}</td>
                <td>
                  <span className={state.className}>{state.label}</span>
                </td>
                <td>
                  <span className={condition.evaluable ? 'chip chip--ok' : 'chip chip--danger'}>
                    {condition.evaluable ? 'AVAILABLE' : 'MISSING INPUT'}
                  </span>
                  {!condition.evaluable && (
                    <p className="condition-note">
                      Required: {condition.required_data.join(', ')}
                      {condition.unavailable_reason
                        ? ` — ${condition.unavailable_reason}`
                        : ' — this condition is never treated as met.'}
                    </p>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function sourceReport(reportId: string) {
  if (!reportId) {
    return (
      <span>
        <span className="mono">N/A</span> · <a href="/reports">reports list</a>
      </span>
    )
  }
  return (
    <a className="rowlink mono" href={`/reports/${encodeURIComponent(reportId)}`}>
      {reportId}
    </a>
  )
}

function llmStateFromProvenance(opportunity: OpportunityData): LlmState {
  const llm = opportunity.provenance.llm
  return {
    configured: true,
    provider: llm.provider,
    model: llm.model,
    prompt_id: llm.prompt_id,
    prompt_version: llm.prompt_version,
    knowledge: {
      status: llm.knowledge_artifact_hash ? 'AVAILABLE' : 'ABSENT',
      artifact_id: llm.knowledge_artifact_hash ?? null,
    },
    validation_status: 'VALID',
    confidence: opportunity.confidence,
    narrative: opportunity.thesis,
    cost: llm.cost,
    usage: llm.usage,
  }
}

export function OpportunityDetailPage({
  opportunityId,
  fetchOpportunityFn = fetchOpportunity,
  isCompact,
}: OpportunityDetailPageProps) {
  const [load, setLoad] = useState<DetailState>({ state: 'loading' })
  const compact = useCompact(isCompact)

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    fetchOpportunityFn(opportunityId)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', message: 'Opportunity response was empty.' })
          return
        }
        setLoad({ state: 'ready', opportunity: envelope.data })
      })
      .catch((error: unknown) => {
        if (active) {
          setLoad({
            state: 'error',
            message: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      })
    return () => {
      active = false
    }
  }, [opportunityId, fetchOpportunityFn])

  if (load.state === 'loading') {
    return (
      <p className="status-message" role="status">
        Loading opportunity {opportunityId}…
      </p>
    )
  }

  if (load.state === 'error' || !load.opportunity) {
    return (
      <section className="panel" role="alert">
        <h1>Opportunity not found</h1>
        <p>
          {opportunityId}: {load.message ?? 'This ledger entry does not exist or was removed.'}
        </p>
        <a className="btn" href="/opportunities">
          Back to opportunities
        </a>
      </section>
    )
  }

  const opportunity = load.opportunity
  const band = confidenceBand(opportunity.confidence)
  const directionClass = opportunity.side === 'SHORT' ? 'value-down' : 'value-up'
  const directionLabel =
    opportunity.side === 'SHORT' ? `▼ ${opportunity.side}` : `▲ ${opportunity.side}`
  const llm = llmStateFromProvenance(opportunity)

  return (
    <section className="system-page" aria-labelledby="opportunity-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/opportunities">
              Opportunities
            </a>{' '}
            / detail
          </p>
          <h1 id="opportunity-heading" className="mono">
            {opportunity.opportunity_id}
          </h1>
          <p className="page-sub">
            {opportunity.symbol} · {opportunity.side} · state {opportunity.state}
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="LLM" />
          <ProvenanceStamp kind="CALC" />
          <DataQualityBadge status={opportunity.data_quality.status} />
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          Read-only at this width. State transitions are executed by code.
        </p>
      )}

      <p className="ledger-banner" role="note">
        {opportunity.state === 'TRIGGERED'
          ? 'Triggered by code; awaiting entry review. Users cannot trigger, accept, or reject from the ledger.'
          : 'The ledger is read-only. Conditions are evaluated by code; users cannot trigger manually.'}
      </p>

      <dl className="stat-grid">
        <div className="stat">
          <dt>Direction</dt>
          <dd className={directionClass}>{directionLabel}</dd>
          <dd className="stat-note">{opportunity.symbol}</dd>
        </div>
        <div className="stat">
          <dt>Zone</dt>
          <dd className="mono">
            {opportunity.zone.low} – {opportunity.zone.high}
          </dd>
          <dd className="stat-note">{opportunity.entry_preference}</dd>
        </div>
        <div className="stat">
          <dt>Confidence</dt>
          <dd>{band === null ? opportunity.confidence : `${opportunity.confidence} · ${band}`}</dd>
          <dd className="stat-note">band ≥ 0.70 is high</dd>
        </div>
        <div className="stat">
          <dt>Expiry</dt>
          <dd className="mono">{utcLabel(opportunity.expires_at)} UTC</dd>
          <dd className="stat-note">fixed UTC instant</dd>
        </div>
      </dl>

      <div className="detail-cols">
        <div className="stack-sm">
          <section className="panel" aria-labelledby="trigger-conditions-heading">
            <div className="panel-head">
              <h2 id="trigger-conditions-heading">Trigger conditions</h2>
              <span className="chip">code evaluated</span>
            </div>
            <ConditionTable
              tableLabel="Trigger conditions"
              conditions={opportunity.trigger_conditions}
            />
            <p className="panel-foot">
              Each condition is evaluated independently by code. The LLM cannot change a threshold,
              and an unevaluable condition is never treated as met.
            </p>
          </section>

          <section className="panel" aria-labelledby="invalidation-heading">
            <div className="panel-head">
              <h2 id="invalidation-heading">Invalidation</h2>
            </div>
            <ConditionTable
              tableLabel="Invalidation conditions"
              conditions={opportunity.invalidation_conditions}
            />
          </section>

          <section className="panel" aria-labelledby="source-heading">
            <div className="panel-head">
              <h2 id="source-heading">Source</h2>
            </div>
            <dl className="kv">
              <dt>Report</dt>
              <dd>{sourceReport(opportunity.source.report_id)}</dd>
              <dt>Scan</dt>
              <dd>
                <a
                  className="rowlink mono"
                  href={`/scans/${encodeURIComponent(opportunity.source.scan_id)}`}
                >
                  {opportunity.source.scan_id}
                </a>
              </dd>
              <dt>Decision</dt>
              <dd className="mono">{opportunity.source.decision_id}</dd>
              <dt>State</dt>
              <dd>code-owned · no manual trigger</dd>
            </dl>
          </section>
        </div>

        <div className="stack-sm">
          <LlmBand llm={llm} narrative={opportunity.thesis} title="LLM rationale (untrusted)" />

          <section className="panel" aria-labelledby="timeline-heading">
            <div className="panel-head">
              <h2 id="timeline-heading">State timeline</h2>
            </div>
            <ol className="timeline" aria-label="State timeline">
              {opportunity.state_history.map((entry, index) => (
                <li key={`${entry.at}-${entry.to_state}-${index}`}>
                  <span className="tl-time mono">{utcLabel(entry.at)} UTC</span>
                  <span className="tl-title">
                    {entry.from_state === null
                      ? entry.to_state
                      : `${entry.from_state} → ${entry.to_state}`}
                  </span>
                  <span className="tl-reason mono">{entry.reason_code}</span>
                  <span className="tl-desc">{entry.note}</span>
                </li>
              ))}
            </ol>
          </section>
        </div>
      </div>
    </section>
  )
}
