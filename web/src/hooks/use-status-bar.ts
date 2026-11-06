import { useEffect, useRef, useState } from 'react'

import { fetchReadiness, type ReadinessEnvelope } from '../api/status'

export interface StatusBarState {
  daemon: 'UP' | 'OFFLINE'
  version: string
  readiness: 'READY' | 'NOT_READY' | 'UNKNOWN'
}

export const OFFLINE_STATUS: StatusBarState = {
  daemon: 'OFFLINE',
  version: 'UNKNOWN',
  readiness: 'UNKNOWN',
}

type ReadinessFetcher = () => Promise<ReadinessEnvelope>

/** Probe `/ready` and expose honest daemon/readiness/version values.
 *
 * The probe runs once on mount. `fetcher` is read through a ref so an inline
 * arrow does not retrigger the effect on every render (no fetch loop).
 */
export function useStatusBar(fetcher: ReadinessFetcher = fetchReadiness): StatusBarState {
  const [state, setState] = useState<StatusBarState>(OFFLINE_STATUS)
  const fetcherRef = useRef(fetcher)

  useEffect(() => {
    let active = true
    fetcherRef
      .current()
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        setState({
          daemon: 'UP',
          version: data?.version ?? 'UNKNOWN',
          readiness: data?.status === 'ready' ? 'READY' : 'NOT_READY',
        })
      })
      .catch(() => {
        if (active) {
          setState(OFFLINE_STATUS)
        }
      })
    return () => {
      active = false
    }
  }, [])

  return state
}
