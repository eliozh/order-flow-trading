import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type {
  HealthEnvelope,
  ReadinessEnvelope,
  StatusEnvelope,
  TelegramStatusData,
} from '../../api/status'
import { SystemPage } from '../system'
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
    checks: [{ name: 'config', status: 'ok', detail: 'settings loaded' }],
    latest_scan: null,
  },
  error: null,
  meta,
}

function statusEnvelope(telegram: TelegramStatusData | null): StatusEnvelope {
  return {
    ok: true,
    data: {
      status: 'ok',
      env: 'paper',
      version: '0.1.0',
      host: '127.0.0.1',
      port: 7611,
      data_dir: '/tmp/data',
      log_level: 'INFO',
      generated_at: '2026-11-06T04:00:01.000Z',
      universe: {
        source: 'fixture',
        as_of: '2026-11-06T00:00:00.000Z',
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
      telegram,
    },
    error: null,
    meta,
  }
}

const FAILING_TELEGRAM: TelegramStatusData = {
  configured: true,
  chat_count: 2,
  allowed_user_count: 1,
  poller_state: 'RUNNING',
  last_poll_at: '2026-11-06T04:00:00.000Z',
  last_send_at: '2026-11-06T03:59:00.000Z',
  failing_receipts: 1,
  receipts: { queued: 1, sending: 0, sent: 4, failed: 1 },
  per_chat: [
    {
      chat_id: 1001,
      queued: 0,
      sending: 0,
      sent: 0,
      failed: 1,
      last_error_code: 'TELEGRAM_UPSTREAM_ERROR',
    },
    { chat_id: 2002, queued: 1, sending: 0, sent: 4, failed: 0, last_error_code: null },
  ],
  recent_receipts: [
    {
      event_key: 'scan:1:report',
      kind: 'scan_report',
      chat_id: 1001,
      state: 'failed',
      attempts: 3,
      message_id: null,
      last_error_code: 'TELEGRAM_UPSTREAM_ERROR',
      created_at: '2026-11-06T03:58:00.000Z',
      sent_at: null,
    },
    {
      event_key: 'scan:2:report',
      kind: 'scan_report',
      chat_id: 2002,
      state: 'sent',
      attempts: 0,
      message_id: 900,
      last_error_code: null,
      created_at: '2026-11-06T03:59:00.000Z',
      sent_at: '2026-11-06T03:59:00.000Z',
    },
  ],
  overflow_code: null,
  last_error_code: 'TELEGRAM_UPSTREAM_ERROR',
  daemon_state: 'UP',
}

const HEALTHY_TELEGRAM: TelegramStatusData = {
  ...FAILING_TELEGRAM,
  failing_receipts: 0,
  receipts: { queued: 0, sending: 0, sent: 4, failed: 0 },
  per_chat: [{ chat_id: 2002, queued: 0, sending: 0, sent: 4, failed: 0, last_error_code: null }],
  recent_receipts: [FAILING_TELEGRAM.recent_receipts[1]],
  last_error_code: null,
}

function renderSystem(telegram: TelegramStatusData | null) {
  return render(
    <SystemPage
      fetchHealthFn={async () => healthEnvelope}
      fetchReadinessFn={async () => readinessEnvelope}
      fetchStatusFn={async () => statusEnvelope(telegram)}
    />,
  )
}

describe('SystemPage Telegram receipts', () => {
  it('shows a persistent alert and the last stable error code when pushes fail', async () => {
    renderSystem(FAILING_TELEGRAM)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Telegram pushes failing')
    expect(alert).toHaveTextContent('1 failed receipt')
    expect(alert).toHaveTextContent('TELEGRAM_UPSTREAM_ERROR')
    expect(alert).toHaveTextContent('Other chats continue')
  })

  it('renders sent/failed/retrying totals and per-chat status', async () => {
    renderSystem(FAILING_TELEGRAM)

    await screen.findByRole('heading', { name: 'Telegram delivery receipts' })
    // Both tables are wrapped in the house scroll region.
    expect(
      screen.getByRole('region', { name: 'Telegram delivery receipts by chat' }),
    ).toBeInTheDocument()
    const chatTable = screen.getByRole('table', { name: 'Telegram delivery receipts by chat' })
    const rows = within(chatTable).getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(within(chatTable).getByText('1001')).toBeInTheDocument()
    expect(within(chatTable).getByText('2002')).toBeInTheDocument()
    expect(within(chatTable).getAllByText('TELEGRAM_UPSTREAM_ERROR')).toHaveLength(1)
    // Totals are rendered as labels with counts, not color alone.
    const panel = (
      await screen.findByRole('heading', { name: 'Telegram delivery receipts' })
    ).closest('section')
    expect(panel).not.toBeNull()
    expect(within(panel as HTMLElement).getAllByText('Sent').length).toBeGreaterThan(0)
    expect(within(panel as HTMLElement).getAllByText('Failed').length).toBeGreaterThan(0)
    expect(within(panel as HTMLElement).getAllByText('Retrying').length).toBeGreaterThan(0)
    expect(within(panel as HTMLElement).getAllByText('4').length).toBeGreaterThan(0)
  })

  it('announces the loaded Telegram summary in a polite status', async () => {
    renderSystem(HEALTHY_TELEGRAM)

    const status = await screen.findByTestId('telegram-status')
    await waitFor(() =>
      expect(status).toHaveTextContent(
        'Telegram delivery receipts loaded: 4 sent, 0 failed, 0 retrying.',
      ),
    )
    expect(status).toHaveClass('visually-hidden')
  })

  it('renders the queue overflow line when the store rejected a burst', async () => {
    renderSystem({ ...FAILING_TELEGRAM, overflow_code: 'TELEGRAM_QUEUE_FULL' })

    const panel = (
      await screen.findByRole('heading', { name: 'Telegram delivery receipts' })
    ).closest('section')
    expect(panel).not.toBeNull()
    expect(panel).toHaveTextContent('Queue overflow: TELEGRAM_QUEUE_FULL')
  })

  it('announces a degraded poller even with no failing receipts', async () => {
    renderSystem({
      ...HEALTHY_TELEGRAM,
      poller_state: 'DEGRADED',
      last_error_code: 'TELEGRAM_UPSTREAM_ERROR',
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Telegram poller DEGRADED')
    expect(alert).toHaveTextContent('TELEGRAM_UPSTREAM_ERROR')
  })

  it('humanizes a conflict poller without a failing receipt', async () => {
    renderSystem({
      ...HEALTHY_TELEGRAM,
      poller_state: 'CONFLICT',
      last_error_code: 'TELEGRAM_CONFLICT',
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('another poller owns this token')
  })

  it('renders the recent receipt message id and state', async () => {
    renderSystem(FAILING_TELEGRAM)

    const receiptTable = await screen.findByRole('table', { name: 'Recent Telegram receipts' })
    expect(screen.getByRole('region', { name: 'Recent Telegram receipts' })).toBeInTheDocument()
    expect(within(receiptTable).getByText('900')).toBeInTheDocument()
    expect(within(receiptTable).getByText('SENT')).toBeInTheDocument()
    expect(within(receiptTable).getByText('FAILED')).toBeInTheDocument()
    expect(within(receiptTable).getByText('scan:1:report')).toBeInTheDocument()
  })

  it('shows the poller state and last successful timestamps', async () => {
    renderSystem(FAILING_TELEGRAM)

    const panel = (
      await screen.findByRole('heading', { name: 'Telegram delivery receipts' })
    ).closest('section')
    expect(panel).not.toBeNull()
    expect(within(panel as HTMLElement).getByText('RUNNING')).toBeInTheDocument()
    expect(panel).toHaveTextContent('last successful poll 2026-11-06T04:00:00.000Z')
    expect(panel).toHaveTextContent('last successful send 2026-11-06T03:59:00.000Z')
  })

  it('shows no failure alert when every receipt is delivered', async () => {
    renderSystem(HEALTHY_TELEGRAM)

    await screen.findByRole('heading', { name: 'Telegram delivery receipts' })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('No Telegram receipts yet')).not.toBeInTheDocument()
  })

  it('renders a stacked, wrap-friendly receipt summary at compact width', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => readinessEnvelope}
        fetchStatusFn={async () => statusEnvelope(FAILING_TELEGRAM)}
        isCompact
      />,
    )

    await screen.findByRole('heading', { name: 'Telegram delivery receipts' })
    // The wide Telegram tables are replaced at the read-only width so the page
    // cannot scroll; the narrower readiness table remains.
    expect(
      screen.queryByRole('table', { name: 'Telegram delivery receipts by chat' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('table', { name: 'Recent Telegram receipts' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByRole('group', { name: 'Telegram delivery receipts by chat' }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/sent 0 · failed 1 · retrying 0/).length).toBeGreaterThan(0)
    // Compact recent receipts keep the event key and chat id, information-
    // equivalent to the desktop table.
    const recent = screen.getByRole('group', { name: 'Recent Telegram receipts' })
    expect(recent).toHaveTextContent('scan:1:report')
    expect(recent).toHaveTextContent('chat 1001')
    expect(screen.getByRole('alert')).toHaveTextContent('Telegram pushes failing')
  })

  it('keeps the rest of the system page usable when the status request fails', async () => {
    render(
      <SystemPage
        fetchHealthFn={async () => healthEnvelope}
        fetchReadinessFn={async () => readinessEnvelope}
        fetchStatusFn={async () => {
          throw new Error('status unavailable')
        }}
      />,
    )

    expect(await screen.findByRole('heading', { name: 'System status' })).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: 'Telegram delivery receipts' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // The failure is not swallowed: an additive, non-blocking note stays visible.
    const note = await screen.findByTestId('telegram-status')
    expect(note).toHaveTextContent('Telegram status unavailable')
    expect(note).toHaveClass('status-message')
  })
})
