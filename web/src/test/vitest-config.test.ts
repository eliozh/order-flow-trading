// @vitest-environment node
import { describe, expect, it } from 'vitest'

import viteConfig from '../../vite.config'

describe('vitest configuration', () => {
  it('collects unit specs from src and excludes the Playwright e2e suite', () => {
    const testConfig = (viteConfig as { test?: { include?: string[]; exclude?: string[] } }).test

    expect(testConfig?.include).toEqual(['src/**/*.{test,spec}.{ts,tsx}'])
    expect(testConfig?.exclude ?? []).toContain('e2e/**')
  })

  it('rewrites the Host header on every dev proxy (loopback allowlist)', () => {
    const proxy = (
      viteConfig as {
        server?: { proxy?: Record<string, { target?: string; changeOrigin?: boolean }> }
      }
    ).server?.proxy

    expect(proxy).toBeDefined()
    for (const [path, options] of Object.entries(proxy ?? {})) {
      expect(options.changeOrigin, `${path} must rewrite Host`).toBe(true)
      expect(options.target).toContain('127.0.0.1:7611')
    }
  })
})
