import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../status'
import { SettingsApiError } from '../settings'
import {
  deleteTelegramToken,
  fetchTelegramSettings,
  type TelegramReceipt,
  type TelegramSettingsData,
  sendTelegramTest,
  setTelegramToken,
  TELEGRAM_SETTINGS_PATH,
  TELEGRAM_TEST_PATH,
  TELEGRAM_TOKEN_PATH,
  telegramTestFailureReason,
  updateTelegramSettings,
} from '../telegram'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-18T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const SENTINEL = '123456789:AAFakeSentinelToken_abcdefghijklmnopqrstuv'

function settingsData(overrides: Partial<TelegramSettingsData> = {}): TelegramSettingsData {
  return {
    token: {
      configured: false,
      masked: null,
      last4: null,
      source: 'unset',
      updated_at: null,
    },
    chat_ids: [],
    allowed_user_ids: [],
    toggles: {
      scan_report: true,
      trigger: true,
      entry_recheck: true,
      position: true,
      risk: true,
      budget: true,
    },
    source: 'default',
    updated_at: null,
    updated_by: null,
    receipts: [],
    receipt_total: 0,
    latest_test_receipt: null,
    ...overrides,
  }
}

function receipt(): TelegramReceipt {
  return {
    event_key: 'test:1001:nonce',
    kind: 'test',
    chat_id: 1001,
    reply_to_message_id: null,
    state: 'queued',
    attempts: 0,
    message_id: null,
    last_error_code: null,
    next_attempt_at: '2026-11-18T10:00:00.000Z',
    created_at: '2026-11-18T10:00:00.000Z',
    sent_at: null,
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

function okToken(): Response {
  return ok({
    configured: true,
    masked: '****stuv',
    last4: 'stuv',
    source: 'stored',
    updated_at: '2026-11-18T10:00:00.000Z',
  })
}

function fetchMockReturning(response: Response | (() => Response)): ReturnType<typeof vi.fn> {
  return vi.fn(async () => (typeof response === 'function' ? response() : response))
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
})

describe('fetchTelegramSettings', () => {
  it('reads the aggregate with the canonical GET path and Accept header', async () => {
    const body = settingsData({ chat_ids: [1001], allowed_user_ids: [2002] })
    const fetchMock = fetchMockReturning(ok(body))
    vi.stubGlobal('fetch', fetchMock)

    const envelope = await fetchTelegramSettings()

    expect(envelope.data).toEqual(body)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(TELEGRAM_SETTINGS_PATH)
    expect(init.method).toBe('GET')
    expect((init.headers as Record<string, string>).Accept).toBe('application/json')
    expect(init.credentials).toBe('same-origin')
  })

  it('rejects an envelope whose data is not the aggregate shape', async () => {
    vi.stubGlobal('fetch', fetchMockReturning(ok({ ...settingsData(), toggles: { risk: true } })))

    await expect(fetchTelegramSettings()).rejects.toMatchObject({
      name: 'SettingsApiError',
      code: 'INVALID_RESPONSE',
    })
  })

  it('rejects a null data body as a typed failure', async () => {
    vi.stubGlobal(
      'fetch',
      fetchMockReturning(jsonResponse({ ok: false, data: null, error: null, meta })),
    )

    await expect(fetchTelegramSettings()).rejects.toBeInstanceOf(SettingsApiError)
  })
})

describe('updateTelegramSettings', () => {
  it('sends a PUT with the patch body, idempotency key, and CSRF header', async () => {
    document.cookie = 'oft_csrf=csrf-token; path=/'
    const fetchMock = fetchMockReturning(ok(settingsData({ chat_ids: [1001] })))
    vi.stubGlobal('fetch', fetchMock)

    await updateTelegramSettings({ chat_ids: [1001] }, 'settings-key')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(TELEGRAM_SETTINGS_PATH)
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(JSON.stringify({ chat_ids: [1001] }))
    const headers = init.headers as Record<string, string>
    expect(headers['Idempotency-Key']).toBe('settings-key')
    expect(headers['X-CSRF-Token']).toBe('csrf-token')
  })
})

describe('token lifecycle', () => {
  it('stores the token write-only through a PUT body', async () => {
    const fetchMock = fetchMockReturning(okToken())
    vi.stubGlobal('fetch', fetchMock)

    await setTelegramToken(SENTINEL, 'token-key')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(TELEGRAM_TOKEN_PATH)
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(JSON.stringify({ bot_token: SENTINEL }))
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('token-key')
  })

  it('deletes the stored token through a DELETE without a value body', async () => {
    document.cookie = 'oft_csrf=csrf-token; path=/'
    const fetchMock = fetchMockReturning(okToken())
    vi.stubGlobal('fetch', fetchMock)

    await deleteTelegramToken('delete-key')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(TELEGRAM_TOKEN_PATH)
    expect(init.method).toBe('DELETE')
    expect(init.body).toBeUndefined()
    const headers = init.headers as Record<string, string>
    expect(headers['Idempotency-Key']).toBe('delete-key')
    expect(headers['X-CSRF-Token']).toBe('csrf-token')
  })
})

describe('sendTelegramTest', () => {
  it('posts an empty body without a chat and a chat_id when supplied', async () => {
    document.cookie = 'oft_csrf=csrf-token; path=/'
    const fetchMock = fetchMockReturning(() => ok(receipt()))
    vi.stubGlobal('fetch', fetchMock)

    await sendTelegramTest('test-key-1')
    await sendTelegramTest('test-key-2', 1001)

    const [firstUrl, first] = fetchMock.mock.calls[0] as [string, RequestInit]
    const [secondUrl, second] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(firstUrl).toBe(TELEGRAM_TEST_PATH)
    expect(secondUrl).toBe(TELEGRAM_TEST_PATH)
    expect(first.method).toBe('POST')
    expect(first.body).toBe(JSON.stringify({}))
    expect(second.body).toBe(JSON.stringify({ chat_id: 1001 }))
    expect((second.headers as Record<string, string>)['Idempotency-Key']).toBe('test-key-2')
    expect((first.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-token')
    expect((second.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-token')
  })
})

describe('telegramTestFailureReason', () => {
  const cases: [string, string][] = [
    ['TELEGRAM_TOKEN_INVALID', '401 Unauthorized'],
    ['TELEGRAM_NOT_CONFIGURED', '未配置 token 或没有可用 chat。'],
    ['TELEGRAM_DELIVERY_FAILED', '投递失败'],
    ['RATE_LIMITED', '触发 Telegram 限流'],
  ]

  it.each(cases)('maps %s to its stable copy', (code, expected) => {
    expect(telegramTestFailureReason({ code }, 'fallback')).toContain(expected)
  })

  it('falls back to the message for an unmapped or absent code', () => {
    expect(telegramTestFailureReason(null, 'network error')).toBe('network error')
    expect(telegramTestFailureReason({ code: 'SOMETHING_ELSE' }, 'server said no')).toBe(
      'server said no',
    )
  })
})
