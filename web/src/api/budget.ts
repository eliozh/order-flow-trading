/**
 * Typed client for the budget thresholds and usage API (slice 37, F10e).
 *
 * Threshold reads and writes share the settings envelope/error helpers; the
 * mutation keeps one stable ``Idempotency-Key`` per logical save. The response
 * is validated before the UI sees it, so malformed money becomes a typed
 * ``INVALID_RESPONSE`` error rather than a fabricated zero.
 */

import type { ApiEnvelope } from './contract'
import { createPendingKeyStore, mutationHeaders } from './idempotency'
import {
  MUTATION_TIMEOUT_MS,
  READ_TIMEOUT_MS,
  requestSettings,
  type SettingsFieldDetail,
} from './settings'

export { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS }
export type { SettingsFieldDetail }

export const BUDGET_PATH = '/api/v1/budget'
export const BUDGET_KEY_PREFIX = 'oft.settings.budget.'
const BUDGET_KEY_ID = 'thresholds'

/** The contract money shape: a non-negative decimal string. */
const MONEY_PATTERN = /^\d+(?:\.\d+)?$/

export type BudgetPeriod = 'day' | 'week' | 'month'
export type BudgetField = 'daily_usd' | 'weekly_usd' | 'monthly_usd' | 'warn_pct'
export type BudgetState = 'OK' | 'WARN' | 'EXCEEDED'
export type UsageQuality = 'COMPLETE' | 'UNAVAILABLE'

export interface BudgetThresholds {
  daily_usd: string
  weekly_usd: string
  monthly_usd: string
  warn_pct: number
}

export interface BudgetPeriodRow {
  period_key: string
  spend_usd: string
  call_count: number
  unpriced_count: number
  budget_state: BudgetState
}

export type BudgetPeriods = Record<BudgetPeriod, BudgetPeriodRow>

export interface BudgetProjectionBasis {
  month_spend_usd: string
  elapsed_fraction: number
  as_of: string
}

export interface BudgetAlert {
  period: BudgetPeriod
  state: 'WARN' | 'EXCEEDED'
  spend_usd: string
  threshold_usd: string
  occurred_at: string
  provider: string
  model: string
}

/**
 * The authoritative read model returned by GET/PUT: thresholds always, usage
 * nullable when the ledger is unavailable (``usage_quality: UNAVAILABLE``).
 */
export interface BudgetSnapshot {
  thresholds: BudgetThresholds
  usage_quality: UsageQuality
  budget_state: BudgetState | null
  periods: BudgetPeriods | null
  projected_month_end_usd: string | null
  projection_basis: BudgetProjectionBasis | null
  last_alert: BudgetAlert | null
}

export type BudgetData = BudgetSnapshot

export interface BudgetUpdatePayload {
  daily_usd?: string
  weekly_usd?: string
  monthly_usd?: string
  warn_pct?: number
}

export interface BudgetUpdateData extends BudgetSnapshot {
  changed: boolean
}

export type BudgetEnvelope = ApiEnvelope<BudgetData>
export type BudgetUpdateEnvelope = ApiEnvelope<BudgetUpdateData>

const budgetKeys = createPendingKeyStore('web-settings-budget', BUDGET_KEY_PREFIX)

export function budgetSettingsKey(): string {
  return budgetKeys.keyFor(BUDGET_KEY_ID)
}

export function rotateBudgetSettingsKey(): string {
  return budgetKeys.rotateKey(BUDGET_KEY_ID)
}

export function clearBudgetSettingsKey(): void {
  budgetKeys.clearKey(BUDGET_KEY_ID)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isString(value: unknown): value is string {
  return typeof value === 'string'
}

function isMoney(value: unknown): value is string {
  return typeof value === 'string' && MONEY_PATTERN.test(value)
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

function isBudgetPeriod(value: unknown): value is BudgetPeriod {
  return value === 'day' || value === 'week' || value === 'month'
}

function isBudgetState(value: unknown): value is BudgetState {
  return value === 'OK' || value === 'WARN' || value === 'EXCEEDED'
}

function isUsageQuality(value: unknown): value is UsageQuality {
  return value === 'COMPLETE' || value === 'UNAVAILABLE'
}

function isThresholds(value: unknown): value is BudgetThresholds {
  if (!isRecord(value)) {
    return false
  }
  return (
    isMoney(value.daily_usd) &&
    isMoney(value.weekly_usd) &&
    isMoney(value.monthly_usd) &&
    typeof value.warn_pct === 'number' &&
    Number.isFinite(value.warn_pct)
  )
}

function isPeriodRow(value: unknown): value is BudgetPeriodRow {
  if (!isRecord(value)) {
    return false
  }
  return (
    isString(value.period_key) &&
    isMoney(value.spend_usd) &&
    isCount(value.call_count) &&
    isCount(value.unpriced_count) &&
    isBudgetState(value.budget_state)
  )
}

function isPeriods(value: unknown): value is BudgetPeriods {
  if (!isRecord(value)) {
    return false
  }
  return (
    isPeriodRow(value.day) &&
    isPeriodRow(value.week) &&
    isPeriodRow(value.month)
  )
}

function isProjectionBasis(value: unknown): value is BudgetProjectionBasis {
  if (!isRecord(value)) {
    return false
  }
  const fraction = value.elapsed_fraction
  return (
    isMoney(value.month_spend_usd) &&
    typeof fraction === 'number' &&
    Number.isFinite(fraction) &&
    fraction > 0 &&
    fraction <= 1 &&
    isString(value.as_of)
  )
}

function isAlert(value: unknown): value is BudgetAlert {
  if (!isRecord(value)) {
    return false
  }
  return (
    isBudgetPeriod(value.period) &&
    (value.state === 'WARN' || value.state === 'EXCEEDED') &&
    isMoney(value.spend_usd) &&
    isMoney(value.threshold_usd) &&
    isString(value.occurred_at) &&
    isString(value.provider) &&
    isString(value.model)
  )
}

function isSnapshot(value: Record<string, unknown>): value is Record<string, unknown> & BudgetSnapshot {
  if (!isThresholds(value.thresholds) || !isUsageQuality(value.usage_quality)) {
    return false
  }
  if (value.usage_quality === 'UNAVAILABLE') {
    return (
      value.budget_state === null &&
      value.periods === null &&
      value.projected_month_end_usd === null &&
      value.projection_basis === null &&
      value.last_alert === null
    )
  }
  return (
    isBudgetState(value.budget_state) &&
    isPeriods(value.periods) &&
    isMoney(value.projected_month_end_usd) &&
    isProjectionBasis(value.projection_basis) &&
    (value.last_alert === null || isAlert(value.last_alert))
  )
}

function isBudgetData(value: unknown): value is BudgetData {
  return isRecord(value) && isSnapshot(value)
}

function isBudgetUpdateData(value: unknown): value is BudgetUpdateData {
  return isRecord(value) && isSnapshot(value) && typeof value.changed === 'boolean'
}

/** Read thresholds plus the current usage/projection/last-alert snapshot. */
export async function fetchBudget(signal?: AbortSignal): Promise<BudgetEnvelope> {
  return requestSettings<BudgetData>(
    BUDGET_PATH,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isBudgetData,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Persist thresholds; the caller supplies the stable idempotency key. */
export async function updateBudgetThresholds(
  payload: BudgetUpdatePayload,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<BudgetUpdateEnvelope> {
  return requestSettings<BudgetUpdateData>(
    BUDGET_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify(payload),
      credentials: 'same-origin',
    },
    isBudgetUpdateData,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}
