import type { LlmState } from '../api/reports'
import { confidenceBand } from '../api/reports'
import { ProvenanceStamp } from './provenance'

interface LlmBandProps {
  /** The rich LLM state from the report payload. */
  llm: LlmState
  /** Optional override for the untrusted prose (used by the opportunity ledger). */
  narrative?: string | null
  title?: string
  decisionId?: string | null
  narrativeRef?: string | null
}

const CHIP: Record<string, string> = {
  VALID: 'chip chip--ok',
  REPAIRED: 'chip chip--ok',
  REJECTED: 'chip chip--danger',
}

function metadataValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return 'N/A'
  }
  return String(value)
}

function knowledgeLabel(llm: LlmState): string {
  const knowledge = llm.knowledge
  if (!knowledge) {
    return 'N/A'
  }
  if (knowledge.artifact_id) {
    return `${knowledge.artifact_id} · ${knowledge.status}`
  }
  return `N/A · ${knowledge.status}`
}

function costLabel(llm: LlmState): string {
  const cost = llm.cost
  if (!cost || cost.amount_usd === null || cost.amount_usd === undefined) {
    return 'N/A'
  }
  const amount = `$${cost.amount_usd}`
  return cost.unpriced ? `${amount} (unpriced)` : amount
}

function confidenceLabel(llm: LlmState): string {
  if (llm.confidence === null || llm.confidence === undefined) {
    return 'N/A'
  }
  const band = confidenceBand(llm.confidence)
  return band === null ? String(llm.confidence) : `${llm.confidence} · ${band}`
}

function promptLabel(llm: LlmState): string {
  if (!llm.prompt_id) {
    return 'N/A'
  }
  return `${llm.prompt_id} v${llm.prompt_version ?? '—'}`
}

/**
 * The LLM band: model-generated narrative, always visually separate from CALC.
 *
 * The narrative is rendered as a React text node only. This component never
 * injects HTML, never renders raw prompts, and never presents prose as a
 * measurement.
 */
export function LlmBand({
  llm,
  narrative,
  title = 'LLM · narrative analysis',
  decisionId,
  narrativeRef,
}: LlmBandProps) {
  const disabled = !llm.configured
  const rejected = llm.validation_status === 'REJECTED'
  const prose = narrative === undefined ? llm.narrative : narrative
  const hasNarrative = !disabled && !rejected && typeof prose === 'string' && prose.length > 0
  const status = llm.validation_status
  const chipClass = disabled ? 'chip chip--warn' : (CHIP[status ?? ''] ?? 'chip')
  const chipLabel = disabled ? 'disabled' : (status ?? 'unvalidated')
  const tokens = llm.usage?.total_tokens

  return (
    <section className="band band--llm" aria-labelledby="llm-heading">
      <div className="band-head">
        <ProvenanceStamp kind="LLM" />
        <h2 id="llm-heading">{title}</h2>
        <span className={chipClass}>{chipLabel}</span>
      </div>

      {disabled ? (
        <>
          <p>{llm.reason ?? 'LLM not configured.'}</p>
          <p className="llm-empty">No narrative was produced.</p>
        </>
      ) : (
        <>
          <dl className="band-meta">
            <div>
              <dt>Provider</dt>
              <dd>{metadataValue(llm.provider)}</dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd>{metadataValue(llm.model)}</dd>
            </div>
            <div>
              <dt>Decision</dt>
              <dd>{metadataValue(llm.decision_type)}</dd>
            </div>
            <div>
              <dt>Prompt</dt>
              <dd>{promptLabel(llm)}</dd>
            </div>
            <div>
              <dt>Knowledge</dt>
              <dd>{knowledgeLabel(llm)}</dd>
            </div>
            <div>
              <dt>Confidence</dt>
              <dd>{confidenceLabel(llm)}</dd>
            </div>
            <div>
              <dt>Tokens</dt>
              <dd>{tokens === undefined ? 'N/A' : `${tokens.toLocaleString('en-US')} tokens`}</dd>
            </div>
            <div>
              <dt>Cost</dt>
              <dd>{costLabel(llm)}</dd>
            </div>
          </dl>

          {rejected ? (
            <>
              <p className="llm-reason">
                <strong>Narrative REJECTED.</strong>{' '}
                <code>{llm.reason ?? 'The narrative failed validation.'}</code>
              </p>
              <p className="llm-empty">
                No narrative was produced. The CALC report remains valid and readable.
              </p>
            </>
          ) : hasNarrative ? (
            <>
              <p className="prose llm-narrative">{prose}</p>
              <p className="band-note">
                Untrusted model text, not a measurement. Narrative never overrides CALC metrics.
              </p>
            </>
          ) : (
            <p className="llm-empty">No narrative was produced.</p>
          )}
        </>
      )}

      {(decisionId !== undefined || narrativeRef !== undefined) && (
        <p className="panel-foot">
          Decision <code>{decisionId ?? 'null'}</code> · narrative{' '}
          <code>{narrativeRef ?? 'null'}</code>. No raw prompt, key, or HTML is rendered.
        </p>
      )}
    </section>
  )
}
