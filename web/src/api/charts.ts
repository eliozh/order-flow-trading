import { fetchWithTimeout } from './request-timeout'
import type {
  ApiError,
  ApiMeta,
  DataGap,
  MetricsDocument,
  ReportListData,
  ReportSummary,
  SnapshotBar,
} from './reports'

/** Periods the timeframe switch may request; the native period is always valid. */
export type ChartPeriod = '15m' | '1h' | '4h'

export type ChartStatus = 'AVAILABLE' | 'MISSING'

/**
 * One resolved chart payload. A frozen report already is published bar and
 * metric data, so the API either returns one report's frozen values verbatim
 * (`AVAILABLE`) or an explicit `MISSING` with a reason. It never upsamples.
 */
export interface ChartData {
  report_id: string
  symbol: string
  native_period: string
  requested_period: string
  status: ChartStatus
  available_periods: string[]
  source_report_id: string | null
  generated_at: string | null
  bars: SnapshotBar[]
  metrics: MetricsDocument | null
  gaps: DataGap[]
  data_quality: string
  quality_reasons: string[]
  reason: string | null
}

export interface ChartEnvelope {
  ok: boolean
  data: ChartData | null
  error: ApiError | null
  meta: ApiMeta
}

export class ChartApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'ChartApiError'
    this.code = code
  }
}

async function getEnvelope<T>(
  path: string,
  signal?: AbortSignal,
): Promise<{ ok: boolean; data: T | null; error: ApiError | null; meta: ApiMeta }> {
  const response = await fetchWithTimeout(
    path,
    { headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    signal,
  )
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
    throw new ChartApiError(
      envelope?.error?.message ?? `Chart request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

/** Read the chart for one report; an omitted period means the native period. */
export async function fetchChart(
  reportId: string,
  period?: string,
  signal?: AbortSignal,
): Promise<ChartEnvelope> {
  const params = new URLSearchParams()
  if (period) {
    params.set('period', period)
  }
  const query = params.toString()
  const path = `/api/v1/reports/${encodeURIComponent(reportId)}/chart${query ? `?${query}` : ''}`
  return getEnvelope<ChartData>(path, signal)
}

/** The newest report for a symbol, or `null` when none has been published. */
export async function fetchLatestReportForSymbol(
  symbol: string,
  signal?: AbortSignal,
): Promise<ReportSummary | null> {
  const params = new URLSearchParams({ symbol, limit: '1' })
  const envelope = await getEnvelope<ReportListData>(`/api/v1/reports?${params.toString()}`, signal)
  return envelope.data?.items[0] ?? null
}
