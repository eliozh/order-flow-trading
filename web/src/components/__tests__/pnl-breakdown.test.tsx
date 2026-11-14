import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PnlBreakdown } from '../pnl-breakdown'
import { pnlFixture, positionFixture } from '../../test/positions-fixtures'

describe('PnlBreakdown', () => {
  it('shows each term on its own base with an explicit basis', () => {
    render(<PnlBreakdown position={positionFixture()} pnl={pnlFixture()} />)

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    expect(within(table).getByText('Price PnL (unrealized)')).toBeInTheDocument()
    expect(within(table).getByText('+588.98')).toBeInTheDocument()
    expect(within(table).getByText('Accumulated gross (realized)')).toBeInTheDocument()
    expect(within(table).getByText('+0.00')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    expect(within(table).getByText('Net uPnL (if closed)')).toBeInTheDocument()
    expect(within(table).getByText('+565.76')).toBeInTheDocument()
    expect(screen.getAllByText(/ACCUMULATED_GROSS/).length).toBeGreaterThan(0)
    expect(screen.getByText(/recorded fees/)).toBeInTheDocument()
  })

  it('renders a measured non-zero realized gross as its own term', () => {
    render(
      <PnlBreakdown
        position={positionFixture({ realized_pnl_usd: '120.00' })}
        pnl={pnlFixture()}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    const grossRow = within(table).getByRole('row', { name: /Accumulated gross/ })
    expect(within(grossRow).getByText('+120.00')).toBeInTheDocument()
    const netRow = within(table).getByRole('row', { name: /Net uPnL/ })
    expect(within(netRow).getByText('+685.76')).toBeInTheDocument()
  })

  it('never renders a fabricated gross for a partial close whose realized term is unmeasured', () => {
    render(
      <PnlBreakdown
        position={positionFixture({
          state: 'OPEN',
          realized_pnl_usd: null,
          management_history: [
            {
              decision_id: 'MGD-0',
              action: 'PARTIAL_CLOSE',
              at: '2026-11-05T12:00:00.000Z',
              executed_order_id: 'PO-2',
              note: null,
            },
          ],
        })}
        pnl={pnlFixture()}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    expect(within(table).queryByText('Accumulated gross (realized)')).not.toBeInTheDocument()
    const netRow = within(table).getByRole('row', { name: /Net uPnL/ })
    expect(within(netRow).getByText('—')).toBeInTheDocument()
  })

  it('shows recorded fees and funding while marking an unmeasured price/net term as a dash', () => {
    render(
      <PnlBreakdown
        position={positionFixture({ mark_price: null, resolved_mark_price: null })}
        pnl={pnlFixture()}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    const priceRow = within(table).getByRole('row', { name: /Price PnL/ })
    const netRow = within(table).getByRole('row', { name: /Net uPnL/ })
    expect(within(priceRow).getByText('—')).toBeInTheDocument()
    expect(within(netRow).getByText('—')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    expect(screen.getByText(/mark 缺失/)).toBeInTheDocument()
    expect(screen.getByText(/不把未测量项当零/)).toBeInTheDocument()
  })

  it('uses the history terms for a closed NET projection instead of relabeling the stored net as gross', () => {
    render(
      <PnlBreakdown
        position={positionFixture({
          state: 'CLOSED',
          realized_pnl_usd: '-250.00',
          mark_price: null,
          resolved_mark_price: null,
        })}
        pnl={pnlFixture({
          basis: 'NET',
          gross_pnl_usd: '300.00',
          fees_usd: '12.44',
          funding_usd: '-10.78',
          net_pnl_usd: '276.78',
        })}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    const grossRow = within(table).getByRole('row', { name: /Price PnL/ })
    expect(within(grossRow).getByText('+300.00')).toBeInTheDocument()
    const netRow = within(table).getByRole('row', { name: /Net PnL/ })
    expect(within(netRow).getByText('+276.78')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    // The stored NET realized value is never relabelled accumulated gross and
    // the history net is not recomputed from the stored mark.
    expect(within(table).queryByText('Accumulated gross (realized)')).not.toBeInTheDocument()
    expect(within(table).queryByText('-250.00')).not.toBeInTheDocument()
    expect(screen.queryByText(/mark 缺失/)).not.toBeInTheDocument()
    expect(screen.getByText(/存储的 realized_pnl_usd 是 NET/)).toBeInTheDocument()
  })

  it('renders a dash for every unmeasured NET history term', () => {
    render(
      <PnlBreakdown
        position={positionFixture({ state: 'CLOSED', realized_pnl_usd: '-250.00' })}
        pnl={pnlFixture({
          basis: 'NET',
          gross_pnl_usd: null,
          fees_usd: null,
          funding_usd: null,
          net_pnl_usd: null,
        })}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    for (const label of [/Price PnL/, /Fees/, /Funding/, /Net PnL/]) {
      const row = within(table).getByRole('row', { name: label })
      expect(within(row).getByText('—')).toBeInTheDocument()
    }
  })

  it('surfaces the PnL data-quality status and reasons from the history projection', () => {
    render(
      <PnlBreakdown
        position={positionFixture()}
        pnl={pnlFixture({
          data_quality: {
            status: 'PARTIAL',
            reasons: ['funding settlement missing a recorded rate'],
          },
        })}
      />,
    )

    expect(screen.getByText(/PnL data quality: PARTIAL/)).toBeInTheDocument()
    expect(screen.getByText(/funding settlement missing a recorded rate/)).toBeInTheDocument()
  })

  it('never presents an unmeasured fee or funding term as zero', () => {
    render(
      <PnlBreakdown
        position={positionFixture({ fees_usd: 'not-a-number', funding_usd: '' })}
        pnl={pnlFixture()}
      />,
    )

    const table = screen.getByRole('table', { name: /PnL 分解/ })
    const netRow = within(table).getByRole('row', { name: /Net uPnL/ })
    expect(within(netRow).getByText('—')).toBeInTheDocument()
  })
})
