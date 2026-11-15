/**
 * Typed client for the versioned prompt API and the F10b list aggregate.
 *
 * Reads never mutate. Every write creates a new immutable version; there is no
 * PUT or in-place edit path. Mutations carry the shared CSRF double-submit
 * header and one stable ``Idempotency-Key`` per logical save, so a retry
 * replays the original response instead of creating a second version.
 */

import type { ApiEnvelope } from './contract'
import { createPendingKeyStore, mutationHeaders } from './idempotency'
import { fetchWithTimeout, MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS } from './request-timeout'
import type { ApiError } from './status'

export { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS }

export const PROMPT_SETTINGS_PATH = '/api/v1/settings/prompts'
export const PROMPTS_PATH = '/api/v1/prompts'
export const PROMPT_KEY_PREFIX = 'oft.settings.prompts.'

export type PromptRole = 'ANALYSIS' | 'ENTRY_RECHECK' | 'MANAGEMENT' | 'REVIEW'
export type PromptStatus = 'ACTIVE' | 'DRAFT' | 'SUPERSEDED'
export type PromptKind = 'SYSTEM' | 'USER'

export const PROMPT_ROLES: readonly PromptRole[] = [
  'ANALYSIS',
  'ENTRY_RECHECK',
  'MANAGEMENT',
  'REVIEW',
]

export const PROMPT_ROLE_LABELS: Record<PromptRole, string> = {
  ANALYSIS: '市场分析 / Analysis',
  ENTRY_RECHECK: '入场复核 / Entry re-check',
  MANAGEMENT: '仓位管理 / Management',
  REVIEW: '复盘总结 / Review',
}

/** One row of the list-only settings aggregate. */
export interface PromptSettingsRow {
  prompt_id: string
  role: PromptRole
  active_version: number
  author: string
  updated_at: string
  version_count: number
  latest_version: number
  required_variables: string[]
  is_builtin_default: boolean
}

export interface PromptSettingsData {
  items: PromptSettingsRow[]
  total: number
}

export interface PromptVersionData {
  prompt_id: string
  version: number
  status: PromptStatus
  is_active: boolean
  body: string
  body_hash: string
  required_variables: string[]
  author: string
  reason: string
  is_default: boolean
  created_at: string
  system_prompt_id: string
  system_prompt_version: number
  system_prompt_hash: string
  knowledge_artifact_hash: string | null
}

export interface PromptSummaryData {
  prompt_id: string
  role: PromptRole
  kind: PromptKind
  active_version: number
  version_count: number
  latest_version: number
  updated_at: string
}

export interface PromptDetailData {
  prompt_id: string
  role: PromptRole
  kind: PromptKind
  active_version: number
  created_at: string
  updated_at: string
  versions: PromptVersionData[]
}

export interface PromptHistoryData {
  prompt_id: string
  items: PromptVersionData[]
  total: number
}

export interface PromptDiffData {
  prompt_id: string
  from_version: number
  to_version: number
  diff: string
}

export interface PromptFieldDetail {
  field?: string | null
  code?: string
  message?: string
}

export type PromptApiErrorKind = 'refusal' | 'unknown'

/**
 * Envelope codes that prove the server rejected the write before creating a
 * version. Only these retire the pending idempotency key; a transport failure
 * may have written and must keep it so the retry replays.
 */
export const DEFINITIVE_PROMPT_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'PROMPT_VALIDATION_FAILED',
  'PROMPT_VERSION_MISSING',
  'CONFLICT',
  'NOT_FOUND',
  'AUTH_REQUIRED',
  'AUTH_INVALID',
  'CSRF_INVALID',
  'ORIGIN_FORBIDDEN',
  'HOST_FORBIDDEN',
  'IDEMPOTENCY_KEY_REQUIRED',
  'IDEMPOTENCY_CONFLICT',
])

export class PromptApiError extends Error {
  readonly code: string | undefined
  readonly httpStatus: number | undefined
  readonly details: PromptFieldDetail[]
  readonly kind: PromptApiErrorKind

  constructor(
    message: string,
    code?: string,
    options: {
      httpStatus?: number
      details?: PromptFieldDetail[]
      kind?: PromptApiErrorKind
    } = {},
  ) {
    super(message)
    this.name = 'PromptApiError'
    this.code = code
    this.httpStatus = options.httpStatus
    this.details = options.details ?? []
    this.kind =
      options.kind ??
      (code !== undefined && DEFINITIVE_PROMPT_REFUSAL_CODES.has(code) ? 'refusal' : 'unknown')
  }
}

export type PromptSettingsEnvelope = ApiEnvelope<PromptSettingsData>
export type PromptDetailEnvelope = ApiEnvelope<PromptDetailData>
export type PromptHistoryEnvelope = ApiEnvelope<PromptHistoryData>
export type PromptDiffEnvelope = ApiEnvelope<PromptDiffData>
export type PromptVersionEnvelope = ApiEnvelope<PromptVersionData>

const promptKeys = createPendingKeyStore('web-settings-prompts', PROMPT_KEY_PREFIX)

function scoped(promptId: string, action: string): string {
  return `${promptId}.${action}`
}

/** The pending key for a create-version save, or a fresh one when none exists. */
export function promptVersionKey(promptId: string): string {
  return promptKeys.keyFor(scoped(promptId, 'versions'))
}

/** Create and store a fresh key for a deliberate new version. */
export function rotatePromptVersionKey(promptId: string): string {
  return promptKeys.rotateKey(scoped(promptId, 'versions'))
}

/** Forget the pending create-version key after success or a refusal. */
export function clearPromptVersionKey(promptId: string): void {
  promptKeys.clearKey(scoped(promptId, 'versions'))
}

export function promptActivationKey(promptId: string): string {
  return promptKeys.keyFor(scoped(promptId, 'activate'))
}

export function rotatePromptActivationKey(promptId: string): string {
  return promptKeys.rotateKey(scoped(promptId, 'activate'))
}

export function clearPromptActivationKey(promptId: string): void {
  promptKeys.clearKey(scoped(promptId, 'activate'))
}

export function promptRestoreKey(promptId: string): string {
  return promptKeys.keyFor(scoped(promptId, 'restore'))
}

export function rotatePromptRestoreKey(promptId: string): string {
  return promptKeys.rotateKey(scoped(promptId, 'restore'))
}

export function clearPromptRestoreKey(promptId: string): void {
  promptKeys.clearKey(scoped(promptId, 'restore'))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isRole(value: unknown): value is PromptRole {
  return (
    value === 'ANALYSIS' ||
    value === 'ENTRY_RECHECK' ||
    value === 'MANAGEMENT' ||
    value === 'REVIEW'
  )
}

function isKind(value: unknown): value is PromptKind {
  return value === 'SYSTEM' || value === 'USER'
}

function isStatus(value: unknown): value is PromptStatus {
  return value === 'ACTIVE' || value === 'DRAFT' || value === 'SUPERSEDED'
}

function isSettingsRow(value: unknown): value is PromptSettingsRow {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.prompt_id === 'string' &&
    isRole(value.role) &&
    typeof value.active_version === 'number' &&
    typeof value.author === 'string' &&
    typeof value.updated_at === 'string' &&
    typeof value.version_count === 'number' &&
    typeof value.latest_version === 'number' &&
    isStringArray(value.required_variables) &&
    typeof value.is_builtin_default === 'boolean'
  )
}

function isSettingsData(value: unknown): value is PromptSettingsData {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    return false
  }
  return typeof value.total === 'number' && value.items.every(isSettingsRow)
}

function isVersion(value: unknown): value is PromptVersionData {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.prompt_id === 'string' &&
    typeof value.version === 'number' &&
    isStatus(value.status) &&
    typeof value.is_active === 'boolean' &&
    typeof value.body === 'string' &&
    typeof value.body_hash === 'string' &&
    isStringArray(value.required_variables) &&
    typeof value.author === 'string' &&
    typeof value.reason === 'string' &&
    typeof value.is_default === 'boolean' &&
    typeof value.created_at === 'string' &&
    typeof value.system_prompt_id === 'string' &&
    typeof value.system_prompt_version === 'number' &&
    typeof value.system_prompt_hash === 'string' &&
    isNullableString(value.knowledge_artifact_hash)
  )
}

function isDetailData(value: unknown): value is PromptDetailData {
  if (!isRecord(value) || !Array.isArray(value.versions)) {
    return false
  }
  return (
    typeof value.prompt_id === 'string' &&
    isRole(value.role) &&
    isKind(value.kind) &&
    typeof value.active_version === 'number' &&
    typeof value.created_at === 'string' &&
    typeof value.updated_at === 'string' &&
    value.versions.every(isVersion)
  )
}

function isHistoryData(value: unknown): value is PromptHistoryData {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    return false
  }
  return (
    typeof value.prompt_id === 'string' &&
    typeof value.total === 'number' &&
    value.items.every(isVersion)
  )
}

function isDiffData(value: unknown): value is PromptDiffData {
  if (!isRecord(value)) {
    return false
  }
  return (
    typeof value.prompt_id === 'string' &&
    typeof value.from_version === 'number' &&
    typeof value.to_version === 'number' &&
    typeof value.diff === 'string'
  )
}

function fieldDetails(error: ApiError | null): PromptFieldDetail[] {
  if (error === null || !Array.isArray(error.details)) {
    return []
  }
  return error.details.flatMap((detail) => (isRecord(detail) ? [detail as PromptFieldDetail] : []))
}

async function requestPrompt<T>(
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
    throw new PromptApiError(
      error?.message ?? `Prompt request failed with HTTP ${response.status}`,
      error?.code,
      { httpStatus: response.status, details: fieldDetails(error) },
    )
  }
  if (!validate(envelope.data)) {
    throw new PromptApiError(
      'The prompt response was not in the expected shape.',
      'INVALID_RESPONSE',
      { httpStatus: response.status },
    )
  }
  return envelope
}

function promptPath(promptId: string): string {
  return `${PROMPTS_PATH}/${encodeURIComponent(promptId)}`
}

/** Read the four USER roles with active-version metadata (list only). */
export async function fetchPromptSettings(signal?: AbortSignal): Promise<PromptSettingsEnvelope> {
  return requestPrompt<PromptSettingsData>(
    PROMPT_SETTINGS_PATH,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isSettingsData,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Read one template and its immutable version history. */
export async function fetchPromptDetail(
  promptId: string,
  signal?: AbortSignal,
): Promise<PromptDetailEnvelope> {
  return requestPrompt<PromptDetailData>(
    promptPath(promptId),
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isDetailData,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Read a bounded page of one template's history. */
export async function fetchPromptHistory(
  promptId: string,
  signal?: AbortSignal,
): Promise<PromptHistoryEnvelope> {
  return requestPrompt<PromptHistoryData>(
    `${promptPath(promptId)}/history`,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isHistoryData,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Read the unified diff between two immutable versions. */
export async function fetchPromptDiff(
  promptId: string,
  fromVersion: number,
  toVersion: number,
  signal?: AbortSignal,
): Promise<PromptDiffEnvelope> {
  return requestPrompt<PromptDiffData>(
    `${promptPath(promptId)}/diff?from_version=${fromVersion}&to_version=${toVersion}`,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isDiffData,
    signal,
    READ_TIMEOUT_MS,
  )
}

export interface CreatePromptVersionInput {
  body: string
  author?: string
  reason?: string
  activate?: boolean
}

/** Create a NEW immutable version; never edits the active one in place. */
export async function createPromptVersion(
  promptId: string,
  input: CreatePromptVersionInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<PromptVersionEnvelope> {
  return requestPrompt<PromptVersionData>(
    `${promptPath(promptId)}/versions`,
    {
      method: 'POST',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({
        body: input.body,
        author: input.author ?? 'web',
        reason: input.reason ?? '',
        activate: input.activate ?? false,
      }),
      credentials: 'same-origin',
    },
    isVersion,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Explicitly switch the active version; audited by the server. */
export async function activatePromptVersion(
  promptId: string,
  version: number,
  idempotencyKey: string,
  options: { actor?: string; reason?: string; signal?: AbortSignal } = {},
): Promise<PromptVersionEnvelope> {
  return requestPrompt<PromptVersionData>(
    `${promptPath(promptId)}/activate`,
    {
      method: 'POST',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({
        version,
        actor: options.actor ?? 'web',
        reason: options.reason ?? '',
      }),
      credentials: 'same-origin',
    },
    isVersion,
    options.signal,
    MUTATION_TIMEOUT_MS,
  )
}

/** Create a NEW version holding the built-in default body; it is a draft. */
export async function restorePromptDefault(
  promptId: string,
  idempotencyKey: string,
  options: { author?: string; reason?: string; signal?: AbortSignal } = {},
): Promise<PromptVersionEnvelope> {
  return requestPrompt<PromptVersionData>(
    `${promptPath(promptId)}/restore`,
    {
      method: 'POST',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({
        author: options.author ?? 'web',
        reason: options.reason ?? 'restore default',
      }),
      credentials: 'same-origin',
    },
    isVersion,
    options.signal,
    MUTATION_TIMEOUT_MS,
  )
}
