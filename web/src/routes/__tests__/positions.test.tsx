import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import type { OpportunityEnvelope } from '../../api/opportunities'
import type { ReportData, ReportEnvelope } from '../../api/reports'
import type { KillSwitchEnvelope } from '../../api/paper'
import type { TriggerDetailEnvelope } from '../../api/triggers'
import type {
  PositionData,
  PositionEnvelope,
  PositionHistoryData,
  PositionHistoryEnvelope,
  PositionListEnvelope,
  PositionManageData,
  PositionManageEnvelope,
} from '../../api/positions'
import { managementIdempotencyKey, requestManagement } from '../../api/positions'
import { KillSwitchBar } from '../../components/kill-switch'
import { PositionDetailPage } from '../position-detail'
import { PositionsPage } from '../positions'
import { resolveRoute } from '../route-match'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

export const NOW = Date.parse('2026-11-06T12:00:00.000Z')

export function position(overrides: Partial<PositionData> = {}): PositionData {
  return {
    position_id: 'POS-20261104-018',
    account_id: 'ACC-PAPER',
    environment: 'PAPER',
    symbol: 'ETHUSDT',
    side: 'LONG',
    state: 'OPEN',
    size: '8.42',
    entry_price: '3214.60',
    avg_entry_price: '3214.60',
    mark_price: '3284.55',
    notional_usd: '27066.93',
    unrealized_pnl_usd: null,
    unrealized_pnl_pct: null,
    stop_price: '3118.40',
    target_prices: ['3420.00'],
    leverage: 5,
    margin_mode: 'ISOLATED',
    margin_usd: '5413.39',
    risk_amount_usd: '796.80',
    risk_pct: 0.8,
    fees_usd: '12.44',
    funding_usd: '-10.78',
    realized_pnl_usd: null,
    opened_at: '2026-11-04T06:00:00.000Z',
    updated_at: '2026-11-06T11:59:50.000Z',
    closed_at: null,
    exit_price: null,
    source_trigger_id: 'TRG-1',
    source_decision_id: 'DEC-1',
    management_history: [],
    data_quality: { status: 'COMPLETE', reasons: [] },
    ...overrides,
  }
}

export function history(overrides: Partial<PositionHistoryData> = {}): PositionHistoryData {
  return {
    position_id: 'POS-20261104-018',
    symbol: 'ETHUSDT',
    side: 'LONG',
    state: 'OPEN',
    size: '8.42',
    entry_price: '3214.60',
    avg_entry_price: '3214.60',
    events: [
      {
        seq: 1,
        at: '2026-11-04T06:00:00.000Z',
        action: 'OPEN',
        order_id: 'PO-1',
        note: 'entry fill',
      },
    ],
    funding: [
      {
        accrual_id: 'FND-1',
        settlement_time: '2026-11-06T00:00:00.000Z',
        rate: '0.0001',
        mark_price: '3250.00',
        amount_usd: '-10.78',
        side: 'LONG',
        size: '8.42',
        created_at: '2026-11-06T00:00:01.000Z',
        accrued: true,
        reason: null,
      },
    ],
    funding_reasons: [],
    pnl: {
      basis: 'ACCUMULATED_GROSS',
      gross_pnl_usd: '0.00',
      realized_gross_pnl_usd: '0.00',
      fees_usd: '12.44',
      funding_usd: '-10.78',
      net_pnl_usd: null,
      r_multiple: null,
      return_pct: null,
      mae_usd: null,
      mfe_usd: null,
      outcome_tags: [],
      data_quality: { status: 'COMPLETE', reasons: [] },
    },
    ...overrides,
  }
}

function listEnvelope(items: PositionData[]): PositionListEnvelope {
  return {
    ok: true,
    data: {
      items,
      total: items.length,
      counts: { OPEN: items.length },
    },
    error: null,
    meta,
  }
}

function positionEnvelope(data: PositionData): PositionEnvelope<PositionData> {
  return { ok: true, data, error: null, meta }
}

function historyEnvelope(data: PositionHistoryData): PositionHistoryEnvelope {
  return { ok: true, data, error: null, meta }
}

function triggerEnvelope(): TriggerDetailEnvelope {
  return {
    ok: true,
    data: {
      trigger_id: 'TRG-1',
      opportunity_id: 'OPP-1',
      symbol: 'ETHUSDT',
      side: 'LONG',
      status: 'ACCEPTED',
      triggered_at: '2026-11-04T05:55:00.000Z',
      trigger_source: 'CALC',
      idempotency_key: 'trg-key',
      created_order_id: 'PO-1',
      schema_version: '1.0.0',
      environment: 'PAPER',
      condition_results: [],
      snapshot: {
        market_snapshot_id: 'SNP-1',
        snapshot_hash: 'a'.repeat(64),
        as_of: '2026-11-04T05:55:00.000Z',
        data_quality: 'COMPLETE',
      },
      entry_decision_id: 'DEC-1',
      risk_verdict: {
        verdict: 'ALLOW',
        requested: {
          entry_price: '3214.60',
          stop_price: '3118.40',
          quantity: '8.42',
          risk_pct: 0.8,
          leverage: 5,
          notional_usd: '27066.93',
        },
        allowed: {
          quantity: '8.42',
          notional_usd: '27066.93',
          leverage: 5,
          risk_amount_usd: '796.80',
        },
        binding_cap: 'PER_TRADE',
        reasons: [],
        stale_inputs: [],
      },
      human_action: null,
      rejection_reason: null,
    },
    error: null,
    meta,
  }
}

function opportunityEnvelope(): OpportunityEnvelope {
  return {
    ok: true,
    data: {
      schema_version: '1.0.0',
      opportunity_id: 'OPP-1',
      symbol: 'ETHUSDT',
      side: 'LONG',
      state: 'TRIGGERED',
      created_at: '2026-11-04T05:50:00.000Z',
      updated_at: '2026-11-04T05:55:00.000Z',
      zone: { low: '3200.00', high: '3220.00' },
      entry_preference: 'LIMIT',
      trigger_conditions: [],
      invalidation_conditions: [],
      expires_at: '2026-11-05T00:00:00.000Z',
      thesis: 'trend continuation',
      confidence: 0.72,
      risk_request: {
        entry_price: '3214.60',
        stop_price: '3118.40',
        quantity: '8.42',
        risk_pct: 0.8,
        leverage: 5,
        notional_usd: '27066.93',
      },
      source: { scan_id: 'SCN-1', report_id: 'RPT-1', decision_id: 'DEC-1' },
      provenance: {
        llm: { provider: 'stub', model: 'fixture' },
        calc_snapshot_id: 'SNP-1',
        calc_snapshot_hash: 'b'.repeat(64),
        calc_metric_version: 'orderflow.v1',
      },
      data_quality: {
        status: 'COMPLETE',
        reasons: [],
        gaps: [],
        last_event_at: null,
        coverage_start: null,
      },
      state_history: [],
    },
    error: null,
    meta,
  }
}

function reportEnvelope(): ReportEnvelope {
  const data = {
    report_id: 'RPT-1',
    scan_id: 'SCN-1',
    symbol: 'ETHUSDT',
    timeframe: '4h',
    generated_at: '2026-11-04T05:50:00.000Z',
    data_quality: 'COMPLETE',
    llm_decision_id: 'DEC-1',
    narrative_ref: null,
    llm_stage: 'VALID',
    llm_tokens: 0,
    cost_usd: '0',
    cost_unpriced: false,
    metrics_manifest: [],
    snapshot: {
      bars: [
        {
          bar_start: '2026-11-04T04:00:00.000Z',
          bar_end: '2026-11-04T08:00:00.000Z',
          open: '3200.00',
          high: '3300.00',
          low: '3190.00',
          close: '3284.55',
          volume: '10',
          quote_volume: '32845.50',
          trade_count: 3,
          taker_buy_volume: '6',
          delta: '2',
          cvd: '2',
          open_interest: null,
          funding_rate: null,
        },
      ],
    },
    metrics: {
      schema_version: '1.0.0',
      symbol: 'ETHUSDT',
      period: '4h',
      window: { start: '2026-11-04T04:00:00.000Z', end: '2026-11-04T08:00:00.000Z' },
      computed_at: '2026-11-04T08:00:00.000Z',
      metric_version: 'orderflow.v1',
      input_manifest_hash: 'c'.repeat(64),
      data_quality: 'COMPLETE',
      quality_reasons: [],
      cvd: {
        unit: 'BASE',
        points: [],
        summary: { delta: '0', cvd_change: '0', buy_volume: '0', sell_volume: '0' },
        divergence_flags: [],
      },
      footprint: { price_bucket: '1', imbalance_threshold: 3, bars: [], top_imbalances: [] },
      volume_profile: {
        window: { start: '2026-11-04T04:00:00.000Z', end: '2026-11-04T08:00:00.000Z' },
        bin_size: '1',
        value_area_pct: 70,
        poc: '3284.55',
        vah: '3300.00',
        val: '3200.00',
        bins: [],
        notes: [],
      },
      open_interest: {
        unit: 'BASE',
        sampling: 'REGULAR',
        points: [],
        delta_over_window: '0',
        last: { ts: '2026-11-04T08:00:00.000Z', open_interest: '0', open_interest_value: '0' },
      },
      funding: {
        current_rate: '0',
        next_funding_time: '2026-11-04T16:00:00.000Z',
        interval_hours: 8,
        mark_price: '3284.55',
        index_price: '3284.55',
        history: [],
      },
      liquidations: {
        coverage: 'NONE',
        coverage_start: null,
        coverage_end: null,
        total_long_notional: '0',
        total_short_notional: '0',
        count: 0,
        buckets: [],
        limitations: [],
      },
      sources: [],
      gaps: [],
    },
  }
  return { ok: true, data: data as unknown as ReportData, error: null, meta }
}

function killSwitchEnvelope(active: boolean): KillSwitchEnvelope {
  return {
    ok: true,
    data: { active, sentinel_path: '/tmp/kill', checked_at: '2026-11-06T12:00:00.000Z' },
    error: null,
    meta,
  }
}

export function manageResult(overrides: Partial<PositionManageData> = {}): PositionManageData {
  return {
    position_id: 'POS-20261104-018',
    decision_id: 'MGD-1',
    action: 'HOLD',
    status: 'NO_ACTION',
    executed: false,
    reason_code: 'HOLD',
    reason: 'the management decision was HOLD; no order was created',
    llm: {
      decision_id: 'MGD-1',
      action: 'HOLD',
      validation_status: 'VALID',
      confidence: 0.64,
      rationale: '趋势未破坏，建议继续持有。',
      urgency: 'LOW',
      provenance: {
        provider: 'stub',
        model: 'fixture',
        prompt_version: 2,
        knowledge_artifact_hash: null,
        cost: { amount_usd: '0', currency: 'USD', unpriced: true },
      },
    },
    risk: null,
    order: null,
    fill: null,
    position: position(),
    trade_log: null,
    account: null,
    refusals: [],
    idempotent_replay: false,
    due_conditions: [],
    ...overrides,
  }
}

function manageEnvelope(data: PositionManageData): PositionManageEnvelope {
  return { ok: true, data, error: null, meta }
}

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
        options.fetchPositionsFn ?? (async () => listEnvelope(options.items ?? [position()]))
      }
    />,
  )
  return screen.findByRole('heading', { name: /未平仓持仓/ })
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
        options.fetchPositionFn ?? (async () => positionEnvelope(options.pos ?? position()))
      }
      fetchHistoryFn={
        options.fetchHistoryFn ?? (async () => historyEnvelope(options.hist ?? history()))
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
  return screen.findByRole('heading', { name: 'POS-20261104-018' })
}

afterEach(() => {
  vi.restoreAllMocks()
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
        position({ updated_at: '2026-11-06T10:00:00.000Z' }),
        position({
          position_id: 'POS-2',
          symbol: 'XAUUSDT',
          mark_price: null,
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
      items: [position(), position({ position_id: 'POS-2', symbol: 'XAUUSDT', mark_price: null })],
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
    const fetchPositionsFn = vi.fn(async () => listEnvelope([position()]))
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
        return listEnvelope([position()])
      }
      throw new Error('refresh down')
    })
    render(<PositionsPage now={NOW} refreshMs={20} fetchPositionsFn={fetchPositionsFn} />)

    await screen.findByRole('heading', { name: /未平仓持仓/ })
    await waitFor(() => expect(screen.getByTestId('refresh-warning')).toBeInTheDocument())
    expect(screen.getByRole('table', { name: /未平仓持仓/ })).toBeInTheDocument()
    expect(screen.getAllByText('+565.76').length).toBeGreaterThan(0)
  })
})

describe('PositionDetailPage', () => {
  it('shows the PnL breakdown with an explicit basis and no fabricated terms', async () => {
    await renderDetail()

    expect(screen.getByRole('heading', { name: /PnL 分解/ })).toBeInTheDocument()
    const table = screen.getByRole('table', { name: /PnL 分解/ })
    expect(within(table).getByText('Price PnL')).toBeInTheDocument()
    expect(within(table).getByText('+588.98')).toBeInTheDocument()
    expect(within(table).getByText('Fees')).toBeInTheDocument()
    expect(within(table).getByText('-12.44')).toBeInTheDocument()
    expect(within(table).getByText('Funding')).toBeInTheDocument()
    expect(within(table).getByText('-10.78')).toBeInTheDocument()
    expect(within(table).getByText('Net uPnL')).toBeInTheDocument()
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
      pos: position({ source_trigger_id: null }),
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
      pos: position({
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
        fetchPositionFn={async () => positionEnvelope(position())}
        fetchHistoryFn={async () => historyEnvelope(history())}
        fetchTriggerFn={async () => triggerEnvelope()}
        fetchOpportunityFn={async () => opportunityEnvelope()}
        fetchReportFn={async () => reportEnvelope()}
        fetchKillSwitchFn={async () => killSwitchEnvelope(true)}
        requestManagementFn={async () => manageEnvelope(manageResult())}
      />,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
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
          positionEnvelope(position({ updated_at: '2026-11-06T10:00:00.000Z' }))
        }
        fetchHistoryFn={async () => historyEnvelope(history())}
        fetchTriggerFn={async () => triggerEnvelope()}
        fetchOpportunityFn={async () => opportunityEnvelope()}
        fetchReportFn={async () => reportEnvelope()}
        fetchKillSwitchFn={async () => killSwitchEnvelope(false)}
      />,
    )
    await screen.findAllByRole('heading', { name: 'POS-20261104-018' })
    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
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
        fetchPositionFn={async () => positionEnvelope(position({ source_trigger_id: null }))}
        fetchHistoryFn={async () => historyEnvelope(history())}
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

    expect(screen.getByText(/入场 risk verdict 不可用/)).toBeInTheDocument()
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
    await renderDetail({ pos: position({ state: 'CLOSED' }) })

    expect(screen.getByRole('button', { name: /请求管理决策/ })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByText(/持仓状态为 CLOSED/)).toBeInTheDocument()
  })

  it('shows STALE without the PARTIAL panel for a stale-but-COMPLETE position', async () => {
    await renderDetail({ pos: position({ updated_at: '2026-11-06T10:00:00.000Z' }) })

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
          refreshMs={20}
          fetchPositionFn={async () => positionEnvelope(position())}
          fetchHistoryFn={async () => historyEnvelope(history())}
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
    const trigger = screen.getByRole('button', { name: /请求管理决策/ })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: /请求管理决策/ })
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus())
    await waitFor(() => expect(screen.getByText(/Kill switch 已启用/)).toBeInTheDocument())

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('heading', { name: 'POS-20261104-018' })).toHaveFocus()
  })

  it('appends the global kill switch to the dialog tab cycle and leaves it non-inert', async () => {
    render(
      <div className="app-shell">
        <KillSwitchBar fetchStatusFn={async () => killSwitchEnvelope(false)} />
        <PositionDetailPage
          positionId="POS-20261104-018"
          now={NOW}
          fetchPositionFn={async () => positionEnvelope(position())}
          fetchHistoryFn={async () => historyEnvelope(history())}
          fetchTriggerFn={async () => triggerEnvelope()}
          fetchOpportunityFn={async () => opportunityEnvelope()}
          fetchReportFn={async () => reportEnvelope()}
          fetchKillSwitchFn={async () => killSwitchEnvelope(false)}
        />
      </div>,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    const trigger = screen.getByRole('button', { name: /请求管理决策/ })
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
        return Promise.resolve(positionEnvelope(position({ version: 2 })))
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
          manageResult({ position: position({ version: 3, size: '4.21', state: 'OPEN' }) }),
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
      resolvePoll?.(positionEnvelope(position({ version: 2, size: '8.42' })))
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
          fetchPositionFn={async () => positionEnvelope(position())}
          fetchHistoryFn={async () => historyEnvelope(history())}
          fetchTriggerFn={async () => triggerEnvelope()}
          fetchOpportunityFn={async () => opportunityEnvelope()}
          fetchReportFn={async () => reportEnvelope()}
          fetchKillSwitchFn={async () => killSwitchEnvelope(killSwitchActive)}
        />
      </div>,
    )
    await screen.findByRole('heading', { name: 'POS-20261104-018' })
    await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ })
    const trigger = screen.getByRole('button', { name: /请求管理决策/ })
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
})

describe('positions API helpers', () => {
  it('computes signed CALC terms and refuses to fabricate a PnL without a mark', async () => {
    const helpers = await import('../../api/positions')
    const open = position()
    expect(helpers.sideSign('LONG')).toBe(1)
    expect(helpers.sideSign('SHORT')).toBe(-1)
    expect(helpers.pricePnl(open)).toBeCloseTo(588.98, 2)
    expect(helpers.netPnl(open)).toBeCloseTo(565.76, 2)
    expect(helpers.pnlPct(open)).toBeCloseTo(2.09, 2)
    expect(helpers.rMultiple(open)).toBeCloseTo(0.71, 2)
    expect(helpers.pricePnl({ ...open, mark_price: null })).toBeNull()
    expect(helpers.netPnl({ ...open, mark_price: null })).toBeNull()
    expect(helpers.markState(open, NOW)).toBe('FRESH')
    expect(helpers.markState({ ...open, updated_at: '2026-11-06T10:00:00.000Z' }, NOW)).toBe(
      'STALE',
    )
    expect(helpers.markState({ ...open, mark_price: null }, NOW)).toBe('MISSING')
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

  it('produces stable, prefixed idempotency keys', () => {
    const key = managementIdempotencyKey('web-manage-POS-1')
    expect(key.startsWith('web-manage-POS-1-')).toBe(true)
    expect(managementIdempotencyKey('web-manage-POS-1')).not.toBe(key)
  })
})
