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
  status: 'ok' | 'degraded' | 'offline'
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

/** The daemon answered `/ready` but readiness is not OK (non-2xx envelope). */
export class ReadinessError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'ReadinessError'
    this.code = code
  }
}

export async function fetchStatus(): Promise<StatusEnvelope> {
  const response = await fetch('/api/v1/status', {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error(`Status request failed with HTTP ${response.status}`)
  }
  const envelope = (await response.json()) as StatusEnvelope
  if (!envelope.ok || envelope.data === null) {
    throw envelopeError(envelope, 'API returned an error')
  }
  return envelope
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

export async function fetchReadiness(): Promise<ReadinessEnvelope> {
  const response = await fetch('/ready', { headers: { Accept: 'application/json' } })
  let envelope: ReadinessEnvelope | null = null
  try {
    envelope = (await response.json()) as ReadinessEnvelope
  } catch {
    envelope = null
  }
  if (!response.ok) {
    // A 503 still proves the daemon answered; surface it as a typed error so
    // the caller can render NOT_READY instead of OFFLINE.
    throw new ReadinessError(
      envelope?.error?.message ?? `Readiness request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  if (!envelope || !envelope.ok || envelope.data === null) {
    throw new ReadinessError(
      envelope?.error?.message ?? 'Readiness check returned an error',
      envelope?.error?.code,
    )
  }
  return envelope
}
