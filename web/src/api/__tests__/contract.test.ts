import { describe, expect, it } from 'vitest'

import { numberOrNull } from '../contract'

describe('numberOrNull', () => {
  it('parses contract decimals without fabricating a value', () => {
    expect(numberOrNull('1.42')).toBe(1.42)
    expect(numberOrNull('-7.84')).toBe(-7.84)
    expect(numberOrNull(0)).toBe(0)
    expect(numberOrNull('')).toBeNull()
    expect(numberOrNull(null)).toBeNull()
    expect(numberOrNull(undefined)).toBeNull()
    expect(numberOrNull('not-a-number')).toBeNull()
    expect(numberOrNull('Infinity')).toBeNull()
  })
})
