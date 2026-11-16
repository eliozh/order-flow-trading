import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../status'
import {
  completeOnboardingStep,
  fetchOnboardingProgress,
  ONBOARDING_STEPS,
  type OnboardingProgressData,
} from '../onboarding'
import { SettingsApiError } from '../settings'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function progress(overrides: Partial<OnboardingProgressData> = {}): OnboardingProgressData {
  return {
    steps: ONBOARDING_STEPS.map((step) => ({ ...step, completed: false })),
    completed_steps: [],
    resumed_step: 1,
    all_complete: false,
    environment: 'paper',
    updated_at: null,
    updated_by: null,
    ...overrides,
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function ok(body: unknown): Response {
  return jsonResponse({ ok: true, data: body, error: null, meta })
}

function fetchMockReturning(response: Response | (() => Response)): ReturnType<typeof vi.fn> {
  return vi.fn(async () => (typeof response === 'function' ? response() : response))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchOnboardingProgress', () => {
  it('matches the canonical metadata of a representative server payload', () => {
    // Copied from a real `GET /api/v1/settings/onboarding` response; the server
    // title is canonical and the client table must not drift from it.
    const serverSteps = [
      {
        step: 1,
        id: 'environment',
        title: '环境与本地绑定',
        deferred: false,
        deferred_milestone: null,
      },
      { step: 2, id: 'llm', title: 'LLM providers', deferred: false, deferred_milestone: null },
      {
        step: 3,
        id: 'binance',
        title: 'Binance 密钥与权限',
        deferred: true,
        deferred_milestone: 5,
      },
      {
        step: 4,
        id: 'telegram',
        title: 'Telegram 只读通道',
        deferred: true,
        deferred_milestone: 6,
      },
      {
        step: 5,
        id: 'watchlist',
        title: 'Watchlist、周期与预算',
        deferred: false,
        deferred_milestone: null,
      },
    ]
    expect(
      serverSteps.map(({ step, id, title, deferred, deferred_milestone }) => ({
        step,
        id,
        title,
        deferred,
        deferred_milestone,
      })),
    ).toEqual(
      ONBOARDING_STEPS.map(({ step, id, title, deferred, deferred_milestone }) => ({
        step,
        id,
        title,
        deferred,
        deferred_milestone,
      })),
    )
  })

  it('accepts a valid progress payload and the canonical step table', async () => {
    const body = progress()
    vi.stubGlobal('fetch', fetchMockReturning(ok(body)))

    const envelope = await fetchOnboardingProgress()

    expect(envelope.data).toEqual(body)
    // The client table is the same canonical metadata the server returns.
    expect(
      envelope.data?.steps.map((step) => [step.step, step.id, step.deferred_milestone]),
    ).toEqual(ONBOARDING_STEPS.map((step) => [step.step, step.id, step.deferred_milestone]))
    expect(envelope.data?.steps.map((step) => step.title)).toEqual(
      ONBOARDING_STEPS.map((step) => step.title),
    )
  })

  it('rejects a null response body as a typed error', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(jsonResponse({ ok: false, data: null, error: null, meta })),
    )

    await expect(fetchOnboardingProgress()).rejects.toBeInstanceOf(SettingsApiError)
  })

  it('rejects a malformed response shape without rendering it', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(ok({ steps: 'nope' })))

    await expect(fetchOnboardingProgress()).rejects.toMatchObject({
      name: 'SettingsApiError',
      code: 'INVALID_RESPONSE',
    })
  })

  const invalid: [string, (data: OnboardingProgressData) => unknown][] = [
    [
      'a missing step',
      (data) => ({
        ...data,
        steps: data.steps.slice(0, 4),
      }),
    ],
    [
      'a duplicate step number',
      (data) => ({
        ...data,
        steps: data.steps.map((step, index) =>
          index === 1 ? { ...step, step: 1, id: 'environment' } : step,
        ),
      }),
    ],
    [
      'an id that disagrees with the step',
      (data) => ({
        ...data,
        steps: data.steps.map((step) => (step.step === 1 ? { ...step, id: 'watchlist' } : step)),
      }),
    ],
    [
      'a deferred flag that disagrees with the step',
      (data) => ({
        ...data,
        steps: data.steps.map((step) =>
          step.step === 3 ? { ...step, deferred: false, deferred_milestone: null } : step,
        ),
      }),
    ],
    [
      'a deferred step with a null milestone',
      (data) => ({
        ...data,
        steps: data.steps.map((step) =>
          step.step === 3 ? { ...step, deferred_milestone: null } : step,
        ),
      }),
    ],
    ['a completed deferred step', (data) => ({ ...data, completed_steps: [3] })],
    ['duplicate completed steps', (data) => ({ ...data, completed_steps: [1, 1] })],
    ['an out-of-range resume step', (data) => ({ ...data, resumed_step: 6 })],
    ['a non-paper environment', (data) => ({ ...data, environment: 'live' })],
  ]

  it.each(invalid)('rejects %s as INVALID_RESPONSE', async (_label, mutate) => {
    vi.stubGlobal('fetch', fetchMockReturning(ok(mutate(progress()))))

    await expect(fetchOnboardingProgress()).rejects.toMatchObject({
      name: 'SettingsApiError',
      code: 'INVALID_RESPONSE',
    })
  })

  it('surfaces a server refusal as a typed refusal', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        jsonResponse(
          {
            ok: false,
            data: null,
            error: {
              code: 'CONFIG_INVALID',
              message: 'persisted onboarding progress is unreadable',
              category: 'CONFIG',
              http_status: 500,
              retryable: false,
              reference_id: 'REQ-500',
              details: [],
            },
            meta,
          },
          500,
        ),
      ),
    )

    const error = await fetchOnboardingProgress().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(SettingsApiError)
    expect(error).toMatchObject({ code: 'CONFIG_INVALID', kind: 'refusal', httpStatus: 500 })
  })
})

describe('completeOnboardingStep', () => {
  it('sends the step and caller-supplied idempotency key', async () => {
    const fetchMock = fetchMockReturning(ok(progress({ completed_steps: [1], resumed_step: 2 })))
    vi.stubGlobal('fetch', fetchMock)

    await completeOnboardingStep(1, 'onboarding-key')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/settings/onboarding')
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(JSON.stringify({ step: 1 }))
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('onboarding-key')
  })
})
