import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

export interface ScanData {
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
  report_id: string | null
  source_mode: string
  metrics_manifest_json: string | null
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
