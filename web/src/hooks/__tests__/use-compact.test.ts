import { describe, expect, it } from 'vitest'

import { COMPACT_QUERY, detectCompact } from '../use-compact'

describe('useCompact breakpoint', () => {
  it('is read-only only at phone width (<=480px)', () => {
    // A 768px tablet must keep Accept/Reject reachable; the brief only
    // requires read-only at 375px. 200% zoom on a 1440px desktop lands at
    // 720px and must not disable the human actions (SC 1.4.4).
    expect(COMPACT_QUERY).toBe('(max-width: 480px)')
  })

  it('matches the phone width query through window.matchMedia', () => {
    const original = window.matchMedia
    window.matchMedia = ((query: string) =>
      ({
        matches: query === COMPACT_QUERY,
        media: query,
      }) as MediaQueryList) as typeof window.matchMedia

    try {
      expect(detectCompact()).toBe(true)
    } finally {
      window.matchMedia = original
    }
  })
})
