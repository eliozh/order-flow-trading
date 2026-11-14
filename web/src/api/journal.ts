import { isAbortError } from './abort'
import { errorMessage } from './errors'
import { formatAge } from './positions'
import { fetchWithTimeout, READ_TIMEOUT_MS, RequestTimeoutError } from './request-timeout'
import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

/** Upper bound on one review mutation; the LLM stage can run for tens of seconds. */
export const REVIEW_TIMEOUT_MS = 120_000

/** Read/watch cadence for the journal list and detail (truthful copy). */
export const JOURNAL_REFRESH_MS = 15_000

export interface DataQualitySummary {
  status: string
  reasons?: string[]
  gaps?: unknown[]
  last_event_at?: string | null
  coverage_start?: string | null
}

/** The closed-trade list projection (`GET /api/v1/journal`). */
export interface TradeLogSummary {
  trade_id: string
  position_id: string
  symbol: string
  side: string
  opened_at: string
  closed_at: string
  gross_pnl_usd: string
  fees_usd: string
  funding_usd: string
  net_pnl_usd: string
  return_pct: number
  r_multiple: number | null
  mae_usd: string
  mfe_usd: string
  outcome_tags: string[]
  data_quality: DataQualitySummary | null
}

export interface TradeLogListData {
  items: TradeLogSummary[]
  total: number
}

export interface JournalPeriod {
  label: string
  start: string
  end: string
  basis: string
}

export interface JournalExclusions {
  open_positions: number
  partial_trades: number
  unreadable_trades: number
  outside_period: number
}

export interface JournalAvgR {
  value: number | null
  samples: number
  excluded_null_r: number
}

export interface JournalTotals {
  gross_pnl_usd: string | null
  fees_usd: string | null
  funding_usd: string | null
  net_pnl_usd: string | null
}

export interface JournalStatsData {
  status: 'DEFINED' | 'UNDEFINED'
  reason: string | null
  period: JournalPeriod | null
  sample_size: number
  denominator: number
  exclusions: JournalExclusions
  definitions: Record<string, string>
  wins: number
  losses: number
  ties: number
  win_rate: number | null
  expectancy_usd: string | null
  avg_r: JournalAvgR
  totals: JournalTotals
  data_quality: 'COMPLETE' | 'PARTIAL' | 'MISSING'
}

export interface JournalExportData {
  format: 'csv' | 'json'
  path: string
  rows: number
  bytes: number
  neutralized_cells: number
  skipped_unreadable: number
  data_quality: string
}

export interface JournalComparisonSide {
  status: string
  reasons: string[]
}

export interface JournalZoneData extends JournalComparisonSide {
  status: 'INSIDE' | 'ABOVE_ZONE' | 'BELOW_ZONE' | 'N/A'
  entry: string | null
  zone_low: string | null
  zone_high: string | null
  inside: boolean | null
  distance_bps: number | null
}

export interface JournalSlippageData extends JournalComparisonSide {
  status: 'OK' | 'PARTIAL' | 'N/A'
  entry_avg_price: string | null
  reference_price: string | null
  reference_as_of: string | null
  bps: number | null
  formula: string
}

export interface JournalFirstTouchData extends JournalComparisonSide {
  status: 'OK' | 'PARTIAL' | 'N/A'
  kind: string | null
  at: string | null
  ambiguous: boolean
}

export interface JournalDecisionData extends JournalComparisonSide {
  status: 'OK' | 'PARTIAL' | 'N/A'
  decision: string | null
  direction_agrees: boolean | null
  matched: boolean | null
  net_pnl_usd: string | null
  decision_id?: string | null
}

export interface JournalCompareData {
  trade_id: string | null
  position_id: string | null
  symbol: string | null
  side: string | null
  opened_at: string | null
  closed_at: string | null
  net_pnl_usd: string | null
  zone: JournalZoneData
  slippage: JournalSlippageData
  first_touch: JournalFirstTouchData
  decision: JournalDecisionData
  reasons: string[]
  data_quality: 'COMPLETE' | 'PARTIAL' | 'MISSING'
}

export interface JournalReviewData {
  trade_id: string
  decision_id: string | null
  status: 'FOUND' | 'MISSING' | 'PARTIAL'
  advisory: boolean
  rationale: string | null
  caveats: string[]
  confidence: number | null
  validation_status: string | null
  provenance: Record<string, unknown> | null
  reason: string | null
  idempotent_replay: boolean
}

export interface JournalSignalRefs {
  scan_id: string | null
  report_id: string | null
  opportunity_id: string | null
  trigger_id: string | null
  decision_id: string | null
}

export interface JournalDecisionQuality {
  matched_rules: boolean | null
  confidence: number | null
  notes: string
}

/** The full closed-trade payload (`GET /api/v1/journal/{trade_id}`). */
export interface JournalTradeData {
  schema_version: string
  trade_id: string
  position_id: string
  account_id: string
  environment: string
  symbol: string
  side: string
  opened_at: string
  closed_at: string
  entry_avg_price: string
  exit_avg_price: string
  size: string
  gross_pnl_usd: string
  fees_usd: string
  funding_usd: string
  net_pnl_usd: string
  r_multiple: number | null
  mae_usd: string
  mfe_usd: string
  return_pct: number
  signal: JournalSignalRefs
  decision_quality: JournalDecisionQuality
  outcome_tags: string[]
  review_decision_id: string | null
  data_quality: DataQualitySummary
  provenance: Record<string, unknown>
}

/** The persisted entry decision read (`GET /api/v1/triggers/{id}/decision`). */
export interface EntryDecisionData {
  trigger_id: string
  entry_decision_id: string | null
  status: string
  decision: {
    confidence?: number | null
    rationale?: string | null
    validation_status?: string | null
    persisted_at?: string | null
    entry_recheck?: {
      decision?: string | null
      reasons?: string[]
      invalidation_conditions?: unknown[]
    } | null
    provenance?: Record<string, unknown> | null
  } | null
  provenance: Record<string, unknown> | null
  validation_status: string | null
  confidence: number | null
  decision_type: string | null
  reasons: string[]
  data_quality: string
}

export interface JournalEnvelope<T> {
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}

export type TradeLogListEnvelope = JournalEnvelope<TradeLogListData>
export type JournalStatsEnvelope = JournalEnvelope<JournalStatsData>
export type JournalTradeEnvelope = JournalEnvelope<JournalTradeData>
export type JournalCompareEnvelope = JournalEnvelope<JournalCompareData>
export type JournalReviewEnvelope = JournalEnvelope<JournalReviewData>
export type JournalExportEnvelope = JournalEnvelope<JournalExportData>
export type EntryDecisionEnvelope = JournalEnvelope<EntryDecisionData>

/**
 * ``definitive`` means the server answered with a well-understood
 * pre-execution refusal (envelope code in the allowlist below); no review was
 * created, so retrying may use a fresh key. ``unknown`` covers everything else
 * — notably ``IDEMPOTENCY_CONFLICT``/``CONFLICT`` (a request with the same key
 * may still be running or may already have persisted a review), timeouts,
 * transport failures, and 5xx. An unknown outcome keeps the key so a retry
 * replays the stored result instead of running a second generation.
 */
export type JournalApiErrorKind = 'definitive' | 'unknown'

/**
 * The only review-POST codes that prove nothing was generated: validation,
 * auth/CSRF, request-shape and lookup refusals raised before the provider runs.
 * Every other code (including 409 ``IDEMPOTENCY_CONFLICT``/``CONFLICT`` and any
 * code missing from the catalog) is an unknown outcome.
 */
export const DEFINITIVE_REVIEW_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'VALIDATION_FAILED',
  'AUTH_REQUIRED',
  'AUTH_INVALID',
  'CSRF_INVALID',
  'ORIGIN_FORBIDDEN',
  'HOST_FORBIDDEN',
  'IDEMPOTENCY_KEY_REQUIRED',
  'KILL_SWITCH_ACTIVE',
  'NOT_FOUND',
  'DATA_UNAVAILABLE',
  'PROMPT_VERSION_MISSING',
  'LLM_SCHEMA_INVALID',
])

export class JournalApiError extends Error {
  readonly code: string | undefined
  readonly httpStatus: number | undefined
  readonly kind: JournalApiErrorKind

  constructor(
    message: string,
    code?: string,
    options: { httpStatus?: number; kind?: JournalApiErrorKind } = {},
  ) {
    super(message)
    this.name = 'JournalApiError'
    this.code = code
    this.httpStatus = options.httpStatus
    // Never derive this from the HTTP status range: a 4xx such as 409 is not
    // necessarily definitive. Only the pre-execution code allowlist is.
    this.kind =
      options.kind ??
      (code !== undefined && DEFINITIVE_REVIEW_REFUSAL_CODES.has(code) ? 'definitive' : 'unknown')
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

/** Generate a fresh, prefixed Idempotency-Key for one logical review request. */
export function journalReviewIdempotencyKey(prefix = 'web-review'): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** Session-scoped storage key for one trade's pending review key. */
export const REVIEW_KEY_PREFIX = 'oft.journal.review.'

/** In-memory fallback when sessionStorage is unavailable (private mode, SSR). */
const memoryReviewKeys = new Map<string, string>()

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

function reviewStorageKey(tradeId: string): string {
  return `${REVIEW_KEY_PREFIX}${tradeId}`
}

/** The pending key for one trade, or null when none is stored. */
export function storedReviewKey(tradeId: string): string | null {
  const store = sessionStore()
  if (store) {
    try {
      const stored = store.getItem(reviewStorageKey(tradeId))
      if (stored !== null) {
        return stored
      }
    } catch {
      // Fall through to the in-memory fallback.
    }
  }
  return memoryReviewKeys.get(tradeId) ?? null
}

/** Create and store a new key for one trade (a deliberate new request). */
export function rotateReviewKey(tradeId: string): string {
  const key = journalReviewIdempotencyKey(`web-review-${tradeId}`)
  const store = sessionStore()
  if (store) {
    try {
      store.setItem(reviewStorageKey(tradeId), key)
      memoryReviewKeys.delete(tradeId)
      return key
    } catch {
      // Fall through to the in-memory fallback.
    }
  }
  memoryReviewKeys.set(tradeId, key)
  return key
}

/** The stored key for this trade, or a fresh one when none exists. */
export function reviewKeyFor(tradeId: string): string {
  return storedReviewKey(tradeId) ?? rotateReviewKey(tradeId)
}

/** Forget the pending key after a success or a definitive structured error. */
export function clearReviewKey(tradeId: string): void {
  const store = sessionStore()
  if (store) {
    try {
      store.removeItem(reviewStorageKey(tradeId))
    } catch {
      // The in-memory fallback is cleared below regardless.
    }
  }
  memoryReviewKeys.delete(tradeId)
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
    timeoutMs?: number
  } = {},
): Promise<JournalEnvelope<T>> {
  const method = options.method ?? 'GET'
  const mutating = method !== 'GET'
  let response: Response
  try {
    response = await fetchWithTimeout(
      path,
      {
        method,
        headers: mutating
          ? mutationHeaders(options.idempotencyKey ?? '')
          : { Accept: 'application/json' },
        body: mutating ? JSON.stringify(options.body ?? {}) : undefined,
        credentials: 'same-origin',
      },
      options.signal,
      options.timeoutMs ?? (mutating ? REVIEW_TIMEOUT_MS : READ_TIMEOUT_MS),
    )
  } catch (error: unknown) {
    if (isAbortError(error)) {
      throw error
    }
    if (error instanceof RequestTimeoutError) {
      throw new JournalApiError('The journal request timed out.', 'TIMEOUT', { kind: 'unknown' })
    }
    throw new JournalApiError(errorMessage(error), undefined, { kind: 'unknown' })
  }

  let envelope: JournalEnvelope<T> | null = null
  try {
    envelope = (await response.json()) as JournalEnvelope<T>
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    const code = envelope?.error?.code
    const kind: JournalApiErrorKind =
      code !== undefined && DEFINITIVE_REVIEW_REFUSAL_CODES.has(code) ? 'definitive' : 'unknown'
    throw new JournalApiError(
      envelope?.error?.message ?? `Journal request failed with HTTP ${response.status}`,
      code,
      { httpStatus: response.status, kind },
    )
  }
  return envelope
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [name, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') {
      continue
    }
    search.set(name, String(value))
  }
  const rendered = search.toString()
  return rendered === '' ? '' : `?${rendered}`
}

/** Closed trade logs only; open positions are counted by the stats endpoint. */
export async function fetchJournalList(
  params: { symbol?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<TradeLogListEnvelope> {
  return requestEnvelope<TradeLogListData>(
    `/api/v1/journal${query({
      symbol: params.symbol,
      limit: params.limit ?? 500,
      offset: params.offset ?? 0,
    })}`,
    { signal },
  )
}

export async function fetchJournalStats(
  params: { period?: string; symbol?: string } = {},
  signal?: AbortSignal,
): Promise<JournalStatsEnvelope> {
  return requestEnvelope<JournalStatsData>(
    `/api/v1/journal/stats${query({ period: params.period, symbol: params.symbol })}`,
    { signal },
  )
}

export async function fetchJournalTrade(
  tradeId: string,
  signal?: AbortSignal,
): Promise<JournalTradeEnvelope> {
  return requestEnvelope<JournalTradeData>(`/api/v1/journal/${encodeURIComponent(tradeId)}`, {
    signal,
  })
}

export async function fetchJournalCompare(
  tradeId: string,
  signal?: AbortSignal,
): Promise<JournalCompareEnvelope> {
  return requestEnvelope<JournalCompareData>(
    `/api/v1/journal/compare/${encodeURIComponent(tradeId)}`,
    { signal },
  )
}

/** The linked advisory review read (never calls a provider). */
export async function fetchJournalReview(
  tradeId: string,
  signal?: AbortSignal,
): Promise<JournalReviewEnvelope> {
  return requestEnvelope<JournalReviewData>(
    `/api/v1/journal/${encodeURIComponent(tradeId)}/review`,
    { signal },
  )
}

/**
 * Generate one advisory review. The caller supplies a stable key and reuses it
 * on an unknown-outcome retry so the server replays instead of creating a
 * second review. ``refresh`` is only for an explicit regenerate.
 */
export async function requestJournalReview(
  tradeId: string,
  options: { refresh?: boolean; idempotencyKey: string },
  signal?: AbortSignal,
): Promise<JournalReviewEnvelope> {
  const refresh = options.refresh ? '?refresh=true' : ''
  return requestEnvelope<JournalReviewData>(
    `/api/v1/journal/${encodeURIComponent(tradeId)}/review${refresh}`,
    {
      method: 'POST',
      body: {},
      idempotencyKey: options.idempotencyKey,
      signal,
      timeoutMs: REVIEW_TIMEOUT_MS,
    },
  )
}

/** Metadata for a server-written export (bytes, rows, neutralized cells). */
export async function fetchJournalExport(
  params: { format: 'csv' | 'json'; period?: string; symbol?: string },
  signal?: AbortSignal,
): Promise<JournalExportEnvelope> {
  return requestEnvelope<JournalExportData>(
    `/api/v1/journal/export${query({
      format: params.format,
      period: params.period,
      symbol: params.symbol,
    })}`,
    { signal },
  )
}

/**
 * The download link for the just-generated server-side export file. It carries
 * only query parameters; it never accepts a path, so the client cannot read an
 * arbitrary file.
 */
export function journalExportFileUrl(params: {
  format: 'csv' | 'json'
  period?: string
  symbol?: string
}): string {
  return `/api/v1/journal/export/file${query({
    format: params.format,
    period: params.period,
    symbol: params.symbol,
  })}`
}

/** The persisted entry decision and its provenance (no provider call). */
export async function fetchTriggerDecision(
  triggerId: string,
  signal?: AbortSignal,
): Promise<EntryDecisionEnvelope> {
  return requestEnvelope<EntryDecisionData>(
    `/api/v1/triggers/${encodeURIComponent(triggerId)}/decision`,
    { signal },
  )
}

export type JournalOutcome = 'WIN' | 'LOSS' | 'TIE' | 'PARTIAL'

function num(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** Parse a contract decimal string without fabricating a value for bad input. */
export function numberOrNull(value: string | number | null | undefined): number | null {
  return num(value)
}

/**
 * The list-row outcome. A trade whose data quality is not COMPLETE (or that
 * carries a PARTIAL/DATA_PARTIAL tag) is PARTIAL; otherwise the net PnL sign
 * decides WIN/LOSS/TIE. An unreadable net PnL is never rendered as a tie.
 */
export function outcomeOf(summary: TradeLogSummary): JournalOutcome {
  const status = summary.data_quality?.status ?? 'COMPLETE'
  const tags = summary.outcome_tags ?? []
  if (status !== 'COMPLETE' || tags.some((tag) => /PARTIAL/i.test(tag))) {
    return 'PARTIAL'
  }
  const net = num(summary.net_pnl_usd)
  if (net === null) {
    return 'PARTIAL'
  }
  if (net > 0) {
    return 'WIN'
  }
  if (net < 0) {
    return 'LOSS'
  }
  return 'TIE'
}

/** R multiple to two decimals, or an em dash when the server recorded null. */
export function rLabel(summary: TradeLogSummary): string {
  return summary.r_multiple === null || summary.r_multiple === undefined
    ? '—'
    : summary.r_multiple.toFixed(2)
}

/** Holding duration (closed − opened), or an em dash for an unparseable time. */
export function durationLabel(openedAt: string, closedAt: string): string {
  const end = Date.parse(closedAt)
  if (Number.isNaN(end)) {
    return '—'
  }
  return formatAge(openedAt, end)
}
