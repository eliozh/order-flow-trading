import { useEffect, useState } from 'react'

// Phone-width read-only surface. A 768px tablet must keep Accept/Reject, and
// 200% zoom on a desktop (1440px -> 720px) must not remove the actions.
export const COMPACT_QUERY = '(max-width: 480px)'

export function detectCompact(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia(COMPACT_QUERY).matches
}

/** Shared reactive compact/read-only breakpoint for every route. */
export function useCompact(override?: boolean): boolean {
  const [matches, setMatches] = useState<boolean>(() => detectCompact())

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return
    }
    const query = window.matchMedia(COMPACT_QUERY)
    const update = () => setMatches(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  return override ?? matches
}
