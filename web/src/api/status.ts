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

export async function fetchStatus(): Promise<StatusEnvelope> {
  const response = await fetch('/api/v1/status', {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error(`Status request failed with HTTP ${response.status}`)
  }
  const envelope = (await response.json()) as StatusEnvelope
  if (!envelope.ok || envelope.data === null) {
    throw new Error(envelope.error?.message ?? 'API returned an error')
  }
  return envelope
}
