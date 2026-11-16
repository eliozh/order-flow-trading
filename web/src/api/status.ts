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
    (value.latest_scan === null || isRecord(value.latest_scan))
  )
}

/** The daemon answered `/ready` but readiness is not OK (non-2xx envelope). */
export class ReadinessError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'ReadinessError'
    this.code = code
  }
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
  return body as unknown as StatusEnvelope
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
    // the caller can render NOT_READY instead of OFFLINE.
    throw new ReadinessError(
      envelope
        ? bodyErrorMessage(envelope, `Readiness request failed with HTTP ${response.status}`)
        : `Readiness request failed with HTTP ${response.status}`,
      bodyErrorCode(envelope),
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
    )
  }
  if (!isReadinessData(data)) {
    throw new ApiShapeError('The readiness response was not in the expected shape.')
  }
  return envelope as unknown as ReadinessEnvelope
}
