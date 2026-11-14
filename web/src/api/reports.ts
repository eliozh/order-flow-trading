import { fetchWithTimeout } from './request-timeout'
import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

export interface SnapshotBar {
  bar_start: string
  bar_end: string
  open: string
  high: string
  low: string
  close: string
  volume: string
  quote_volume: string
  trade_count: number
  taker_buy_volume: string
  delta: string
  cvd: string
  open_interest: string | null
  funding_rate: string | null
}

export interface CvdPoint {
  bar_start: string
  bar_end: string
  buy_volume: string
  sell_volume: string
  delta: string
  cvd: string
  taker_buy_ratio: number | null
}

export interface CvdSummary {
  delta: string
  cvd_change: string
  buy_volume: string
  sell_volume: string
}

export interface CvdSection {
  unit: string
  points: CvdPoint[]
  summary: CvdSummary
  divergence_flags: { bar_start: string; kind: string; note: string }[]
}

export interface FootprintCell {
  price: string
  bid_volume: string
  ask_volume: string
  delta: string
  total_volume: string
  imbalance_ratio: number | null
  is_stacked_imbalance: boolean
}

export interface FootprintBar {
  bar_start: string
  bar_end: string
  cells: FootprintCell[]
}

export interface FootprintSection {
  price_bucket: string
  imbalance_threshold: number
  bars: FootprintBar[]
  top_imbalances: { bar_start: string; price: string; side: string; ratio: number }[]
}

export interface VolumeBin {
  price_low: string
  price_high: string
  volume: string
  buy_volume: string
  sell_volume: string
  is_poc: boolean
  in_value_area: boolean
}

export interface VolumeProfileSection {
  window: { start: string; end: string }
  bin_size: string
  value_area_pct: number
  poc: string
  vah: string
  val: string
  bins: VolumeBin[]
  notes: string[]
}

export interface OpenInterestSection {
  unit: string
  sampling: string
  points: {
    ts: string
    open_interest: string
    open_interest_value: string
    long_short_ratio: number | null
    taker_ratio: number | null
  }[]
  delta_over_window: string
  last: { ts: string; open_interest: string; open_interest_value: string }
}

export interface FundingSection {
  current_rate: string
  next_funding_time: string
  interval_hours: number
  mark_price: string
  index_price: string
  history: { ts: string; rate: string; interval_hours: number }[]
}

export interface LiquidationsSection {
  coverage: string
  coverage_start: string | null
  coverage_end: string | null
  total_long_notional: string
  total_short_notional: string
  count: number
  buckets: {
    bucket_start: string
    bucket_end: string
    long_notional: string
    short_notional: string
    count: number
  }[]
  limitations: string[]
}

export interface SourceRef {
  kind: string
  ref: string
  as_of: string
  version?: string | null
  hash?: string | null
}

export interface DataGap {
  gap_id: string | null
  data_type: string
  start: string
  end: string
  reason: string
  severity: string
  resolved: boolean
}

export interface MetricsDocument {
  schema_version: string
  symbol: string
  period: string
  window: { start: string; end: string }
  computed_at: string
  metric_version: string
  input_manifest_hash: string
  data_quality: string
  quality_reasons: string[]
  cvd: CvdSection
  footprint: FootprintSection
  volume_profile: VolumeProfileSection
  open_interest: OpenInterestSection
  funding: FundingSection
  liquidations: LiquidationsSection
  sources: SourceRef[]
  gaps: DataGap[]
}

export interface MarketSnapshot {
  schema_version: string
  snapshot_id: string
  symbol: string
  timeframe: string
  as_of: string
  window: { start: string; end: string }
  source_mode: string
  data_quality: string
  quality_reasons: string[]
  last_price: string
  mark_price: string
  bars: SnapshotBar[]
  orderflow: MetricsDocument
  session: { underlying_session: string; is_no_trading: boolean; note: string }
  contract_specs: Record<string, unknown>
  sources: SourceRef[]
  gaps: DataGap[]
  snapshot_hash: string
}

export interface MetricManifestEntry {
  path: string
  data_type: string
  symbol: string
  date: string
  rows: number
  bytes: number
  checksum: string
  source: string
  created_at: string
  status: string
  metric_version: string | null
  input_manifest_hash: string | null
  window_start: string | null
  window_end: string | null
  run_id: string | null
}

/** The knowledge artifact injected into one LLM call. Never contains the raw prompt. */
export interface LlmKnowledgeState {
  status: string
  mode?: string
  artifact_id?: string | null
  selected_section_ids?: string[]
  selected_topics?: string[]
  token_count?: number
  budget_tokens?: number
  dropped_section_ids?: string[]
  excluded_sections?: { section_id: string; reason: string; tokens: number }[]
  warning?: string | null
  reason?: string | null
  exact_replay?: boolean
}

export interface LlmCostState {
  amount_usd?: string | null
  currency?: string
  price_version?: string
  unpriced?: boolean
}

export interface LlmUsageState {
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  reasoning_tokens?: number
  total_tokens?: number
}

export type ConfidenceBand = 'high' | 'medium' | 'low'

/** Contract bands: 0.70+ high, 0.40–0.69 medium, below 0.40 low. */
export function confidenceBand(confidence: number | null | undefined): ConfidenceBand | null {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) {
    return null
  }
  if (confidence >= 0.7) {
    return 'high'
  }
  if (confidence >= 0.4) {
    return 'medium'
  }
  return 'low'
}

/**
 * The rich LLM band returned by `GET /api/v1/reports/{id}`.
 *
 * When no provider runs, the payload is only `{configured: false, reason}`;
 * every other field is optional so the CALC report stays renderable.
 */
export interface LlmState {
  configured: boolean
  reason?: string | null
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: number
  prompt_hash?: string
  knowledge?: LlmKnowledgeState
  system_prompt_id?: string
  system_prompt_version?: number
  system_prompt_hash?: string
  validation_status?: string | null
  decision_type?: string | null
  confidence?: number | null
  narrative?: string | null
  rationale?: string | null
  opportunity_ids?: string[]
  cost?: LlmCostState
  usage?: LlmUsageState
  reference_price?: string | null
}

/** The compact LLM cell returned by `GET /api/v1/reports`. */
export interface LlmSummaryState {
  configured: boolean
  validation_status: string | null
  opportunity_ids: string[]
}

export interface ReportData {
  report_id: string
  scan_id: string
  symbol: string
  timeframe: string
  generated_at: string
  data_quality: string
  llm_decision_id: string | null
  narrative_ref: string | null
  llm: LlmState
  llm_stage: string
  llm_tokens: number
  cost_usd: string
  cost_unpriced: boolean
  window_policy?: unknown
  delivery?: { configured: boolean; reason: string }
  metrics_manifest: MetricManifestEntry[]
  snapshot: MarketSnapshot
  metrics: MetricsDocument
}

export interface ReportSummary {
  report_id: string
  scan_id: string
  symbol: string
  timeframe: string
  generated_at: string
  data_quality: string
  llm_decision_id: string | null
  narrative_ref: string | null
  llm_stage: string
  llm_tokens: number
  cost_usd: string
  cost_unpriced: boolean
  llm: LlmSummaryState
}

export interface ReportListData {
  items: ReportSummary[]
  total: number
  scan_id: string | null
}

export interface ReportEnvelope {
  ok: boolean
  data: ReportData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface ReportListEnvelope {
  ok: boolean
  data: ReportListData | null
  error: ApiError | null
  meta: ApiMeta
}

export class ReportApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'ReportApiError'
    this.code = code
  }
}

async function getEnvelope<T>(
  path: string,
  signal?: AbortSignal,
): Promise<{
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}> {
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
    throw new ReportApiError(
      envelope?.error?.message ?? `Report request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

export async function fetchReport(reportId: string, signal?: AbortSignal): Promise<ReportEnvelope> {
  return getEnvelope<ReportData>(`/api/v1/reports/${encodeURIComponent(reportId)}`, signal)
}

export async function fetchReports(options: { limit?: number; scanId?: string } = {}) {
  const params = new URLSearchParams()
  params.set('limit', String(options.limit ?? 50))
  if (options.scanId) {
    params.set('scan_id', options.scanId)
  }
  return getEnvelope<ReportListData>(`/api/v1/reports?${params.toString()}`)
}

/** Render an ISO instant as a compact UTC label (no locale drift). */
export function utcLabel(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) {
    return iso
  }
  return `${parsed.toISOString().slice(0, 16).replace('T', ' ')}`
}

/** Prefix positive decimals with ``+`` without changing precision. */
export function signedDecimal(value: string): string {
  if (value.startsWith('-') || value === '0') {
    return value
  }
  return `+${value}`
}
