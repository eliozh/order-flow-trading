import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  SETTINGS_READONLY_QUERY,
  detectSettingsReadonly,
  useSettingsReadonly,
} from '../settings-field'

interface FakeMatchMedia {
  setMatches: (next: boolean) => void
  listenerCount: () => number
  restore: () => void
}

/** Install a controllable ``window.matchMedia`` for one test. */
function installMatchMedia(initial: boolean): FakeMatchMedia {
  const original = window.matchMedia
  const listeners = new Set<() => void>()
  const media = {
    matches: initial,
    media: SETTINGS_READONLY_QUERY,
    addEventListener: (_type: string, listener: () => void) => {
      listeners.add(listener)
    },
    removeEventListener: (_type: string, listener: () => void) => {
      listeners.delete(listener)
    },
  }
  window.matchMedia = (() => media) as unknown as typeof window.matchMedia
  return {
    setMatches(next: boolean) {
      media.matches = next
      for (const listener of listeners) {
        listener()
      }
    },
    listenerCount: () => listeners.size,
    restore: () => {
      window.matchMedia = original
    },
  }
}

describe('settings readonly media query', () => {
  it('detects the phone-width coarse-pointer query through window.matchMedia', () => {
    expect(SETTINGS_READONLY_QUERY).toBe('(max-width: 767.98px) and (pointer: coarse)')

    const matching = installMatchMedia(true)
    try {
      expect(detectSettingsReadonly()).toBe(true)
    } finally {
      matching.restore()
    }

    const fine = installMatchMedia(false)
    try {
      expect(detectSettingsReadonly()).toBe(false)
    } finally {
      fine.restore()
    }
  })

  it('updates the reactive hook when the media query changes and unsubscribes on unmount', () => {
    const helper = installMatchMedia(true)
    try {
      const { result, unmount } = renderHook(() => useSettingsReadonly())
      expect(result.current).toBe(true)

      act(() => helper.setMatches(false))
      expect(result.current).toBe(false)

      act(() => helper.setMatches(true))
      expect(result.current).toBe(true)

      expect(helper.listenerCount()).toBe(1)
      unmount()
      expect(helper.listenerCount()).toBe(0)
    } finally {
      helper.restore()
    }
  })

  it('lets an explicit override win without subscribing', () => {
    const helper = installMatchMedia(true)
    try {
      const { result, unmount } = renderHook(() => useSettingsReadonly(false))
      expect(result.current).toBe(false)
      expect(helper.listenerCount()).toBe(0)
      unmount()
    } finally {
      helper.restore()
    }
  })
})
