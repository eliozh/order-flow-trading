import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchSymbols } from './symbols'

const emptyEnvelope = {
  ok: true,
  data: { items: [], total: 0, query: null, group: null },
  error: null,
  meta: {
    request_id: 'REQ-TEST',
    generated_at: '2026-11-06T00:00:00.000Z',
    duration_ms: 1,
    provenance: 'CALC',
    data_quality: 'MISSING',
    warnings: [],
    pagination: null,
    idempotent_replay: false,
  },
}

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(emptyEnvelope), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('fetchSymbols', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('requests enough rows for the 942-symbol universe', async () => {
    const fetchMock = stubFetch()

    await fetchSymbols()

    expect(String(fetchMock.mock.calls[0][0])).toContain('limit=2000')
  })

  it('preserves an explicit limit and query', async () => {
    const fetchMock = stubFetch()

    await fetchSymbols({ limit: 25, q: 'xau' })

    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('limit=25')
    expect(url).toContain('q=xau')
  })
})
