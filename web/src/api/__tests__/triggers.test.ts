import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { acceptTrigger, getTrigger, recheckTrigger, rejectTrigger } from '../triggers'

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify({ ok: true, data: {}, error: null, meta: {} }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function headersOf(fetchMock: ReturnType<typeof vi.fn>, call = 0): Record<string, string> {
  const init = fetchMock.mock.calls[call][1] as RequestInit
  return (init.headers ?? {}) as Record<string, string>
}

describe('trigger API mutations', () => {
  beforeEach(() => {
    document.cookie = 'oft_csrf=csrf-test-token'
  })

  afterEach(() => {
    document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    vi.unstubAllGlobals()
  })

  it('sends the CSRF header on every mutation', async () => {
    const fetchMock = stubFetch()

    await recheckTrigger('TRG-1', 'recheck-key')
    await acceptTrigger('TRG-1', 'accept-key')
    await rejectTrigger('TRG-1', 'structure broken', 'reject-key')

    expect(fetchMock).toHaveBeenCalledTimes(3)
    for (const call of [0, 1, 2]) {
      expect(headersOf(fetchMock, call)['X-CSRF-Token']).toBe('csrf-test-token')
      expect((fetchMock.mock.calls[call][1] as RequestInit).method).toBe('POST')
    }
  })

  it('sends a stable Idempotency-Key across accept retries', async () => {
    const fetchMock = stubFetch()

    await acceptTrigger('TRG-1', 'stable-accept-key')
    await acceptTrigger('TRG-1', 'stable-accept-key')

    expect(headersOf(fetchMock, 0)['Idempotency-Key']).toBe('stable-accept-key')
    expect(headersOf(fetchMock, 1)['Idempotency-Key']).toBe('stable-accept-key')
  })

  it('does not attach a CSRF token to GET reads', async () => {
    const fetchMock = stubFetch()

    await getTrigger('TRG-1')

    expect(headersOf(fetchMock)['X-CSRF-Token']).toBeUndefined()
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('GET')
  })
})
