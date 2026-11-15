/**
 * Typed client for the watchlist read/mutation surface.
 *
 * ``GET /api/v1/watchlist`` returns the grouped registry plus per-row star
 * state and the next-scan count. ``POST``/``DELETE /api/v1/watchlist/{symbol}``
 * star/unstar one symbol; both are semantically idempotent on the server. The
 * mutation still carries the shared CSRF double-submit header, and a fresh
 * ``Idempotency-Key`` names one deliberate user action.
 */

import type { ApiEnvelope } from './contract'
import { mutationHeaders, newIdempotencyKey } from './idempotency'
import { fetchWithTimeout, MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS } from './request-timeout'
import type { ApiError } from './status'
import type { SymbolContract } from './symbols'

export const WATCHLIST_PATH = '/api/v1/watchlist'

export interface WatchlistRow extends SymbolContract {
  starred: boolean
  starred_at: string | null
}

export interface WatchlistGroup {
  name: string
  count: number
  symbols: string[]
}

export interface WatchlistData {
  groups: WatchlistGroup[]
  total: number
  rows: WatchlistRow[]
  next_scan_count: number
  /** The bounded page omits registry rows; ``total`` is still the true count. */
  truncated: boolean
  star_mutation: boolean
}

export interface StarResult {
  symbol: string
  starred: boolean
  starred_at: string | null
}

export type WatchlistEnvelope = ApiEnvelope<WatchlistData>
export type StarEnvelope = ApiEnvelope<StarResult>

/** A typed watchlist failure (message and code from the API error envelope). */
export class WatchlistApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'WatchlistApiError'
    this.code = code
  }
}

/**
 * A 2xx response whose body could not be trusted as a confirmation.
 *
 * The server may already have committed the change (a truncated stream, or an
 * unexpected ``ok``/``data`` shape), so the caller must refetch rather than
 * roll back. ``parseCause`` keeps the original JSON parse cause.
 */
export class WatchlistOutcomeUnknownError extends Error {
  readonly parseCause: unknown

  constructor(message: string, parseCause: unknown) {
    super(message)
    this.name = 'WatchlistOutcomeUnknownError'
    this.parseCause = parseCause
  }
}

async function requestEnvelope<T>(
  path: string,
  init: RequestInit,
  signal: AbortSignal | undefined,
  timeoutMs: number,
): Promise<ApiEnvelope<T>> {
  const response = await fetchWithTimeout(path, init, signal, timeoutMs)
  let envelope: ApiEnvelope<T> | null = null
  let parseCause: unknown = null
  try {
    envelope = (await response.json()) as ApiEnvelope<T>
  } catch (cause: unknown) {
    envelope = null
    parseCause = cause
  }
  if (!response.ok) {
    // Only a non-2xx response is a definitive refusal; use the envelope error
    // when it parsed, otherwise the HTTP status.
    const error: ApiError | null = envelope?.error ?? null
    throw new WatchlistApiError(
      error?.message ?? `Watchlist request failed with HTTP ${response.status}`,
      error?.code,
    )
  }
  if (envelope === null) {
    throw new WatchlistOutcomeUnknownError(
      `Watchlist response could not be parsed (HTTP ${response.status}).`,
      parseCause,
    )
  }
  if (envelope.ok !== true || envelope.data === null) {
    throw new WatchlistOutcomeUnknownError(
      'Watchlist response did not confirm the request.',
      parseCause,
    )
  }
  return envelope
}

/** A ``StarResult`` whose fields can be trusted as a confirmation. */
function isStarResult(data: unknown): data is StarResult {
  if (typeof data !== 'object' || data === null) {
    return false
  }
  const candidate = data as Record<string, unknown>
  return (
    typeof candidate.symbol === 'string' &&
    typeof candidate.starred === 'boolean' &&
    (candidate.starred_at === null || typeof candidate.starred_at === 'string')
  )
}

/** A ``WatchlistData`` page the route can render without inventing state. */
function isWatchlistData(data: unknown): data is WatchlistData {
  if (typeof data !== 'object' || data === null) {
    return false
  }
  const candidate = data as Record<string, unknown>
  return (
    Array.isArray(candidate.rows) &&
    typeof candidate.total === 'number' &&
    typeof candidate.next_scan_count === 'number' &&
    typeof candidate.truncated === 'boolean'
  )
}

/** Read the grouped registry, every row's star state, and the next-scan count. */
export async function fetchWatchlist(signal?: AbortSignal): Promise<WatchlistEnvelope> {
  const envelope = await requestEnvelope<WatchlistData>(
    WATCHLIST_PATH,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    signal,
    READ_TIMEOUT_MS,
  )
  // A 2xx body with a valid envelope but an unusable page is not a usable
  // load: the caller must treat it as a failure, never apply it as data.
  if (!isWatchlistData(envelope.data)) {
    throw new WatchlistOutcomeUnknownError(
      'Watchlist response did not include a usable page.',
      null,
    )
  }
  return envelope
}

/** Star (POST) or unstar (DELETE) one symbol; the server is idempotent. */
export async function setStar(
  symbol: string,
  starred: boolean,
  signal?: AbortSignal,
): Promise<StarEnvelope> {
  const envelope = await requestEnvelope<StarResult>(
    `${WATCHLIST_PATH}/${encodeURIComponent(symbol)}`,
    {
      method: starred ? 'POST' : 'DELETE',
      headers: mutationHeaders(newIdempotencyKey('watchlist')),
      credentials: 'same-origin',
    },
    signal,
    MUTATION_TIMEOUT_MS,
  )
  // Never confirm from a body the client cannot trust: the server may or may
  // not have committed, so the caller refetches instead.
  if (!isStarResult(envelope.data)) {
    throw new WatchlistOutcomeUnknownError(
      'Watchlist response did not confirm the star change.',
      null,
    )
  }
  return envelope
}
