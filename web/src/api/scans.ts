import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

export interface ScanSymbolRow {
  symbol: string
  stage: string
  report_id: string | null
  data_quality: string | null
  error_code: string | null
  error_message: string | null
  failed_stage?: string | null
  llm_status: string | null
  llm_tokens: number
  cost_usd: string
  cost_unpriced: boolean
  data_stage?: string
  metrics_stage?: string
  llm_stage?: string
  report_stage?: string
}

export interface ReportLink {
  symbol: string
  report_id: string
  href: string
}

export interface DeliveryState {
  configured: boolean
  reason: string
}

export interface ScanData {
  scan_id: string
  symbol?: string
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
  report_id: string | null
  source_mode: string
  metrics_manifest_json: string | null
  batch_id?: string | null
  llm_configured?: boolean
  llm_status?: string | null
  llm_tokens?: number
  cost_usd?: string
  cost_unpriced?: boolean
  error_code?: string | null
  error_message?: string | null
  degraded?: boolean
  symbol_stage?: string | null
  symbols?: ScanSymbolRow[]
  symbols_total?: number
  symbols_completed?: number
  symbols_degraded?: number
  symbols_failed?: number
  total_tokens?: number
  total_cost_usd?: string
  report_links?: ReportLink[]
  delivery?: DeliveryState
}

export interface ScanListData {
  items: ScanData[]
  total: number
}

export interface ScanEnvelope {
  ok: boolean
  data: ScanData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface ScanListEnvelope {
  ok: boolean
  data: ScanListData | null
  error: ApiError | null
  meta: ApiMeta
}

export class ScanApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'ScanApiError'
    this.code = code
  }
}

async function getEnvelope<T>(path: string): Promise<{
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}> {
  const response = await fetch(path, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  })
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
    throw new ScanApiError(
      envelope?.error?.message ?? `Scan request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

export async function fetchScan(scanId: string): Promise<ScanEnvelope> {
  return getEnvelope<ScanData>(`/api/v1/scans/${encodeURIComponent(scanId)}`)
}

export async function fetchScans(limit = 50): Promise<ScanListEnvelope> {
  return getEnvelope<ScanListData>(`/api/v1/scans?limit=${limit}`)
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

function idempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `web-${crypto.randomUUID()}`
  }
  return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/**
 * Trigger one manual starred-symbol batch from the /scans schedule card.
 *
 * The POST runs the whole batch synchronously, so a successful response is a
 * finished scan rather than a queued job; callers should refetch the history.
 */
export async function triggerScan(period = '4h'): Promise<ScanEnvelope> {
  const csrf = readCookie('oft_csrf')
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'Idempotency-Key': idempotencyKey(),
  }
  if (csrf) {
    headers['X-CSRF-Token'] = csrf
  }
  const response = await fetch('/api/v1/scans', {
    method: 'POST',
    headers,
    credentials: 'same-origin',
    // Source and metrics_only follow the server defaults so the UI can never
    // silently force a metrics-only or backfill-only batch.
    body: JSON.stringify({ period }),
  })
  let envelope: ScanEnvelope | null = null
  try {
    envelope = (await response.json()) as ScanEnvelope
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    throw new ScanApiError(
      envelope?.error?.message ?? `Scan request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}
