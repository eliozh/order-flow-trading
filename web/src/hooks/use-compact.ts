import { useEffect, useState } from 'react'

// Global read-only surface: tablet width and below (<=768px).
export const COMPACT_QUERY = '(max-width: 768px)'

// Narrow phone-only read-only signal. Used by the trigger queue, trigger
// detail, and paper account so a 768px tablet and 200% zoom on a desktop
// (1440px -> 720px) keep Accept/Reject reachable.
export const NARROW_QUERY = '(max-width: 480px)'

export function detectCompact(): boolean {
  return matchesQuery(COMPACT_QUERY)
}

export function detectNarrow(): boolean {
  return matchesQuery(NARROW_QUERY)
}

function matchesQuery(query: string): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia(query).matches
}

function useMediaQuery(query: string, detect: () => boolean): boolean {
  const [matches, setMatches] = useState<boolean>(detect)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return
    }
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [query])

  return matches
}

/** Shared reactive compact/read-only breakpoint for every route. */
export function useCompact(override?: boolean): boolean {
  const matches = useMediaQuery(COMPACT_QUERY, detectCompact)
  return override ?? matches
}

/** Reactive phone-only read-only signal for the trigger and paper surfaces. */
export function useNarrowCompact(override?: boolean): boolean {
  const matches = useMediaQuery(NARROW_QUERY, detectNarrow)
  return override ?? matches
}
