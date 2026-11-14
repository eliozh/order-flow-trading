import { createContext, useContext } from 'react'

export interface LiveUpdatesState {
  paused: boolean
  setPaused: (paused: boolean) => void
  toggle: () => void
}

/**
 * Safe default so a page renders (unpaused) even when mounted without the
 * provider, e.g. in an isolated component test.
 */
export const DEFAULT_LIVE_UPDATES: LiveUpdatesState = {
  paused: false,
  setPaused: () => undefined,
  toggle: () => undefined,
}

export const LiveUpdatesContext = createContext<LiveUpdatesState>(DEFAULT_LIVE_UPDATES)

export function useLiveUpdates(): LiveUpdatesState {
  return useContext(LiveUpdatesContext)
}
