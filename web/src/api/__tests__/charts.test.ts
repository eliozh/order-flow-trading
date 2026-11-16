import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChartApiError, fetchLatestReportForSymbol } from '../charts'
import type { ReportSummary } from '../reports'
import type { ApiMeta } from '../status'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T04:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const summary: ReportSummary = {
  report_id: 'RPT-BTCUSDT-1',
  scan_id: 'SCAN-1',
  symbol: 'BTCUSDT',
  timeframe: '4h',
  generated_at: '2026-11-06T04:00:00.000Z',
  data_quality: 'PARTIAL',
  llm_decision_id: null,
  narrative_ref: null,
  llm_stage: 'SKIPPED',
  llm_tokens: 0,
  cost_usd: '0',
  cost_unpriced: false,
  llm: { configured: false, validation_status: null, opportunity_ids: [] },
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchLatestReportForSymbol', () => {
  it('requests the newest report with a bounded limit and same-origin credentials', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        ok: true,
        data: { items: [summary], total: 1, scan_id: null },
        error: null,
        meta,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const report = await fetchLatestReportForSymbol('BTCUSDT')

    expect(report).toEqual(summary)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/reports?symbol=BTCUSDT&limit=1')
    // Fetch defaults to GET when no method is supplied.
    expect(init.method ?? 'GET').toBe('GET')
    expect(init.credentials).toBe('same-origin')
  })

  it('returns null when no report has been published', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          ok: true,
          data: { items: [], total: 0, scan_id: null },
          error: null,
          meta,
        }),
      ),
    )

    await expect(fetchLatestReportForSymbol('BTCUSDT')).resolves.toBeNull()
  })

  it('throws a ChartApiError carrying the envelope message and code', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            ok: false,
            data: null,
            error: { code: 'DATA_UNAVAILABLE', message: 'reports store offline' },
            meta,
          },
          503,
        ),
      ),
    )

    const failure = await fetchLatestReportForSymbol('BTCUSDT').catch((cause: unknown) => cause)

    expect(failure).toBeInstanceOf(ChartApiError)
    expect(failure).toMatchObject({
      name: 'ChartApiError',
      code: 'DATA_UNAVAILABLE',
      message: 'reports store offline',
    })
  })

  it('throws when a 2xx body is not valid JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{"ok":true,', {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    )

    const failure = await fetchLatestReportForSymbol('BTCUSDT').catch((cause: unknown) => cause)

    expect(failure).toBeInstanceOf(ChartApiError)
    expect(failure).toMatchObject({ message: 'Chart request failed with HTTP 200' })
  })
})
