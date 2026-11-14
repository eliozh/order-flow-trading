import type { ApiError, ApiMeta } from './status'

/** The shared API envelope every route returns. */
export interface ApiEnvelope<T> {
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}

/**
 * Parse a contract decimal string without fabricating a value for bad input.
 *
 * This is the single implementation shared by the journal and positions API
 * modules; callers that prefer the short name import it as ``num``.
 */
export function numberOrNull(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
