import { useEffect, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'

export interface OnboardingLoad<T> {
  state: 'loading' | 'ready' | 'error'
  data: T | null
  message: string | null
  retry: () => void
}

/**
 * The shared async-load shape for every onboarding step.
 *
 * Aborts the in-flight request on cleanup, ignores a caller-driven abort, and
 * exposes a retry that re-runs the loader. `loader` must be stable (wrap it in
 * `useCallback`) or every render re-fetches.
 */
export function useOnboardingLoad<T>(
  loader: (signal: AbortSignal) => Promise<T>,
): OnboardingLoad<T> {
  const [state, setState] = useState<Omit<OnboardingLoad<T>, 'retry'>>({
    state: 'loading',
    data: null,
    message: null,
  })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setState((previous) => ({ ...previous, state: 'loading', message: null }))
    loader(controller.signal)
      .then((data) => {
        if (active) {
          setState({ state: 'ready', data, message: null })
        }
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setState({ state: 'error', data: null, message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [loader, attempt])

  return { ...state, retry: () => setAttempt((value) => value + 1) }
}
