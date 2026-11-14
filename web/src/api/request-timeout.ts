/** Upper bound on one auxiliary read; a hung call becomes a typed TIMEOUT. */
export const READ_TIMEOUT_MS = 15_000

/**
 * Upper bound on one auxiliary mutation (re-check, accept, reject, kill-switch
 * toggle). Those can wait on an LLM stage, so the short read timeout would
 * abort a request that may still execute.
 */
export const MUTATION_TIMEOUT_MS = 120_000

/** A request that exceeded its deadline; typed so callers can offer a retry. */
export class RequestTimeoutError extends Error {
  readonly code = 'TIMEOUT'

  constructor(message = 'The request timed out.') {
    super(message)
    this.name = 'RequestTimeoutError'
  }
}

/**
 * Fetch with the caller's abort signal composed with a deadline.
 *
 * A deadline abort is surfaced as a typed ``RequestTimeoutError``; a
 * caller-driven abort stays an ``AbortError`` so cleanup paths keep ignoring
 * it. Never leaves a fetch hanging forever.
 */
export async function fetchWithTimeout(
  input: string,
  init: Omit<RequestInit, 'signal'>,
  signal?: AbortSignal,
  timeoutMs: number = READ_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController()
  let timedOut = false
  const forwardAbort = (): void => controller.abort()
  if (signal) {
    if (signal.aborted) {
      controller.abort()
    } else {
      signal.addEventListener('abort', forwardAbort, { once: true })
    }
  }
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } catch (error: unknown) {
    if (timedOut) {
      throw new RequestTimeoutError()
    }
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', forwardAbort)
  }
}
