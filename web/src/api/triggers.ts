import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

/** A trigger status as persisted by the backend. Unknown values stay strings. */
export type TriggerStatus = string

export interface TriggerConditionResult {
  condition_id: string
  type: string
  met: boolean
  observed_value: string | null
  threshold: string | null
  evaluated_at: string
  data_quality: string
  evaluable: boolean
  note: string
}

export interface TriggerSnapshot {
  market_snapshot_id: string
  snapshot_hash: string
  as_of: string
  data_quality: string
}

export interface RiskRequested {
  entry_price?: string | null
  stop_price?: string | null
  quantity?: string | null
  risk_pct?: number | null
  leverage?: number | null
  notional_usd?: string | null
}

export interface RiskAllowed {
  quantity?: string | null
  notional_usd?: string | null
  leverage?: number | null
  risk_amount_usd?: string | null
}

/** The persisted deterministic risk verdict (`common.schema.json#/$defs/riskVerdict`). */
export interface RiskVerdict {
  verdict: string
  requested?: RiskRequested
  allowed?: RiskAllowed
  binding_cap?: string
  reasons?: string[]
  computed_at?: string
  expires_at?: string
  inputs_hash?: string
  stale_inputs?: string[]
  provenance?: string
}

export interface TriggerHumanAction {
  action?: string
  actor?: { kind?: string; id?: string }
  at?: string
  reason?: string | null
  confirmation_token_used?: boolean
}

export interface TriggerSummary {
  trigger_id: string
  opportunity_id: string
  symbol: string
  side: 'LONG' | 'SHORT'
  status: TriggerStatus
  triggered_at: string
  trigger_source: string
  idempotency_key: string
  created_order_id: string | null
}

export interface TriggerPayload extends TriggerSummary {
  schema_version: string
  environment: string
  condition_results: TriggerConditionResult[]
  snapshot: TriggerSnapshot
  entry_decision_id: string | null
  risk_verdict: RiskVerdict | null
  human_action: TriggerHumanAction | null
  rejection_reason: string | null
}

export interface DecisionCardTrigger {
  trigger_id: string
  opportunity_id: string | null
  symbol: string | null
  side: string | null
  status: string | null
  trigger_source: string | null
  triggered_at: string | null
  snapshot?: TriggerSnapshot | null
  condition_results: TriggerConditionResult[]
  provenance?: string
}

export interface DecisionProvenance {
  provider: string
  model: string
  model_snapshot: string | null
  prompt_id: string
  prompt_version: number
  prompt_hash: string
  knowledge_artifact_hash: string | null
  knowledge_status: string | null
  knowledge_reason: string | null
  input_hash: string
  schema_version: string
  temperature: number
  seed: string | null
  request_id: string
  latency_ms: number
  usage: Record<string, number>
  cost: {
    amount_usd: string | null
    currency: string
    price_version: string
    unpriced: boolean
  }
  recorded_at: string
}

export interface DecisionCardLlm {
  decision_id?: string | null
  decision?: string | null
  validation_status?: string | null
  confidence?: number | null
  rationale?: string | null
  reasons?: string[]
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: number
  cost?: { amount_usd?: string | null; currency?: string; unpriced?: boolean }
  usage?: { total_tokens?: number }
  provenance?: DecisionProvenance | null
}

/**
 * @deprecated Pre-slice-27 cards carried a bare stage marker as `provenance`.
 * The backend now always emits the structured `DecisionProvenance` object (or
 * null); this variant exists only so older fixtures keep compiling and must not
 * be produced by new code.
 */
export interface LegacyDecisionCardLlm extends Omit<DecisionCardLlm, 'provenance'> {
  provenance?: 'CALC' | 'LLM' | 'MIXED' | null
}

export interface DecisionCard {
  decision_id?: string | null
  trigger_id: string
  status?: string | null
  rejection_reason?: string | null
  data_quality?: string | null
  error_code?: string | null
  stage_order?: string[]
  trigger: DecisionCardTrigger
  llm: DecisionCardLlm | LegacyDecisionCardLlm
  risk: RiskVerdict | null
  human_action: TriggerHumanAction | null
}

export interface TriggerListData {
  items: TriggerSummary[]
  total: number
}

export interface TriggerAcceptData {
  status: string
  decision_id: string
  risk_verdict: RiskVerdict
  order: Record<string, unknown> | null
  fill: Record<string, unknown> | null
  position: Record<string, unknown> | null
  account: Record<string, unknown> | null
}

export interface TriggerRejectData {
  trigger_id?: string
  opportunity_id?: string
  status?: string
  rejection_reason?: string | null
  human_action?: TriggerHumanAction | null
}

export interface TriggerEnvelope<E> {
  ok: boolean
  data: E | null
  error: ApiError | null
  meta: ApiMeta
}

export type TriggerListEnvelope = TriggerEnvelope<TriggerListData>
export type TriggerDetailEnvelope = TriggerEnvelope<TriggerPayload>
export type DecisionCardEnvelope = TriggerEnvelope<DecisionCard>
export type TriggerAcceptEnvelope = TriggerEnvelope<TriggerAcceptData>
export type TriggerRejectEnvelope = TriggerEnvelope<TriggerRejectData>

export class TriggerApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'TriggerApiError'
    this.code = code
  }
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null
  }
  const match = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null
}

/** A stable key is required for replay: duplicate accepts must not create a second order. */
export function idempotencyKey(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function mutationHeaders(options: {
  idempotencyKey?: string
  json?: boolean
}): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options.json) {
    headers['Content-Type'] = 'application/json'
  }
  if (options.idempotencyKey) {
    headers['Idempotency-Key'] = options.idempotencyKey
  }
  const csrf = readCookie('oft_csrf')
  if (csrf) {
    headers['X-CSRF-Token'] = csrf
  }
  return headers
}

async function requestEnvelope<T>(
  path: string,
  options: {
    method?: 'GET' | 'POST'
    body?: unknown
    idempotencyKey?: string
    signal?: AbortSignal
  } = {},
): Promise<TriggerEnvelope<T>> {
  const method = options.method ?? 'GET'
  const mutating = method !== 'GET'
  const response = await fetch(path, {
    method,
    headers: mutating
      ? mutationHeaders({
          idempotencyKey: options.idempotencyKey,
          json: options.body !== undefined,
        })
      : { Accept: 'application/json' },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: 'same-origin',
    signal: options.signal,
  })
  let envelope: TriggerEnvelope<T> | null = null
  try {
    envelope = (await response.json()) as TriggerEnvelope<T>
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    throw new TriggerApiError(
      envelope?.error?.message ?? `Trigger request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

export async function fetchTriggers(
  options: { limit?: number; status?: string; opportunityId?: string } = {},
  signal?: AbortSignal,
): Promise<TriggerListEnvelope> {
  const params = new URLSearchParams()
  params.set('limit', String(options.limit ?? 50))
  if (options.status) {
    params.set('status', options.status)
  }
  if (options.opportunityId) {
    params.set('opportunity_id', options.opportunityId)
  }
  return requestEnvelope<TriggerListData>(`/api/v1/triggers?${params.toString()}`, { signal })
}

export async function getTrigger(
  triggerId: string,
  signal?: AbortSignal,
): Promise<TriggerDetailEnvelope> {
  return requestEnvelope<TriggerPayload>(`/api/v1/triggers/${encodeURIComponent(triggerId)}`, {
    signal,
  })
}

export async function recheckTrigger(
  triggerId: string,
  key: string,
  signal?: AbortSignal,
): Promise<DecisionCardEnvelope> {
  return requestEnvelope<DecisionCard>(
    `/api/v1/triggers/${encodeURIComponent(triggerId)}/recheck`,
    { method: 'POST', idempotencyKey: key, signal },
  )
}

export async function acceptTrigger(
  triggerId: string,
  key: string,
  signal?: AbortSignal,
): Promise<TriggerAcceptEnvelope> {
  return requestEnvelope<TriggerAcceptData>(
    `/api/v1/triggers/${encodeURIComponent(triggerId)}/accept`,
    { method: 'POST', idempotencyKey: key, signal },
  )
}

export async function rejectTrigger(
  triggerId: string,
  reason: string,
  key: string,
  signal?: AbortSignal,
): Promise<TriggerRejectEnvelope> {
  return requestEnvelope<TriggerRejectData>(
    `/api/v1/triggers/${encodeURIComponent(triggerId)}/reject`,
    { method: 'POST', body: { reason }, idempotencyKey: key, signal },
  )
}

/** Statuses where a human may still accept or reject (mirrors the backend). */
export function isPendingStatus(status: TriggerStatus): boolean {
  return status === 'PENDING_RECHECK' || status === 'RECHECKED'
}

/** A short, fixed design age label; never fabricates a value for bad input. */
export function formatAge(triggeredAt: string, nowMs: number): string {
  const started = Date.parse(triggeredAt)
  if (Number.isNaN(started)) {
    return '—'
  }
  const seconds = Math.max(0, Math.floor((nowMs - started) / 1000))
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    return `${minutes}m`
  }
  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`
  }
  return `${Math.floor(hours / 24)}d ${String(hours % 24).padStart(2, '0')}h`
}

export function riskVerdictChip(verdict: RiskVerdict | null): { label: string; className: string } {
  if (!verdict) {
    return { label: 'PENDING', className: 'chip' }
  }
  if (verdict.verdict === 'ALLOW') {
    return { label: 'PASS', className: 'chip chip--ok' }
  }
  if (verdict.verdict === 'REDUCE') {
    return { label: 'REDUCE', className: 'chip chip--warn' }
  }
  if (verdict.verdict === 'BLOCK') {
    return { label: 'BLOCKED', className: 'chip chip--danger' }
  }
  return { label: verdict.verdict, className: 'chip chip--warn' }
}

export function isStaleMark(verdict: RiskVerdict | null): boolean {
  const values = verdict?.stale_inputs
  if (!Array.isArray(values)) {
    return false
  }
  return values.some((name) => typeof name === 'string' && /mark|price/i.test(name))
}

/**
 * The single reason accept is unavailable, or null when a human may act.
 *
 * Only a blocked/unmeasured gate or a stale mark disables accept. A gate that
 * reports PARTIAL for inputs the caps do not require (e.g. spread, daily loss)
 * still allows the stricter computed size, exactly as the CLI does.
 */
export function acceptBlockedReason(verdict: RiskVerdict | null): string | null {
  if (!verdict) {
    return '接受不可用：风险门尚未评估，请先运行 LLM 复核。'
  }
  const stale = Array.isArray(verdict.stale_inputs)
    ? verdict.stale_inputs.filter((name) => typeof name === 'string')
    : []
  if (verdict.verdict === 'BLOCK') {
    const quantity = verdict.allowed?.quantity
    const zero =
      quantity === null || quantity === undefined || quantity === '' || Number(quantity) <= 0
        ? '；最终规模为 0'
        : ''
    const suffix = stale.length > 0 ? `；stale ${stale.join(', ')}` : ''
    return `接受不可用：风险门 BLOCK（绑定上限 ${verdict.binding_cap ?? 'N/A'}${zero}${suffix}）。`
  }
  if (verdict.verdict !== 'ALLOW' && verdict.verdict !== 'REDUCE') {
    return `接受不可用：风险门 ${verdict.verdict}。`
  }
  const quantity = verdict.allowed?.quantity
  if (quantity === null || quantity === undefined || quantity === '' || Number(quantity) <= 0) {
    return '接受不可用：最终规模为 0，不能提交订单。'
  }
  if (isStaleMark(verdict)) {
    return `接受不可用：mark 价格已过期（stale ${stale.join(', ')}）。`
  }
  return null
}
