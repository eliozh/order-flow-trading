import { useCallback, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { LiveUpdatesContext, type LiveUpdatesState } from './use-live-updates'

/** Global "pause live updates" state (DESIGN A6 / WCAG 2.2.2). */
export function LiveUpdatesProvider({ children }: { children: ReactNode }) {
  const [paused, setPaused] = useState(false)
  const toggle = useCallback(() => setPaused((current) => !current), [])
  const value = useMemo<LiveUpdatesState>(() => ({ paused, setPaused, toggle }), [paused, toggle])
  return <LiveUpdatesContext.Provider value={value}>{children}</LiveUpdatesContext.Provider>
}
