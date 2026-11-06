// @vitest-environment node
import { describe, expect, it } from 'vitest'

import viteConfig from '../../vite.config'

describe('vitest configuration', () => {
  it('collects unit specs from src and excludes the Playwright e2e suite', () => {
    const testConfig = (viteConfig as { test?: { include?: string[]; exclude?: string[] } }).test

    expect(testConfig?.include).toEqual(['src/**/*.{test,spec}.{ts,tsx}'])
    expect(testConfig?.exclude ?? []).toContain('e2e/**')
  })
})
