/**
 * Typed client for the persisted settings API (slice 32: ``scan.period``).
 *
 * The read/mutation envelope is validated before it reaches the UI. Mutations
 * carry the shared CSRF double-submit header and one stable ``Idempotency-Key``
 * per logical save so a retry replays the original response instead of writing
 * twice.
 */

import type { ApiEnvelope } from './contract'
import { createPendingKeyStore, mutationHeaders } from './idempotency'
import { fetchWithTimeout, MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS } from './request-timeout'
import type { ApiError } from './status'

export { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS }

export const SETTINGS_SECTIONS_PATH = '/api/v1/settings'
export const SCAN_SETTINGS_PATH = '/api/v1/settings/scan'
export const RISK_SETTINGS_PATH = '/api/v1/settings/risk'
export const LLM_SETTINGS_PATH = '/api/v1/settings/llm'
export const LLM_USAGE_PATH = '/api/v1/llm/usage'
export const LLM_TEST_PATH = '/api/v1/llm/test'
export const SCAN_PERIOD_KEY_PREFIX = 'oft.settings.scan.'
export const RISK_SETTINGS_KEY_PREFIX = 'oft.settings.risk.'
export const LLM_SETTINGS_KEY_PREFIX = 'oft.settings.llm.'
const SCAN_PERIOD_KEY_ID = 'period'
const RISK_SETTINGS_KEY_ID = 'caps'
const LLM_SETTINGS_KEY_ID = 'document'

export type SettingSource = 'default' | 'user'

export type RiskCapField =
  | 'per_trade_risk_pct'
  | 'max_concurrent_positions'
  | 'daily_loss_pct'
  | 'max_leverage'
  | 'max_notional_pct'
  | 'max_slippage_bps'
  | 'max_spread_bps'
  | 'stale_mark_seconds'

export const RISK_CAP_FIELDS: readonly RiskCapField[] = [
  'per_trade_risk_pct',
  'max_concurrent_positions',
  'daily_loss_pct',
  'max_leverage',
  'max_notional_pct',
  'max_slippage_bps',
  'max_spread_bps',
  'stale_mark_seconds',
]

/**
 * The server's numeric ceilings, in one client source.
 *
 * These mirror ``domain/risk.validate_caps``: percentages are ``> 0`` and
 * ``<= 100``, leverage ``> 0`` and ``<= 125``, bps ``> 0`` and ``<= 10000``,
 * and the integer caps are ``>= 1`` with no upper bound. ``null`` means the
 * server enforces no maximum, so the form must not invent one.
 */
export const RISK_CAP_BOUNDS: Record<RiskCapField, number | null> = {
  per_trade_risk_pct: 100,
  max_concurrent_positions: null,
  daily_loss_pct: 100,
  max_leverage: 125,
  max_notional_pct: 100,
  max_slippage_bps: 10000,
  max_spread_bps: 10000,
  stale_mark_seconds: null,
}

export type RiskCaps = Record<RiskCapField, number>
export type RiskMetricSource = 'paper_account' | 'none'
export type RiskQuality = 'COMPLETE' | 'PARTIAL' | 'UNAVAILABLE'

export interface RiskUtilizationMetric {
  used: number | null
  cap: number
  unit: string
  source: RiskMetricSource
  quality: RiskQuality
  note: string | null
}

export interface RiskUtilization {
  source: RiskMetricSource
  quality: RiskQuality
  stale_inputs: string[]
  metrics: Record<RiskCapField, RiskUtilizationMetric>
}

export interface RiskSettingsData {
  caps: RiskCaps
  source: SettingSource
  defaults: RiskCaps
  utilization: RiskUtilization
  updated_at: string | null
  updated_by: string | null
}

export interface SettingsFieldDetail {
  field?: string | null
  code?: string
  message?: string
  /** For ``confirmation_required``: the server's committed value. */
  before?: number | null
  /** For ``confirmation_required``: the looser value the server refused. */
  after?: number | null
}

export interface ScanSettingsData {
  key: string
  value: string
  source: SettingSource
  supported: string[]
  next_run: string
  timezone: string
  updated_at: string | null
  updated_by: string | null
}

export interface SettingsSectionData {
  section: string
  source: SettingSource
  last_changed: string | null
  keys: string[]
}

export interface SettingsSectionsData {
  sections: SettingsSectionData[]
}

export type ScanSettingsEnvelope = ApiEnvelope<ScanSettingsData>
export type RiskSettingsEnvelope = ApiEnvelope<RiskSettingsData>
export type SettingsSectionsEnvelope = ApiEnvelope<SettingsSectionsData>

/**
 * Envelope codes that prove the server rejected the save before writing (or,
 * for ``IDEMPOTENCY_CONFLICT``, that this payload is definitively dead for the
 * presented key). Only these clear/rotate the pending idempotency key; anything
 * else (a transport failure or a timeout) may have written and must keep it.
 */
export const DEFINITIVE_SETTINGS_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'VALIDATION_FAILED',
  'AUTH_REQUIRED',
  'AUTH_INVALID',
  'CSRF_INVALID',
  'ORIGIN_FORBIDDEN',
  'HOST_FORBIDDEN',
  'IDEMPOTENCY_KEY_REQUIRED',
  'IDEMPOTENCY_CONFLICT',
  'CONFIG_INVALID',
])

export type SettingsApiErrorKind = 'refusal' | 'unknown'

export class SettingsApiError extends Error {
  readonly code: string | undefined
  readonly httpStatus: number | undefined
  readonly details: SettingsFieldDetail[]
  readonly kind: SettingsApiErrorKind

  constructor(
    message: string,
    code?: string,
    options: {
      httpStatus?: number
      details?: SettingsFieldDetail[]
      kind?: SettingsApiErrorKind
    } = {},
  ) {
    super(message)
    this.name = 'SettingsApiError'
    this.code = code
    this.httpStatus = options.httpStatus
    this.details = options.details ?? []
    this.kind =
      options.kind ??
      (code !== undefined && DEFINITIVE_SETTINGS_REFUSAL_CODES.has(code) ? 'refusal' : 'unknown')
  }
}

const scanKeys = createPendingKeyStore('web-settings', SCAN_PERIOD_KEY_PREFIX)
const riskKeys = createPendingKeyStore('web-settings-risk', RISK_SETTINGS_KEY_PREFIX)

/** The pending key for the scan-period save, or a fresh one when none exists. */
export function scanPeriodKey(): string {
  return scanKeys.keyFor(SCAN_PERIOD_KEY_ID)
}

/** Create and store a fresh key for a deliberate new save. */
export function rotateScanPeriodKey(): string {
  return scanKeys.rotateKey(SCAN_PERIOD_KEY_ID)
}

/** Forget the pending key after success or a definitive refusal. */
export function clearScanPeriodKey(): void {
  scanKeys.clearKey(SCAN_PERIOD_KEY_ID)
}

/** The pending key for the risk-cap save, or a fresh one when none exists. */
export function riskSettingsKey(): string {
  return riskKeys.keyFor(RISK_SETTINGS_KEY_ID)
}

/** Create and store a fresh key for a deliberate new risk-cap save. */
export function rotateRiskSettingsKey(): string {
  return riskKeys.rotateKey(RISK_SETTINGS_KEY_ID)
}

/** Forget the pending risk-cap key after success or a definitive refusal. */
export function clearRiskSettingsKey(): void {
  riskKeys.clearKey(RISK_SETTINGS_KEY_ID)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isSource(value: unknown): value is SettingSource {
  return value === 'default' || value === 'user'
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isScanSettings(value: unknown): value is ScanSettingsData {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.key === 'string' &&
    typeof value.value === 'string' &&
    isSource(value.source) &&
    isStringArray(value.supported) &&
    typeof value.next_run === 'string' &&
    typeof value.timezone === 'string' &&
    (value.updated_at === null || typeof value.updated_at === 'string') &&
    (value.updated_by === null || typeof value.updated_by === 'string')
  )
}

function isSections(value: unknown): value is SettingsSectionsData {
  if (!isRecord(value) || !Array.isArray(value.sections)) {
    return false
  }
  return value.sections.every(
    (section) =>
      isRecord(section) &&
      typeof section.section === 'string' &&
      isSource(section.source) &&
      (section.last_changed === null || typeof section.last_changed === 'string') &&
      isStringArray(section.keys),
  )
}

function isRiskCaps(value: unknown): value is RiskCaps {
  if (!isRecord(value)) {
    return false
  }
  return RISK_CAP_FIELDS.every((field) => typeof value[field] === 'number')
}

function isRiskSource(value: unknown): value is RiskMetricSource {
  return value === 'paper_account' || value === 'none'
}

function isRiskQuality(value: unknown): value is RiskQuality {
  return value === 'COMPLETE' || value === 'PARTIAL' || value === 'UNAVAILABLE'
}

function isRiskMetric(value: unknown): value is RiskUtilizationMetric {
  if (!isRecord(value)) {
    return false
  }
  return (
    (value.used === null || typeof value.used === 'number') &&
    typeof value.cap === 'number' &&
    typeof value.unit === 'string' &&
    isRiskSource(value.source) &&
    isRiskQuality(value.quality) &&
    (value.note === null || typeof value.note === 'string')
  )
}

function isRiskUtilization(value: unknown): value is RiskUtilization {
  if (
    !isRecord(value) ||
    !isRiskSource(value.source) ||
    !isRiskQuality(value.quality) ||
    !isStringArray(value.stale_inputs)
  ) {
    return false
  }
  const metrics = value.metrics
  if (!isRecord(metrics)) {
    return false
  }
  return RISK_CAP_FIELDS.every((field) => isRiskMetric(metrics[field]))
}

function isRiskSettings(value: unknown): value is RiskSettingsData {
  if (!isRecord(value)) {
    return false
  }
  return (
    isRiskCaps(value.caps) &&
    isSource(value.source) &&
    isRiskCaps(value.defaults) &&
    isRiskUtilization(value.utilization) &&
    (value.updated_at === null || typeof value.updated_at === 'string') &&
    (value.updated_by === null || typeof value.updated_by === 'string')
  )
}

function fieldDetails(error: ApiError | null): SettingsFieldDetail[] {
  if (error === null || !Array.isArray(error.details)) {
    return []
  }
  return error.details.flatMap((detail) =>
    isRecord(detail) ? [detail as SettingsFieldDetail] : [],
  )
}

async function requestSettings<T>(
  path: string,
  init: RequestInit,
  validate: (data: unknown) => data is T,
  signal: AbortSignal | undefined,
  timeoutMs: number,
): Promise<ApiEnvelope<T>> {
  const response = await fetchWithTimeout(path, init, signal, timeoutMs)
  let envelope: ApiEnvelope<T> | null = null
  try {
    envelope = (await response.json()) as ApiEnvelope<T>
  } catch {
    envelope = null
  }
  if (!response.ok || envelope === null || !envelope.ok || envelope.data === null) {
    const error = envelope?.error ?? null
    throw new SettingsApiError(
      error?.message ?? `Settings request failed with HTTP ${response.status}`,
      error?.code,
      { httpStatus: response.status, details: fieldDetails(error) },
    )
  }
  if (!validate(envelope.data)) {
    throw new SettingsApiError(
      'The settings response was not in the expected shape.',
      'INVALID_RESPONSE',
      {
        httpStatus: response.status,
      },
    )
  }
  return envelope
}

/** Read the section summaries (source/last change only; never values). */
export async function fetchSettingsSections(
  signal?: AbortSignal,
): Promise<SettingsSectionsEnvelope> {
  return requestSettings<SettingsSectionsData>(
    SETTINGS_SECTIONS_PATH,
    {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    },
    isSections,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Read the effective ``scan.period``, its source, and the next UTC boundary. */
export async function fetchScanSettings(signal?: AbortSignal): Promise<ScanSettingsEnvelope> {
  return requestSettings<ScanSettingsData>(
    SCAN_SETTINGS_PATH,
    {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    },
    isScanSettings,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Persist one scan period; the caller supplies the stable idempotency key. */
export async function updateScanPeriod(
  period: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<ScanSettingsEnvelope> {
  return requestSettings<ScanSettingsData>(
    SCAN_SETTINGS_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({ period }),
      credentials: 'same-origin',
    },
    isScanSettings,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Read the effective risk caps, their provenance, defaults, and utilization. */
export async function fetchRiskSettings(signal?: AbortSignal): Promise<RiskSettingsEnvelope> {
  return requestSettings<RiskSettingsData>(
    RISK_SETTINGS_PATH,
    {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    },
    isRiskSettings,
    signal,
    READ_TIMEOUT_MS,
  )
}

/**
 * Persist a partial risk-cap patch. ``confirm`` authorizes a looser value; the
 * caller supplies a fresh idempotency key for the confirmed request.
 */
export async function updateRiskSettings(
  caps: Partial<RiskCaps>,
  confirm: boolean,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<RiskSettingsEnvelope> {
  return requestSettings<RiskSettingsData>(
    RISK_SETTINGS_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({ caps, confirm }),
      credentials: 'same-origin',
    },
    isRiskSettings,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

// -- slice 35: LLM provider/model settings and write-only keys ---------------

export type LLMProviderName = 'openai' | 'anthropic' | 'deepseek'
export type LLMRoleName = 'ANALYSIS' | 'ENTRY_RECHECK' | 'MANAGEMENT' | 'REVIEW'
export type LLMKeySource = 'env' | 'keyring' | 'encrypted_file' | 'stored' | 'unset' | 'unavailable'
export type LLMCapabilityStatus = 'STRICT' | 'DEGRADED' | 'FAILED' | 'UNCHECKED'
export type LMUsagePeriod = 'day' | 'week' | 'month'

export const LLM_PROVIDERS: readonly LLMProviderName[] = ['openai', 'anthropic', 'deepseek']
export const LLM_ROLE_NAMES: readonly LLMRoleName[] = [
  'ANALYSIS',
  'ENTRY_RECHECK',
  'MANAGEMENT',
  'REVIEW',
]

export interface LLMCapability {
  status: LLMCapabilityStatus
  strict_schema: boolean
  degraded: boolean
  checked_at: string
  notes: string
}

export interface LLMProviderRow {
  provider: LLMProviderName
  configured: boolean
  key_source: LLMKeySource
  /** The only key-shaped value: the fixed mask plus the last four characters. */
  masked: string | null
  fingerprint: string | null
  updated_at: string | null
  updated_by: string | null
  base_url: string
  capability: LLMCapability | null
}

export type LLMRoles = Record<LLMRoleName, string | null>

export interface LLMSettingsData {
  provider: LLMProviderName | null
  providers: LLMProviderRow[]
  roles: LLMRoles
  base_urls: Record<LLMProviderName, string>
  allow_custom_base_url: boolean
  concurrency: { global: number; deepseek: number }
  source: SettingSource
  updated_at: string | null
  updated_by: string | null
}

export interface LLMSettingsPatch {
  provider?: LLMProviderName | null
  roles?: Partial<LLMRoles>
  base_urls?: Partial<Record<LLMProviderName, string>>
  allow_custom_base_url?: boolean
  max_concurrency?: number
  max_concurrency_deepseek?: number
}

export interface LLMKeyStatus {
  provider: LLMProviderName
  configured: boolean
  key_source: LLMKeySource
  masked: string | null
  fingerprint: string | null
  updated_at: string | null
  updated_by: string | null
}

export interface LLMTestResult {
  provider: LLMProviderName
  model: string
  status: LLMCapabilityStatus
  strict_schema: boolean
  degraded: boolean
  max_output_tokens: number
  error_code: string | null
  notes: string
  latency_ms: number
  checked_at: string
}

export interface LLMUsageData {
  period: LMUsagePeriod
  period_key: string
  spend_usd: string
  call_count: number
  unpriced_count: number
  budget_state: string
  thresholds: {
    daily_usd: string
    weekly_usd: string
    monthly_usd: string
    warn_pct: number
  }
  by_provider: { provider: string; spend_usd: string; call_count: number; unpriced_count: number }[]
  by_model: { model: string; spend_usd: string; call_count: number; unpriced_count: number }[]
}

export type LLMSettingsEnvelope = ApiEnvelope<LLMSettingsData>
export type LLMKeyStatusEnvelope = ApiEnvelope<LLMKeyStatus>
export type LLMTestEnvelope = ApiEnvelope<LLMTestResult>
export type LLMUsageEnvelope = ApiEnvelope<LLMUsageData>

const llmKeys = createPendingKeyStore('web-settings-llm', LLM_SETTINGS_KEY_PREFIX)

export function llmSettingsKey(): string {
  return llmKeys.keyFor(LLM_SETTINGS_KEY_ID)
}

export function rotateLLMSettingsKey(): string {
  return llmKeys.rotateKey(LLM_SETTINGS_KEY_ID)
}

export function clearLLMSettingsKey(): void {
  llmKeys.clearKey(LLM_SETTINGS_KEY_ID)
}

export function llmProviderKey(provider: LLMProviderName): string {
  return llmKeys.keyFor(`${provider}.key`)
}

export function rotateLLMProviderKey(provider: LLMProviderName): string {
  return llmKeys.rotateKey(`${provider}.key`)
}

export function clearLLMProviderKey(provider: LLMProviderName): void {
  llmKeys.clearKey(`${provider}.key`)
}

function isProviderName(value: unknown): value is LLMProviderName {
  return value === 'openai' || value === 'anthropic' || value === 'deepseek'
}

function isKeySource(value: unknown): value is LLMKeySource {
  return (
    value === 'env' ||
    value === 'keyring' ||
    value === 'encrypted_file' ||
    value === 'stored' ||
    value === 'unset' ||
    value === 'unavailable'
  )
}

function isCapabilityStatus(value: unknown): value is LLMCapabilityStatus {
  return value === 'STRICT' || value === 'DEGRADED' || value === 'FAILED' || value === 'UNCHECKED'
}

function isCapability(value: unknown): value is LLMCapability {
  if (!isRecord(value)) {
    return false
  }
  return (
    isCapabilityStatus(value.status) &&
    typeof value.strict_schema === 'boolean' &&
    typeof value.degraded === 'boolean' &&
    typeof value.checked_at === 'string' &&
    typeof value.notes === 'string'
  )
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isProviderRow(value: unknown): value is LLMProviderRow {
  if (!isRecord(value)) {
    return false
  }
  return (
    isProviderName(value.provider) &&
    typeof value.configured === 'boolean' &&
    isKeySource(value.key_source) &&
    isNullableString(value.masked) &&
    isNullableString(value.fingerprint) &&
    isNullableString(value.updated_at) &&
    isNullableString(value.updated_by) &&
    typeof value.base_url === 'string' &&
    (value.capability === null || isCapability(value.capability))
  )
}

function isRoles(value: unknown): value is LLMRoles {
  if (!isRecord(value)) {
    return false
  }
  return LLM_ROLE_NAMES.every((role) => isNullableString(value[role]))
}

function isBaseUrls(value: unknown): value is Record<LLMProviderName, string> {
  if (!isRecord(value)) {
    return false
  }
  return LLM_PROVIDERS.every((provider) => typeof value[provider] === 'string')
}

function isLLMSettings(value: unknown): value is LLMSettingsData {
  if (!isRecord(value) || !Array.isArray(value.providers) || !isRecord(value.concurrency)) {
    return false
  }
  return (
    (value.provider === null || isProviderName(value.provider)) &&
    value.providers.every(isProviderRow) &&
    isRoles(value.roles) &&
    isBaseUrls(value.base_urls) &&
    typeof value.allow_custom_base_url === 'boolean' &&
    typeof value.concurrency.global === 'number' &&
    typeof value.concurrency.deepseek === 'number' &&
    isSource(value.source) &&
    isNullableString(value.updated_at) &&
    isNullableString(value.updated_by)
  )
}

function isKeyStatus(value: unknown): value is LLMKeyStatus {
  if (!isRecord(value)) {
    return false
  }
  return (
    isProviderName(value.provider) &&
    typeof value.configured === 'boolean' &&
    isKeySource(value.key_source) &&
    isNullableString(value.masked) &&
    isNullableString(value.fingerprint) &&
    isNullableString(value.updated_at) &&
    isNullableString(value.updated_by)
  )
}

function isTestResult(value: unknown): value is LLMTestResult {
  if (!isRecord(value)) {
    return false
  }
  return (
    isProviderName(value.provider) &&
    typeof value.model === 'string' &&
    isCapabilityStatus(value.status) &&
    typeof value.strict_schema === 'boolean' &&
    typeof value.degraded === 'boolean' &&
    typeof value.max_output_tokens === 'number' &&
    (value.error_code === null || typeof value.error_code === 'string') &&
    typeof value.notes === 'string' &&
    typeof value.latency_ms === 'number' &&
    typeof value.checked_at === 'string'
  )
}

function isUsage(value: unknown): value is LLMUsageData {
  if (!isRecord(value) || !isRecord(value.thresholds)) {
    return false
  }
  return (
    (value.period === 'day' || value.period === 'week' || value.period === 'month') &&
    typeof value.period_key === 'string' &&
    typeof value.spend_usd === 'string' &&
    typeof value.call_count === 'number' &&
    typeof value.unpriced_count === 'number' &&
    typeof value.budget_state === 'string' &&
    typeof value.thresholds.daily_usd === 'string' &&
    typeof value.thresholds.weekly_usd === 'string' &&
    typeof value.thresholds.monthly_usd === 'string' &&
    typeof value.thresholds.warn_pct === 'number' &&
    Array.isArray(value.by_provider) &&
    Array.isArray(value.by_model)
  )
}

/** Read the effective LLM document plus masked provider metadata. */
export async function fetchLLMSettings(signal?: AbortSignal): Promise<LLMSettingsEnvelope> {
  return requestSettings<LLMSettingsData>(
    LLM_SETTINGS_PATH,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isLLMSettings,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Persist a partial LLM document; the response carries only masked keys. */
export async function updateLLMSettings(
  patch: LLMSettingsPatch,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<LLMSettingsEnvelope> {
  return requestSettings<LLMSettingsData>(
    LLM_SETTINGS_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify(patch),
      credentials: 'same-origin',
    },
    isLLMSettings,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Store or rotate one provider key; the request body is never echoed. */
export async function setLLMKey(
  provider: LLMProviderName,
  apiKey: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<LLMKeyStatusEnvelope> {
  return requestSettings<LLMKeyStatus>(
    `${LLM_SETTINGS_PATH}/${provider}/key`,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({ api_key: apiKey }),
      credentials: 'same-origin',
    },
    isKeyStatus,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Delete one provider key; the provider becomes unconfigured (no fallback). */
export async function deleteLLMKey(
  provider: LLMProviderName,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<LLMKeyStatusEnvelope> {
  return requestSettings<LLMKeyStatus>(
    `${LLM_SETTINGS_PATH}/${provider}/key`,
    {
      method: 'DELETE',
      headers: mutationHeaders(idempotencyKey),
      credentials: 'same-origin',
    },
    isKeyStatus,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Run the capability probe for one provider/role. */
export async function probeLLM(
  provider: LLMProviderName,
  role: LLMRoleName = 'ANALYSIS',
  signal?: AbortSignal,
): Promise<LLMTestEnvelope> {
  return requestSettings<LLMTestResult>(
    LLM_TEST_PATH,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        ...mutationHeaders('probe'),
      },
      body: JSON.stringify({ provider, role }),
      credentials: 'same-origin',
    },
    isTestResult,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Spend and unpriced counts for one budget window. */
export async function fetchLLMUsage(
  period: LMUsagePeriod,
  signal?: AbortSignal,
): Promise<LLMUsageEnvelope> {
  return requestSettings<LLMUsageData>(
    `${LLM_USAGE_PATH}?period=${period}`,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isUsage,
    signal,
    READ_TIMEOUT_MS,
  )
}
