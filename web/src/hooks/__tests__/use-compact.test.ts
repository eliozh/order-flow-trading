import { describe, expect, it } from 'vitest'

import { COMPACT_QUERY, NARROW_QUERY, detectCompact, detectNarrow } from '../use-compact'

function withMatchMedia(queries: string[], run: () => void): void {
  const original = window.matchMedia
  window.matchMedia = ((query: string) =>
    ({
      matches: queries.includes(query),
      media: query,
    }) as MediaQueryList) as typeof window.matchMedia

  try {
    run()
  } finally {
    window.matchMedia = original
  }
}

describe('useCompact breakpoints', () => {
  it('keeps the global read-only contract at 768px or narrower', () => {
    expect(COMPACT_QUERY).toBe('(max-width: 768px)')
  })

  it('marks only phone width (<=480px) read-only on trigger and paper surfaces', () => {
    // The trigger queue, trigger detail, and paper account keep Accept/Reject
    // at 768px and at 200% zoom on a 1440px desktop (720px), while 375px stays
    // read-only (SC 1.4.4).
    expect(NARROW_QUERY).toBe('(max-width: 480px)')
  })

  it('resolves each breakpoint independently through window.matchMedia', () => {
    withMatchMedia([COMPACT_QUERY], () => {
      expect(detectCompact()).toBe(true)
      expect(detectNarrow()).toBe(false)
    })

    // A 375px phone satisfies both queries; only the narrow signal is true at
    // 768px, which keeps the global route contract read-only there.
    withMatchMedia([COMPACT_QUERY, NARROW_QUERY], () => {
      expect(detectCompact()).toBe(true)
      expect(detectNarrow()).toBe(true)
    })
  })
})
