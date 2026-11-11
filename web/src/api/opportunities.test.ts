import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  fetchOpportunities,
  fetchOpportunity,
  fetchOpportunityTimeline,
  OpportunityApiError,
  type OpportunityEnvelope,
  type OpportunityListEnvelope,
} from './opportunities'

const meta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T00:00:00.000Z',
  duration_ms: 1,
  provenance: 'MIXED',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const emptyEnvelope: OpportunityListEnvelope = {
  ok: true,
  data: {
    items: [],
    total: 0,
    counts: { OPEN: 0, TRIGGERED: 0, EXPIRED: 0, INVALIDATED: 0 },
  },
  error: null,
  meta,
}

function stubFetch(body: unknown, status = 200): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('fetchOpportunities', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists the ledger with a bounded default page and no state filter', async () => {
    const fetchMock = stubFetch(emptyEnvelope)

    await fetchOpportunities()

    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/v1/opportunities?')
    expect(url).toContain('limit=50')
    expect(url).not.toContain('state=')
  })

  it('threads the abort signal into fetch', async () => {
    const fetchMock = stubFetch(emptyEnvelope)
    const controller = new AbortController()

    await fetchOpportunities({ state: 'OPEN', limit: 50 }, controller.signal)

    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal })
  })

  it('encodes the state filter and offset', async () => {
    const fetchMock = stubFetch(emptyEnvelope)

    await fetchOpportunities({ state: 'TRIGGERED', limit: 25, offset: 10 })

    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('state=TRIGGERED')
    expect(url).toContain('limit=25')
    expect(url).toContain('offset=10')
  })

  it('turns an error envelope into a typed OpportunityApiError', async () => {
    stubFetch(
      {
        ok: false,
        data: null,
        error: {
          code: 'DATA_UNAVAILABLE',
          message: 'ledger unavailable',
          category: 'DATA',
          http_status: 503,
          retryable: true,
          reference_id: 'REQ-TEST',
          details: [],
        },
        meta,
      },
      503,
    )

    const error = await fetchOpportunities().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(OpportunityApiError)
    expect((error as OpportunityApiError).code).toBe('DATA_UNAVAILABLE')
  })
})

describe('fetchOpportunity', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('requests one opportunity by encoded id', async () => {
    const fetchMock = stubFetch({
      ok: true,
      data: null,
      error: null,
      meta,
    } as OpportunityEnvelope)
    const path = 'OPP/../1'

    await fetchOpportunity(path).catch(() => undefined)

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `/api/v1/opportunities/${encodeURIComponent(path)}`,
    )
  })
})

describe('fetchOpportunityTimeline', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns the state-history envelope', async () => {
    stubFetch({
      ok: true,
      data: {
        opportunity_id: 'OPP-1',
        items: [
          {
            from_state: null,
            to_state: 'OPEN',
            at: '2026-11-06T00:00:00.000Z',
            reason_code: 'CREATED',
            note: 'created by scan',
          },
        ],
      },
      error: null,
      meta,
    })

    const envelope = await fetchOpportunityTimeline('OPP-1')

    expect(envelope.data?.items[0].to_state).toBe('OPEN')
  })
})
