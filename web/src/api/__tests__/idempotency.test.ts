import { beforeEach, describe, expect, it } from 'vitest'

import { createPendingKeyStore, newIdempotencyKey } from '../idempotency'

describe('newIdempotencyKey', () => {
  it('prefixes a fresh random key', () => {
    const first = newIdempotencyKey('web-test')
    const second = newIdempotencyKey('web-test')
    expect(first.startsWith('web-test-')).toBe(true)
    expect(first).not.toBe(second)
  })
})

describe('createPendingKeyStore', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('stores, reuses, rotates, and clears one key per id', () => {
    const store = createPendingKeyStore('web-test', 'oft.test.')

    const first = store.keyFor('A')
    expect(first.startsWith('web-test-A-')).toBe(true)
    expect(window.sessionStorage.getItem('oft.test.A')).toBe(first)
    expect(store.storedKey('A')).toBe(first)
    expect(store.keyFor('A')).toBe(first)

    const other = store.keyFor('B')
    expect(other).not.toBe(first)
    expect(store.storedKey('B')).toBe(other)

    const rotated = store.rotateKey('A')
    expect(rotated).not.toBe(first)
    expect(store.storedKey('A')).toBe(rotated)
    expect(store.storedKey('B')).toBe(other)

    store.clearKey('A')
    expect(store.storedKey('A')).toBeNull()
    expect(store.storedKey('B')).toBe(other)
  })

  it('keeps separate stores isolated by prefix', () => {
    const journal = createPendingKeyStore('web-review', 'oft.journal.review.')
    const positions = createPendingKeyStore('web-manage', 'oft.position.manage.')

    const reviewKey = journal.keyFor('ID-1')
    const manageKey = positions.keyFor('ID-1')

    expect(reviewKey.startsWith('web-review-ID-1-')).toBe(true)
    expect(manageKey.startsWith('web-manage-ID-1-')).toBe(true)
    expect(reviewKey).not.toBe(manageKey)
    expect(window.sessionStorage.getItem('oft.journal.review.ID-1')).toBe(reviewKey)
    expect(window.sessionStorage.getItem('oft.position.manage.ID-1')).toBe(manageKey)
  })
})
