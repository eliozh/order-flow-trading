import { fetchWithTimeout, READ_TIMEOUT_MS } from './request-timeout'

export interface SymbolInfo {
  symbol: string
  base_asset: string
  quote_asset: string
  contract_type: string
  tradifi: boolean
}

export interface UniverseSummary {
  source: string
  as_of: string
  count: number
  tradifi_count: number
  symbols: SymbolInfo[]
}

export interface ScheduleData {
  timezone: string
  period: string
  pending_period: string | null
  next_run_at: string
}

export interface TelegramChatStatusData {
  chat_id: number
  queued: number
  sending: number
  sent: number
  failed: number
  last_error_code: string | null
}

export interface TelegramReceiptData {
  event_key: string
  kind: string
  chat_id: number
  state: string
  attempts: number
  message_id: number | null
  last_error_code: string | null
  created_at: string
  sent_at: string | null
}

export interface TelegramStatusData {
  configured: boolean
  chat_count: number
  allowed_user_count: number
  poller_state: string
  last_poll_at: string | null
  last_send_at: string | null
  failing_receipts: number
  receipts: Record<string, number>
  per_chat: TelegramChatStatusData[]
  recent_receipts: TelegramReceiptData[]
  overflow_code: string | null
  last_error_code: string | null
  daemon_state: string
}

export interface StatusData {
  status: 'ok'
  env: string
  version: string
  host: string
  port: number
  data_dir: string
  log_level: string
  generated_at: string
  universe: UniverseSummary
  schedule?: ScheduleData | null
  telegram?: TelegramStatusData | null
}

export interface ApiError {
  code: string
  message: string
  category: string
  http_status: number
  retryable: boolean
  reference_id: string
  details: unknown[]
}

export interface ApiMeta {
  request_id: string
  generated_at: string
  duration_ms: number
  provenance: string
  data_quality: string | null
  warnings: unknown[]
  pagination: unknown | null
  idempotent_replay: boolean
}

export interface StatusEnvelope {
  ok: boolean
  data: StatusData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface HealthData {
  status: string
  version: string
}

export interface ReadyCheck {
  name: string
  status: 'ok' | 'degraded' | 'offline' | 'unknown'
  detail: string
}

export interface ScanRecordData {
  scan_id: string
  symbol: string
  period: string
  status: string
  trigger_source: string
  metrics_only: boolean
  fixture_path: string
  started_at: string
  finished_at: string
  window_start: string
  window_end: string
  trade_count: number
  buy_volume: string
  sell_volume: string
  delta: string
  cvd: string
  data_quality: string
  manifest_path: string
  parquet_rows: number
  created_at: string
}

export interface ReadinessData {
  status: 'ready' | 'not_ready'
  version: string
  env: string
  data_dir: string
  checked_at: string
  checks: ReadyCheck[]
  latest_scan: ScanRecordData | null
}

export interface HealthEnvelope {
  ok: boolean
  data: HealthData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface ReadinessEnvelope {
  ok: boolean
  data: ReadinessData | null
  error: ApiError | null
  meta: ApiMeta
}

function envelopeError(envelope: { ok: boolean; error: ApiError | null }, fallback: string): Error {
  return new Error(envelope.error?.message ?? fallback)
}

/** Read an envelope error message without dereferencing an unvalidated body. */
function bodyErrorMessage(body: Record<string, unknown>, fallback: string): string {
  const error = body.error
  return isRecord(error) && typeof error.message === 'string' ? error.message : fallback
}

/** Read an envelope error code without dereferencing an unvalidated body. */
function bodyErrorCode(body: Record<string, unknown> | null): string | undefined {
  if (!body) {
    return undefined
  }
  const error = body.error
  return isRecord(error) && typeof error.code === 'string' ? error.code : undefined
}

/** A 2xx response whose body does not match the documented contract. */
export class ApiShapeError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ApiShapeError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

/** Shape guard for the envelope metadata every success response carries. */
export function isApiMeta(value: unknown): value is ApiMeta {
  return (
    isRecord(value) &&
    typeof value.request_id === 'string' &&
    typeof value.generated_at === 'string' &&
    typeof value.duration_ms === 'number' &&
    typeof value.provenance === 'string' &&
    isNullableString(value.data_quality) &&
    Array.isArray(value.warnings) &&
    typeof value.idempotent_replay === 'boolean'
  )
}

function isReadyCheck(value: unknown): value is ReadyCheck {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.name === 'string' &&
    (value.status === 'ok' ||
      value.status === 'degraded' ||
      value.status === 'offline' ||
      value.status === 'unknown') &&
    typeof value.detail === 'string'
  )
}

/** Shape guard for one universe item; the consumer reads every field. */
export function isSymbolInfo(value: unknown): value is SymbolInfo {
  return (
    isRecord(value) &&
    typeof value.symbol === 'string' &&
    typeof value.base_asset === 'string' &&
    typeof value.quote_asset === 'string' &&
    typeof value.contract_type === 'string' &&
    typeof value.tradifi === 'boolean'
  )
}

/** Shape guard for ``latest_scan``; every declared field is verified. */
export function isScanRecordData(value: unknown): value is ScanRecordData {
  return (
    isRecord(value) &&
    typeof value.scan_id === 'string' &&
    typeof value.symbol === 'string' &&
    typeof value.period === 'string' &&
    typeof value.status === 'string' &&
    typeof value.trigger_source === 'string' &&
    typeof value.metrics_only === 'boolean' &&
    typeof value.fixture_path === 'string' &&
    typeof value.started_at === 'string' &&
    typeof value.finished_at === 'string' &&
    typeof value.window_start === 'string' &&
    typeof value.window_end === 'string' &&
    typeof value.trade_count === 'number' &&
    typeof value.buy_volume === 'string' &&
    typeof value.sell_volume === 'string' &&
    typeof value.delta === 'string' &&
    typeof value.cvd === 'string' &&
    typeof value.data_quality === 'string' &&
    typeof value.manifest_path === 'string' &&
    typeof value.parquet_rows === 'number' &&
    typeof value.created_at === 'string'
  )
}

/** Shape guard for one per-chat Telegram receipt summary. */
export function isTelegramChatStatusData(value: unknown): value is TelegramChatStatusData {
  return (
    isRecord(value) &&
    typeof value.chat_id === 'number' &&
    typeof value.queued === 'number' &&
    typeof value.sending === 'number' &&
    typeof value.sent === 'number' &&
    typeof value.failed === 'number' &&
    isNullableString(value.last_error_code)
  )
}

/** Shape guard for one bounded recent Telegram receipt (no payload text). */
export function isTelegramReceiptData(value: unknown): value is TelegramReceiptData {
  return (
    isRecord(value) &&
    typeof value.event_key === 'string' &&
    typeof value.kind === 'string' &&
    typeof value.chat_id === 'number' &&
    typeof value.state === 'string' &&
    typeof value.attempts === 'number' &&
    (value.message_id === null || typeof value.message_id === 'number') &&
    isNullableString(value.last_error_code) &&
    typeof value.created_at === 'string' &&
    isNullableString(value.sent_at)
  )
}

/** Shape guard for the stable Telegram delivery document. */
export function isTelegramStatusData(value: unknown): value is TelegramStatusData {
  return (
    isRecord(value) &&
    typeof value.configured === 'boolean' &&
    typeof value.chat_count === 'number' &&
    typeof value.allowed_user_count === 'number' &&
    typeof value.poller_state === 'string' &&
    isNullableString(value.last_poll_at) &&
    isNullableString(value.last_send_at) &&
    typeof value.failing_receipts === 'number' &&
    isRecord(value.receipts) &&
    typeof value.receipts.queued === 'number' &&
    typeof value.receipts.sending === 'number' &&
    typeof value.receipts.sent === 'number' &&
    typeof value.receipts.failed === 'number' &&
    Array.isArray(value.per_chat) &&
    value.per_chat.every(isTelegramChatStatusData) &&
    Array.isArray(value.recent_receipts) &&
    value.recent_receipts.every(isTelegramReceiptData) &&
    isNullableString(value.overflow_code) &&
    isNullableString(value.last_error_code) &&
    typeof value.daemon_state === 'string'
  )
}

/** Shape guard for `StatusData`; a contract drift becomes a typed load error. */
export function isStatusData(value: unknown): value is StatusData {
  if (!isRecord(value) || !isRecord(value.universe)) {
    return false
  }
  const universe = value.universe
  const schedule = value.schedule
  return (
    value.status === 'ok' &&
    typeof value.env === 'string' &&
    typeof value.version === 'string' &&
    typeof value.host === 'string' &&
    typeof value.port === 'number' &&
    typeof value.data_dir === 'string' &&
    typeof value.log_level === 'string' &&
    typeof value.generated_at === 'string' &&
    typeof universe.source === 'string' &&
    typeof universe.as_of === 'string' &&
    typeof universe.count === 'number' &&
    typeof universe.tradifi_count === 'number' &&
    Array.isArray(universe.symbols) &&
    universe.symbols.every(isSymbolInfo) &&
    (value.telegram === undefined ||
      value.telegram === null ||
      isTelegramStatusData(value.telegram)) &&
    (schedule === undefined ||
      schedule === null ||
      (isRecord(schedule) &&
        typeof schedule.timezone === 'string' &&
        typeof schedule.period === 'string' &&
        isNullableString(schedule.pending_period) &&
        typeof schedule.next_run_at === 'string'))
  )
}

/** Shape guard for `ReadinessData`; never dereference an unvalidated body. */
export function isReadinessData(value: unknown): value is ReadinessData {
  if (!isRecord(value)) {
    return false
  }
  return (
    (value.status === 'ready' || value.status === 'not_ready') &&
    typeof value.version === 'string' &&
    typeof value.env === 'string' &&
    typeof value.data_dir === 'string' &&
    typeof value.checked_at === 'string' &&
    Array.isArray(value.checks) &&
    value.checks.every(isReadyCheck) &&
    (value.latest_scan === null || isScanRecordData(value.latest_scan))
  )
}

/** One named failing readiness check carried by a non-2xx `/ready` envelope. */
export interface ReadinessErrorDetail {
  readonly field: string
  readonly code: string
  readonly message: string
}

/** The daemon answered `/ready` but readiness is not OK (non-2xx envelope). */
export class ReadinessError extends Error {
  readonly code: string | undefined
  readonly details: readonly ReadinessErrorDetail[]

  constructor(message: string, code?: string, details: readonly ReadinessErrorDetail[] = []) {
    super(message)
    this.name = 'ReadinessError'
    this.code = code
    this.details = details
  }
}

/** Extract the named check details from an untrusted error envelope. */
function bodyErrorDetails(body: Record<string, unknown> | null): readonly ReadinessErrorDetail[] {
  if (!body || !isRecord(body.error) || !Array.isArray(body.error.details)) {
    return []
  }
  const details: ReadinessErrorDetail[] = []
  for (const item of body.error.details) {
    if (
      isRecord(item) &&
      typeof item.field === 'string' &&
      typeof item.code === 'string' &&
      typeof item.message === 'string'
    ) {
      details.push({ field: item.field, code: item.code, message: item.message })
    }
  }
  return details
}

export async function fetchStatus(signal?: AbortSignal): Promise<StatusEnvelope> {
  const response = await fetchWithTimeout(
    '/api/v1/status',
    { headers: { Accept: 'application/json' } },
    signal,
    READ_TIMEOUT_MS,
  )
  if (!response.ok) {
    throw new Error(`Status request failed with HTTP ${response.status}`)
  }
  const body: unknown = await response.json()
  if (!isRecord(body)) {
    throw new ApiShapeError('The status response was not in the expected shape.')
  }
  const data = body.data
  if (!body.ok || data === null || data === undefined) {
    throw new Error(bodyErrorMessage(body, 'API returned an error'))
  }
  if (!isStatusData(data)) {
    throw new ApiShapeError('The status response was not in the expected shape.')
  }
  // Build the envelope explicitly: `meta` is validated, never asserted.
  if (!isApiMeta(body.meta)) {
    throw new ApiShapeError('The status response meta was not in the expected shape.')
  }
  return { ok: true, data, error: null, meta: body.meta }
}

export async function fetchHealth(): Promise<HealthEnvelope> {
  const response = await fetch('/health', { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    throw new Error(`Health request failed with HTTP ${response.status}`)
  }
  const envelope = (await response.json()) as HealthEnvelope
  if (!envelope.ok || envelope.data === null) {
    throw envelopeError(envelope, 'Health check returned an error')
  }
  return envelope
}

export async function fetchReadiness(signal?: AbortSignal): Promise<ReadinessEnvelope> {
  const response = await fetchWithTimeout(
    '/ready',
    { headers: { Accept: 'application/json' } },
    signal,
    READ_TIMEOUT_MS,
  )
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  const envelope = isRecord(body) ? body : null
  if (!response.ok) {
    // A 503 still proves the daemon answered; surface it as a typed error so
    // the caller can render NOT_READY (and its named checks) instead of OFFLINE.
    throw new ReadinessError(
      envelope
        ? bodyErrorMessage(envelope, `Readiness request failed with HTTP ${response.status}`)
        : `Readiness request failed with HTTP ${response.status}`,
      bodyErrorCode(envelope),
      bodyErrorDetails(envelope),
    )
  }
  if (!envelope) {
    throw new ApiShapeError('The readiness response was not in the expected shape.')
  }
  const data = envelope.data
  if (!envelope.ok || data === null || data === undefined) {
    throw new ReadinessError(
      bodyErrorMessage(envelope, 'Readiness check returned an error'),
      bodyErrorCode(envelope),
      bodyErrorDetails(envelope),
    )
  }
  if (!isReadinessData(data)) {
    throw new ApiShapeError('The readiness response was not in the expected shape.')
  }
  if (!isApiMeta(envelope.meta)) {
    throw new ApiShapeError('The readiness response meta was not in the expected shape.')
  }
  return { ok: true, data, error: null, meta: envelope.meta }
}
