// @vitest-environment node
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import viteConfig from '../../vite.config'

describe('vitest configuration', () => {
  it('collects unit specs from src and excludes the Playwright e2e suite', () => {
    const testConfig = (viteConfig as { test?: { include?: string[]; exclude?: string[] } }).test

    expect(testConfig?.include).toEqual(['src/**/*.{test,spec}.{ts,tsx}'])
    expect(testConfig?.exclude ?? []).toContain('e2e/**')
  })

  it('does not register the onboarding project in the aggregate Playwright config', () => {
    // The onboarding journey owns its daemon and generated config via
    // `e2e/run-onboarding-e2e.sh`; the aggregate runner starts no 7654 daemon.
    const source = readFileSync(new URL('../../playwright.config.ts', import.meta.url), 'utf8')
    // Match code, not prose: strip comments first so documentation may name a
    // dedicated runner without tripping the structural guard.
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
    expect(code).not.toMatch(/onboarding/i)
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
      const target = new URL(options.target ?? '')
      expect(
        ['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname),
        `${path} target must be a loopback address`,
      ).toBe(true)
    }
  })
})
