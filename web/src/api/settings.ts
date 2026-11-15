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
export const SCAN_PERIOD_KEY_PREFIX = 'oft.settings.scan.'
export const RISK_SETTINGS_KEY_PREFIX = 'oft.settings.risk.'
const SCAN_PERIOD_KEY_ID = 'period'
const RISK_SETTINGS_KEY_ID = 'caps'

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
