import { useCallback, useRef } from 'react'

export interface RunGeneration {
  /** Start a run and return its monotonically increasing generation. */
  begin: () => number
  /** True only while this generation is the most recently started run. */
  isCurrent: (generation: number) => boolean
}

/**
 * The run-generation guard for a screen with an initial load plus background
 * polling. Only the most recently started run may apply its result, so:
 *
 * - a late initial rejection cannot replace a page a poll already made ready;
 * - a stale poll cannot regress a newer initial (or retry) result.
 */
export function useRunGeneration(): RunGeneration {
  const next = useRef(0)
  const begin = useCallback(() => {
    next.current += 1
    return next.current
  }, [])
  const isCurrent = useCallback((generation: number) => generation === next.current, [])
  return { begin, isCurrent }
}
