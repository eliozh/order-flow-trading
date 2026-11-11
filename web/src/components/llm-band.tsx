import { useId } from 'react'
import type { ReactNode } from 'react'

import type { LlmCostState, LlmState, LlmUsageState } from '../api/reports'
import { confidenceBand } from '../api/reports'
import { ProvenanceStamp } from './provenance'

/**
 * The ledger stores a validated LLM draft, but it does not persist a
 * validation status to re-display. This shape is deliberately explicit: it
 * carries only values that exist in `provenance.llm` and never invents a
 * `configured` flag, a `validation_status`, or a knowledge status.
 */
export interface LedgerLlmState {
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: number
  knowledge_hash?: string | null
  confidence?: number | null
  narrative?: string | null
  cost?: LlmCostState
  usage?: LlmUsageState
}

interface LlmBandCommonProps {
  /** Optional override for the untrusted prose (used by the opportunity ledger). */
  narrative?: string | null
  title?: string
  decisionId?: string | null
  narrativeRef?: string | null
}

export type LlmBandProps = LlmBandCommonProps &
  (
    | {
        /** The rich LLM state from the report payload. */ mode?: 'report'
        llm: LlmState
        ledger?: never
      }
    | {
        /** Provenance-only state; never re-validated on this page. */ mode: 'ledger'
        ledger: LedgerLlmState
        llm?: never
      }
  )

interface LlmMetadata {
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: number
  confidence?: number | null
  cost?: LlmCostState
  usage?: LlmUsageState
}

const CHIP: Record<string, string> = {
  VALID: 'chip chip--ok',
  REPAIRED: 'chip chip--ok',
  REJECTED: 'chip chip--danger',
}

const LEDGER_CHIP_LABEL = 'from ledger · not re-validated'

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

function costLabel(llm: LlmMetadata): string {
  const cost = llm.cost
  if (!cost) {
    return 'N/A'
  }
  if (cost.amount_usd === null || cost.amount_usd === undefined) {
    // Never drop the unpriced signal: an unknown price is not a measured zero.
    return cost.unpriced ? 'unpriced · N/A' : 'N/A'
  }
  const amount = `$${cost.amount_usd}`
  return cost.unpriced ? `${amount} (unpriced)` : amount
}

function confidenceLabel(llm: LlmMetadata): string {
  if (llm.confidence === null || llm.confidence === undefined) {
    return 'N/A'
  }
  const band = confidenceBand(llm.confidence)
  return band === null ? String(llm.confidence) : `${llm.confidence} · ${band}`
}

function promptLabel(llm: LlmMetadata): string {
  if (!llm.prompt_id) {
    return 'N/A'
  }
  return `${llm.prompt_id} v${llm.prompt_version ?? '—'}`
}

function tokensLabel(llm: LlmMetadata): string {
  const tokens = llm.usage?.total_tokens
  return tokens === undefined ? 'N/A' : `${tokens.toLocaleString('en-US')} tokens`
}

function MetadataRows({ llm }: { llm: LlmMetadata }) {
  return (
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
        <dt>Prompt</dt>
        <dd>{promptLabel(llm)}</dd>
      </div>
      <div>
        <dt>Confidence</dt>
        <dd>{confidenceLabel(llm)}</dd>
      </div>
      <div>
        <dt>Tokens</dt>
        <dd>{tokensLabel(llm)}</dd>
      </div>
      <div>
        <dt>Cost</dt>
        <dd>{costLabel(llm)}</dd>
      </div>
    </dl>
  )
}

function BandFrame({
  headingId,
  title,
  chipClass,
  chipLabel,
  children,
  foot,
}: {
  headingId: string
  title: string
  chipClass: string
  chipLabel: string
  children: ReactNode
  foot?: ReactNode
}) {
  return (
    <section className="band band--llm" aria-labelledby={headingId}>
      <div className="band-head">
        <ProvenanceStamp kind="LLM" />
        <h2 id={headingId}>{title}</h2>
        <span className={chipClass}>{chipLabel}</span>
      </div>
      {children}
      {foot}
    </section>
  )
}

function LedgerBand({
  headingId,
  ledger,
  narrative,
  title,
}: {
  headingId: string
  ledger: LedgerLlmState
  narrative: string | null | undefined
  title: string
}) {
  const prose = narrative === undefined ? ledger.narrative : narrative
  const hasNarrative = typeof prose === 'string' && prose.length > 0
  return (
    <BandFrame headingId={headingId} title={title} chipClass="chip" chipLabel={LEDGER_CHIP_LABEL}>
      <dl className="band-meta">
        <div>
          <dt>Provider</dt>
          <dd>{metadataValue(ledger.provider)}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{metadataValue(ledger.model)}</dd>
        </div>
        <div>
          <dt>Prompt</dt>
          <dd>{promptLabel(ledger)}</dd>
        </div>
        <div>
          <dt>Knowledge hash</dt>
          <dd className="mono">{metadataValue(ledger.knowledge_hash)}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{confidenceLabel(ledger)}</dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd>{tokensLabel(ledger)}</dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>{costLabel(ledger)}</dd>
        </div>
      </dl>

      {hasNarrative ? (
        <>
          <p className="prose llm-narrative">{prose}</p>
          <p className="band-note">
            Untrusted model text stored with the ledger entry, not a measurement. It was not
            re-validated on this page and never overrides the code-owned conditions.
          </p>
        </>
      ) : (
        <p className="llm-empty">No narrative was stored with this opportunity.</p>
      )}
    </BandFrame>
  )
}

function ReportBand({
  headingId,
  llm,
  narrative,
  title,
  decisionId,
  narrativeRef,
}: {
  headingId: string
  llm: LlmState
  narrative: string | null | undefined
  title: string
  decisionId: string | null | undefined
  narrativeRef: string | null | undefined
}) {
  const disabled = !llm.configured
  const rejected = llm.validation_status === 'REJECTED'
  const prose = narrative === undefined ? llm.narrative : narrative
  const hasNarrative = !disabled && !rejected && typeof prose === 'string' && prose.length > 0
  const status = llm.validation_status
  const chipClass = disabled ? 'chip chip--warn' : (CHIP[status ?? ''] ?? 'chip')
  const chipLabel = disabled ? 'disabled' : (status ?? 'unvalidated')

  const foot =
    decisionId !== undefined || narrativeRef !== undefined ? (
      <p className="panel-foot">
        Decision <code>{decisionId ?? 'null'}</code> · narrative{' '}
        <code>{narrativeRef ?? 'null'}</code>. No raw prompt, key, or HTML is rendered.
      </p>
    ) : undefined

  return (
    <BandFrame
      headingId={headingId}
      title={title}
      chipClass={chipClass}
      chipLabel={chipLabel}
      foot={foot}
    >
      {disabled ? (
        <>
          <p>{llm.reason ?? 'LLM not configured.'}</p>
          <p className="llm-empty">No narrative was produced.</p>
        </>
      ) : (
        <>
          <MetadataRows llm={llm} />
          <dl className="band-meta">
            <div>
              <dt>Decision</dt>
              <dd>{metadataValue(llm.decision_type)}</dd>
            </div>
            <div>
              <dt>Knowledge</dt>
              <dd>{knowledgeLabel(llm)}</dd>
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
    </BandFrame>
  )
}

/**
 * The LLM band: model-generated narrative, always visually separate from CALC.
 *
 * The narrative is rendered as a React text node only. This component never
 * injects HTML, never renders raw prompts, and never presents prose as a
 * measurement. Ledger entries use the `ledger` variant: it shows the stored
 * provenance and labels the knowledge value as a hash, and it never claims a
 * validation status it does not have.
 */
export function LlmBand(props: LlmBandProps) {
  const headingId = useId()
  const title =
    props.title ??
    (props.llm !== undefined ? 'LLM · narrative analysis' : 'LLM rationale (untrusted)')

  if (props.mode === 'ledger') {
    return (
      <LedgerBand
        headingId={headingId}
        ledger={props.ledger}
        narrative={props.narrative}
        title={title}
      />
    )
  }

  return (
    <ReportBand
      headingId={headingId}
      llm={props.llm}
      narrative={props.narrative}
      title={title}
      decisionId={props.decisionId}
      narrativeRef={props.narrativeRef}
    />
  )
}
