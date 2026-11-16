import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiShapeError, fetchReadiness, fetchStatus, ReadinessError } from './status'
import { READ_TIMEOUT_MS } from './request-timeout'

const notReadyEnvelope = {
  ok: false,
  data: null,
  error: {
    code: 'DATA_UNAVAILABLE',
    message: 'Required data does not exist or cannot be fetched.',
    category: 'DATA',
    http_status: 503,
    retryable: true,
    reference_id: 'REQ-TEST',
    details: [],
  },
  meta: {
    request_id: 'REQ-TEST',
    generated_at: '2026-11-06T00:00:00.000Z',
    duration_ms: 1,
    provenance: 'NONE',
    data_quality: null,
    warnings: [],
    pagination: null,
    idempotent_replay: false,
  },
}

function stubFetch(body: unknown, status: number): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
  )
}

describe('fetchReadiness', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('turns a non-2xx readiness envelope into a typed ReadinessError', async () => {
    stubFetch(notReadyEnvelope, 503)

    const error = await fetchReadiness().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ReadinessError)
    expect((error as ReadinessError).code).toBe('DATA_UNAVAILABLE')
    expect((error as ReadinessError).message).toBe(
      'Required data does not exist or cannot be fetched.',
    )
  })

  it('rejects a JSON null body as a shape error, never a TypeError', async () => {
    stubFetch(null, 200)

    await expect(fetchReadiness()).rejects.toBeInstanceOf(ApiShapeError)
    await expect(fetchStatus()).rejects.toBeInstanceOf(ApiShapeError)
  })

  it('times out a wedged readiness read instead of loading forever', async () => {
    vi.useFakeTimers()
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_input: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
            })
          }),
      ),
    )

    const pending = fetchReadiness()
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'RequestTimeoutError',
      code: 'TIMEOUT',
    })
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS)
    await assertion
  })

  it('forwards a caller abort to the status request', async () => {
    const controller = new AbortController()
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_input: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
            })
          }),
      ),
    )

    const pending = fetchStatus(controller.signal)
    controller.abort()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })
})
