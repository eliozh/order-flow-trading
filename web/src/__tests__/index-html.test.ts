import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const html = readFileSync(resolve(process.cwd(), 'index.html'), 'utf-8')

describe('index.html', () => {
  it('declares the primary document language as zh-CN', () => {
    expect(html).toContain('<html lang="zh-CN">')
  })
})
