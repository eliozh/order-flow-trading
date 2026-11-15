import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { OpportunityEnvelope } from '../../api/opportunities'
import type { KillSwitchEnvelope } from '../../api/paper'
import type { TriggerDetailEnvelope } from '../../api/triggers'
import {
  clearManagementKey,
  DEFINITIVE_MANAGE_REFUSAL_CODES,
  MANAGEMENT_KEY_PREFIX,
  MANAGE_TIMEOUT_MS,
  managementIdempotencyKey,
  managementKeyFor,
  PositionApiError,
  requestManagement,
  rotateManagementKey,
  storedManagementKey,
  type PositionData,
  type PositionEnvelope,
  type PositionHistoryData,
  type PositionHistoryEnvelope,
  type PositionListEnvelope,
  type PositionManageEnvelope,
} from '../../api/positions'
import type { ReportEnvelope } from '../../api/reports'
import { KillSwitchBar } from '../../components/kill-switch'
import {
  FRESH_MARK_AS_OF,
  historyEnvelope,
  historyFixture,
  killSwitchEnvelope,
  listEnvelope,
  manageEnvelope,
  manageResult,
  NOW,
  opportunityEnvelope,
  paginatedListEnvelope,
  positionEnvelope,
  positionFixture,
  reportEnvelope,
  STALE_MARK_AS_OF,
  triggerEnvelope,
} from '../../test/positions-fixtures'
import { PositionDetailPage } from '../position-detail'
import { PositionsPage } from '../positions'
import { resolveRoute } from '../route-match'

async function renderList(
  options: {
    items?: PositionData[]
    fetchPositionsFn?: () => Promise<PositionListEnvelope>
    isCompact?: boolean
    refreshMs?: number
  } = {},
) {
  render(
    <PositionsPage
      now={NOW}
      isCompact={options.isCompact}
      refreshMs={options.refreshMs}
      fetchPositionsFn={
        options.fetchPositionsFn ?? (async () => listEnvelope(options.items ?? [positionFixture()]))
      }
    />,
  )
  await screen.findByRole('heading', { name: '未平仓持仓' })
  // The heading also renders during the initial ``loading`` state, so it does
  // not prove the load settled. Gate on the loading status disappearing, which
  // happens in both the ready and error states.
  await waitFor(() => expect(screen.queryByText(/加载未平仓持仓/)).not.toBeInTheDocument())
  return screen.getByRole('heading', { name: '未平仓持仓' })
}

async function renderDetail(
  options: {
    pos?: PositionData
    hist?: PositionHistoryData
    requestManagementFn?: (
      id: string,
      key: string,
      signal?: AbortSignal,
    ) => Promise<PositionManageEnvelope>
    killSwitchFn?: () => Promise<KillSwitchEnvelope>
    reportFn?: () => Promise<ReportEnvelope>
    fetchPositionFn?: (
      positionId: string,
      signal?: AbortSignal,
    ) => Promise<PositionEnvelope<PositionData>>
    fetchHistoryFn?: (positionId: string, signal?: AbortSignal) => Promise<PositionHistoryEnvelope>
    fetchTriggerFn?: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
    fetchOpportunityFn?: (
      opportunityId: string,
      signal?: AbortSignal,
    ) => Promise<OpportunityEnvelope>
    isNarrow?: boolean
    refreshMs?: number
  } = {},
) {
  render(
    <PositionDetailPage
      positionId="POS-20261104-018"
      now={NOW}
      isNarrow={options.isNarrow}
      refreshMs={options.refreshMs}
      fetchPositionFn={
        options.fetchPositionFn ?? (async () => positionEnvelope(options.pos ?? positionFixture()))
      }
      fetchHistoryFn={
        options.fetchHistoryFn ?? (async () => historyEnvelope(options.hist ?? historyFixture()))
      }
      fetchTriggerFn={options.fetchTriggerFn ?? (async () => triggerEnvelope())}
      fetchOpportunityFn={options.fetchOpportunityFn ?? (async () => opportunityEnvelope())}
      fetchReportFn={options.reportFn ?? (async () => reportEnvelope())}
      fetchKillSwitchFn={options.killSwitchFn ?? (async () => killSwitchEnvelope(false))}
      requestManagementFn={
        options.requestManagementFn ?? (async () => manageEnvelope(manageResult()))
      }
    />,
  )
  await screen.findByRole('heading', { name: 'POS-20261104-018' })
  // The same heading renders while the initial load is in flight. Wait until
  // the loading status is gone so the ready (or error) branch is committed
  // before callers run synchronous queries against page content.
  await waitFor(() =>
    expect(screen.queryByText(/^加载持仓 POS-20261104-018/)).not.toBeInTheDocument(),
  )
  return screen.getByRole('heading', { name: 'POS-20261104-018' })
}

/** Open the management dialog and submit its confirmation once. */
function submitManagementRequest(): HTMLElement {
  fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
  const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
  fireEvent.click(within(dialog).getByRole('button', { name: /确认请求管理/ }))
  return dialog
}

type DetailProps = Parameters<typeof PositionDetailPage>[0]

/** A complete set of detail props with per-test overrides. */
function detailProps(overrides: Partial<DetailProps> = {}): DetailProps {
  return {
    positionId: 'POS-20261104-018',
    now: NOW,
    fetchPositionFn: async () => positionEnvelope(positionFixture()),
    fetchHistoryFn: async () => historyEnvelope(historyFixture()),
    fetchTriggerFn: async () => triggerEnvelope(),
    fetchOpportunityFn: async () => opportunityEnvelope(),
    fetchReportFn: async () => reportEnvelope(),
    fetchKillSwitchFn: async () => killSwitchEnvelope(false),
    requestManagementFn: async () => manageEnvelope(manageResult()),
    ...overrides,
  }
}

beforeEach(() => {
  window.sessionStorage.clear()
  for (const id of ['POS-20261104-018', 'POS-1', 'POS-A', 'POS-B']) {
    clearManagementKey(id)
  }
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('positions route matching', () => {
  it('routes the list and exactly one detail segment', () => {
    const list = resolveRoute('/positions')
    expect(list.isPositions).toBe(true)
    expect(list.positionId).toBeNull()

    const detail = resolveRoute('/positions/POS%2D1')
    expect(detail.isPositions).toBe(true)
    expect(detail.positionId).toBe('POS-1')

    const deep = resolveRoute('/positions/POS-1/extra')
    expect(deep.isPositions).toBe(false)
    expect(deep.positionId).toBeNull()
  })

  it('does not affect the paper route', () => {
    expect(resolveRoute('/paper').isPaper).toBe(true)
    expect(resolveRoute('/paper').isPositions).toBe(false)
  })
})

describe('PositionsPage', () => {
  it('shows every documented column', async () => {
    await renderList()

    const table = screen.getByRole('table', { name: /未平仓持仓/ })
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((header) => header.textContent ?? '')
    for (const label of [
      'Symbol',
      'Side',
      'Size',
      'Entry',
      'Mark',
      'uPnL',
      'R',
      'Stop',
      'Target',
      'Lev',
      'Margin',
      'Risk',
      'Age',
      'Env',
    ]) {
      expect(headers).toContain(label)
    }

    const row = within(table).getByRole('row', { name: /ETHUSDT/ })
    expect(within(row).getByText('▲ LONG')).toBeInTheDocument()
    expect(within(row).getByText('+565.76')).toBeInTheDocument()
    expect(within(row).getByText('0.71')).toBeInTheDocument()
    expect(within(row).getByText('5x')).toBeInTheDocument()
    expect(within(row).getByText('PAPER')).toBeInTheDocument()
  })

  it('shows an empty state that explains paper vs live and links only to opportunities and paper', async () => {
    await renderList({ items: [] })

    expect(screen.getByText(/没有未平仓持仓/)).toBeInTheDocument()
    expect(screen.getByText(/Paper 与 Live 范围不同/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看机会/ })).toHaveAttribute('href', '/opportunities')
    expect(screen.getByRole('link', { name: /Paper 账户/ })).toHaveAttribute('href', '/paper')
    expect(document.querySelector('a[href="/journal"]')).toBeNull()
  })

  it('renders a stale mark as STALE, keeps the last values, and never fabricates a missing mark', async () => {
    await renderList({
      items: [
        positionFixture({ resolved_mark_as_of: STALE_MARK_AS_OF }),
        positionFixture({
          position_id: 'POS-2',
          symbol: 'XAUUSDT',
          mark_price: null,
          resolved_mark_price: null,
          resolved_mark_as_of: null,
          data_quality: { status: 'PARTIAL', reasons: ['mark missing'] },
        }),
      ],
    })

    const stale = screen.getByRole('row', { name: /ETHUSDT/ })
    expect(within(stale).getByText(/STALE/)).toBeInTheDocument()
    expect(within(stale).getByText('+565.76')).toBeInTheDocument()

    const missing = screen.getByRole('row', { name: /XAUUSDT/ })
    expect(within(missing).getByText(/STALE/)).toBeInTheDocument()
    expect(within(missing).getByText(/PARTIAL/)).toBeInTheDocument()
    expect(within(missing).queryByText(/^[+-]\d/)).toBeNull()
  })

  it('labels an aggregate PARTIAL and excludes missing marks from the uPnL sum', async () => {
    await renderList({
      items: [
        positionFixture(),
        positionFixture({
          position_id: 'POS-2',
          symbol: 'XAUUSDT',
          mark_price: null,
          resolved_mark_price: null,
          resolved_mark_as_of: null,
        }),
      ],
    })

    expect(screen.getAllByText(/PARTIAL/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('+565.76').length).toBeGreaterThan(0)
    expect(screen.getByText(/caps enforced server-side/)).toBeInTheDocument()
  })

  it('never invents values when the initial load fails', async () => {
    await renderList({
      fetchPositionsFn: async () => {
        throw new Error('positions feed down')
      },
    })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(/positions feed down/)
    expect(screen.queryByRole('table', { name: /未平仓持仓/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/\+565\.76/)).not.toBeInTheDocument()
  })

  it('is structurally read-only at compact width', async () => {
    await renderList({ isCompact: true })

    expect(screen.getByText(/移动端只读/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /请求管理/ })).not.toBeInTheDocument()
  })

  it('revalidates on the configured cadence without blanking the table', async () => {
    const fetchPositionsFn = vi.fn(async () => listEnvelope([positionFixture()]))
    render(<PositionsPage now={NOW} refreshMs={20} fetchPositionsFn={fetchPositionsFn} />)

    await screen.findByRole('heading', { name: /未平仓持仓/ })
    await waitFor(() => expect(fetchPositionsFn.mock.calls.length).toBeGreaterThanOrEqual(2))
    expect(screen.getByRole('table', { name: /未平仓持仓/ })).toBeInTheDocument()
    expect(screen.getAllByText('+565.76').length).toBeGreaterThan(0)
  })

  it('keeps the last values and warns when a revalidation fails', async () => {
    let calls = 0
    const fetchPositionsFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        return listEnvelope([positionFixture()])
      }
      throw new Error('refresh down')
    })
    render(<PositionsPage now={NOW} refreshMs={20} fetchPositionsFn={fetchPositionsFn} />)

    await screen.findByRole('heading', { name: /未平仓持仓/ })
    await waitFor(() => expect(screen.getByTestId('refresh-warning')).toBeInTheDocument())
    expect(screen.getByRole('table', { name: /未平仓持仓/ })).toBeInTheDocument()
    expect(screen.getAllByText('+565.76').length).toBeGreaterThan(0)
  })

  it('freezes polling while paused and revalidates once on resume', async () => {
    const fetchPositionsFn = vi.fn(async () => listEnvelope([positionFixture()]))
    const { rerender } = render(
      <PositionsPage now={NOW} refreshMs={20} paused fetchPositionsFn={fetchPositionsFn} />,
    )

    await screen.findByRole('heading', { name: /未平仓持仓/ })
    expect(screen.getByTestId('positions-paused')).toHaveTextContent(/实时更新已暂停/)
    // The page panel is not a live region: only the status bar announces once.
    expect(screen.getByTestId('positions-paused')).toHaveAttribute('role', 'note')
    expect(fetchPositionsFn).toHaveBeenCalledTimes(1)

    // No interval fires while paused.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchPositionsFn).toHaveBeenCalledTimes(1)

    // A focus event is ignored while paused.
    fireEvent(window, new Event('focus'))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(fetchPositionsFn).toHaveBeenCalledTimes(1)

    rerender(
      <PositionsPage
        now={NOW}
        refreshMs={60_000}
        paused={false}
        fetchPositionsFn={fetchPositionsFn}
      />,
    )
    await waitFor(() => expect(fetchPositionsFn).toHaveBeenCalledTimes(2))
  })

  it('marks a partial server page as PARTIAL with the real total', async () => {
    await renderList({
      fetchPositionsFn: async () => paginatedListEnvelope([positionFixture()], 4, true),
    })

    expect(screen.getAllByText(/PARTIAL/).length).toBeGreaterThan(0)
    expect(screen.getByTestId('position-table-truncated')).toHaveTextContent(/1 \/ 4/)
    expect(screen.getAllByText(/列表已分页/).length).toBeGreaterThan(0)
  })

  it('never claims there are no positions when the ledger total is non-zero', async () => {
    await renderList({
      fetchPositionsFn: async () => paginatedListEnvelope([], 2, false),
    })

    expect(screen.queryByRole('heading', { name: '没有未平仓持仓' })).not.toBeInTheDocument()
    expect(screen.getByTestId('position-table-page-empty')).toHaveTextContent(/台账共有 2/)
  })

  it('aborts an in-flight revalidation when paused', async () => {
    let pollSignal: AbortSignal | undefined
    let calls = 0
    const fetchPositionsFn = vi.fn((signal?: AbortSignal) => {
      calls += 1
      if (calls === 1) {
        return Promise.resolve(listEnvelope([positionFixture()]))
      }
      pollSignal = signal
      return new Promise<PositionListEnvelope>(() => undefined)
    })
    const { rerender } = render(
      <PositionsPage now={NOW} refreshMs={10} paused={false} fetchPositionsFn={fetchPositionsFn} />,
    )

    await screen.findByRole('heading', { name: /未平仓持仓/ })
    await waitFor(() => expect(fetchPositionsFn.mock.calls.length).toBeGreaterThanOrEqual(2))
    expect(pollSignal?.aborted).toBe(false)

    rerender(<PositionsPage now={NOW} refreshMs={10} paused fetchPositionsFn={fetchPositionsFn} />)

    await waitFor(() => expect(pollSignal?.aborted).toBe(true))
    expect(screen.getByTestId('positions-paused')).toBeInTheDocument()
  })

  it('does not let a late initial failure replace a page a poll already made ready', async () => {
    let rejectInitial: ((error: unknown) => void) | undefined
    let calls = 0
    const fetchPositionsFn = vi.fn(() => {
      calls += 1
      if (calls === 1) {
        return new Promise<PositionListEnvelope>((_resolve, reject) => {
          rejectInitial = reject
        })
      }
      return Promise.resolve(listEnvelope([positionFixture()]))
    })
    render(<PositionsPage now={NOW} refreshMs={10} fetchPositionsFn={fetchPositionsFn} />)

    await waitFor(() =>
      expect(screen.getByRole('table', { name: /未平仓持仓/ })).toBeInTheDocument(),
    )
    await act(async () => {
      rejectInitial?.(new Error('late initial failure'))
    })

    expect(screen.getByRole('table', { name: /未平仓持仓/ })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText(/late initial failure/)).not.toBeInTheDocument()
  })

  it('does not let a stale poll regress a newer retry result', async () => {
    let resolveStalePoll: ((value: PositionListEnvelope) => void) | undefined
    let calls = 0
    const fetchPositionsFn = vi.fn(() => {
      calls += 1
      if (calls === 1) {
        return Promise.reject(new Error('initial down'))
      }
      if (calls === 2) {
        return new Promise<PositionListEnvelope>((resolve) => {
          resolveStalePoll = resolve
        })
      }
      return Promise.resolve(listEnvelope([positionFixture({ symbol: 'NEWUSDT' })]))
    })
    render(<PositionsPage now={NOW} refreshMs={10} fetchPositionsFn={fetchPositionsFn} />)

    const alert = await screen.findByRole('alert')
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2))
    fireEvent.click(within(alert).getByRole('button', { name: /重试加载持仓/ }))
    await screen.findByText('NEWUSDT')

    await act(async () => {
      resolveStalePoll?.(listEnvelope([positionFixture({ symbol: 'OLDUSDT' })]))
    })

    expect(screen.queryByText('OLDUSDT')).not.toBeInTheDocument()
    expect(screen.getByText('NEWUSDT')).toBeInTheDocument()
  })

  it('offers a retry on the initial-load error panel and recovers', async () => {
    let calls = 0
    const fetchPositionsFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('positions feed down')
      }
      return listEnvelope([positionFixture()])
    })
    render(<PositionsPage now={NOW} refreshMs={60_000} fetchPositionsFn={fetchPositionsFn} />)

    const alert = await screen.findByRole('alert')
    fireEvent.click(within(alert).getByRole('button', { name: /重试加载持仓/ }))

    await waitFor(() =>
      expect(screen.getByRole('table', { name: /未平仓持仓/ })).toBeInTheDocument(),
    )
    expect(fetchPositionsFn).toHaveBeenCalledTimes(2)
  })
})

describe('PositionDetailPage', () => {
  it('shows the PnL breakdown with an explicit basis and no fabricated terms', async () => {
    await renderDetail()

    expect(screen.getByRole('heading', { name: /PnL 分解/ })).toBeInTheDocument()
    const table = screen.getByRole('table', { name: /PnL 分解/ })
    expect(within(table).getByText('Price PnL (unrealized)')).toBeInTheDocument()
    expect(within(table).getByText('+588.98')).toBeInTheDocument()
    expect(within(table).getByText('Accumulated gross (realized)')).toBeInTheDocument()
    expect(within(table).getByText('+0.00')).toBeInTheDocument()
    expect(within(table).getByText('Fees')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('Funding')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    expect(within(table).getByText('Net uPnL (if closed)')).toBeInTheDocument()
    expect(within(table).getByText('+565.76')).toBeInTheDocument()
    expect(screen.getAllByText(/ACCUMULATED_GROSS/).length).toBeGreaterThan(0)
  })

  it('renders the chart with an additive entry marker and the linked signal chain', async () => {
    await renderDetail()

    expect(screen.getByTestId('chart-entry-marker')).toBeInTheDocument()
    expect(screen.getByText(/ENTRY 3214\.60/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /TRG-1/ })).toHaveAttribute('href', '/triggers/TRG-1')
    expect(screen.getByRole('link', { name: /OPP-1/ })).toHaveAttribute(
      'href',
      '/opportunities/OPP-1',
    )
    expect(screen.getByRole('link', { name: /RPT-1/ })).toHaveAttribute('href', '/reports/RPT-1')
  })

  it('degrades the link chain and chart to N/A instead of breaking the page', async () => {
    await renderDetail({
      pos: positionFixture({ source_trigger_id: null }),
      reportFn: async () => {
        throw new Error('report unavailable')
      },
    })

    expect(screen.getByText(/Chart unavailable/)).toBeInTheDocument()
    expect(screen.getAllByText(/N\/A/).length).toBeGreaterThan(0)
  })

  it('confirms the paper-only consequence and sends exactly one idempotent request on a duplicate click', async () => {
    let resolveRequest: ((value: PositionManageEnvelope) => void) | undefined
    const requestManagementFn = vi.fn(
      () =>
        new Promise<PositionManageEnvelope>((resolve) => {
          resolveRequest = resolve
        }),
    )
    await renderDetail({ requestManagementFn })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))

    const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveTextContent(/LLM 只提议/)
    expect(dialog).toHaveTextContent(/Idempotency-Key/)

    const confirm = within(dialog).getByRole('button', { name: /确认请求管理/ })
    fireEvent.click(confirm)
    expect(confirm).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(confirm)

    expect(requestManagementFn).toHaveBeenCalledTimes(1)
    const [positionId, key] = requestManagementFn.mock.calls[0] as unknown as [string, string]
    expect(positionId).toBe('POS-20261104-018')
    expect(key.length).toBeGreaterThan(0)

    await act(async () => {
      resolveRequest?.(manageEnvelope(manageResult()))
    })

    await waitFor(() => expect(screen.getByText('LLM 提议')).toBeInTheDocument())
    expect(screen.getByText('CALC 校验')).toBeInTheDocument()
    expect(screen.getByText('执行')).toBeInTheDocument()
    expect(screen.getByText(/无订单/)).toBeInTheDocument()
    expect(screen.getByText(/stub \/ fixture/)).toBeInTheDocument()
    expect(screen.getByText(/趋势未破坏/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens the confirmation with initial focus on cancel, cancels with Escape, and restores focus', async () => {
    await renderDetail()
    const trigger = screen.getByRole('button', { name: /请求管理决策/ })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
    const cancel = within(dialog).getByRole('button', { name: '取消' })
    await waitFor(() => expect(cancel).toHaveFocus())

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('renders the persisted management history entries', async () => {
    await renderDetail({
      pos: positionFixture({
        management_history: [
          {
            decision_id: 'MGD-0',
            action: 'PARTIAL_CLOSE',
            at: '2026-11-05T12:00:00.000Z',
            executed_order_id: 'PO-2',
            note: 'reduced size',
          },
        ],
      }),
    })

    expect(screen.getByText('PARTIAL_CLOSE')).toBeInTheDocument()
    expect(screen.getByText(/PO-2/)).toBeInTheDocument()
    expect(screen.getByText(/reduced size/)).toBeInTheDocument()
  })

  it('renders the binding cap, refusals, and a blocked result without inventing an order', async () => {
    const blocked = manageResult({
      action: 'ADD',
      status: 'BLOCKED',
      executed: false,
      reason_code: 'RISK_BLOCKED',
      reason: 'risk gate blocked the add',
      risk: {
        verdict: 'BLOCK',
        binding_cap: 'MAX_POSITIONS',
        stale_inputs: ['spread_bps'],
        reasons: ['max concurrent positions reached'],
      },
      refusals: [
        { code: 'STOP_LOOSEN_REFUSED', field: 'new_stop_price', message: 'stop cannot loosen' },
      ],
    })
    await renderDetail({ requestManagementFn: async () => manageEnvelope(blocked) })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认请求管理/ }))

    await waitFor(() => expect(screen.getByText('MAX_POSITIONS')).toBeInTheDocument())
    expect(screen.getByText(/STOP_LOOSEN_REFUSED/)).toBeInTheDocument()
    expect(screen.getByText(/stop cannot loosen/)).toBeInTheDocument()
    expect(screen.getByText(/无订单/)).toBeInTheDocument()
  })

  it('disables management with a rendered reason for kill switch, stale mark, non-open state, and narrow width', async () => {
    const { unmount } = render(
      <PositionDetailPage
        positionId="POS-20261104-018"
        now={NOW}
        fetchPositionFn={async () => positionEnvelope(positionFixture())}
        fetchHistoryFn={async () => historyEnvelope(historyFixture())}
        fetchTriggerFn={async () => triggerEnvelope()}
        fetchOpportunityFn={async () => opportunityEnvelope()}
        fetchReportFn={async () => reportEnvelope()}
        fetchKillSwitchFn={async () => killSwitchEnvelope(true)}
        requestManagementFn={async () => manageEnvelope(manageResult())}
      />,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    expect(await screen.findByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/Kill switch 已启用/)).toBeInTheDocument()
    unmount()

    render(
      <PositionDetailPage
        positionId="POS-20261104-018"
        now={NOW}
        fetchPositionFn={async () =>
          positionEnvelope(positionFixture({ resolved_mark_as_of: STALE_MARK_AS_OF }))
        }
        fetchHistoryFn={async () => historyEnvelope(historyFixture())}
        fetchTriggerFn={async () => triggerEnvelope()}
        fetchOpportunityFn={async () => opportunityEnvelope()}
        fetchReportFn={async () => reportEnvelope()}
        fetchKillSwitchFn={async () => killSwitchEnvelope(false)}
      />,
    )
    await screen.findAllByRole('heading', { name: 'POS-20261104-018' })
    expect(await screen.findByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/mark 价格已过期/)).toBeInTheDocument()
  })

  it('is read-only at 375px', async () => {
    await renderDetail({ isNarrow: true })

    expect(screen.getByText(/移动端只读/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/375px 只读/)).toBeInTheDocument()
  })

  it('falls back to position risk values when the entry verdict is unavailable', async () => {
    render(
      <PositionDetailPage
        positionId="POS-20261104-018"
        now={NOW}
        fetchPositionFn={async () => positionEnvelope(positionFixture({ source_trigger_id: null }))}
        fetchHistoryFn={async () => historyEnvelope(historyFixture())}
        fetchTriggerFn={async () => {
          throw new Error('no trigger')
        }}
        fetchOpportunityFn={async () => {
          throw new Error('no opportunity')
        }}
        fetchReportFn={async () => {
          throw new Error('no report')
        }}
        fetchKillSwitchFn={async () => killSwitchEnvelope(false)}
      />,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })

    expect(await screen.findByText(/入场 risk verdict 不可用/)).toBeInTheDocument()
    expect(screen.getAllByText(/796\.80/).length).toBeGreaterThan(0)
  })

  it('keeps the loaded position when a blocked result carries position: null', async () => {
    const blocked = manageResult({
      action: 'ADD',
      status: 'BLOCKED',
      executed: false,
      reason_code: 'RISK_BLOCKED',
      reason: 'risk gate blocked the add',
      risk: {
        verdict: 'BLOCK',
        binding_cap: 'MAX_POSITIONS',
        reasons: ['max concurrent positions reached'],
      },
      refusals: [
        { code: 'STOP_LOOSEN_REFUSED', field: 'new_stop_price', message: 'stop cannot loosen' },
      ],
      position: null,
    })
    await renderDetail({ requestManagementFn: async () => manageEnvelope(blocked) })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认请求管理/ }))

    await waitFor(() => expect(screen.getByText('MAX_POSITIONS')).toBeInTheDocument())
    expect(screen.queryByText(/未找到该持仓/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'POS-20261104-018' })).toBeInTheDocument()
    expect(screen.getByText(/risk gate blocked the add/)).toBeInTheDocument()
    expect(screen.getByText(/STOP_LOOSEN_REFUSED/)).toBeInTheDocument()
  })

  it('keeps the dialog open and resets acting when cancel is pressed during an in-flight request', async () => {
    let resolveRequest: ((value: PositionManageEnvelope) => void) | undefined
    const requestManagementFn = vi.fn(
      () =>
        new Promise<PositionManageEnvelope>((resolve) => {
          resolveRequest = resolve
        }),
    )
    await renderDetail({ requestManagementFn })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
    fireEvent.click(within(dialog).getByRole('button', { name: /确认请求管理/ }))

    expect(within(dialog).getByRole('button', { name: '取消' })).toBeDisabled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: /请求管理决策/ })).toBeInTheDocument()
    expect(requestManagementFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolveRequest?.(manageEnvelope(manageResult()))
    })

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /请求管理决策/ })).toBeEnabled()
  })

  it('reuses one idempotency key per position until a definitive result', async () => {
    const keys: string[] = []
    let call = 0
    const requestManagementFn = vi.fn(async (_id: string, key: string) => {
      keys.push(key)
      call += 1
      if (call === 1) {
        throw new Error('manage down')
      }
      return manageEnvelope(manageResult())
    })
    await renderDetail({ requestManagementFn })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认请求管理/ }))
    await waitFor(() => expect(screen.getAllByText(/管理请求失败/).length).toBeGreaterThan(0))

    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认请求管理/ }))
    await waitFor(() => expect(requestManagementFn).toHaveBeenCalledTimes(2))

    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })

  it('fails closed with a distinct reason when the kill-switch state is unknown', async () => {
    await renderDetail({
      killSwitchFn: async () => {
        throw new Error('kill probe down')
      },
    })

    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/Kill switch 状态未知/)).toBeInTheDocument()
  })

  it('blocks management for a non-OPEN position and names the state', async () => {
    await renderDetail({ pos: positionFixture({ state: 'CLOSED' }) })

    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/持仓状态为 CLOSED/)).toBeInTheDocument()
  })

  it('shows STALE without the PARTIAL panel for a stale-but-COMPLETE position', async () => {
    await renderDetail({ pos: positionFixture({ resolved_mark_as_of: STALE_MARK_AS_OF }) })

    expect(screen.getAllByText(/STALE/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/部分数据缺失/)).not.toBeInTheDocument()
  })

  it('moves focus to the page heading when the invoker is disabled at close', async () => {
    let probe = 0
    render(
      <div className="app-shell">
        <PositionDetailPage
          positionId="POS-20261104-018"
          now={NOW}
          refreshMs={60_000}
          fetchPositionFn={async () => positionEnvelope(positionFixture())}
          fetchHistoryFn={async () => historyEnvelope(historyFixture())}
          fetchTriggerFn={async () => triggerEnvelope()}
          fetchOpportunityFn={async () => opportunityEnvelope()}
          fetchReportFn={async () => reportEnvelope()}
          fetchKillSwitchFn={async () => {
            probe += 1
            return killSwitchEnvelope(probe > 1)
          }}
        />
      </div>,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    const trigger = await screen.findByRole('button', { name: /请求管理决策/ })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus())

    // An explicit revalidation (not a timing race) flips the kill switch.
    fireEvent(window, new Event('focus'))
    await waitFor(() => expect(screen.getByText(/Kill switch 已启用/)).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByRole('heading', { name: 'POS-20261104-018' })).toHaveFocus()
  })

  it('appends the global kill switch to the dialog tab cycle and leaves it non-inert', async () => {
    render(
      <div className="app-shell">
        <KillSwitchBar fetchStatusFn={async () => killSwitchEnvelope(false)} />
        <PositionDetailPage
          positionId="POS-20261104-018"
          now={NOW}
          fetchPositionFn={async () => positionEnvelope(positionFixture())}
          fetchHistoryFn={async () => historyEnvelope(historyFixture())}
          fetchTriggerFn={async () => triggerEnvelope()}
          fetchOpportunityFn={async () => opportunityEnvelope()}
          fetchReportFn={async () => reportEnvelope()}
          fetchKillSwitchFn={async () => killSwitchEnvelope(false)}
        />
      </div>,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    const trigger = await screen.findByRole('button', { name: /请求管理决策/ })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
    const cancel = within(dialog).getByRole('button', { name: '取消' })
    await waitFor(() => expect(cancel).toHaveFocus())

    const killAction = document.querySelector<HTMLElement>('[data-kill-switch-action]')
    expect(killAction).not.toBeNull()
    expect(document.querySelector('.kill-switch-bar')).not.toHaveAttribute('inert')
    expect(document.querySelector('.system-page')).toHaveAttribute('inert')

    // Shift+Tab from the first dialog control reaches the kill switch.
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(killAction).toHaveFocus()

    // Tab from the kill switch wraps back to the first dialog control.
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(cancel).toHaveFocus()

    // Tab from the last dialog control reaches the kill switch.
    const submit = within(dialog).getByRole('button', { name: /确认请求管理/ })
    submit.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(killAction).toHaveFocus()
  })

  it('does not regress a newer management position when an older poll resolves', async () => {
    let positionCalls = 0
    let resolvePoll: ((value: PositionEnvelope<PositionData>) => void) | undefined
    const fetchPositionFn = vi.fn<
      (positionId: string, signal?: AbortSignal) => Promise<PositionEnvelope<PositionData>>
    >(() => {
      positionCalls += 1
      if (positionCalls === 1) {
        return Promise.resolve(positionEnvelope(positionFixture({ version: 2 })))
      }
      return new Promise<PositionEnvelope<PositionData>>((resolve) => {
        resolvePoll = resolve
      })
    })
    await renderDetail({
      fetchPositionFn,
      refreshMs: 60_000,
      requestManagementFn: async () =>
        manageEnvelope(
          manageResult({ position: positionFixture({ version: 3, size: '4.21', state: 'OPEN' }) }),
        ),
    })

    expect(screen.getByText('8.42')).toBeInTheDocument()

    // The management result applies the executed position (a newer version).
    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    fireEvent.click(screen.getByRole('button', { name: /确认请求管理/ }))
    await waitFor(() => expect(screen.getByText('4.21')).toBeInTheDocument())

    // A poll started after the result resolves with an older snapshot.
    fireEvent(window, new Event('focus'))
    await waitFor(() => expect(fetchPositionFn).toHaveBeenCalledTimes(2))
    await act(async () => {
      resolvePoll?.(positionEnvelope(positionFixture({ version: 2, size: '8.42' })))
    })

    await waitFor(() => expect(screen.getByText('4.21')).toBeInTheDocument())
    expect(screen.queryByText('8.42')).not.toBeInTheDocument()
  })

  it('keeps the rendered chart and links when a later link-chain refresh fails', async () => {
    let reportCalls = 0
    const reportFn = vi.fn(async () => {
      reportCalls += 1
      if (reportCalls === 1) {
        return reportEnvelope()
      }
      throw new Error('report refresh down')
    })
    await renderDetail({ reportFn, refreshMs: 60_000 })

    expect(screen.getByTestId('chart-entry-marker')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /RPT-1/ })).toHaveAttribute('href', '/reports/RPT-1')

    // A manual revalidation refetches the immutable chain; this one fails.
    fireEvent(window, new Event('focus'))

    await waitFor(() => expect(screen.getByTestId('link-refresh-warning')).toBeInTheDocument())
    expect(screen.getByTestId('link-refresh-warning')).toHaveTextContent(/report refresh down/)
    expect(screen.getByTestId('chart-entry-marker')).toBeInTheDocument()
    expect(screen.queryByText(/Chart unavailable/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /TRG-1/ })).toHaveAttribute('href', '/triggers/TRG-1')
    expect(screen.getByRole('link', { name: /OPP-1/ })).toHaveAttribute(
      'href',
      '/opportunities/OPP-1',
    )
    expect(screen.getByRole('link', { name: /RPT-1/ })).toHaveAttribute('href', '/reports/RPT-1')
  })

  it('closes the management dialog with an announced reason when the kill switch activates', async () => {
    let killSwitchActive = false
    render(
      <div className="app-shell">
        <KillSwitchBar fetchStatusFn={async () => killSwitchEnvelope(true)} />
        <PositionDetailPage
          positionId="POS-20261104-018"
          now={NOW}
          refreshMs={60_000}
          fetchPositionFn={async () => positionEnvelope(positionFixture())}
          fetchHistoryFn={async () => historyEnvelope(historyFixture())}
          fetchTriggerFn={async () => triggerEnvelope()}
          fetchOpportunityFn={async () => opportunityEnvelope()}
          fetchReportFn={async () => reportEnvelope()}
          fetchKillSwitchFn={async () => killSwitchEnvelope(killSwitchActive)}
        />
      </div>,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ })
    const trigger = await screen.findByRole('button', { name: /请求管理决策/ })
    trigger.focus()
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog', { name: /请求管理决策/ })).toBeInTheDocument()

    // The global bar is already active, so its action is a resume control that
    // must never be part of the management dialog's focus cycle.
    const resume = document.querySelector<HTMLElement>('[data-kill-switch-action="resume"]')
    expect(resume).not.toBeNull()
    expect(document.querySelector('[data-kill-switch-action="activate"]')).toBeNull()

    // The kill switch turns active while the dialog is open: close the dialog
    // with an announced, visible reason instead of stacking a resume dialog.
    killSwitchActive = true
    fireEvent(window, new Event('focus'))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByTestId('manage-dialog-closed-reason')).toHaveTextContent(
      /Kill switch 已启用/,
    )
    expect(screen.getByTestId('manage-live')).toHaveTextContent(/Kill switch 已启用/)
    expect(document.querySelectorAll('[role="dialog"]').length).toBe(0)
    expect(document.activeElement).not.toBe(resume)
    expect(screen.getByRole('heading', { name: 'POS-20261104-018' })).toHaveFocus()
  })

  it('keeps management enabled for a fresh resolved bar mark when the persisted updated_at is old', async () => {
    await renderDetail({
      pos: positionFixture({
        updated_at: '2026-11-06T09:00:00.000Z',
        resolved_mark_price: '3300.00',
        resolved_mark_as_of: FRESH_MARK_AS_OF,
        resolved_mark_source: 'BARS',
      }),
    })

    const button = screen.getByRole('button', { name: /请求管理决策/ })
    expect(button).not.toHaveAttribute('aria-disabled')
    expect(screen.queryByText(/mark 价格已过期/)).not.toBeInTheDocument()
    expect(screen.getAllByText(/3300\.00/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/mark as of .* UTC/).length).toBeGreaterThan(0)
  })

  it('falls back to the persisted mark/updated_at when the resolved fields are absent', async () => {
    const legacy = positionFixture({ updated_at: STALE_MARK_AS_OF })
    delete legacy.resolved_mark_price
    delete legacy.resolved_mark_as_of
    delete legacy.resolved_mark_source
    await renderDetail({ pos: legacy })

    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/mark 价格已过期/)).toBeInTheDocument()
  })

  it('freezes position/history polling while paused and revalidates once on resume', async () => {
    const fetchPositionFn = vi.fn(async () => positionEnvelope(positionFixture()))
    const fetchHistoryFn = vi.fn(async () => historyEnvelope(historyFixture()))
    const props = detailProps({
      refreshMs: 20,
      paused: true,
      fetchPositionFn,
      fetchHistoryFn,
    })
    const { rerender } = render(<PositionDetailPage {...props} />)

    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    expect(await screen.findByTestId('position-detail-paused')).toHaveTextContent(/实时更新已暂停/)
    expect(screen.getByTestId('position-detail-paused')).toHaveAttribute('role', 'note')
    expect(fetchPositionFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchPositionFn).toHaveBeenCalledTimes(1)
    expect(fetchHistoryFn).toHaveBeenCalledTimes(1)

    fireEvent(window, new Event('focus'))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(fetchPositionFn).toHaveBeenCalledTimes(1)

    rerender(<PositionDetailPage {...props} refreshMs={60_000} paused={false} />)
    await waitFor(() => expect(fetchPositionFn).toHaveBeenCalledTimes(2))
  })

  it('renders the detail heading while loading', () => {
    render(
      <PositionDetailPage
        {...detailProps({
          fetchPositionFn: () => new Promise<PositionEnvelope<PositionData>>(() => undefined),
        })}
      />,
    )

    const heading = screen.getByRole('heading', { name: 'POS-20261104-018' })
    expect(heading).toHaveAttribute('id', 'position-detail-heading')
    expect(screen.getByText(/加载持仓/)).toBeInTheDocument()
  })

  it('does not let a late initial position failure replace a ready page from a poll', async () => {
    let rejectInitial: ((error: unknown) => void) | undefined
    let calls = 0
    const fetchPositionFn = vi.fn(() => {
      calls += 1
      if (calls === 1) {
        return new Promise<PositionEnvelope<PositionData>>((_resolve, reject) => {
          rejectInitial = reject
        })
      }
      return Promise.resolve(positionEnvelope(positionFixture()))
    })
    render(<PositionDetailPage {...detailProps({ fetchPositionFn, refreshMs: 10 })} />)

    await screen.findByRole('table', { name: /PnL 分解/ })
    await act(async () => {
      rejectInitial?.(new Error('late initial failure'))
    })

    expect(screen.getByRole('table', { name: /PnL 分解/ })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '持仓数据不可用' })).not.toBeInTheDocument()
    expect(screen.queryByText(/late initial failure/)).not.toBeInTheDocument()
  })

  it('renders the position with a history-unavailable panel when only history fails', async () => {
    await renderDetail({
      fetchHistoryFn: async () => {
        throw new PositionApiError('history service down', undefined, { httpStatus: 500 })
      },
    })

    expect(screen.getByTestId('history-unavailable')).toHaveTextContent(/history service down/)
    expect(screen.queryByText(/未找到该持仓/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /PnL 分解/ })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'POS-20261104-018' })).toBeInTheDocument()
  })

  it('shows a non-blocking history-refresh warning when a later history poll fails', async () => {
    let historyCalls = 0
    const fetchHistoryFn = vi.fn(async () => {
      historyCalls += 1
      if (historyCalls === 1) {
        return historyEnvelope(historyFixture())
      }
      throw new Error('history refresh down')
    })
    await renderDetail({ fetchHistoryFn, refreshMs: 60_000 })
    expect(screen.getByRole('heading', { name: /PnL 分解/ })).toBeInTheDocument()

    fireEvent(window, new Event('focus'))

    await waitFor(() => expect(screen.getByTestId('history-refresh-warning')).toBeInTheDocument())
    expect(screen.getByTestId('history-refresh-warning')).toHaveTextContent(/history refresh down/)
    expect(screen.queryByTestId('history-unavailable')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /PnL 分解/ })).toBeInTheDocument()
  })

  it('preserves the unknown-outcome warning when the kill switch auto-closes the dialog', async () => {
    let killSwitchActive = false
    await renderDetail({
      refreshMs: 60_000,
      killSwitchFn: async () => killSwitchEnvelope(killSwitchActive),
      requestManagementFn: async () => {
        throw new Error('network down')
      },
    })

    submitManagementRequest()
    const alert = await screen.findByTestId('manage-error')
    expect(alert).toHaveAttribute('data-kind', 'unknown')

    killSwitchActive = true
    fireEvent(window, new Event('focus'))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByTestId('manage-dialog-closed-reason')).toHaveTextContent(
      /Kill switch 已启用/,
    )
    const preserved = screen.getByTestId('manage-error')
    expect(preserved).toHaveAttribute('data-kind', 'unknown')
    expect(preserved).toHaveTextContent(/结果未知/)
  })

  it('reserves not-found for a definitive position 404', async () => {
    render(
      <PositionDetailPage
        {...detailProps({
          fetchPositionFn: async () => {
            throw new PositionApiError('position missing', 'NOT_FOUND', { httpStatus: 404 })
          },
        })}
      />,
    )

    expect(await screen.findByRole('heading', { name: '未找到该持仓' })).toBeInTheDocument()
    expect(screen.getByText(/position missing/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重试加载持仓/ })).not.toBeInTheDocument()
  })

  it('renders an explicit position-unavailable panel for a non-404 position failure', async () => {
    render(
      <PositionDetailPage
        {...detailProps({
          fetchPositionFn: async () => {
            throw new PositionApiError('position service down', undefined, { httpStatus: 500 })
          },
        })}
      />,
    )

    expect(await screen.findByRole('heading', { name: '持仓数据不可用' })).toBeInTheDocument()
    expect(screen.getByText(/position service down/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重试加载持仓/ })).toBeInTheDocument()
    expect(screen.queryByText(/未找到该持仓/)).not.toBeInTheDocument()
  })

  it('renders a PARTIAL position with a missing mark without fabricating PnL', async () => {
    await renderDetail({
      pos: positionFixture({
        mark_price: null,
        resolved_mark_price: null,
        resolved_mark_as_of: null,
        data_quality: { status: 'PARTIAL', reasons: ['mark missing'] },
      }),
    })

    expect(screen.getByText(/部分数据缺失/)).toBeInTheDocument()
    expect(screen.getByText(/mark 缺失/)).toBeInTheDocument()
    expect(screen.getByText(/mark 价格缺失/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('renders an executed management result with its order and fill ids', async () => {
    const executed = manageResult({
      action: 'PARTIAL_CLOSE',
      status: 'EXECUTED',
      executed: true,
      reason_code: 'PARTIAL_CLOSE',
      reason: 'reduced the position by half',
      order: { order_id: 'PO-9' },
      fill: { fill_id: 'PF-9' },
      risk: {
        verdict: 'ALLOW',
        binding_cap: 'PER_TRADE',
        stale_inputs: [],
        reasons: [],
      },
    })
    await renderDetail({ requestManagementFn: async () => manageEnvelope(executed) })

    submitManagementRequest()

    await waitFor(() => expect(screen.getByText(/已执行/)).toBeInTheDocument())
    expect(screen.getAllByText(/PO-9/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/PF-9/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/PER_TRADE/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/无订单/)).not.toBeInTheDocument()
  })

  it('shows the definitive refusal copy only for an allowlisted pre-execution rejection', async () => {
    await renderDetail({
      requestManagementFn: async () => {
        throw new PositionApiError('stop cannot loosen', 'RISK_BLOCKED', {
          httpStatus: 422,
        })
      },
    })

    submitManagementRequest()

    const alert = await screen.findByTestId('manage-error')
    expect(alert).toHaveAttribute('data-kind', 'refusal')
    expect(alert).toHaveTextContent(/未创建任何订单/)
    expect(alert).toHaveTextContent(/持仓未改变/)
    expect(alert).toHaveTextContent(/同一 Idempotency-Key 重试是安全的/)
    // A definitive pre-execution refusal clears the key for the next request.
    expect(storedManagementKey('POS-20261104-018')).toBeNull()
  })

  it('says the outcome is unknown, avoids the no-order claim, and re-polls after a transport failure', async () => {
    const fetchPositionFn = vi.fn(async () => positionEnvelope(positionFixture()))
    await renderDetail({
      refreshMs: 60_000,
      fetchPositionFn,
      requestManagementFn: async () => {
        throw new Error('network down')
      },
    })

    submitManagementRequest()

    const alert = await screen.findByTestId('manage-error')
    expect(alert).toHaveAttribute('data-kind', 'unknown')
    expect(alert).toHaveTextContent(/结果未知/)
    expect(alert).toHaveTextContent(/服务器可能已执行/)
    expect(alert).toHaveTextContent(/重放已存结果/)
    expect(alert).not.toHaveTextContent(/未创建任何订单/)
    // An unknown outcome triggers an immediate position/history re-poll.
    await waitFor(() => expect(fetchPositionFn.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('treats a 409 IDEMPOTENCY_CONFLICT as unknown, keeps the key, and re-polls', async () => {
    const fetchPositionFn = vi.fn(async () => positionEnvelope(positionFixture()))
    const keys: string[] = []
    let call = 0
    const requestManagementFn = vi.fn(async (_id: string, key: string) => {
      keys.push(key)
      call += 1
      if (call === 1) {
        throw new PositionApiError(
          'a request with this key is still running',
          'IDEMPOTENCY_CONFLICT',
          {
            httpStatus: 409,
          },
        )
      }
      return manageEnvelope(manageResult())
    })
    await renderDetail({ refreshMs: 60_000, fetchPositionFn, requestManagementFn })

    submitManagementRequest()

    const alert = await screen.findByTestId('manage-error')
    expect(alert).toHaveAttribute('data-kind', 'unknown')
    expect(alert).toHaveTextContent(/结果未知/)
    expect(alert).not.toHaveTextContent(/未创建任何订单/)
    // The key is kept: a retry must replay the in-flight/stored result.
    expect(storedManagementKey('POS-20261104-018')).toBe(keys[0])
    // An unknown 409 also triggers an immediate re-poll.
    await waitFor(() => expect(fetchPositionFn.mock.calls.length).toBeGreaterThanOrEqual(2))

    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))
    submitManagementRequest()
    await waitFor(() => expect(requestManagementFn).toHaveBeenCalledTimes(2))
    expect(keys[1]).toBe(keys[0])
  })

  it('treats a 409 CONFLICT raised after a committed transition as unknown and keeps the key', async () => {
    window.sessionStorage.setItem(
      `${MANAGEMENT_KEY_PREFIX}POS-20261104-018`,
      'conflict-persisted-key',
    )
    await renderDetail({
      refreshMs: 60_000,
      requestManagementFn: async () => {
        throw new PositionApiError('position closed while managing', 'CONFLICT', {
          httpStatus: 409,
        })
      },
    })

    submitManagementRequest()

    const alert = await screen.findByTestId('manage-error')
    expect(alert).toHaveAttribute('data-kind', 'unknown')
    expect(alert).toHaveTextContent(/服务器可能已执行/)
    expect(alert).not.toHaveTextContent(/未创建任何订单/)
    expect(storedManagementKey('POS-20261104-018')).toBe('conflict-persisted-key')
  })

  it('rotates the key after a 422 RISK_BLOCKED pre-execution refusal', async () => {
    const keys: string[] = []
    let call = 0
    const requestManagementFn = vi.fn(async (_id: string, key: string) => {
      keys.push(key)
      call += 1
      if (call === 1) {
        throw new PositionApiError('risk gate blocked the request', 'RISK_BLOCKED', {
          httpStatus: 422,
        })
      }
      return manageEnvelope(manageResult())
    })
    await renderDetail({ refreshMs: 60_000, requestManagementFn })

    submitManagementRequest()

    await waitFor(() =>
      expect(screen.getByTestId('manage-error')).toHaveAttribute('data-kind', 'refusal'),
    )
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))
    submitManagementRequest()

    await waitFor(() => expect(requestManagementFn).toHaveBeenCalledTimes(2))
    expect(keys[1]).not.toBe(keys[0])
  })

  it('surfaces a timeout and re-enables Cancel', async () => {
    await renderDetail({
      requestManagementFn: async () => {
        throw new PositionApiError('The position request timed out.', 'TIMEOUT', {
          kind: 'unknown',
        })
      },
    })

    const dialog = submitManagementRequest()

    await waitFor(() =>
      expect(within(dialog).getByTestId('manage-dialog-error')).toHaveTextContent(/timed out/),
    )
    expect(within(dialog).getByRole('button', { name: '取消' })).toBeEnabled()
  })

  it('does not open the dialog or send a request at 375px', async () => {
    const requestManagementFn = vi.fn(async () => manageEnvelope(manageResult()))
    await renderDetail({ isNarrow: true, requestManagementFn })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(requestManagementFn).not.toHaveBeenCalled()
  })

  it('does not stack a kill-switch resume dialog on an open management dialog', async () => {
    render(
      <div className="app-shell">
        <KillSwitchBar fetchStatusFn={async () => killSwitchEnvelope(true)} />
        <PositionDetailPage {...detailProps({ refreshMs: 60_000 })} />
      </div>,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ })

    fireEvent.click(await screen.findByRole('button', { name: /请求管理决策/ }))
    expect(screen.getByRole('dialog', { name: /请求管理决策/ })).toBeInTheDocument()

    const resume = document.querySelector<HTMLElement>('[data-kill-switch-action="resume"]')
    expect(resume).not.toBeNull()
    fireEvent.click(resume as HTMLElement)

    expect(document.querySelectorAll('[role="dialog"]').length).toBe(1)
    expect(screen.getByText(/另一个弹窗已打开/)).toBeInTheDocument()
    expect(screen.getByText(/请先关闭它/)).toBeInTheDocument()
  })

  it('uses the session-persisted key after a reload mid-flight', async () => {
    window.sessionStorage.setItem(
      `${MANAGEMENT_KEY_PREFIX}POS-20261104-018`,
      'persisted-reload-key',
    )
    const requestManagementFn = vi.fn(async () => manageEnvelope(manageResult()))
    await renderDetail({ requestManagementFn })

    submitManagementRequest()

    await waitFor(() => expect(requestManagementFn).toHaveBeenCalledTimes(1))
    const [, key] = requestManagementFn.mock.calls[0] as unknown as [string, string]
    expect(key).toBe('persisted-reload-key')
  })

  it('rotates the key after a definitive success and after a definitive refusal', async () => {
    const keys: string[] = []
    let call = 0
    const requestManagementFn = vi.fn(async (_id: string, key: string) => {
      keys.push(key)
      call += 1
      if (call === 1) {
        throw new PositionApiError('bad request', 'VALIDATION_FAILED', { httpStatus: 422 })
      }
      return manageEnvelope(manageResult())
    })
    await renderDetail({ requestManagementFn, refreshMs: 60_000 })

    // First request: a definitive refusal clears the key.
    submitManagementRequest()
    await waitFor(() => expect(screen.getByTestId('manage-error')).toBeInTheDocument())
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))

    // Second request: a definitive success; its key must differ.
    submitManagementRequest()
    await waitFor(() => expect(requestManagementFn).toHaveBeenCalledTimes(2))
    expect(keys[0]).not.toBe(keys[1])

    // Third request after the success: yet another fresh key.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    submitManagementRequest()
    await waitFor(() => expect(requestManagementFn).toHaveBeenCalledTimes(3))
    expect(new Set(keys).size).toBe(3)
  })

  it('re-gates at submit time when the resolved mark goes stale while the dialog is open', async () => {
    let stale = false
    const fetchPositionFn = vi.fn(async () =>
      positionEnvelope(
        positionFixture(
          stale
            ? { resolved_mark_as_of: STALE_MARK_AS_OF }
            : { resolved_mark_as_of: FRESH_MARK_AS_OF },
        ),
      ),
    )
    const requestManagementFn = vi.fn(async () => manageEnvelope(manageResult()))
    await renderDetail({ fetchPositionFn, requestManagementFn, refreshMs: 60_000 })

    fireEvent.click(screen.getByRole('button', { name: /请求管理决策/ }))
    expect(screen.getByRole('dialog', { name: /请求管理决策/ })).toBeInTheDocument()

    stale = true
    fireEvent(window, new Event('focus'))
    await waitFor(() => expect(screen.getByText(/mark 价格已过期/)).toBeInTheDocument())

    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: /确认请求管理/ }),
    )

    await waitFor(() =>
      expect(screen.getByTestId('manage-dialog-error')).toHaveTextContent(/mark 价格已过期/),
    )
    expect(requestManagementFn).not.toHaveBeenCalled()
  })
})

describe('positions API helpers', () => {
  it('computes signed CALC terms and refuses to fabricate a PnL without a mark', async () => {
    const helpers = await import('../../api/positions')
    const open = positionFixture()
    expect(helpers.sideSign('LONG')).toBe(1)
    expect(helpers.sideSign('SHORT')).toBe(-1)
    expect(helpers.pricePnl(open)).toBeCloseTo(588.98, 2)
    expect(helpers.netPnl(open)).toBeCloseTo(565.76, 2)
    expect(helpers.pnlPct(open)).toBeCloseTo(2.09, 2)
    expect(helpers.rMultiple(open)).toBeCloseTo(0.71, 2)
    expect(helpers.pricePnl({ ...open, mark_price: null, resolved_mark_price: null })).toBeNull()
    expect(helpers.netPnl({ ...open, mark_price: null, resolved_mark_price: null })).toBeNull()
    expect(helpers.markState(open, NOW)).toBe('FRESH')
    expect(helpers.markState({ ...open, resolved_mark_as_of: STALE_MARK_AS_OF }, NOW)).toBe('STALE')
    expect(
      helpers.markState(
        { ...open, mark_price: null, resolved_mark_price: null, resolved_mark_as_of: null },
        NOW,
      ),
    ).toBe('MISSING')
  })

  it('derives freshness and PnL from the resolved mark with a documented fallback', async () => {
    const helpers = await import('../../api/positions')
    const freshResolved = positionFixture({
      updated_at: '2026-11-06T09:00:00.000Z',
      resolved_mark_price: '3290.00',
      resolved_mark_as_of: FRESH_MARK_AS_OF,
      resolved_mark_source: 'BARS',
    })
    expect(helpers.resolvedMark(freshResolved)).toBe('3290.00')
    expect(helpers.markAsOf(freshResolved)).toBe(FRESH_MARK_AS_OF)
    expect(helpers.markState(freshResolved, NOW)).toBe('FRESH')
    // PnL uses the resolved mark, not the stored one.
    expect(helpers.pricePnl(freshResolved)).toBeCloseTo((3290 - 3214.6) * 8.42, 2)

    // Older payloads without the additive fields fall back to mark_price/updated_at.
    const legacy = positionFixture()
    delete legacy.resolved_mark_price
    delete legacy.resolved_mark_as_of
    delete legacy.resolved_mark_source
    expect(helpers.resolvedMark(legacy)).toBe(legacy.mark_price)
    expect(helpers.markAsOf(legacy)).toBe(legacy.updated_at)
    expect(helpers.markState(legacy, NOW)).toBe('FRESH')
    expect(helpers.markState({ ...legacy, updated_at: STALE_MARK_AS_OF }, NOW)).toBe('STALE')
  })

  it('adds the accumulated gross to the net-if-closed term and never fabricates an unmeasured one', async () => {
    const helpers = await import('../../api/positions')
    const withRealized = positionFixture({ realized_pnl_usd: '120.00' })
    expect(helpers.accumulatedGross(withRealized)).toBe(120)
    expect(helpers.netPnl(withRealized)).toBeCloseTo(588.98 + 120 - 12.44 - 10.78, 2)

    const closedWithoutMark = positionFixture({
      state: 'CLOSED',
      realized_pnl_usd: null,
    })
    expect(helpers.accumulatedGross(closedWithoutMark)).toBeNull()

    const partialUnmeasured = positionFixture({
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
    })
    expect(helpers.accumulatedGross(partialUnmeasured)).toBeNull()
    expect(helpers.netPnl(partialUnmeasured)).toBeNull()
  })

  it('posts one JSON management request with CSRF and Idempotency-Key headers', async () => {
    const fetchMock = vi.fn<
      (
        input: string,
        init?: RequestInit,
      ) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>
    >(async () => ({
      ok: true,
      status: 200,
      json: async () => manageEnvelope(manageResult()),
    }))
    vi.stubGlobal('fetch', fetchMock)
    document.cookie = 'oft_csrf=csrf-token'

    await requestManagement('POS-20261104-018', 'manage-key-1')

    const [input, init] = fetchMock.mock.calls[0]
    expect(input).toBe('/api/v1/positions/POS-20261104-018/manage')
    expect((init as RequestInit).method).toBe('POST')
    expect((init as RequestInit).body).toBe('{}')
    expect((init as RequestInit).credentials).toBe('same-origin')
    expect((init as RequestInit).headers).toMatchObject({
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-CSRF-Token': 'csrf-token',
      'Idempotency-Key': 'manage-key-1',
    })

    document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    vi.unstubAllGlobals()
  })

  it('generates a fresh, prefixed key for every call', () => {
    const key = managementIdempotencyKey('web-manage-POS-1')
    expect(key.startsWith('web-manage-POS-1-')).toBe(true)
    expect(managementIdempotencyKey('web-manage-POS-1')).not.toBe(key)
  })

  it('persists one management key per position in sessionStorage until cleared', () => {
    window.sessionStorage.clear()
    clearManagementKey('POS-A')
    clearManagementKey('POS-B')

    const first = managementKeyFor('POS-A')
    expect(first.startsWith('web-manage-POS-A-')).toBe(true)
    expect(window.sessionStorage.getItem(`${MANAGEMENT_KEY_PREFIX}POS-A`)).toBe(first)
    expect(storedManagementKey('POS-A')).toBe(first)
    // Repeated reads reuse the stored key.
    expect(managementKeyFor('POS-A')).toBe(first)

    // A different position is isolated.
    const other = managementKeyFor('POS-B')
    expect(other).not.toBe(first)
    expect(storedManagementKey('POS-A')).toBe(first)

    // A deliberate rotation replaces only that position's key.
    const rotated = rotateManagementKey('POS-A')
    expect(rotated).not.toBe(first)
    expect(storedManagementKey('POS-A')).toBe(rotated)
    expect(storedManagementKey('POS-B')).toBe(other)

    // A definitive result clears it; the next request creates a fresh key.
    clearManagementKey('POS-A')
    expect(storedManagementKey('POS-A')).toBeNull()
    expect(window.sessionStorage.getItem(`${MANAGEMENT_KEY_PREFIX}POS-A`)).toBeNull()
    const next = managementKeyFor('POS-A')
    expect(next).not.toBe(rotated)
  })

  it('fails a hung request with a typed TIMEOUT error instead of a silent AbortError', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(
      (_input: string, init?: RequestInit) =>
        new Promise<never>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
          })
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const pending = requestManagement('POS-20261104-018', 'timeout-key')
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'PositionApiError',
      code: 'TIMEOUT',
      kind: 'unknown',
    })
    await vi.advanceTimersByTimeAsync(MANAGE_TIMEOUT_MS)
    await assertion

    expect(fetchMock).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('sends the mutation without X-CSRF-Token when no cookie is present', async () => {
    const fetchMock = vi.fn<
      (
        input: string,
        init?: RequestInit,
      ) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>
    >(async () => ({
      ok: true,
      status: 200,
      json: async () => manageEnvelope(manageResult()),
    }))
    vi.stubGlobal('fetch', fetchMock)
    document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'

    await requestManagement('POS-20261104-018', 'no-csrf-key')

    const [, init] = fetchMock.mock.calls[0]
    const headers = (init as RequestInit).headers as Record<string, string>
    expect(headers['Idempotency-Key']).toBe('no-csrf-key')
    expect(headers['X-CSRF-Token']).toBeUndefined()
    vi.unstubAllGlobals()
  })

  it('classifies management failures by the envelope code, not the HTTP status range', async () => {
    const envelopeFor = (code: string, httpStatus: number) => ({
      ok: false,
      data: null,
      error: {
        code,
        message: 'failure',
        category: 'VALIDATION',
        http_status: httpStatus,
        retryable: false,
        reference_id: 'REQ-1',
        details: [],
      },
      meta: null,
    })

    expect(DEFINITIVE_MANAGE_REFUSAL_CODES.has('RISK_BLOCKED')).toBe(true)
    expect(DEFINITIVE_MANAGE_REFUSAL_CODES.has('IDEMPOTENCY_CONFLICT')).toBe(false)
    expect(DEFINITIVE_MANAGE_REFUSAL_CODES.has('CONFLICT')).toBe(false)

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 422,
        json: async () => envelopeFor('RISK_BLOCKED', 422),
      })),
    )
    await expect(requestManagement('POS-1', 'k')).rejects.toMatchObject({
      kind: 'refusal',
      httpStatus: 422,
      code: 'RISK_BLOCKED',
    })
    vi.unstubAllGlobals()

    for (const conflictCode of ['IDEMPOTENCY_CONFLICT', 'CONFLICT']) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => ({
          ok: false,
          status: 409,
          json: async () => envelopeFor(conflictCode, 409),
        })),
      )
      await expect(requestManagement('POS-1', 'k')).rejects.toMatchObject({
        kind: 'unknown',
        httpStatus: 409,
        code: conflictCode,
      })
      vi.unstubAllGlobals()
    }

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ ...envelopeFor('DATA_UNAVAILABLE', 503), error: null }),
      })),
    )
    await expect(requestManagement('POS-1', 'k')).rejects.toMatchObject({
      kind: 'unknown',
      httpStatus: 503,
    })
    vi.unstubAllGlobals()
  })
})
