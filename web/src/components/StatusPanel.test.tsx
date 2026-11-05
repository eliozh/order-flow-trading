import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { StatusEnvelope } from '../api/status'
import { StatusPanel } from './StatusPanel'

const envelope: StatusEnvelope = {
  ok: true,
  data: {
    status: 'ok',
    env: 'paper',
    version: '0.1.0',
    host: '127.0.0.1',
    port: 8080,
    data_dir: './data',
    log_level: 'INFO',
    generated_at: '2026-11-06T00:00:00.000Z',
    universe: {
      source: 'fixture/static',
      as_of: '2026-11-06T00:00:00.000Z',
      count: 3,
      tradifi_count: 1,
      symbols: [
        {
          symbol: 'BTCUSDT',
          base_asset: 'BTC',
          quote_asset: 'USDT',
          contract_type: 'PERPETUAL',
          tradifi: false,
        },
        {
          symbol: 'ETHUSDT',
          base_asset: 'ETH',
          quote_asset: 'USDT',
          contract_type: 'PERPETUAL',
          tradifi: false,
        },
        {
          symbol: 'AAPLUSDT',
          base_asset: 'AAPL',
          quote_asset: 'USDT',
          contract_type: 'TRADIFI_PERPETUAL',
          tradifi: true,
        },
      ],
    },
  },
  error: null,
  meta: {
    request_id: 'REQ-TEST',
    generated_at: '2026-11-06T00:00:00.000Z',
    duration_ms: 1,
    provenance: 'CALC',
    data_quality: 'COMPLETE',
    warnings: [],
    pagination: null,
    idempotent_replay: false,
  },
}

describe('StatusPanel', () => {
  it('shows a loading state before the request settles', () => {
    render(<StatusPanel fetcher={() => new Promise<StatusEnvelope>(() => {})} />)

    expect(screen.getByRole('status')).toHaveTextContent('Loading system status')
  })

  it('renders the fixture universe from the API envelope', async () => {
    render(<StatusPanel fetcher={async () => envelope} />)

    expect(await screen.findByRole('heading', { name: 'System status' })).toBeInTheDocument()
    expect(screen.getByText('PAPER')).toBeInTheDocument()
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
    expect(screen.getByText('AAPLUSDT')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('renders an error state when the request fails', async () => {
    render(
      <StatusPanel
        fetcher={async () => {
          throw new Error('daemon down')
        }}
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('daemon down')
  })
})
