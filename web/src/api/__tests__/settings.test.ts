import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../status'
import {
  clearScanPeriodKey,
  fetchScanSettings,
  fetchSettingsSections,
  SCAN_SETTINGS_PATH,
  SettingsApiError,
  updateScanPeriod,
} from '../settings'

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

const scanData = {
  key: 'scan.period',
  value: '4h',
  source: 'default' as const,
  supported: ['4h', '8h'],
  next_run: '2026-11-06T12:00:00.000Z',
  timezone: 'UTC',
  updated_at: null,
  updated_by: null,
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

function fetchMockReturning(response: Response | (() => Response)): ReturnType<typeof vi.fn> {
  return vi.fn(async () => (typeof response === 'function' ? response() : response))
}

afterEach(() => {
  vi.unstubAllGlobals()
  clearScanPeriodKey()
  document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
})

describe('fetchScanSettings', () => {
  it('reads and validates the scan settings envelope', async () => {
    const fetchMock = fetchMockReturning(ok(scanData))
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchScanSettings()

    expect(envelope.data?.value).toBe('4h')
    expect(envelope.data?.source).toBe('default')
    expect(envelope.data?.supported).toEqual(['4h', '8h'])
    expect(fetchMock).toHaveBeenCalledWith(
      SCAN_SETTINGS_PATH,
      expect.objectContaining({ method: 'GET', credentials: 'same-origin' }),
    )
  })

  it('rejects a structurally invalid payload instead of trusting it', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(ok({ ...scanData, value: 4 })))

    await expect(fetchScanSettings()).rejects.toMatchObject({
      name: 'SettingsApiError',
      code: 'INVALID_RESPONSE',
    })
  })

  it('raises a typed error carrying the envelope code and field details', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        jsonResponse(
          {
            ok: false,
            data: null,
            error: {
              code: 'VALIDATION_FAILED',
              message: 'scan.period must be one of: 4h, 8h',
              category: 'VALIDATION',
              http_status: 422,
              retryable: false,
              reference_id: 'REQ-1',
              details: [{ field: 'period', code: 'unsupported', message: 'invalid period' }],
            },
            meta,
          },
          422,
        ),
      ),
    )

    await expect(fetchScanSettings()).rejects.toMatchObject({
      name: 'SettingsApiError',
      code: 'VALIDATION_FAILED',
      httpStatus: 422,
      kind: 'refusal',
      details: [{ field: 'period', code: 'unsupported', message: 'invalid period' }],
    })
  })
})

describe('updateScanPeriod', () => {
  it('sends the mutation with the CSRF and idempotency headers', async () => {
    document.cookie = 'oft_csrf=csrf-token; path=/'
    const fetchMock = fetchMockReturning(
      ok({ ...scanData, value: '8h', source: 'user', updated_by: 'web' }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await updateScanPeriod('8h', 'idem-key-1')

    expect(envelope.data?.value).toBe('8h')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe(SCAN_SETTINGS_PATH)
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(JSON.stringify({ period: '8h' }))
    expect(init.headers).toMatchObject({
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': 'idem-key-1',
      'X-CSRF-Token': 'csrf-token',
    })
  })

  it('surfaces a replay flag from the envelope', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(ok({ ...scanData, value: '8h' }, { idempotent_replay: true })),
    )

    const envelope = await updateScanPeriod('8h', 'idem-key-2')

    expect(envelope.meta.idempotent_replay).toBe(true)
  })

  it('classifies a 409 conflict as a definitive refusal for this payload', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        jsonResponse(
          {
            ok: false,
            data: null,
            error: {
              code: 'IDEMPOTENCY_CONFLICT',
              message: 'still running',
              category: 'CONFLICT',
              http_status: 409,
              retryable: false,
              reference_id: 'REQ-2',
              details: [],
            },
            meta,
          },
          409,
        ),
      ),
    )

    const error = await updateScanPeriod('8h', 'idem-key-3').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(SettingsApiError)
    expect((error as SettingsApiError).kind).toBe('refusal')
    expect((error as SettingsApiError).code).toBe('IDEMPOTENCY_CONFLICT')
  })

  it('treats an unlisted error code as an unknown outcome (safe default)', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        jsonResponse(
          {
            ok: false,
            data: null,
            error: {
              code: 'SOMETHING_NEW',
              message: 'a code this client does not know',
              category: 'INTERNAL',
              http_status: 500,
              retryable: false,
              reference_id: 'REQ-3',
              details: [],
            },
            meta,
          },
          500,
        ),
      ),
    )

    const error = await updateScanPeriod('8h', 'idem-key-4').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(SettingsApiError)
    expect((error as SettingsApiError).kind).toBe('unknown')
    expect((error as SettingsApiError).code).toBe('SOMETHING_NEW')
  })
})

describe('fetchSettingsSections', () => {
  it('reads the section summaries without values', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(
        ok({
          sections: [
            {
              section: 'scan',
              source: 'user',
              last_changed: '2026-11-06T10:00:00.000Z',
              keys: ['scan.period'],
            },
          ],
        }),
      ),
    )

    const envelope = await fetchSettingsSections()

    expect(envelope.data?.sections).toHaveLength(1)
    expect(envelope.data?.sections[0].source).toBe('user')
  })
})
