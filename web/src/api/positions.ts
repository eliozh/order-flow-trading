import { isAbortError } from './abort'
import { errorMessage } from './errors'
import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

/** Upper bound on one positions read; a hang becomes a typed TIMEOUT error. */
export const FETCH_TIMEOUT_MS = 15_000

/**
 * Upper bound on one management mutation. The server's LLM stage can take up
 * to ~45s per attempt, so the read timeout would abort a request that may still
 * execute. A timeout here is still classified as an unknown outcome.
 */
export const MANAGE_TIMEOUT_MS = 120_000

/**
 * Envelope error codes that prove a management request was rejected before any
 * execution. Only these may clear the pending key and claim nothing was
 * created. Everything else — notably `IDEMPOTENCY_CONFLICT` (a concurrent
 * request with the same key is still running) and `CONFLICT` (raised after a
 * close committed) — is an unknown outcome.
 */
export const DEFINITIVE_MANAGE_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'VALIDATION_FAILED',
  'AUTH_REQUIRED',
  'AUTH_INVALID',
  'CSRF_INVALID',
  'ORIGIN_FORBIDDEN',
  'HOST_FORBIDDEN',
  'IDEMPOTENCY_KEY_REQUIRED',
  'KILL_SWITCH_ACTIVE',
  'RISK_BLOCKED',
  'RISK_DATA_INCOMPLETE',
  'LIVE_DISABLED',
])

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
  /**
   * Server-resolved mark for freshness/PnL. Optional for older payloads; when
   * absent the UI falls back to ``mark_price`` (documented, never fabricated).
   */
  resolved_mark_price?: string | null
  /** When the resolved mark was observed. Falls back to ``updated_at``. */
  resolved_mark_as_of?: string | null
  /** Where the resolved mark came from, when the server names it. */
  resolved_mark_source?: 'BARS' | 'POSITION' | null
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

/**
 * A persistence/refusal failure classification.
 *
 * ``refusal`` is a pre-execution rejection whose envelope code is in
 * ``DEFINITIVE_MANAGE_REFUSAL_CODES``: nothing was written, so retrying with a
 * fresh key is safe. ``unknown`` covers everything else (including 409
 * ``IDEMPOTENCY_CONFLICT``/``CONFLICT``, ``NOT_FOUND``, ``DATA_UNAVAILABLE``,
 * transport failures, timeouts, and 5xx): the server may have executed, so the
 * caller must keep the Idempotency-Key and retry with the same one.
 */
export type PositionApiErrorKind = 'refusal' | 'unknown'

export class PositionApiError extends Error {
  readonly code: string | undefined
  readonly httpStatus: number | undefined
  readonly kind: PositionApiErrorKind

  constructor(
    message: string,
    code?: string,
    options: { httpStatus?: number; kind?: PositionApiErrorKind } = {},
  ) {
    super(message)
    this.name = 'PositionApiError'
    this.code = code
    this.httpStatus = options.httpStatus
    // Never derive this from the HTTP status range: a 4xx such as 409 is not
    // necessarily definitive. Only the pre-execution code allowlist is.
    this.kind =
      options.kind ??
      (code !== undefined && DEFINITIVE_MANAGE_REFUSAL_CODES.has(code) ? 'refusal' : 'unknown')
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
 * Generate a fresh idempotency key for one logical management request.
 *
 * This always returns a new key; it neither stores nor stabilises one. Callers
 * must persist one key per position/logical request (see ``managementKeyFor``)
 * and reuse it across retries so the server replays the stored result instead
 * of creating a second order.
 */
export function managementIdempotencyKey(prefix = 'web-manage'): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** Session-scoped storage key for one position's pending management key. */
export const MANAGEMENT_KEY_PREFIX = 'oft.position.manage.'

/** In-memory fallback when sessionStorage is unavailable (private mode, SSR). */
const memoryManagementKeys = new Map<string, string>()

function sessionStore(): Storage | null {
  if (typeof window === 'undefined') {
    return null
  }
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function managementStorageKey(positionId: string): string {
  return `${MANAGEMENT_KEY_PREFIX}${positionId}`
}

/** The pending key for one position, or null when none is stored. */
export function storedManagementKey(positionId: string): string | null {
  const store = sessionStore()
  if (store) {
    try {
      const stored = store.getItem(managementStorageKey(positionId))
      if (stored !== null) {
        return stored
      }
    } catch {
      // Fall through to the in-memory fallback.
    }
  }
  return memoryManagementKeys.get(positionId) ?? null
}

/** Create and store a new key for one position (a deliberate new request). */
export function rotateManagementKey(positionId: string): string {
  const key = managementIdempotencyKey(`web-manage-${positionId}`)
  const store = sessionStore()
  if (store) {
    try {
      store.setItem(managementStorageKey(positionId), key)
      memoryManagementKeys.delete(positionId)
      return key
    } catch {
      // Fall through to the in-memory fallback.
    }
  }
  memoryManagementKeys.set(positionId, key)
  return key
}

/** The stored key for this position, or a fresh one when none exists. */
export function managementKeyFor(positionId: string): string {
  return storedManagementKey(positionId) ?? rotateManagementKey(positionId)
}

/**
 * Forget the pending key after a definitive outcome: a success, or a
 * pre-execution refusal (an allowlisted envelope code). The next logical
 * request then rotates to a fresh key. An unknown outcome keeps it.
 */
export function clearManagementKey(positionId: string): void {
  const store = sessionStore()
  if (store) {
    try {
      store.removeItem(managementStorageKey(positionId))
    } catch {
      // The in-memory fallback is cleared below regardless.
    }
  }
  memoryManagementKeys.delete(positionId)
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
    /** Override the read timeout (mutations use MANAGE_TIMEOUT_MS). */
    timeoutMs?: number
  } = {},
): Promise<PositionEnvelope<T>> {
  const method = options.method ?? 'GET'
  // Compose the caller's abort signal with our own timeout. A timeout aborts
  // the same fetch but is surfaced as a typed TIMEOUT error, never a silent
  // AbortError, so the UI can offer a retry.
  const controller = new AbortController()
  const external = options.signal
  const forwardAbort = (): void => controller.abort()
  if (external) {
    if (external.aborted) {
      controller.abort()
    } else {
      external.addEventListener('abort', forwardAbort, { once: true })
    }
  }
  let timedOut = false
  const timeout = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, options.timeoutMs ?? FETCH_TIMEOUT_MS)

  try {
    const response = await fetch(path, {
      method,
      headers:
        method === 'GET'
          ? { Accept: 'application/json' }
          : mutationHeaders(options.idempotencyKey ?? ''),
      body: method === 'GET' ? undefined : JSON.stringify(options.body ?? {}),
      credentials: 'same-origin',
      signal: controller.signal,
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
        { httpStatus: response.status },
      )
    }
    return envelope
  } catch (error: unknown) {
    if (timedOut) {
      throw new PositionApiError('The position request timed out.', 'TIMEOUT', {
        kind: 'unknown',
      })
    }
    // A caller-driven abort is cleanup, never a user-visible failure.
    if (isAbortError(error) || (external?.aborted ?? false)) {
      throw error
    }
    if (error instanceof PositionApiError) {
      throw error
    }
    // A network failure is never definitive: the request may have executed.
    throw new PositionApiError(errorMessage(error), undefined, { kind: 'unknown' })
  } finally {
    clearTimeout(timeout)
    external?.removeEventListener('abort', forwardAbort)
  }
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
    {
      method: 'POST',
      body: {},
      idempotencyKey,
      signal,
      // The server LLM stage may run for tens of seconds; never abort a
      // mutation that may still execute with the short read timeout.
      timeoutMs: MANAGE_TIMEOUT_MS,
    },
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

/**
 * The server-resolved mark, falling back to the persisted ``mark_price`` for
 * older payloads that do not carry the additive fields. Never fabricated.
 */
export function resolvedMark(position: PositionData): string | null {
  if (num(position.resolved_mark_price) !== null) {
    return position.resolved_mark_price ?? null
  }
  return position.mark_price
}

/** When the resolved mark was observed; falls back to ``updated_at``. */
export function markAsOf(position: PositionData): string {
  return position.resolved_mark_as_of ?? position.updated_at
}

/**
 * The accumulated gross realized term. The server records ``realized_pnl_usd``
 * when a partial close has happened; an OPEN position with no PARTIAL_CLOSE in
 * its management history has realized exactly zero. When a partial close
 * happened but the gross is unmeasured, this stays null (never a fake zero).
 */
export function accumulatedGross(position: PositionData): number | null {
  const recorded = num(position.realized_pnl_usd)
  if (recorded !== null) {
    return recorded
  }
  const partiallyClosed = position.management_history.some(
    (entry) => entry.action.toUpperCase() === 'PARTIAL_CLOSE',
  )
  if (position.state === 'OPEN' && !partiallyClosed) {
    return 0
  }
  return null
}

/** The mark-to-entry price term; null when the mark is unmeasured. */
export function pricePnl(
  position: PositionData,
  mark: string | number | null = resolvedMark(position),
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

/**
 * Net uPnL if the position were closed now: price PnL (unrealized) plus the
 * accumulated gross (realized) minus recorded fees plus recorded funding.
 */
export function netPnl(
  position: PositionData,
  mark: string | number | null = resolvedMark(position),
): number | null {
  const price = pricePnl(position, mark)
  const gross = accumulatedGross(position)
  const fees = recordedFees(position)
  const funding = recordedFunding(position)
  if (price === null || gross === null || fees === null || funding === null) {
    return null
  }
  return price + gross - fees + funding
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
  mark: string | number | null = resolvedMark(position),
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
  mark: string | number | null = resolvedMark(position),
): number | null {
  const net = netPnl(position, mark)
  const risk = num(position.risk_amount_usd)
  if (net === null || risk === null || risk <= 0) {
    return null
  }
  return net / risk
}

export type MarkState = 'FRESH' | 'STALE' | 'MISSING'

/**
 * Mark freshness derived from the resolved mark and its as-of timestamp.
 *
 * The server resolves a fresh bar mark even when the persisted ``updated_at``
 * only moves on entry/exit; the persisted pair is the documented fallback.
 */
export function markState(position: PositionData, nowMs: number): MarkState {
  if (num(resolvedMark(position)) === null) {
    return 'MISSING'
  }
  const asOf = Date.parse(markAsOf(position))
  if (Number.isNaN(asOf)) {
    return 'STALE'
  }
  return nowMs - asOf > MARK_FRESHNESS_MS ? 'STALE' : 'FRESH'
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
