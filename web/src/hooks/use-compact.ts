import { useEffect, useState } from 'react'

export const COMPACT_QUERY = '(max-width: 768px)'

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
