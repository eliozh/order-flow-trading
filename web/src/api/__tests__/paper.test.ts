import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { activateKillSwitch, deactivateKillSwitch, fetchKillSwitch } from '../paper'

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

describe('paper API mutations', () => {
  beforeEach(() => {
    document.cookie = 'oft_csrf=csrf-test-token'
  })

  afterEach(() => {
    document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    vi.unstubAllGlobals()
  })

  it('sends the CSRF header when activating and resuming the kill switch', async () => {
    const fetchMock = stubFetch()

    await activateKillSwitch()
    await deactivateKillSwitch('RESUME')

    expect(headersOf(fetchMock, 0)['X-CSRF-Token']).toBe('csrf-test-token')
    expect(headersOf(fetchMock, 1)['X-CSRF-Token']).toBe('csrf-test-token')
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST')
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe('DELETE')
  })

  it('does not attach a CSRF token to kill-switch status reads', async () => {
    const fetchMock = stubFetch()

    await fetchKillSwitch()

    expect(headersOf(fetchMock)['X-CSRF-Token']).toBeUndefined()
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('GET')
  })
})
