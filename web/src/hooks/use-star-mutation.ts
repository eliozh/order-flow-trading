/**
 * The optimistic star protocol shared by `/watchlist` and `/watchlist/search`.
 *
 * One request per symbol at a time (single-flight pending set), an optimistic
 * apply before the server answers, the server-confirmed state on success, a
 * visible rollback only for a definitive refusal, and a refetch (never a
 * blind rollback) when the outcome is unknown (timeout or network failure).
 * Pages supply the state updaters and the announcement copy.
 */

import { useEffect, useRef, useState } from 'react'

import { RequestTimeoutError } from '../api/request-timeout'
import type { StarEnvelope } from '../api/watchlist'

export interface StarMutationHandlers {
  /** Show the requested state before the server answers. */
  onOptimistic: (symbol: string, starred: boolean) => void
  /** Apply the server's confirmed state. */
  onConfirmed: (symbol: string, starred: boolean, starredAt: string | null) => void
  /** Definitive refusal: restore the pre-toggle state and show an error. */
  onRollback: (symbol: string, cause: unknown) => void
  /**
   * Unknown outcome: the server may already have committed. Refetch and let
   * the server state reconcile instead of guessing.
   */
  onUnknownOutcome: (symbol: string, cause: unknown) => void
  /** Content for the page's single polite live region on success. */
  onAnnounce: (message: string) => void
  successMessage: (symbol: string, starred: boolean) => string
}

interface UseStarMutationOptions extends StarMutationHandlers {
  setStarFn: (symbol: string, starred: boolean) => Promise<StarEnvelope>
}

export interface StarMutation {
  pending: ReadonlySet<string>
  toggle: (symbol: string, next: boolean) => void
}

/**
 * A timeout or a network-level fetch failure leaves the outcome unknown.
 * ``TypeError`` is what ``fetch`` throws when the request never reached the
 * server. A typed API error is definitive and rolls back.
 */
export function isUnknownStarOutcome(cause: unknown): boolean {
  return cause instanceof RequestTimeoutError || cause instanceof TypeError
}

export function useStarMutation(options: UseStarMutationOptions): StarMutation {
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set())
  const pendingRef = useRef<Set<string>>(new Set())
  const optionsRef = useRef(options)

  useEffect(() => {
    optionsRef.current = options
  }, [options])

  function toggle(symbol: string, next: boolean): void {
    if (pendingRef.current.has(symbol)) {
      return
    }
    const current = optionsRef.current
    pendingRef.current.add(symbol)
    setPending(new Set(pendingRef.current))
    current.onOptimistic(symbol, next)
    current
      .setStarFn(symbol, next)
      .then((envelope) => {
        const data = envelope.data
        if (data === null) {
          throw new Error('The star response was empty.')
        }
        current.onConfirmed(symbol, data.starred, data.starred_at)
        current.onAnnounce(current.successMessage(symbol, data.starred))
      })
      .catch((cause: unknown) => {
        const latest = optionsRef.current
        if (isUnknownStarOutcome(cause)) {
          latest.onUnknownOutcome(symbol, cause)
          return
        }
        latest.onRollback(symbol, cause)
      })
      .finally(() => {
        pendingRef.current.delete(symbol)
        setPending(new Set(pendingRef.current))
      })
  }

  return { pending, toggle }
}
