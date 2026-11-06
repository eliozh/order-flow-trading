import { act, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { HealthEnvelope, ReadinessEnvelope, ScanRecordData } from '../api/status'
import { SystemPage } from './system'

const meta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T00:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const scan: ScanRecordData = {
  scan_id: 'SCAN-20261106-ABCD1234',
  symbol: 'BTCUSDT',
  period: '4h',
  status: 'DONE',
  trigger_source: 'MANUAL',
  metrics_only: true,
  fixture_path: 'tests/fixtures/market/btcusdt_agg-trades_sample.csv',
  started_at: '2026-11-06T04:00:00.000Z',
  finished_at: '2026-11-06T04:00:01.000Z',
  window_start: '2025-11-05T00:00:00.000Z',
  window_end: '2025-11-05T04:00:00.000Z',
  trade_count: 6,
  buy_volume: '4.25',
  sell_volume: '2.75',
  delta: '1.50',
  cvd: '1.50',
  data_quality: 'COMPLETE',
  manifest_path: '/tmp/data/market/um/agg_trades/symbol=BTCUSDT/date=2025-11-05/part-0000.parquet',
  parquet_rows: 6,
  created_at: '2026-11-06T04:00:01.000Z',
}

const healthEnvelope: HealthEnvelope = {
  ok: true,
  data: { status: 'ok', version: '0.1.0' },
  error: null,
  meta,
}

const readinessEnvelope: ReadinessEnvelope = {
  ok: true,
  data: {
    status: 'ready',
    version: '0.1.0',
    env: 'paper',
    data_dir: '/tmp/data',
    checked_at: '2026-11-06T04:00:01.000Z',
    checks: [
      { name: 'config', status: 'ok', detail: 'settings loaded' },
      { name: 'storage.sqlite', status: 'ok', detail: 'integrity ok' },
      { name: 'storage.parquet', status: 'ok', detail: '/tmp/data/market' },
      { name: 'clock', status: 'ok', detail: '2026-11-06T04:00:01.000Z' },
      { name: 'market_data', status: 'ok', detail: '3 symbols' },
    ],
    latest_scan: scan,
  },
  error: null,
  meta,
}

describe('SystemPage', () => {
  it('shows a loading state before the requests settle', () => {
    render(
      <SystemPage
        fetchHealthFn={() => new Promise<HealthEnvelope>(() => {})}
        fetchReadinessFn={() => new Promise<ReadinessEnvelope>(() => {})}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent('Loading system status')
  })

  it('renders health, readiness, version, data dir, and the fixture scan', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => readinessEnvelope}
        isCompact={false}
      />,
    )

    expect(await screen.findByRole('heading', { name: 'System status' })).toBeInTheDocument()
    expect(screen.getAllByText('OK').length).toBeGreaterThan(0)
    expect(screen.getByText('READY')).toBeInTheDocument()
    expect(screen.getAllByText('0.1.0').length).toBeGreaterThan(0)
    expect(screen.getByText('/tmp/data')).toBeInTheDocument()
    expect(screen.getByText('SCAN-20261106-ABCD1234')).toBeInTheDocument()
    expect(screen.getAllByText('1.50').length).toBeGreaterThan(0)
    expect(screen.getByText('storage.sqlite')).toBeInTheDocument()
    expect(screen.getByText('market_data')).toBeInTheDocument()
  })

  it('renders a positive delta with an up arrow and accessible sign text', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => readinessEnvelope}
        isCompact={false}
      />,
    )

    const delta = await screen.findByLabelText('Delta positive 1.50')
    expect(delta).toHaveTextContent('▲ +1.50')
  })

  it('renders a negative delta with a down arrow and accessible sign text', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => ({
          ...readinessEnvelope,
          data: readinessEnvelope.data && {
            ...readinessEnvelope.data,
            latest_scan: scan && { ...scan, delta: '-2.50' },
          },
        })}
        isCompact={false}
      />,
    )

    const delta = await screen.findByLabelText('Delta negative -2.50')
    expect(delta).toHaveTextContent('▼ -2.50')
    expect(delta).toHaveClass('value-down')
  })

  it('renders an empty scan state when no fixture scan exists', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => ({
          ...readinessEnvelope,
          data: readinessEnvelope.data && { ...readinessEnvelope.data, latest_scan: null },
        })}
      />,
    )

    expect(await screen.findByText(/No fixture scan yet/)).toBeInTheDocument()
  })

  it('marks the 375px shell read-only', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => readinessEnvelope}
        isCompact
      />,
    )

    expect(await screen.findByRole('note')).toHaveTextContent('Read-only at this width')
    expect(
      screen.getByRole('heading', { name: 'System status' }).closest('section'),
    ).toHaveAttribute('data-readonly', 'true')
  })

  it('recomputes compact mode when the viewport media query changes', async () => {
    const listeners = new Set<() => void>()
    let matches = false
    const mediaQuery = {
      get matches() {
        return matches
      },
      addEventListener: (_event: string, listener: () => void) => {
        listeners.add(listener)
      },
      removeEventListener: (_event: string, listener: () => void) => {
        listeners.delete(listener)
      },
    }
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => mediaQuery),
    )

    try {
      const { unmount } = render(
        <SystemPage
          fetchHealthFn={async () => healthEnvelope}
          fetchReadinessFn={async () => readinessEnvelope}
        />,
      )

      expect(await screen.findByRole('heading', { name: 'System status' })).toBeInTheDocument()
      expect(screen.queryByRole('note')).not.toBeInTheDocument()

      act(() => {
        matches = true
        listeners.forEach((listener) => listener())
      })

      expect(screen.getByRole('note')).toHaveTextContent('Read-only at this width')
      expect(
        screen.getByRole('heading', { name: 'System status' }).closest('section'),
      ).toHaveAttribute('data-readonly', 'true')

      unmount()
      expect(listeners.size).toBe(0)
    } finally {
      vi.unstubAllGlobals()
      vi.restoreAllMocks()
    }
  })

  it('renders an error state when a request fails', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => {
          throw new Error('daemon down')
        }}
        fetchReadinessFn={async () => readinessEnvelope}
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('daemon down')
  })
})
