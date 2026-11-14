import { useCallback, useEffect, useRef } from 'react'

import { isAbortError } from '../api/abort'

export type PollRunner = (signal: AbortSignal, manual: boolean) => Promise<void> | void

export interface PollingOptions {
  /** Revalidation cadence while live. */
  refreshMs: number
  /** When paused, no interval or focus/visibility listener exists. */
  paused: boolean
  /** A non-abort failure of a background revalidation. */
  onError?: (error: unknown) => void
}

export interface PollingResult {
  /** Run one revalidation immediately (also used after an unknown outcome). */
  revalidate: (manual?: boolean) => void
}

/**
 * The shared revalidation skeleton for the positions list and detail.
 *
 * While paused it registers nothing and the effect cleanup aborts the in-flight
 * revalidation; resuming performs exactly one immediate revalidation. The
 * page's own initial load is independent and still runs.
 */
export function usePolling(run: PollRunner, options: PollingOptions): PollingResult {
  const runRef = useRef(run)
  runRef.current = run
  const errorRef = useRef(options.onError)
  errorRef.current = options.onError
  const { refreshMs, paused } = options
  const controllerRef = useRef<AbortController | null>(null)
  const wasPausedRef = useRef(paused)

  const revalidate = useCallback((manual = false): void => {
    controllerRef.current?.abort()
    const next = new AbortController()
    controllerRef.current = next
    Promise.resolve(runRef.current(next.signal, manual)).catch((error: unknown) => {
      if (next.signal.aborted || isAbortError(error)) {
        return
      }
      errorRef.current?.(error)
    })
  }, [])

  useEffect(() => {
    const resumed = wasPausedRef.current && !paused
    wasPausedRef.current = paused
    if (paused) {
      return
    }
    const interval = window.setInterval(() => revalidate(false), refreshMs)
    const onFocus = (): void => revalidate(true)
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') {
        revalidate(true)
      }
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    if (resumed) {
      revalidate(false)
    }
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
      controllerRef.current?.abort()
    }
  }, [paused, refreshMs, revalidate])

  return { revalidate }
}
