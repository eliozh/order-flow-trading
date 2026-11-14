import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchWithTimeout, READ_TIMEOUT_MS, RequestTimeoutError } from '../request-timeout'

function neverResolvingFetch(): ReturnType<typeof vi.fn> {
  return vi.fn(
    (_input: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
        })
      }),
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('fetchWithTimeout', () => {
  it('throws a typed TIMEOUT error when the deadline passes', async () => {
    vi.useFakeTimers()
    const fetchMock = neverResolvingFetch()
    vi.stubGlobal('fetch', fetchMock)

    const pending = fetchWithTimeout('/api/v1/aux', {})
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'RequestTimeoutError',
      code: 'TIMEOUT',
    })
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS - 1)
    // Still pending before the deadline.
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1)
    await assertion
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(new RequestTimeoutError().code).toBe('TIMEOUT')
  })

  it('honours a per-call deadline for longer auxiliary mutations', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', neverResolvingFetch())

    const pending = fetchWithTimeout('/api/v1/aux', {}, undefined, 120_000)
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError)
    await vi.advanceTimersByTimeAsync(119_999)
    await vi.advanceTimersByTimeAsync(1)
    await assertion
  })

  it('does not translate a caller-driven abort into a timeout', async () => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', neverResolvingFetch())

    const pending = fetchWithTimeout('/api/v1/aux', {}, controller.signal)
    const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    controller.abort()
    await assertion
  })
})
