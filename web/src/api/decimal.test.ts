import { describe, expect, it } from 'vitest'

import { addDecimals } from './decimal'

describe('addDecimals', () => {
  it('keeps exact scale and never introduces binary-float drift', () => {
    expect(addDecimals(['0.10', '0.20'])).toBe('0.30')
    expect(addDecimals(['1', '0.001'])).toBe('1.001')
    expect(addDecimals(['-0.10', '0.05'])).toBe('-0.05')
    expect(addDecimals([])).toBe('0')
  })
})
