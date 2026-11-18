/**
 * Typed client for the Telegram settings aggregate (slice 53, F10f).
 *
 * The token is write-only: the server returns only ``configured``/``masked``/
 * ``last4``/``source``/``updated_at``, and this client never stores a typed
 * token. Every mutation carries the shared CSRF double-submit header and one
 * stable idempotency key per logical save.
 */

import type { ApiEnvelope } from './contract'
import { createPendingKeyStore, mutationHeaders } from './idempotency'
import { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS, requestSettings } from './settings'

export { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS, requestSettings, SettingsApiError } from './settings'
export type { SettingsApiErrorKind, SettingsFieldDetail } from './settings'

export const TELEGRAM_SETTINGS_PATH = '/api/v1/settings/telegram'
export const TELEGRAM_TOKEN_PATH = '/api/v1/settings/telegram/token'
export const TELEGRAM_TEST_PATH = '/api/v1/telegram/test'
export const TELEGRAM_SETTINGS_KEY_PREFIX = 'oft.settings.telegram.'

export type TelegramKeySource =
  'env' | 'keyring' | 'encrypted_file' | 'stored' | 'unset' | 'unavailable'
export type SettingSource = 'default' | 'user'
export type TelegramReceiptState = 'queued' | 'sending' | 'sent' | 'failed'

export const TELEGRAM_TOGGLE_NAMES = [
  'scan_report',
  'trigger',
  'entry_recheck',
  'position',
  'risk',
  'budget',
] as const

export type TelegramToggleName = (typeof TELEGRAM_TOGGLE_NAMES)[number]
export type TelegramToggles = Record<TelegramToggleName, boolean>

export interface TelegramTokenStatus {
  configured: boolean
  masked: string | null
  last4: string | null
  source: TelegramKeySource
  updated_at: string | null
}

export interface TelegramReceipt {
  event_key: string
  kind: string
  chat_id: number
  reply_to_message_id: number | null
  state: TelegramReceiptState
  attempts: number
  message_id: number | null
  last_error_code: string | null
  next_attempt_at: string
  created_at: string
  sent_at: string | null
}

export interface TelegramSettingsData {
  token: TelegramTokenStatus
  chat_ids: number[]
  allowed_user_ids: number[]
  toggles: TelegramToggles
  source: SettingSource
  updated_at: string | null
  updated_by: string | null
  receipts: TelegramReceipt[]
  receipt_total: number
  latest_test_receipt: TelegramReceipt | null
}

export interface TelegramSettingsPatch {
  chat_ids?: number[]
  allowed_user_ids?: number[]
  toggles?: Partial<TelegramToggles>
  reason?: string
}

export type TelegramSettingsEnvelope = ApiEnvelope<TelegramSettingsData>
export type TelegramTokenEnvelope = ApiEnvelope<TelegramTokenStatus>
export type TelegramReceiptEnvelope = ApiEnvelope<TelegramReceipt>

const telegramKeys = createPendingKeyStore('web-settings-telegram', TELEGRAM_SETTINGS_KEY_PREFIX)

/** One stable pending key per logical Telegram mutation. */
export function telegramOperationKey(label: string): string {
  return telegramKeys.keyFor(label)
}

/** Create a fresh key for a deliberate new save (e.g. after a 409 conflict). */
export function rotateTelegramOperationKey(label: string): string {
  return telegramKeys.rotateKey(label)
}

/** Forget a pending key after success or a definitive refusal. */
export function clearTelegramOperationKey(label: string): void {
  telegramKeys.clearKey(label)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isNumberArray(value: unknown): value is number[] {
  return Array.isArray(value) && value.every((item) => Number.isInteger(item))
}

function isKeySource(value: unknown): value is TelegramKeySource {
  return (
    value === 'env' ||
    value === 'keyring' ||
    value === 'encrypted_file' ||
    value === 'stored' ||
    value === 'unset' ||
    value === 'unavailable'
  )
}

function isTokenStatus(value: unknown): value is TelegramTokenStatus {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.configured === 'boolean' &&
    isNullableString(value.masked) &&
    isNullableString(value.last4) &&
    isKeySource(value.source) &&
    isNullableString(value.updated_at)
  )
}

function isToggles(value: unknown): value is TelegramToggles {
  if (!isRecord(value)) {
    return false
  }
  return TELEGRAM_TOGGLE_NAMES.every((name) => typeof value[name] === 'boolean')
}

function isReceiptState(value: unknown): value is TelegramReceiptState {
  return value === 'queued' || value === 'sending' || value === 'sent' || value === 'failed'
}

function isReceipt(value: unknown): value is TelegramReceipt {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.event_key === 'string' &&
    typeof value.kind === 'string' &&
    typeof value.chat_id === 'number' &&
    (value.reply_to_message_id === null || typeof value.reply_to_message_id === 'number') &&
    isReceiptState(value.state) &&
    typeof value.attempts === 'number' &&
    (value.message_id === null || typeof value.message_id === 'number') &&
    isNullableString(value.last_error_code) &&
    typeof value.next_attempt_at === 'string' &&
    typeof value.created_at === 'string' &&
    isNullableString(value.sent_at)
  )
}

function isSettings(value: unknown): value is TelegramSettingsData {
  if (!isRecord(value) || !Array.isArray(value.receipts)) {
    return false
  }
  return (
    isTokenStatus(value.token) &&
    isNumberArray(value.chat_ids) &&
    isNumberArray(value.allowed_user_ids) &&
    isToggles(value.toggles) &&
    (value.source === 'default' || value.source === 'user') &&
    isNullableString(value.updated_at) &&
    isNullableString(value.updated_by) &&
    value.receipts.every(isReceipt) &&
    typeof value.receipt_total === 'number' &&
    (value.latest_test_receipt === null || isReceipt(value.latest_test_receipt))
  )
}

const READ_INIT: RequestInit = {
  method: 'GET',
  headers: { Accept: 'application/json' },
  credentials: 'same-origin',
}

/** Read the aggregate: masked token, allowlists, toggles, and receipts. */
export async function fetchTelegramSettings(
  signal?: AbortSignal,
): Promise<TelegramSettingsEnvelope> {
  return requestSettings<TelegramSettingsData>(
    TELEGRAM_SETTINGS_PATH,
    READ_INIT,
    isSettings,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Persist a partial patch; the response is the same aggregate shape. */
export async function updateTelegramSettings(
  patch: TelegramSettingsPatch,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TelegramSettingsEnvelope> {
  return requestSettings<TelegramSettingsData>(
    TELEGRAM_SETTINGS_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify(patch),
      credentials: 'same-origin',
    },
    isSettings,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Store or rotate the bot token; the response is the mask only. */
export async function setTelegramToken(
  botToken: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TelegramTokenEnvelope> {
  return requestSettings<TelegramTokenStatus>(
    TELEGRAM_TOKEN_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({ bot_token: botToken }),
      credentials: 'same-origin',
    },
    isTokenStatus,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Delete the stored token; an environment token, if set, still applies. */
export async function deleteTelegramToken(
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TelegramTokenEnvelope> {
  return requestSettings<TelegramTokenStatus>(
    TELEGRAM_TOKEN_PATH,
    {
      method: 'DELETE',
      headers: mutationHeaders(idempotencyKey),
      credentials: 'same-origin',
    },
    isTokenStatus,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Enqueue one test message; the response is its receipt. */
export async function sendTelegramTest(
  idempotencyKey: string,
  chatId?: number,
  signal?: AbortSignal,
): Promise<TelegramReceiptEnvelope> {
  return requestSettings<TelegramReceipt>(
    TELEGRAM_TEST_PATH,
    {
      method: 'POST',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify(chatId === undefined ? {} : { chat_id: chatId }),
      credentials: 'same-origin',
    },
    isReceipt,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** A stable, non-secret failure reason for a test send. */
export function telegramTestFailureReason(
  error: { code?: string } | null,
  message: string,
): string {
  const code = error?.code
  if (code === 'TELEGRAM_TOKEN_INVALID') {
    return '401 Unauthorized：token 无效或已被撤销。'
  }
  if (code === 'TELEGRAM_NOT_CONFIGURED') {
    return '未配置 token 或没有可用 chat。'
  }
  if (code === 'TELEGRAM_DELIVERY_FAILED') {
    return '投递失败：网络错误、限流或 chat 被阻塞；其他 chat 不受影响。'
  }
  if (code === 'RATE_LIMITED') {
    return '触发 Telegram 限流；稍后重试。'
  }
  return message
}
