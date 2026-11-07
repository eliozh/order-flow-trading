import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta, ScanData, ScanEnvelope, ScanListEnvelope } from '../../api/scans'
import { SCAN_POLL_INTERVAL_MS, ScanDetailPage } from '../scan-detail'
import { ScansPage } from '../scans'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-07T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function scan(overrides: Partial<ScanData> = {}): ScanData {
  return {
    scan_id: 'SCAN-20261107-120000-ABCD1234',
    symbol: 'BTCUSDT',
    period: '4h',
    status: 'DONE',
    trigger_source: 'MANUAL',
    metrics_only: false,
    fixture_path: '',
    started_at: '2026-11-07T12:00:00.000Z',
    finished_at: '2026-11-07T12:00:02.000Z',
    window_start: '2026-10-01T00:00:00.000Z',
    window_end: '2026-10-08T00:00:00.000Z',
    trade_count: 100,
    buy_volume: '10',
    sell_volume: '9',
    delta: '1',
    cvd: '1',
    data_quality: 'COMPLETE',
    manifest_path: 'part.parquet',
    parquet_rows: 100,
    created_at: '2026-11-07T12:00:02.000Z',
    report_id: 'RPT-BTCUSDT-20261107-120002-ABCD1234',
    source_mode: 'BACKFILL',
    metrics_manifest_json: '[]',
    batch_id: 'SCAN-20261107-120000-ABCD1234',
    llm_configured: true,
    llm_status: 'VALID',
    llm_tokens: 12480,
    cost_usd: '0.842',
    cost_unpriced: false,
    ...overrides,
  }
}

function listEnvelope(items: ScanData[]): ScanListEnvelope {
  return { ok: true, data: { items, total: items.length }, error: null, meta }
}

function detailEnvelope(data: ScanData): ScanEnvelope {
  return { ok: true, data, error: null, meta }
}

describe('ScansPage', () => {
  afterEach(() => {
    vi.useRealTimers()
  })
  it('shows the LLM stage, cost, and a report link for each scan', async () => {
    render(<ScansPage fetchScansFn={async () => listEnvelope([scan()])} />)

    expect(await screen.findByRole('table', { name: /Scan history/i })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /LLM/i })).toBeInTheDocument()
    expect(screen.getByText('VALID')).toBeInTheDocument()
    expect(screen.getByText('$0.842')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: scan().report_id as string })).toHaveAttribute(
      'href',
      `/reports/${scan().report_id}`,
    )
  })

  it('renders DEGRADED and FAILED states without inventing a percentage', async () => {
    render(
      <ScansPage
        fetchScansFn={async () =>
          listEnvelope([
            scan({
              scan_id: 'SCAN-DEGRADED',
              symbol: 'ETHUSDT',
              status: 'PARTIAL',
              llm_status: 'REJECTED',
            }),
            scan({
              scan_id: 'SCAN-FAILED',
              symbol: 'FTTUSDT',
              status: 'FAILED',
              llm_status: null,
              report_id: null,
              data_quality: 'MISSING',
            }),
          ])
        }
      />,
    )

    expect(await screen.findByText('DEGRADED')).toBeInTheDocument()
    expect(screen.getByText('FAILED')).toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('stays a read-only summary in compact mode', async () => {
    const { container } = render(
      <ScansPage isCompact={true} fetchScansFn={async () => listEnvelope([scan()])} />,
    )

    expect(await screen.findByRole('table', { name: /Scan history/i })).toBeInTheDocument()
    const page = container.querySelector('section[aria-labelledby="scans-heading"]')
    expect(page).toHaveAttribute('data-readonly', 'true')
    expect(screen.queryByRole('columnheader', { name: /Cost/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /run/i })).not.toBeInTheDocument()
  })

  it('matches S03.1: schedule card, manual trigger, and batch history columns', async () => {
    const batch = scan({
      scan_id: 'SCAN-BATCH-0001',
      symbol: '',
      status: 'PARTIAL',
      symbols_total: 2,
      symbols_completed: 1,
      symbols_degraded: 0,
      symbols_failed: 1,
      total_cost_usd: '0.500',
      report_id: null,
      symbols: [
        {
          symbol: 'BTCUSDT',
          stage: 'DONE',
          report_id: 'RPT-BTCUSDT-1',
          data_quality: 'COMPLETE',
          error_code: null,
          error_message: null,
          llm_status: 'VALID',
          llm_tokens: 8000,
          cost_usd: '0.500',
          cost_unpriced: false,
        },
        {
          symbol: 'XAUUSDT',
          stage: 'FAILED',
          report_id: null,
          data_quality: null,
          error_code: 'DATA_UNAVAILABLE',
          error_message: 'Data gap 2026-11-01 -> 2026-11-06',
          llm_status: null,
          llm_tokens: 0,
          cost_usd: '0',
          cost_unpriced: true,
        },
      ],
      delivery: { configured: false, reason: 'TELEGRAM_NOT_CONFIGURED' },
    })
    render(<ScansPage fetchScansFn={async () => listEnvelope([batch])} />)

    expect(await screen.findByText('Schedule state')).toBeInTheDocument()
    expect(screen.getByText(/Next run/i)).toBeInTheDocument()
    expect(screen.getByText(/Cadence/i)).toBeInTheDocument()
    expect(screen.getByText(/Last run/i)).toBeInTheDocument()
    expect(screen.getByText(/Budget today/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run scan now/i })).toBeInTheDocument()

    const table = screen.getByRole('table', { name: /Scan history/i })
    expect(within(table).getByRole('columnheader', { name: /Symbols/i })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: /Duration/i })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: /Telegram/i })).toBeInTheDocument()
    expect(within(table).getByText(/1\/2/)).toBeInTheDocument()
    expect(within(table).getByText(/XAUUSDT/)).toBeInTheDocument()
    expect(within(table).getAllByText(/not configured/i).length).toBeGreaterThan(0)
  })

  it('triggers a manual scan from the schedule card', async () => {
    const triggerScanFn = vi.fn(async () => detailEnvelope(scan()))
    render(
      <ScansPage fetchScansFn={async () => listEnvelope([scan()])} triggerScanFn={triggerScanFn} />,
    )

    const button = await screen.findByRole('button', { name: /run scan now/i })
    fireEvent.click(button)

    await waitFor(() => expect(triggerScanFn).toHaveBeenCalledTimes(1))
    // The POST runs the batch synchronously, so the result is a finished scan.
    expect(await screen.findByText(/ABCD1234 scan finished/)).toBeInTheDocument()
  })

  it('refetches the history after a successful trigger', async () => {
    const fresh = scan({ scan_id: 'SCAN-NEW-0001', status: 'COMPLETE' })
    const fetchScansFn = vi
      .fn<() => Promise<ScanListEnvelope>>()
      .mockResolvedValueOnce(listEnvelope([scan()]))
      .mockResolvedValueOnce(listEnvelope([fresh]))
    const triggerScanFn = vi.fn(async () => detailEnvelope(fresh))
    render(<ScansPage fetchScansFn={fetchScansFn} triggerScanFn={triggerScanFn} />)

    const button = await screen.findByRole('button', { name: /run scan now/i })
    fireEvent.click(button)

    await waitFor(() => expect(fetchScansFn).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('SCAN-NEW-0001')).toBeInTheDocument()
    expect(screen.getByText(/scan finished/)).toBeInTheDocument()
  })

  it('keeps keyboard focus and announces status while a trigger is in flight', async () => {
    // @testing-library/user-event is not a dependency of this workspace, so
    // simulate a focused button receiving a native Enter activation (keydown
    // followed by the click the browser synthesises).
    let resolveTrigger: ((value: ScanEnvelope) => void) | undefined
    const triggerScanFn = vi.fn(
      () =>
        new Promise<ScanEnvelope>((resolve) => {
          resolveTrigger = resolve
        }),
    )
    const fetchScansFn = vi.fn<() => Promise<ScanListEnvelope>>(async () => listEnvelope([]))
    render(<ScansPage fetchScansFn={fetchScansFn} triggerScanFn={triggerScanFn} />)

    const button = await screen.findByRole('button', { name: /run scan now/i })
    button.focus()
    expect(button).toHaveFocus()

    fireEvent.keyDown(button, { key: 'Enter' })
    fireEvent.click(button)
    await waitFor(() => expect(triggerScanFn).toHaveBeenCalledTimes(1))
    // The focused button is not disabled; it advertises busy state instead.
    expect(button).not.toBeDisabled()
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toHaveFocus()

    // A second activation while in flight must not start a duplicate run.
    fireEvent.click(button)
    expect(triggerScanFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolveTrigger?.(detailEnvelope(scan()))
    })
    await waitFor(() => expect(screen.getByText(/scan finished/)).toBeInTheDocument())
    expect(button).toHaveAttribute('aria-disabled', 'false')
    expect(button).toHaveFocus()
  })
})

describe('ScanDetailPage', () => {
  const detail = scan({
    status: 'PARTIAL',
    cost_usd: '0.842',
    llm_tokens: 12480,
    symbols: [
      {
        symbol: 'BTCUSDT',
        stage: 'DONE',
        report_id: 'RPT-BTCUSDT-1',
        data_quality: 'COMPLETE',
        error_code: null,
        error_message: null,
        llm_status: 'VALID',
        llm_tokens: 8000,
        cost_usd: '0.500',
        cost_unpriced: false,
        data_stage: 'DONE',
        metrics_stage: 'DONE',
        llm_stage: 'DONE',
        report_stage: 'DONE',
      },
      {
        symbol: 'ETHUSDT',
        stage: 'DEGRADED',
        report_id: 'RPT-ETHUSDT-1',
        data_quality: 'PARTIAL',
        error_code: null,
        error_message: 'LLM_SCHEMA_INVALID: risk.stop_price',
        llm_status: 'REJECTED',
        llm_tokens: 4480,
        cost_usd: '0.342',
        cost_unpriced: false,
      },
      {
        symbol: 'FTTUSDT',
        stage: 'FAILED',
        report_id: null,
        data_quality: null,
        error_code: 'DATA_UNAVAILABLE',
        error_message: 'Data gap 2026-10-01T00:00:00.000Z -> 2026-10-08T00:00:00.000Z',
        llm_status: null,
        llm_tokens: 0,
        cost_usd: '0',
        cost_unpriced: true,
      },
    ],
    report_links: [
      { symbol: 'BTCUSDT', report_id: 'RPT-BTCUSDT-1', href: '/reports/RPT-BTCUSDT-1' },
      { symbol: 'ETHUSDT', report_id: 'RPT-ETHUSDT-1', href: '/reports/RPT-ETHUSDT-1' },
    ],
    delivery: { configured: false, reason: 'Telegram is not configured in this build.' },
  })

  it('shows the LLM stage and per-symbol quality, cost, and report links', async () => {
    render(
      <ScanDetailPage scanId={detail.scan_id} fetchScanFn={async () => detailEnvelope(detail)} />,
    )

    expect(await screen.findByRole('heading', { name: detail.scan_id })).toBeInTheDocument()
    const stageList = screen.getByRole('list', { name: /Named scan stages/i })
    expect(within(stageList).getByText('LLM')).toBeInTheDocument()
    expect(within(stageList).getByText('REPORT')).toBeInTheDocument()

    const table = screen.getByRole('table', { name: /Per-symbol scan stages/i })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(4)
    expect(within(table).getByRole('columnheader', { name: /DATA/i })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: /METRICS/i })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: /REPORT/i })).toBeInTheDocument()
    expect(within(table).getByText('DEGRADED')).toBeInTheDocument()
    expect(within(table).getByText('FAILED')).toBeInTheDocument()
    expect(within(table).getByText(/\$0\.342/)).toBeInTheDocument()
    expect(within(table).getByText(/Data gap 2026-10-01/)).toBeInTheDocument()
    expect(within(table).getByRole('link', { name: 'RPT-ETHUSDT-1' })).toHaveAttribute(
      'href',
      '/reports/RPT-ETHUSDT-1',
    )
    expect(screen.getByText(/Telegram is not configured/i)).toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('stays read-only in compact mode', async () => {
    const { container } = render(
      <ScanDetailPage
        isCompact={true}
        scanId={detail.scan_id}
        fetchScanFn={async () => detailEnvelope(detail)}
      />,
    )

    expect(await screen.findByRole('heading', { name: detail.scan_id })).toBeInTheDocument()
    const page = container.querySelector('section[aria-labelledby="scan-heading"]')
    expect(page).toHaveAttribute('data-readonly', 'true')
  })

  it('refetches while RUNNING and stops once the scan is terminal', async () => {
    vi.useFakeTimers()
    try {
      const running = scan({ status: 'RUNNING', finished_at: '' })
      const done = scan({ status: 'DONE' })
      const fetchScanFn = vi
        .fn()
        .mockResolvedValueOnce(detailEnvelope(running))
        .mockResolvedValueOnce(detailEnvelope(running))
        .mockResolvedValueOnce(detailEnvelope(done))
      render(<ScanDetailPage scanId={running.scan_id} fetchScanFn={fetchScanFn} />)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(fetchScanFn).toHaveBeenCalledTimes(1)
      expect(screen.getByText(/Stages update in place/)).toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(SCAN_POLL_INTERVAL_MS)
      })
      expect(fetchScanFn).toHaveBeenCalledTimes(2)
      expect(screen.getByText(/Stages update in place/)).toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(SCAN_POLL_INTERVAL_MS)
      })
      expect(fetchScanFn).toHaveBeenCalledTimes(3)
      // Terminal status stops polling and removes the in-place footer.
      expect(screen.queryByText(/Stages update in place/)).not.toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(SCAN_POLL_INTERVAL_MS * 3)
      })
      expect(fetchScanFn).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })
})
