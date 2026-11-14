import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

/**
 * A mark older than this is STALE; management is conservatively gated on it.
 *
 * Coupled to the backend default `OFT_RISK_STALE_MARK_SECONDS` (60s). If an
 * operator overrides that env value, the UI gate and the server stale rule
 * disagree; the server remains authoritative for risk.
 */
export const MARK_FRESHNESS_MS = 60_000

/** UI revalidation cadence for the positions list and detail (truthful copy). */
export const POSITION_REFRESH_MS = 5_000

/** Position direction, constrained to the two values the ledger can hold. */
export type PositionSide = 'LONG' | 'SHORT' | string

/** A position state as persisted; unknown values stay strings for display. */
export type PositionState = 'OPEN' | 'CLOSED' | 'LIQUIDATED' | 'RECONCILING' | string

export interface PositionDataQuality {
  status: string
  reasons: string[]
  gaps?: unknown[]
  last_event_at?: string | null
  coverage_start?: string | null
}

export interface PositionManagementEntry {
  decision_id?: string
  action: string
  at: string
  executed_order_id: string | null
  note?: string | null
}

/** The persisted position contract (`GET /api/v1/positions/{id}`). */
export interface PositionData {
  schema_version?: string
  position_id: string
  account_id: string
  environment: string
  symbol: string
  side: PositionSide
  state: PositionState
  size: string
  entry_price: string
  avg_entry_price: string
  mark_price: string | null
  notional_usd: string
  /** Persisted as null on OPEN positions; the UI computes the CALC term. */
  unrealized_pnl_usd: string | null
  unrealized_pnl_pct: string | null
  stop_price: string | null
  target_prices: string[]
  leverage: number
  margin_mode: string
  margin_usd: string
  risk_amount_usd: string
  risk_pct: number
  fees_usd: string
  funding_usd: string
  realized_pnl_usd: string | null
  opened_at: string
  updated_at: string
  closed_at: string | null
  exit_price: string | null
  source_trigger_id: string | null
  source_decision_id: string | null
  management_history: PositionManagementEntry[]
  data_quality: PositionDataQuality
  version?: number
}

export interface PositionListData {
  items: PositionData[]
  total: number
  counts?: Record<string, number> | null
}

export interface PositionFundingItem {
  accrual_id: string | null
  settlement_time: string
  rate: string | null
  mark_price: string | null
  amount_usd: string | null
  side: string
  size: string | null
  created_at: string | null
  accrued: boolean
  reason: string | null
}

export interface PositionPnlData {
  basis: string
  gross_pnl_usd: string | null
  realized_gross_pnl_usd: string | null
  fees_usd: string | null
  funding_usd: string | null
  net_pnl_usd: string | null
  r_multiple: number | null
  return_pct: number | null
  mae_usd: string | null
  mfe_usd: string | null
  outcome_tags: string[]
  data_quality: PositionDataQuality
}

export interface PositionHistoryEvent {
  seq: number
  at: string
  action: string
  order_id: string | null
  note: string | null
}

export interface PositionHistoryData {
  position_id: string
  symbol: string | null
  side: string | null
  state: string | null
  size: string | null
  entry_price: string | null
  avg_entry_price: string | null
  events: PositionHistoryEvent[]
  funding: PositionFundingItem[]
  funding_reasons: string[]
  pnl: PositionPnlData
}

export interface PositionManageRefusal {
  code: string
  field: string
  message: string
}

export interface PositionManageRisk {
  verdict: string
  binding_cap?: string
  stale_inputs?: string[]
  reasons?: string[]
  requested?: Record<string, string | number | null>
  allowed?: Record<string, string | number | null>
}

export interface PositionManageLlm {
  decision_id?: string
  action?: string
  validation_status?: string | null
  confidence?: number | null
  rationale?: string | null
  urgency?: string | null
  provenance?: {
    provider?: string
    model?: string
    prompt_version?: number
    knowledge_artifact_hash?: string | null
    cost?: { amount_usd?: string | null; currency?: string; unpriced?: boolean }
  }
}

/** The validated management result (`POST /api/v1/positions/{id}/manage`). */
export interface PositionManageData {
  position_id: string
  decision_id: string
  action: string
  status: string
  executed: boolean
  reason_code: string
  reason: string | null
  llm: PositionManageLlm | null
  risk: PositionManageRisk | null
  order: Record<string, unknown> | null
  fill: Record<string, unknown> | null
  position: PositionData | null
  trade_log: Record<string, unknown> | null
  account: Record<string, unknown> | null
  refusals: PositionManageRefusal[]
  idempotent_replay: boolean
  due_conditions: string[]
}

export interface PositionEnvelope<T> {
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}

export type PositionListEnvelope = PositionEnvelope<PositionListData>
export type PositionDetailEnvelope = PositionEnvelope<PositionData>
export type PositionHistoryEnvelope = PositionEnvelope<PositionHistoryData>
export type PositionManageEnvelope = PositionEnvelope<PositionManageData>

export class PositionApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'PositionApiError'
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

/**
 * One stable key for one logical management request. Duplicate clicks or a
 * retry of the same request must reuse it so the server replays, never doubles.
 */
export function managementIdempotencyKey(prefix = 'web-manage'): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function mutationHeaders(idempotencyKey: string): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'Idempotency-Key': idempotencyKey,
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
): Promise<PositionEnvelope<T>> {
  const method = options.method ?? 'GET'
  const response = await fetch(path, {
    method,
    headers:
      method === 'GET'
        ? { Accept: 'application/json' }
        : mutationHeaders(options.idempotencyKey ?? ''),
    body: method === 'GET' ? undefined : JSON.stringify(options.body ?? {}),
    credentials: 'same-origin',
    signal: options.signal,
  })
  let envelope: PositionEnvelope<T> | null = null
  try {
    envelope = (await response.json()) as PositionEnvelope<T>
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    throw new PositionApiError(
      envelope?.error?.message ?? `Position request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

/** The open-position ledger; closed trades are the journal's business (slice 30). */
export async function fetchPositions(signal?: AbortSignal): Promise<PositionListEnvelope> {
  return requestEnvelope<PositionListData>('/api/v1/positions?state=OPEN', { signal })
}

export async function fetchPosition(
  positionId: string,
  signal?: AbortSignal,
): Promise<PositionDetailEnvelope> {
  return requestEnvelope<PositionData>(`/api/v1/positions/${encodeURIComponent(positionId)}`, {
    signal,
  })
}

export async function fetchPositionHistory(
  positionId: string,
  signal?: AbortSignal,
): Promise<PositionHistoryEnvelope> {
  return requestEnvelope<PositionHistoryData>(
    `/api/v1/positions/${encodeURIComponent(positionId)}/history`,
    { signal },
  )
}

/** Request one validated management decision (paper only). */
export async function requestManagement(
  positionId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<PositionManageEnvelope> {
  return requestEnvelope<PositionManageData>(
    `/api/v1/positions/${encodeURIComponent(positionId)}/manage`,
    { method: 'POST', body: {}, idempotencyKey, signal },
  )
}

function num(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** Direction sign: LONG is +1, SHORT is -1. */
export function sideSign(side: string): 1 | -1 {
  return side.toUpperCase() === 'SHORT' ? -1 : 1
}

/** The mark-to-entry price term; null when the mark is unmeasured. */
export function pricePnl(
  position: PositionData,
  mark: string | number | null = position.mark_price,
): number | null {
  const markValue = num(mark)
  const entry = num(position.avg_entry_price) ?? num(position.entry_price)
  const size = num(position.size)
  if (markValue === null || entry === null || size === null) {
    return null
  }
  return (markValue - entry) * size * sideSign(position.side)
}

export function recordedFees(position: PositionData): number | null {
  return num(position.fees_usd)
}

export function recordedFunding(position: PositionData): number | null {
  return num(position.funding_usd)
}

/** Price PnL plus recorded fees (a cost) and recorded funding (signed cash flow). */
export function netPnl(
  position: PositionData,
  mark: string | number | null = position.mark_price,
): number | null {
  const price = pricePnl(position, mark)
  const fees = recordedFees(position)
  const funding = recordedFunding(position)
  if (price === null || fees === null || funding === null) {
    return null
  }
  return price - fees + funding
}

/** Entry notional is the average entry times the current size. */
export function entryNotional(position: PositionData): number | null {
  const entry = num(position.avg_entry_price) ?? num(position.entry_price)
  const size = num(position.size)
  if (entry === null || size === null || size <= 0) {
    return null
  }
  return entry * size
}

/** Net PnL as a percentage of entry notional; null when either is unmeasured. */
export function pnlPct(
  position: PositionData,
  mark: string | number | null = position.mark_price,
): number | null {
  const net = netPnl(position, mark)
  const notional = entryNotional(position)
  if (net === null || notional === null || notional === 0) {
    return null
  }
  return (net / notional) * 100
}

/** R multiple = net PnL / the risk recorded at entry. */
export function rMultiple(
  position: PositionData,
  mark: string | number | null = position.mark_price,
): number | null {
  const net = netPnl(position, mark)
  const risk = num(position.risk_amount_usd)
  if (net === null || risk === null || risk <= 0) {
    return null
  }
  return net / risk
}

export type MarkState = 'FRESH' | 'STALE' | 'MISSING'

/** Mark freshness derived from the persisted mark and its ``updated_at``. */
export function markState(position: PositionData, nowMs: number): MarkState {
  if (num(position.mark_price) === null) {
    return 'MISSING'
  }
  const updated = Date.parse(position.updated_at)
  if (Number.isNaN(updated)) {
    return 'STALE'
  }
  return nowMs - updated > MARK_FRESHNESS_MS ? 'STALE' : 'FRESH'
}

export function sumNotional(positions: PositionData[]): number {
  return positions.reduce((total, item) => total + (num(item.notional_usd) ?? 0), 0)
}

export function sumMargin(positions: PositionData[]): number {
  return positions.reduce((total, item) => total + (num(item.margin_usd) ?? 0), 0)
}

export function sumRiskPct(positions: PositionData[]): number {
  return positions.reduce((total, item) => total + (num(item.risk_pct) ?? 0), 0)
}

export interface NetPnlAggregate {
  value: number | null
  measured: number
  missing: number
  stale: number
}

/**
 * Aggregate net uPnL over the positions that have a recorded mark.
 *
 * A missing mark is excluded from the sum rather than treated as zero, so the
 * caller can label the aggregate PARTIAL and name how many were excluded.
 */
export function aggregateNetPnl(positions: PositionData[], nowMs: number): NetPnlAggregate {
  let value: number | null = null
  let measured = 0
  let missing = 0
  let stale = 0
  for (const item of positions) {
    const state = markState(item, nowMs)
    if (state === 'MISSING') {
      missing += 1
      continue
    }
    if (state === 'STALE') {
      stale += 1
    }
    const net = netPnl(item)
    if (net === null) {
      missing += 1
      continue
    }
    value = (value ?? 0) + net
    measured += 1
  }
  return { value, measured, missing, stale }
}

export function dash(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return String(value)
}

function fixed(value: number, digits: number): string {
  return value.toFixed(digits)
}

/** Signed USDT value with two decimals and no locale separators (dense tables). */
export function formatSignedMoney(value: number): string {
  const text = fixed(Math.abs(value), 2)
  return value < 0 ? `-${text}` : `+${text}`
}

export function formatMoney(value: number, digits = 2): string {
  return fixed(value, digits)
}

export function formatSignedPercent(value: number): string {
  const text = fixed(Math.abs(value), 2)
  return value < 0 ? `-${text}%` : `+${text}%`
}

/** A short, fixed design age label; never fabricates a value for bad input. */
export function formatAge(iso: string, nowMs: number): string {
  const started = Date.parse(iso)
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
