import { fetchWithTimeout } from './request-timeout'
import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

export type OpportunityState = 'OPEN' | 'TRIGGERED' | 'EXPIRED' | 'INVALIDATED'

/** Position direction, constrained to the two values the ledger can hold. */
export type Side = 'LONG' | 'SHORT'

export interface OpportunityCondition {
  condition_id: string
  type: string
  params: Record<string, unknown>
  window: { start: string; end: string }
  required_data: string[]
  evaluable: boolean
  unavailable_reason?: string | null
  last_value?: string | null
  last_evaluated_at?: string | null
  last_result?: boolean | null
}

export interface OpportunityStateHistoryEntry {
  from_state: OpportunityState | null
  to_state: OpportunityState
  at: string
  reason_code: string
  note: string
}

export interface OpportunitySource {
  scan_id: string
  report_id: string
  decision_id: string
}

export interface OpportunityLlmProvenance {
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: number
  prompt_hash?: string
  knowledge_artifact_hash?: string | null
  cost?: {
    amount_usd?: string | null
    currency?: string
    price_version?: string
    unpriced?: boolean
  }
  usage?: { total_tokens?: number }
}

export interface OpportunityProvenance {
  llm: OpportunityLlmProvenance
  calc_snapshot_id: string
  calc_snapshot_hash: string
  calc_metric_version: string
}

export interface OpportunityDataQuality {
  status: string
  reasons: string[]
  gaps: unknown[]
  last_event_at: string | null
  coverage_start: string | null
}

export interface OpportunityRiskRequest {
  entry_price: string
  stop_price: string
  quantity: string | null
  risk_pct: number
  leverage: number
  notional_usd: string | null
}

export interface OpportunityData {
  schema_version: string
  opportunity_id: string
  symbol: string
  side: Side
  state: OpportunityState
  created_at: string
  updated_at: string
  zone: { low: string; high: string }
  entry_preference: string
  trigger_conditions: OpportunityCondition[]
  invalidation_conditions: OpportunityCondition[]
  expires_at: string
  thesis: string
  confidence: number
  risk_request: OpportunityRiskRequest
  source: OpportunitySource
  provenance: OpportunityProvenance
  data_quality: OpportunityDataQuality
  state_history: OpportunityStateHistoryEntry[]
}

export interface OpportunitySummary {
  opportunity_id: string
  symbol: string
  side: Side
  state: OpportunityState
  confidence: number
  zone: { low: string; high: string }
  entry_preference: string
  trigger_conditions: OpportunityCondition[]
  expires_at: string
  created_at: string
  updated_at: string
  source: OpportunitySource
}

export interface OpportunityListData {
  items: OpportunitySummary[]
  /** The unfiltered total for the requested ``state`` filter. */
  total: number
  /** Server-side COUNT per state; tab counts never come from the page. */
  counts: Record<OpportunityState, number>
}

export interface OpportunityTimelineData {
  opportunity_id: string
  items: OpportunityStateHistoryEntry[]
}

export interface OpportunityEnvelope {
  ok: boolean
  data: OpportunityData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface OpportunityListEnvelope {
  ok: boolean
  data: OpportunityListData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface OpportunityTimelineEnvelope {
  ok: boolean
  data: OpportunityTimelineData | null
  error: ApiError | null
  meta: ApiMeta
}

export class OpportunityApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'OpportunityApiError'
    this.code = code
  }
}

async function getEnvelope<T>(
  path: string,
  signal?: AbortSignal,
): Promise<{ ok: boolean; data: T | null; error: ApiError | null; meta: ApiMeta }> {
  const response = await fetchWithTimeout(
    path,
    { headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    signal,
  )
  let envelope: { ok: boolean; data: T | null; error: ApiError | null; meta: ApiMeta } | null = null
  try {
    envelope = (await response.json()) as {
      ok: boolean
      data: T | null
      error: ApiError | null
      meta: ApiMeta
    }
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    throw new OpportunityApiError(
      envelope?.error?.message ?? `Opportunity request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

export async function fetchOpportunities(
  options: { state?: OpportunityState; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<OpportunityListEnvelope> {
  const params = new URLSearchParams()
  params.set('limit', String(options.limit ?? 50))
  if (options.offset !== undefined) {
    params.set('offset', String(options.offset))
  }
  if (options.state) {
    params.set('state', options.state)
  }
  return getEnvelope<OpportunityListData>(`/api/v1/opportunities?${params.toString()}`, signal)
}

export async function fetchOpportunity(
  opportunityId: string,
  signal?: AbortSignal,
): Promise<OpportunityEnvelope> {
  return getEnvelope<OpportunityData>(
    `/api/v1/opportunities/${encodeURIComponent(opportunityId)}`,
    signal,
  )
}

export async function fetchOpportunityTimeline(
  opportunityId: string,
  signal?: AbortSignal,
): Promise<OpportunityTimelineEnvelope> {
  return getEnvelope<OpportunityTimelineData>(
    `/api/v1/opportunities/${encodeURIComponent(opportunityId)}/timeline`,
    signal,
  )
}

function stringParam(params: Record<string, unknown>, key: string): string | null {
  const value = params[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** A short, deterministic label for a condition. Never derived from narrative. */
export function describeCondition(condition: OpportunityCondition): string {
  const type = condition.type.replaceAll('_', ' ').toLowerCase()
  const detail = conditionThreshold(condition)
  return detail === '—' ? type : `${type} · ${detail}`
}

/** The threshold/value part of a condition, shown next to its current value. */
export function conditionThreshold(condition: OpportunityCondition): string {
  const params = condition.params
  const price = stringParam(params, 'price')
  if (price !== null) {
    return `@ ${price}`
  }
  const low = stringParam(params, 'price_low')
  const high = stringParam(params, 'price_high')
  if (low !== null || high !== null) {
    return `${low ?? '—'} – ${high ?? '—'}`
  }
  const threshold = stringParam(params, 'threshold')
  if (threshold !== null) {
    const comparator = stringParam(params, 'comparator') ?? ''
    const unit = stringParam(params, 'unit') ?? ''
    return `${comparator} ${threshold} ${unit}`.trim()
  }
  const sessionStates = params.session_states
  if (Array.isArray(sessionStates) && sessionStates.length > 0) {
    return sessionStates.map(String).join(', ')
  }
  return '—'
}
