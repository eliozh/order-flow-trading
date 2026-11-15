import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../status'
import {
  activatePromptVersion,
  clearPromptActivationKey,
  clearPromptRestoreKey,
  clearPromptVersionKey,
  createPromptVersion,
  fetchPromptDetail,
  fetchPromptDiff,
  fetchPromptHistory,
  fetchPromptSettings,
  MAX_PROMPT_BODY_BYTES,
  PROMPT_SETTINGS_PATH,
  PromptApiError,
  restorePromptDefault,
} from '../prompts'

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

function versionData(overrides: Record<string, unknown> = {}) {
  return {
    prompt_id: 'analysis',
    version: 1,
    status: 'ACTIVE',
    is_active: true,
    body: 'Symbol: {{symbol}}',
    body_hash: 'a'.repeat(64),
    required_variables: ['symbol'],
    author: 'system',
    reason: 'built-in default',
    is_default: true,
    created_at: '2026-11-08T12:00:00.000Z',
    system_prompt_id: 'analysis:system',
    system_prompt_version: 1,
    system_prompt_hash: 'b'.repeat(64),
    knowledge_artifact_hash: null,
    ...overrides,
  }
}

function settingsData() {
  return {
    items: [
      {
        prompt_id: 'analysis',
        role: 'ANALYSIS',
        active_version: 1,
        author: 'system',
        updated_at: '2026-11-08T12:00:00.000Z',
        version_count: 1,
        latest_version: 1,
        required_variables: ['symbol'],
        is_builtin_default: true,
      },
    ],
    total: 1,
  }
}

function detailData() {
  return {
    prompt_id: 'analysis',
    role: 'ANALYSIS',
    kind: 'USER',
    active_version: 1,
    created_at: '2026-11-08T12:00:00.000Z',
    updated_at: '2026-11-08T12:00:00.000Z',
    versions: [versionData()],
  }
}

function historyData() {
  return { prompt_id: 'analysis', items: [versionData()], total: 1 }
}

function diffData() {
  return { prompt_id: 'analysis', from_version: 1, to_version: 2, diff: '- a\n+ b' }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function ok(body: unknown, overrides: Partial<ApiMeta> = {}): Response {
  return jsonResponse({ ok: true, data: body, error: null, meta: { ...meta, ...overrides } })
}

function failure(code: string, status: number, details: unknown[] = []): Response {
  return jsonResponse(
    {
      ok: false,
      data: null,
      error: {
        code,
        message: `refused with ${code}`,
        category: 'VALIDATION',
        http_status: status,
        retryable: false,
        reference_id: 'REQ-ERR',
        details,
      },
      meta,
    },
    status,
  )
}

function fetchMockReturning(response: Response | (() => Response)): ReturnType<typeof vi.fn> {
  return vi.fn(async () => (typeof response === 'function' ? response() : response))
}

function requestInit(fetchMock: ReturnType<typeof vi.fn>, call = 0): RequestInit {
  return fetchMock.mock.calls[call][1] as RequestInit
}

function requestHeaders(fetchMock: ReturnType<typeof vi.fn>, call = 0): Record<string, string> {
  return (requestInit(fetchMock, call).headers ?? {}) as Record<string, string>
}

afterEach(() => {
  vi.unstubAllGlobals()
  clearPromptVersionKey('analysis')
  clearPromptActivationKey('analysis')
  clearPromptRestoreKey('analysis')
  document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
})

describe('prompt reads', () => {
  it('reads the settings aggregate with no CSRF header', async () => {
    const fetchMock = fetchMockReturning(ok(settingsData()))
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchPromptSettings()

    expect(envelope.data?.items[0]?.prompt_id).toBe('analysis')
    expect(fetchMock.mock.calls[0][0]).toBe(PROMPT_SETTINGS_PATH)
    expect(requestInit(fetchMock).method).toBe('GET')
    expect(requestHeaders(fetchMock)).toMatchObject({ Accept: 'application/json' })
    expect(requestHeaders(fetchMock)['X-CSRF-Token']).toBeUndefined()
  })

  it('reads one detail by prompt id', async () => {
    const fetchMock = fetchMockReturning(ok(detailData()))
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchPromptDetail('analysis')

    expect(envelope.data?.active_version).toBe(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/prompts/analysis')
    expect(requestInit(fetchMock).method).toBe('GET')
  })

  it('reads a history page', async () => {
    const fetchMock = fetchMockReturning(ok(historyData()))
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchPromptHistory('analysis')

    expect(envelope.data?.total).toBe(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/prompts/analysis/history')
  })

  it('reads a bounded version diff with both version query parameters', async () => {
    const fetchMock = fetchMockReturning(ok(diffData()))
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchPromptDiff('analysis', 1, 2)

    expect(envelope.data?.diff).toContain('+ b')
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/prompts/analysis/diff?from_version=1&to_version=2',
    )
    expect(requestInit(fetchMock).method).toBe('GET')
  })

  it('rejects a structurally invalid payload instead of trusting it', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(ok({ items: 'not-an-array', total: 0 })))

    await expect(fetchPromptSettings()).rejects.toMatchObject({
      name: 'PromptApiError',
      code: 'INVALID_RESPONSE',
    })
  })

  it('rejects an invalid detail shape', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(ok({ ...detailData(), versions: [{ prompt_id: 'analysis' }] })),
    )

    await expect(fetchPromptDetail('analysis')).rejects.toMatchObject({
      code: 'INVALID_RESPONSE',
    })
  })
})

describe('prompt mutations', () => {
  it('creates a version with CSRF, Idempotency-Key, and the defaulted body', async () => {
    document.cookie = 'oft_csrf=csrf-token; path=/'
    const fetchMock = fetchMockReturning(ok(versionData({ version: 2, status: 'DRAFT' })))
    vi.stubGlobal('fetch', fetchMock)

    await createPromptVersion('analysis', { body: 'new body' }, 'create-key')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/prompts/analysis/versions')
    expect(requestInit(fetchMock).method).toBe('POST')
    expect(requestHeaders(fetchMock)).toMatchObject({
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': 'create-key',
      'X-CSRF-Token': 'csrf-token',
    })
    expect(requestInit(fetchMock).body).toBe(
      JSON.stringify({ body: 'new body', author: 'web', reason: '', activate: false }),
    )
  })

  it('passes an explicit author, reason, and activate flag through', async () => {
    const fetchMock = fetchMockReturning(ok(versionData({ version: 2, status: 'ACTIVE' })))
    vi.stubGlobal('fetch', fetchMock)

    await createPromptVersion(
      'analysis',
      { body: 'new body', author: 'operator', reason: 'wording', activate: true },
      'create-key',
    )

    expect(requestInit(fetchMock).body).toBe(
      JSON.stringify({
        body: 'new body',
        author: 'operator',
        reason: 'wording',
        activate: true,
      }),
    )
  })

  it('activates a version with the idempotency key and supplied actor/reason', async () => {
    document.cookie = 'oft_csrf=csrf-token; path=/'
    const fetchMock = fetchMockReturning(ok(versionData({ version: 3, status: 'ACTIVE' })))
    vi.stubGlobal('fetch', fetchMock)

    await activatePromptVersion('analysis', 3, 'activate-key', {
      actor: 'operator',
      reason: 'approved',
    })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/prompts/analysis/activate')
    expect(requestInit(fetchMock).method).toBe('POST')
    expect(requestHeaders(fetchMock)['Idempotency-Key']).toBe('activate-key')
    expect(requestHeaders(fetchMock)['X-CSRF-Token']).toBe('csrf-token')
    expect(requestInit(fetchMock).body).toBe(
      JSON.stringify({ version: 3, actor: 'operator', reason: 'approved' }),
    )
  })

  it('restores the default as a new version with the defaulted author/reason', async () => {
    const fetchMock = fetchMockReturning(
      ok(versionData({ version: 4, status: 'DRAFT', is_active: false, is_default: true })),
    )
    vi.stubGlobal('fetch', fetchMock)

    await restorePromptDefault('analysis', 'restore-key')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/prompts/analysis/restore')
    expect(requestInit(fetchMock).method).toBe('POST')
    expect(requestHeaders(fetchMock)['Idempotency-Key']).toBe('restore-key')
    expect(requestInit(fetchMock).body).toBe(
      JSON.stringify({ author: 'web', reason: 'restore default' }),
    )
  })
})

describe('prompt error mapping', () => {
  it('classifies PROMPT_VALIDATION_FAILED as a definitive refusal', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(failure('PROMPT_VALIDATION_FAILED', 422)))

    const error = await createPromptVersion('analysis', { body: 'x' }, 'k').catch(
      (thrown: unknown) => thrown,
    )

    expect(error).toBeInstanceOf(PromptApiError)
    expect((error as PromptApiError).kind).toBe('refusal')
    expect((error as PromptApiError).code).toBe('PROMPT_VALIDATION_FAILED')
  })

  it('classifies a FastAPI VALIDATION_FAILED 422 as a definitive refusal', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(failure('VALIDATION_FAILED', 422)))

    const error = await createPromptVersion('analysis', { body: 'x' }, 'k').catch(
      (thrown: unknown) => thrown,
    )

    expect(error).toBeInstanceOf(PromptApiError)
    expect((error as PromptApiError).kind).toBe('refusal')
  })

  it('keeps an unlisted code as an unknown outcome', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(failure('SOMETHING_NEW', 500)))

    const error = await createPromptVersion('analysis', { body: 'x' }, 'k').catch(
      (thrown: unknown) => thrown,
    )

    expect(error).toBeInstanceOf(PromptApiError)
    expect((error as PromptApiError).kind).toBe('unknown')
    expect((error as PromptApiError).code).toBe('SOMETHING_NEW')
  })

  it('propagates a transport failure without inventing a refusal', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network down')
      }),
    )

    const error = await createPromptVersion('analysis', { body: 'x' }, 'k').catch(
      (thrown: unknown) => thrown,
    )

    expect(error).toBeInstanceOf(TypeError)
    expect(error).not.toBeInstanceOf(PromptApiError)
  })

  it('extracts details for the caller to surface', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        failure('VALIDATION_FAILED', 422, [
          { field: 'author', code: 'too_long', message: 'author is too long' },
        ]),
      ),
    )

    const error = await createPromptVersion('analysis', { body: 'x' }, 'k').catch(
      (thrown: unknown) => thrown,
    )

    expect((error as PromptApiError).details).toEqual([
      { field: 'author', code: 'too_long', message: 'author is too long' },
    ])
  })
})

describe('prompt body budget', () => {
  it('exposes the 64 KiB limit', () => {
    expect(MAX_PROMPT_BODY_BYTES).toBe(64 * 1024)
  })
})
