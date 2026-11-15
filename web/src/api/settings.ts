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
export const SCAN_PERIOD_KEY_PREFIX = 'oft.settings.scan.'
const SCAN_PERIOD_KEY_ID = 'period'

export type SettingSource = 'default' | 'user'

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
export type SettingsSectionsEnvelope = ApiEnvelope<SettingsSectionsData>

/**
 * Envelope codes that prove the server rejected the save before writing. Only
 * these clear the pending idempotency key; anything else (a transport failure,
 * a timeout, or ``IDEMPOTENCY_CONFLICT``) may have written and must keep it.
 */
export const DEFINITIVE_SETTINGS_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'VALIDATION_FAILED',
  'AUTH_REQUIRED',
  'AUTH_INVALID',
  'CSRF_INVALID',
  'ORIGIN_FORBIDDEN',
  'HOST_FORBIDDEN',
  'IDEMPOTENCY_KEY_REQUIRED',
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
