import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchReadiness, ReadinessError } from './status'

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
})
