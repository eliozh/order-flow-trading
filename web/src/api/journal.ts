import { isAbortError } from './abort'
import { triggerBlobDownload } from './blob-download'
import { numberOrNull, type ApiEnvelope } from './contract'
import { errorMessage } from './errors'
import { createPendingKeyStore, mutationHeaders, newIdempotencyKey } from './idempotency'
import { formatAge } from './positions'
import { fetchWithTimeout, READ_TIMEOUT_MS, RequestTimeoutError } from './request-timeout'
import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }
export { numberOrNull }

/** Upper bound on one review mutation; the LLM stage can run for tens of seconds. */
export const REVIEW_TIMEOUT_MS = 120_000

/** Upper bound on one export download (the server renders the file first). */
export const EXPORT_TIMEOUT_MS = 30_000

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

/** The result of one server export download. */
export interface JournalExportDownload {
  filename: string
  bytes: number
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/**
 * Runtime guard for the detail payload.
 *
 * Opening a reserved journal path such as `/journal/stats` hits the stats/export
 * routes, which return `ok: true` with a non-trade `data` object. The detail page
 * must reject that before dereferencing any trade field. The response therefore
 * has to carry the requested `trade_id` and the fields the page reads.
 */
export function isJournalTradeData(value: unknown, tradeId: string): value is JournalTradeData {
  if (!isRecord(value) || value.trade_id !== tradeId) {
    return false
  }
  for (const field of ['symbol', 'side', 'opened_at', 'closed_at', 'net_pnl_usd'] as const) {
    if (typeof value[field] !== 'string') {
      return false
    }
  }
  if (!(value.signal === null || isRecord(value.signal))) {
    return false
  }
  if (!(value.data_quality === null || isRecord(value.data_quality))) {
    return false
  }
  return true
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

export type JournalEnvelope<T> = ApiEnvelope<T>

export type TradeLogListEnvelope = JournalEnvelope<TradeLogListData>
export type JournalStatsEnvelope = JournalEnvelope<JournalStatsData>
export type JournalTradeEnvelope = JournalEnvelope<JournalTradeData>
export type JournalCompareEnvelope = JournalEnvelope<JournalCompareData>
export type JournalReviewEnvelope = JournalEnvelope<JournalReviewData>
export type EntryDecisionEnvelope = JournalEnvelope<EntryDecisionData>

/**
 * ``definitive`` is a structured, server-returned refusal whose envelope code
 * is in the allowlist below. In the common path it does not persist a new
 * review linked to the trade, so a retry may rotate to a fresh key. It does NOT
 * mean "nothing was generated" or "the provider never ran":
 * ``LLM_SCHEMA_INVALID`` follows a provider response that failed validation,
 * and the post-save link-failure path can leave a decision row while the
 * request still fails.
 *
 * ``unknown`` covers everything else — notably ``IDEMPOTENCY_CONFLICT``/
 * ``CONFLICT`` (a request with the same key may still be running or may already
 * have persisted a review), timeouts, transport failures, and non-allowlisted
 * 5xx. Status does not decide the kind: ``DATA_UNAVAILABLE`` is a 503 that IS
 * allowlisted as definitive, and it may be raised after a link/audit failure,
 * so a fresh-key retry relies on the server's link/orphan replay. An unknown
 * outcome keeps the key so a retry replays the stored result instead of running
 * a second generation.
 */
export type JournalApiErrorKind = 'definitive' | 'unknown'

/**
 * The review-POST codes treated as a structured refusal inside the allowlist:
 * validation, auth/CSRF, request-shape, lookup, and storage-availability
 * refusals. Membership means the response did not persist a new linked review
 * in the common path, not that no review was generated: ``LLM_SCHEMA_INVALID``
 * may follow a provider call, and ``DATA_UNAVAILABLE`` may follow a link/audit
 * failure. A fresh-key retry therefore relies on the server's link/orphan
 * replay. Every non-allowlisted code (including 409
 * ``IDEMPOTENCY_CONFLICT``/``CONFLICT`` and any code missing from the catalog)
 * is an unknown outcome.
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
    // necessarily definitive, and a 5xx DATA_UNAVAILABLE is allowlisted. Only
    // the structured-refusal allowlist decides; membership means no new linked
    // review in the common path, not that the provider never ran.
    this.kind =
      options.kind ??
      (code !== undefined && DEFINITIVE_REVIEW_REFUSAL_CODES.has(code) ? 'definitive' : 'unknown')
  }
}

/** Generate a fresh, prefixed Idempotency-Key for one logical review request. */
export function journalReviewIdempotencyKey(prefix = 'web-review'): string {
  return newIdempotencyKey(prefix)
}

/** Session-scoped storage key for one trade's pending review key. */
export const REVIEW_KEY_PREFIX = 'oft.journal.review.'

const reviewKeys = createPendingKeyStore('web-review', REVIEW_KEY_PREFIX)

/** The pending key for one trade, or null when none is stored. */
export function storedReviewKey(tradeId: string): string | null {
  return reviewKeys.storedKey(tradeId)
}

/** Create and store a new key for one trade (a deliberate new request). */
export function rotateReviewKey(tradeId: string): string {
  return reviewKeys.rotateKey(tradeId)
}

/** The stored key for this trade, or a fresh one when none exists. */
export function reviewKeyFor(tradeId: string): string {
  return reviewKeys.keyFor(tradeId)
}

/** Forget the pending key after a success or a structured refusal. */
export function clearReviewKey(tradeId: string): void {
  reviewKeys.clearKey(tradeId)
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

/**
 * The download URL for the just-generated server-side export file. It carries
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

/** Derive a safe download filename from Content-Disposition (no path input). */
export function exportFilename(
  contentDisposition: string | null,
  format: 'csv' | 'json',
  now: Date = new Date(),
): string {
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(contentDisposition ?? '')
  const plain = /filename="?([^";]+)"?/i.exec(contentDisposition ?? '')
  const candidate = encoded?.[1] ?? plain?.[1]
  if (candidate) {
    let decoded = candidate
    try {
      decoded = decodeURIComponent(candidate)
    } catch {
      decoded = candidate
    }
    const basename = decoded.split(/[\\/]/).pop() ?? ''
    // Drop control characters without a control-character regex literal.
    const safe = Array.from(basename)
      .filter((character) => character.charCodeAt(0) >= 0x20)
      .join('')
    if (safe !== '') {
      return safe
    }
  }
  const stamp = now.toISOString().replace(/[-:.]/g, '').replace('T', 't').replace('Z', 'z')
  return `journal-${stamp}.${format}`
}

/**
 * Download a server-generated export.
 *
 * The file is fetched (not linked) so a 401/422/500 envelope is surfaced as a
 * typed error instead of being saved as a bogus download. On success the blob is
 * saved through an object URL whose filename comes from Content-Disposition,
 * with a safe ``journal-<stamp>.<ext>`` fallback.
 */
export async function downloadJournalExport(
  params: { format: 'csv' | 'json'; period?: string; symbol?: string },
  signal?: AbortSignal,
): Promise<JournalExportDownload> {
  let response: Response
  try {
    response = await fetchWithTimeout(
      journalExportFileUrl(params),
      { headers: { Accept: 'text/csv, application/json' }, credentials: 'same-origin' },
      signal,
      EXPORT_TIMEOUT_MS,
    )
  } catch (error: unknown) {
    if (isAbortError(error)) {
      throw error
    }
    if (error instanceof RequestTimeoutError) {
      throw new JournalApiError('The journal export timed out.', 'TIMEOUT', { kind: 'unknown' })
    }
    throw new JournalApiError(errorMessage(error), undefined, { kind: 'unknown' })
  }

  if (!response.ok) {
    let envelope: JournalEnvelope<unknown> | null = null
    try {
      envelope = (await response.json()) as JournalEnvelope<unknown>
    } catch {
      envelope = null
    }
    throw new JournalApiError(
      envelope?.error?.message ?? `Journal export failed with HTTP ${response.status}`,
      envelope?.error?.code,
      { httpStatus: response.status, kind: 'unknown' },
    )
  }

  const blob = await response.blob()
  const filename = exportFilename(response.headers.get('Content-Disposition'), params.format)
  triggerBlobDownload(blob, filename)
  return { filename, bytes: blob.size }
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

/**
 * The list-row outcome. Unknown or missing data quality is treated as MISSING
 * (i.e. PARTIAL), matching the MISSING quality chip; a COMPLETE row with
 * unreadable net PnL is PARTIAL too. Only a readable, COMPLETE row is WIN/LOSS/TIE.
 */
export function outcomeOf(summary: TradeLogSummary): JournalOutcome {
  const status = summary.data_quality?.status ?? 'MISSING'
  const tags = summary.outcome_tags ?? []
  if (status !== 'COMPLETE' || tags.some((tag) => /PARTIAL/i.test(tag))) {
    return 'PARTIAL'
  }
  const net = numberOrNull(summary.net_pnl_usd)
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
