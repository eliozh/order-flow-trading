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
  starred_count: number
  next_scan_count: number
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

async function requestEnvelope<T>(
  path: string,
  init: RequestInit,
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
    const error: ApiError | null = envelope?.error ?? null
    throw new WatchlistApiError(
      error?.message ?? `Watchlist request failed with HTTP ${response.status}`,
      error?.code,
    )
  }
  return envelope
}

/** Read the grouped registry, every row's star state, and the next-scan count. */
export async function fetchWatchlist(signal?: AbortSignal): Promise<WatchlistEnvelope> {
  return requestEnvelope<WatchlistData>(
    WATCHLIST_PATH,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Star (POST) or unstar (DELETE) one symbol; the server is idempotent. */
export async function setStar(
  symbol: string,
  starred: boolean,
  signal?: AbortSignal,
): Promise<StarEnvelope> {
  return requestEnvelope<StarResult>(
    `${WATCHLIST_PATH}/${encodeURIComponent(symbol)}`,
    {
      method: starred ? 'POST' : 'DELETE',
      headers: mutationHeaders(newIdempotencyKey('watchlist')),
      credentials: 'same-origin',
    },
    signal,
    MUTATION_TIMEOUT_MS,
  )
}
