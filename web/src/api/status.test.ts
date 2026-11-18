import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ApiShapeError,
  fetchReadiness,
  fetchStatus,
  isStatusData,
  isTelegramChatStatusData,
  isTelegramReceiptData,
  isTelegramStatusData,
  ReadinessError,
} from './status'
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

const validMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T00:00:00.000Z',
  duration_ms: 3,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const scanRecord = {
  scan_id: 'SCAN-1',
  symbol: 'BTCUSDT',
  period: '4h',
  status: 'COMPLETED',
  trigger_source: 'scheduler',
  metrics_only: false,
  fixture_path: '/tmp/oft/fixture',
  started_at: '2026-11-06T00:00:00.000Z',
  finished_at: '2026-11-06T00:01:00.000Z',
  window_start: '2026-11-05T20:00:00.000Z',
  window_end: '2026-11-06T00:00:00.000Z',
  trade_count: 10,
  buy_volume: '5.0',
  sell_volume: '4.0',
  delta: '1.0',
  cvd: '1.0',
  data_quality: 'COMPLETE',
  manifest_path: '/tmp/oft/manifest',
  parquet_rows: 10,
  created_at: '2026-11-06T00:01:00.000Z',
}

function readinessBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ok: true,
    data: {
      status: 'ready',
      version: '0.1.0',
      env: 'paper',
      data_dir: '/tmp/oft',
      checked_at: '2026-11-06T00:00:00.000Z',
      checks: [{ name: 'config', status: 'ok', detail: 'settings loaded' }],
      latest_scan: null,
      ...overrides,
    },
    error: null,
    meta: validMeta,
  }
}

function statusBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ok: true,
    data: {
      status: 'ok',
      env: 'paper',
      version: '0.1.0',
      host: '127.0.0.1',
      port: 7611,
      data_dir: '/tmp/oft',
      log_level: 'INFO',
      generated_at: '2026-11-06T00:00:00.000Z',
      universe: {
        source: 'fixture',
        as_of: '2026-11-06',
        count: 1,
        tradifi_count: 0,
        symbols: [
          {
            symbol: 'BTCUSDT',
            base_asset: 'BTC',
            quote_asset: 'USDT',
            contract_type: 'PERPETUAL',
            tradifi: false,
          },
        ],
      },
      schedule: null,
      ...overrides,
    },
    error: null,
    meta: validMeta,
  }
}

const validTelegram = {
  configured: true,
  chat_count: 2,
  allowed_user_count: 1,
  poller_state: 'RUNNING',
  last_poll_at: '2026-11-17T09:30:00.000Z',
  last_send_at: null,
  failing_receipts: 0,
  receipts: { queued: 0, sending: 0, sent: 1, failed: 0 },
  per_chat: [{ chat_id: 1001, queued: 0, sending: 0, sent: 1, failed: 0, last_error_code: null }],
  recent_receipts: [
    {
      event_key: 'scan:1:report',
      kind: 'scan_report',
      chat_id: 1001,
      state: 'sent',
      attempts: 0,
      message_id: 900,
      last_error_code: null,
      created_at: '2026-11-17T09:30:00.000Z',
      sent_at: '2026-11-17T09:30:00.000Z',
    },
  ],
  overflow_code: null,
  last_error_code: null,
  daemon_state: 'UP',
}

function statusData(): Record<string, unknown> {
  const body = statusBody()
  return body.data as Record<string, unknown>
}

describe('Telegram status shape guards', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('accepts a valid Telegram document on the status contract', () => {
    expect(isTelegramStatusData(validTelegram)).toBe(true)
    expect(isStatusData({ ...statusData(), telegram: validTelegram })).toBe(true)
    expect(isTelegramChatStatusData(validTelegram.per_chat[0])).toBe(true)
    expect(isTelegramReceiptData(validTelegram.recent_receipts[0])).toBe(true)
  })

  it('accepts an absent or null telegram document for backward compatibility', () => {
    expect(isStatusData(statusData())).toBe(true)
    expect(isStatusData({ ...statusData(), telegram: null })).toBe(true)
  })

  it('rejects a malformed per_chat entry', () => {
    const malformed = {
      ...validTelegram,
      per_chat: [{ chat_id: '1001', queued: 0, sending: 0, sent: 1, failed: 0 }],
    }
    expect(isTelegramStatusData(malformed)).toBe(false)
    expect(isStatusData({ ...statusData(), telegram: malformed })).toBe(false)
  })

  it('rejects non-numeric receipt totals', () => {
    const malformed = {
      ...validTelegram,
      receipts: { queued: '0', sending: 0, sent: 1, failed: 0 },
    }
    expect(isTelegramStatusData(malformed)).toBe(false)
  })

  it('rejects a malformed recent receipt entry', () => {
    const malformed = {
      ...validTelegram,
      recent_receipts: [{ event_key: 'scan:1:report', chat_id: 1001 }],
    }
    expect(isTelegramStatusData(malformed)).toBe(false)
    expect(isStatusData({ ...statusData(), telegram: malformed })).toBe(false)
  })

  it('rejects a malformed Telegram document through the fetch boundary', async () => {
    stubFetch(statusBody({ telegram: { configured: true } }), 200)

    await expect(fetchStatus()).rejects.toBeInstanceOf(ApiShapeError)
  })

  it('constructs the Telegram document through the fetch boundary', async () => {
    stubFetch(statusBody({ telegram: validTelegram }), 200)

    const envelope = await fetchStatus()

    expect(envelope.data?.telegram?.per_chat[0].chat_id).toBe(1001)
    expect(envelope.data?.telegram?.recent_receipts).toHaveLength(1)
  })
})

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

  it('exposes the named per-check details from a real 503 failure envelope', async () => {
    stubFetch(
      {
        ...notReadyEnvelope,
        error: {
          ...notReadyEnvelope.error,
          details: [
            { field: 'storage.sqlite', code: 'degraded', message: 'integrity check pending' },
            { field: 'clock', code: 'offline', message: 'clock unavailable' },
          ],
        },
      },
      503,
    )

    const error = await fetchReadiness().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ReadinessError)
    expect((error as ReadinessError).details).toEqual([
      { field: 'storage.sqlite', code: 'degraded', message: 'integrity check pending' },
      { field: 'clock', code: 'offline', message: 'clock unavailable' },
    ])
  })

  it('handles malformed or absent details without trusting the body', async () => {
    for (const details of ['not-a-list', undefined, [null, 3, { field: 1 }]]) {
      stubFetch(
        {
          ok: false,
          data: null,
          error: { code: 'DATA_UNAVAILABLE', message: 'not ready', details },
          meta: {},
        },
        503,
      )

      const error = await fetchReadiness().catch((caught: unknown) => caught)
      expect(error).toBeInstanceOf(ReadinessError)
      expect((error as ReadinessError).details).toEqual([])
      vi.unstubAllGlobals()
    }
  })

  it('accepts a full 2xx readiness envelope and rejects a partial latest_scan', async () => {
    stubFetch(readinessBody({ latest_scan: scanRecord }), 200)
    const envelope = await fetchReadiness()
    expect(envelope.data?.latest_scan?.delta).toBe('1.0')
    expect(envelope.meta.request_id).toBe('REQ-TEST')

    vi.unstubAllGlobals()
    stubFetch(readinessBody({ latest_scan: { scan_id: 'SCAN-1' } }), 200)
    await expect(fetchReadiness()).rejects.toBeInstanceOf(ApiShapeError)
  })

  it('rejects a partial universe symbol item', async () => {
    stubFetch(
      statusBody({
        universe: {
          source: 'fixture',
          as_of: '2026-11-06',
          count: 1,
          tradifi_count: 0,
          symbols: [{ symbol: 'BTCUSDT' }],
        },
      }),
      200,
    )

    await expect(fetchStatus()).rejects.toBeInstanceOf(ApiShapeError)
  })

  it('rejects a missing or malformed meta instead of casting it', async () => {
    const withoutMeta = statusBody()
    delete withoutMeta.meta
    stubFetch(withoutMeta, 200)
    await expect(fetchStatus()).rejects.toBeInstanceOf(ApiShapeError)

    vi.unstubAllGlobals()
    stubFetch({ ...statusBody(), meta: { request_id: 'REQ-TEST', duration_ms: 1 } }, 200)
    await expect(fetchStatus()).rejects.toBeInstanceOf(ApiShapeError)
  })

  it('constructs a fully typed status envelope on success', async () => {
    stubFetch(statusBody(), 200)

    const envelope = await fetchStatus()

    expect(envelope.ok).toBe(true)
    expect(envelope.error).toBeNull()
    expect(envelope.data?.host).toBe('127.0.0.1')
    expect(envelope.meta.idempotent_replay).toBe(false)
  })
})
