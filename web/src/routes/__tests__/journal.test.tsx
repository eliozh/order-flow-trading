import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearReviewKey,
  downloadJournalExport,
  durationLabel,
  exportFilename,
  fetchJournalReview,
  isJournalTradeData,
  JournalApiError,
  journalExportFileUrl,
  outcomeOf,
  requestJournalReview,
  REVIEW_KEY_PREFIX,
  REVIEW_TIMEOUT_MS,
  reviewKeyFor,
  rLabel,
  rotateReviewKey,
  storedReviewKey,
  type EntryDecisionData,
  type JournalCompareData,
  type JournalEnvelope,
  type JournalReviewEnvelope,
  type JournalStatsData,
  type JournalTradeData,
  type TradeLogListData,
  type TradeLogSummary,
} from '../../api/journal'
import type { PositionData, PositionListEnvelope } from '../../api/positions'
import type { ApiMeta } from '../../api/status'
import {
  historyEnvelope,
  historyFixture,
  meta,
  opportunityEnvelope,
  positionEnvelope,
  positionFixture,
  triggerEnvelope,
} from '../../test/positions-fixtures'
import { JournalDetailPage } from '../journal-detail'
import { JournalPage } from '../journal'

const TRADE_IDS = ['TRD-1', 'TRD-A', 'TRD-B']

function envelope<T>(data: T, overrides: Partial<ApiMeta> = {}): JournalEnvelope<T> {
  return { ok: true, data, error: null, meta: { ...meta, ...overrides } }
}

function summaryFixture(overrides: Partial<TradeLogSummary> = {}): TradeLogSummary {
  return {
    trade_id: 'TRD-1',
    position_id: 'POS-20261104-018',
    symbol: 'BTCUSDT',
    side: 'LONG',
    opened_at: '2026-10-28T00:00:00.000Z',
    closed_at: '2026-10-30T08:16:02.000Z',
    gross_pnl_usd: '500.00',
    fees_usd: '10.00',
    funding_usd: '-7.84',
    net_pnl_usd: '482.16',
    return_pct: 1.42,
    r_multiple: 1.42,
    mae_usd: '0',
    mfe_usd: '0',
    outcome_tags: ['WIN'],
    data_quality: { status: 'COMPLETE', reasons: [] },
    ...overrides,
  }
}

function statsFixture(overrides: Partial<JournalStatsData> = {}): JournalStatsData {
  return {
    status: 'DEFINED',
    reason: null,
    period: {
      label: '8w',
      start: '2026-09-11T00:00:00.000Z',
      end: '2026-11-06T00:00:00.000Z',
      basis: 'closed_at',
    },
    sample_size: 2,
    denominator: 2,
    exclusions: {
      open_positions: 1,
      partial_trades: 2,
      unreadable_trades: 1,
      outside_period: 3,
    },
    definitions: {
      win_rate: 'closed trades with net PnL > 0 ÷ closed trades',
      r_multiple: 'net PnL ÷ initial risk',
    },
    wins: 1,
    losses: 1,
    ties: 0,
    win_rate: 0.5,
    expectancy_usd: '200.00',
    avg_r: { value: 0.7, samples: 2, excluded_null_r: 0 },
    totals: {
      gross_pnl_usd: '500.00',
      fees_usd: '10.00',
      funding_usd: '-7.84',
      net_pnl_usd: '482.16',
    },
    data_quality: 'COMPLETE',
    ...overrides,
  }
}

function undefinedStats(): JournalStatsData {
  return statsFixture({
    status: 'UNDEFINED',
    reason: 'no eligible closed trades',
    period: null,
    sample_size: 0,
    denominator: 0,
    exclusions: {
      open_positions: 0,
      partial_trades: 0,
      unreadable_trades: 0,
      outside_period: 0,
    },
    wins: 0,
    losses: 0,
    ties: 0,
    win_rate: null,
    expectancy_usd: null,
    avg_r: { value: null, samples: 0, excluded_null_r: 0 },
    totals: {
      gross_pnl_usd: null,
      fees_usd: null,
      funding_usd: null,
      net_pnl_usd: null,
    },
    data_quality: 'MISSING',
  })
}

function missingReviewEnvelope(): JournalReviewEnvelope {
  return envelope({
    trade_id: 'TRD-1',
    decision_id: null,
    status: 'MISSING' as const,
    advisory: true,
    rationale: null,
    caveats: [],
    confidence: null,
    validation_status: null,
    provenance: null,
    reason: 'no advisory review is linked to this trade',
    idempotent_replay: false,
  })
}

function foundReviewEnvelope(): JournalReviewEnvelope {
  return envelope({
    trade_id: 'TRD-1',
    decision_id: 'REV-9',
    status: 'FOUND' as const,
    advisory: true,
    rationale: '入场与信号一致。',
    caveats: [],
    confidence: 0.62,
    validation_status: 'VALID',
    provenance: { provider: 'stub', model: 'fixture', prompt_id: 'review', prompt_version: 1 },
    reason: null,
    idempotent_replay: false,
  })
}

function tradeFixture(overrides: Partial<JournalTradeData> = {}): JournalTradeData {
  return {
    schema_version: '1.0.0',
    trade_id: 'TRD-1',
    position_id: 'POS-20261104-018',
    account_id: 'ACC-PAPER',
    environment: 'PAPER',
    symbol: 'BTCUSDT',
    side: 'LONG',
    opened_at: '2026-10-28T00:00:00.000Z',
    closed_at: '2026-10-30T08:16:02.000Z',
    entry_avg_price: '139.82',
    exit_avg_price: '152.00',
    size: '38.0',
    gross_pnl_usd: '500.00',
    fees_usd: '10.00',
    funding_usd: '-7.84',
    net_pnl_usd: '482.16',
    r_multiple: 1.42,
    mae_usd: '0',
    mfe_usd: '0',
    return_pct: 1.42,
    signal: {
      scan_id: 'SCAN-1',
      report_id: 'RPT-1',
      opportunity_id: 'OPP-1',
      trigger_id: 'TRG-1',
      decision_id: 'DEC-ENTRY-1',
    },
    decision_quality: { matched_rules: true, confidence: 0.68, notes: '按规则执行' },
    outcome_tags: ['WIN'],
    review_decision_id: null,
    data_quality: { status: 'COMPLETE', reasons: [] },
    provenance: {
      calc_metric_version: '1',
      prompt_version: 1,
      llm_provider: 'stub',
      llm_model: 'fixture',
    },
    ...overrides,
  }
}

function compareFixture(overrides: Partial<JournalCompareData> = {}): JournalCompareData {
  return {
    trade_id: 'TRD-1',
    position_id: 'POS-20261104-018',
    symbol: 'BTCUSDT',
    side: 'LONG',
    opened_at: '2026-10-28T00:00:00.000Z',
    closed_at: '2026-10-30T08:16:02.000Z',
    net_pnl_usd: '482.16',
    zone: {
      status: 'INSIDE',
      reasons: [],
      entry: '139.82',
      zone_low: '138.20',
      zone_high: '139.80',
      inside: true,
      distance_bps: 1.4,
    },
    slippage: {
      status: 'OK',
      reasons: [],
      entry_avg_price: '139.82',
      reference_price: '139.80',
      reference_as_of: '2026-10-28T04:14:02.000Z',
      bps: 1.4,
      formula: 'trigger_to_fill_bps',
    },
    first_touch: {
      status: 'OK',
      reasons: [],
      kind: 'TARGET',
      at: '2026-10-30T08:00:00.000Z',
      ambiguous: false,
    },
    decision: {
      status: 'OK',
      reasons: [],
      decision: 'ENTER_LONG',
      direction_agrees: true,
      matched: true,
      net_pnl_usd: '482.16',
      decision_id: 'DEC-ENTRY-1',
    },
    reasons: [],
    data_quality: 'COMPLETE',
    ...overrides,
  }
}

function entryDecisionFixture(overrides: Partial<EntryDecisionData> = {}): EntryDecisionData {
  return {
    trigger_id: 'TRG-1',
    entry_decision_id: 'DEC-ENTRY-1',
    status: 'FOUND',
    decision: {
      confidence: 0.68,
      rationale: '价格突破上沿后回踩未跌回价值区，CVD 转正。',
      validation_status: 'VALID',
      persisted_at: '2026-10-28T04:14:07.100Z',
      entry_recheck: {
        decision: 'ENTER_LONG',
        reasons: ['trigger confirmed'],
        invalidation_conditions: [],
      },
      provenance: {
        provider: 'stub',
        model: 'fixture',
        prompt_id: 'entry_recheck',
        prompt_version: 2,
        cost: { amount_usd: '0.0100', currency: 'USD' },
        usage: { total_tokens: 200 },
        knowledge_artifact_hash: 'knowledge-hash',
      },
    },
    provenance: {
      provider: 'stub',
      model: 'fixture',
      prompt_id: 'entry_recheck',
      prompt_version: 2,
      cost: { amount_usd: '0.0100', currency: 'USD' },
      usage: { total_tokens: 200 },
      knowledge_artifact_hash: 'knowledge-hash',
    },
    validation_status: 'VALID',
    confidence: 0.68,
    decision_type: 'ENTRY_RECHECK',
    reasons: [],
    data_quality: 'COMPLETE',
    ...overrides,
  }
}

function openPositionFixture(overrides: Partial<PositionData> = {}): PositionData {
  return positionFixture({ position_id: 'POS-OPEN-1', symbol: 'BTCUSDT', ...overrides })
}

function listEnvelope(
  items: TradeLogSummary[],
  total = items.length,
): JournalEnvelope<TradeLogListData> {
  return envelope({ items, total })
}

function stubDownloadEnvironment(contentDisposition: string | null, blob: Blob) {
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<unknown>>(async () => ({
    ok: true,
    status: 200,
    headers: {
      get: (name: string) =>
        name.toLowerCase() === 'content-disposition' ? contentDisposition : null,
    },
    blob: async () => blob,
    json: async () => {
      throw new Error('not json')
    },
  }))
  vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)
  const createObjectURL = vi.fn(() => 'blob:journal-export')
  const revokeObjectURL = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  return { fetchMock, createObjectURL, revokeObjectURL, click }
}

async function renderList(
  options: {
    stats?: JournalStatsData
    items?: TradeLogSummary[]
    total?: number
    openPositions?: PositionData[]
    fetchStatsFn?: (
      params: { period?: string; symbol?: string },
      signal?: AbortSignal,
    ) => Promise<JournalEnvelope<JournalStatsData>>
    fetchListFn?: (
      params: { symbol?: string; limit?: number; offset?: number },
      signal?: AbortSignal,
    ) => Promise<JournalEnvelope<TradeLogListData>>
    fetchOpenPositionsFn?: (signal?: AbortSignal) => Promise<PositionListEnvelope>
    isCompact?: boolean
    refreshMs?: number
    paused?: boolean
  } = {},
) {
  render(
    <JournalPage
      isCompact={options.isCompact}
      refreshMs={options.refreshMs ?? 60_000}
      paused={options.paused}
      fetchStatsFn={options.fetchStatsFn ?? (async () => envelope(options.stats ?? statsFixture()))}
      fetchListFn={
        options.fetchListFn ??
        (async () =>
          listEnvelope(
            options.items ?? [summaryFixture()],
            options.total ?? options.items?.length ?? 1,
          ))
      }
      fetchOpenPositionsFn={
        options.fetchOpenPositionsFn ??
        (async () => ({
          ok: true,
          data: { items: options.openPositions ?? [], total: options.openPositions?.length ?? 0 },
          error: null,
          meta,
        }))
      }
    />,
  )
  return screen.findByRole('heading', { name: /交易日志与复盘/ })
}

type DetailProps = Parameters<typeof JournalDetailPage>[0]

function detailProps(overrides: Partial<DetailProps> = {}): DetailProps {
  return {
    tradeId: 'TRD-1',
    refreshMs: 60_000,
    fetchTradeFn: async () => envelope(tradeFixture()),
    fetchCompareFn: async () => envelope(compareFixture()),
    fetchReviewFn: async () =>
      envelope({
        trade_id: 'TRD-1',
        decision_id: null,
        status: 'MISSING' as const,
        advisory: true,
        rationale: null,
        caveats: [],
        confidence: null,
        validation_status: null,
        provenance: null,
        reason: 'no advisory review is linked to this trade',
        idempotent_replay: false,
      }),
    fetchTriggerFn: async () => triggerEnvelope(),
    fetchTriggerDecisionFn: async () => envelope(entryDecisionFixture()),
    fetchOpportunityFn: async () => opportunityEnvelope(),
    fetchPositionFn: async () => positionEnvelope(positionFixture()),
    fetchHistoryFn: async () =>
      historyEnvelope(
        historyFixture({
          events: [
            {
              seq: 1,
              at: '2026-10-28T04:16:02.000Z',
              action: 'OPEN',
              order_id: 'PO-1',
              note: 'entry fill',
            },
            {
              seq: 2,
              at: '2026-10-29T08:00:00.000Z',
              action: 'PARTIAL_CLOSE',
              order_id: 'PO-2',
              note: 'reduced size',
            },
          ],
        }),
      ),
    requestReviewFn: async () =>
      envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-1',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: ['单样本不构成优势'],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: { provider: 'stub', model: 'fixture', prompt_id: 'review', prompt_version: 1 },
        reason: null,
        idempotent_replay: false,
      }),
    ...overrides,
  }
}

async function renderDetail(overrides: Partial<DetailProps> = {}) {
  render(<JournalDetailPage {...detailProps(overrides)} />)
  return screen.findByRole('heading', { name: 'TRD-1' })
}

beforeEach(() => {
  window.sessionStorage.clear()
  for (const id of TRADE_IDS) {
    clearReviewKey(id)
  }
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  delete (URL as unknown as { createObjectURL?: unknown }).createObjectURL
  delete (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL
  document.cookie = 'oft_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})

describe('journal route matching', () => {
  it('is covered by the shared route matcher', async () => {
    const { resolveRoute } = await import('../route-match')
    expect(resolveRoute('/journal').isJournal).toBe(true)
    expect(resolveRoute('/journal').journalTradeId).toBeNull()
  })
})

describe('JournalPage', () => {
  it('shows the documented metrics, sample, exclusions, and verbatim definitions', async () => {
    await renderList()

    const stats = screen.getByTestId('journal-stats')
    expect(stats).toHaveTextContent('482.16')
    expect(stats).toHaveTextContent('50.0%')
    expect(stats).toHaveTextContent('200.00')
    expect(stats).toHaveTextContent('0.70')
    expect(stats).toHaveTextContent('2 closed')
    expect(stats).toHaveTextContent('8w')

    expect(screen.getByText('closed trades with net PnL > 0 ÷ closed trades')).toBeInTheDocument()
    expect(screen.getByText('net PnL ÷ initial risk')).toBeInTheDocument()

    const exclusions = screen.getByTestId('journal-exclusions')
    expect(exclusions).toHaveTextContent('1')
    expect(exclusions).toHaveTextContent('2')
    expect(exclusions).toHaveTextContent('3')
    expect(screen.getByText(/统计与表格使用同一 period \+ symbol 范围/)).toBeInTheDocument()
  })

  it('never renders fabricated zeroes when the metrics are undefined', async () => {
    await renderList({ stats: undefinedStats() })

    const panel = screen.getByTestId('journal-metrics-undefined')
    expect(panel).toHaveTextContent(/metrics undefined/)
    expect(panel).toHaveTextContent(/no eligible closed trades/)
    const stats = screen.getByTestId('journal-stats')
    expect(stats).toHaveTextContent('—')
    expect(stats).not.toHaveTextContent('0.0%')
    expect(stats).not.toHaveTextContent('0.00')
  })

  it('applies filters only on explicit submit and uppercases the symbol', async () => {
    const fetchStatsFn = vi.fn<
      (
        params: { period?: string; symbol?: string },
        signal?: AbortSignal,
      ) => Promise<JournalEnvelope<JournalStatsData>>
    >(async () => envelope(statsFixture()))
    const fetchListFn = vi.fn<
      (
        params: { symbol?: string; limit?: number; offset?: number },
        signal?: AbortSignal,
      ) => Promise<JournalEnvelope<TradeLogListData>>
    >(async () => listEnvelope([summaryFixture()]))
    await renderList({ fetchStatsFn, fetchListFn })

    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: '  btcusdt ' } })
    fireEvent.change(screen.getByLabelText('Period'), { target: { value: '4w' } })
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))

    await waitFor(() => expect(fetchStatsFn).toHaveBeenCalledTimes(2))
    expect(fetchStatsFn.mock.calls[1][0]).toEqual({ period: '4w', symbol: 'BTCUSDT' })
    expect(fetchListFn.mock.calls[1][0]).toMatchObject({ symbol: 'BTCUSDT' })
  })

  it('filters the table client-side by side and outcome without refetching stats', async () => {
    const fetchStatsFn = vi.fn(async () => envelope(statsFixture()))
    await renderList({ fetchStatsFn })

    fireEvent.change(screen.getByLabelText('Side'), { target: { value: 'SHORT' } })
    fireEvent.change(screen.getByLabelText('Outcome'), { target: { value: 'LOSS' } })
    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))

    await screen.findByRole('heading', { name: '没有匹配筛选条件的已平仓交易' })
    expect(screen.queryByText('BTCUSDT')).not.toBeInTheDocument()
    // period + symbol unchanged, so the server stats do not refetch.
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)
  })

  it('filters the closed-trade table by the applied period window', async () => {
    const inside = summaryFixture({
      trade_id: 'TRD-IN',
      closed_at: '2026-10-30T08:16:02.000Z',
    })
    const outside = summaryFixture({
      trade_id: 'TRD-OUT',
      closed_at: '2026-12-01T00:00:00.000Z',
    })
    await renderList({ items: [inside, outside], total: 2 })

    // The default 8w window is 2026-09-11 → 2026-11-06; the December row is out.
    expect(screen.getByText('TRD-IN')).toBeInTheDocument()
    expect(screen.queryByText('TRD-OUT')).not.toBeInTheDocument()
    expect(screen.queryByText(/period 仅作用于统计与导出/)).not.toBeInTheDocument()
    expect(screen.queryByText(/表格不受 period 影响/)).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Period'), { target: { value: 'all' } })
    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))
    expect(await screen.findByText('TRD-OUT')).toBeInTheDocument()
  })

  it('renders the OPEN excluded row when only open positions exist', async () => {
    await renderList({
      stats: undefinedStats(),
      items: [],
      total: 0,
      openPositions: [openPositionFixture()],
    })

    expect(screen.getByRole('heading', { name: '还没有已平仓交易' })).toBeInTheDocument()
    const row = screen.getByTestId('journal-open-row-POS-OPEN-1')
    expect(within(row).getByText('OPEN excluded')).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: '没有匹配筛选条件的已平仓交易' }),
    ).not.toBeInTheDocument()
  })

  it('discloses a truncated list when the server total exceeds the fetched rows', async () => {
    await renderList({ items: [summaryFixture()], total: 900 })

    expect(screen.getByTestId('journal-truncation-note')).toHaveTextContent(
      '仅显示前 1 条，共 900 条',
    )
    // The caption reports the server truncation, not a client-filtered count.
    expect(screen.getByText(/已平仓交易，1 条 · 共 900 条/)).toBeInTheDocument()
  })

  it('does not report period-excluded rows as server truncation in the caption', async () => {
    const inside = summaryFixture({
      trade_id: 'TRD-IN',
      closed_at: '2026-10-30T08:16:02.000Z',
    })
    const outside = summaryFixture({
      trade_id: 'TRD-OUT',
      closed_at: '2026-09-15T00:00:00.000Z',
    })
    const stats4w = statsFixture({
      period: {
        label: '4w',
        start: '2026-10-09T00:00:00.000Z',
        end: '2026-11-06T00:00:00.000Z',
        basis: 'closed_at',
      },
    })
    await renderList({
      items: [inside, outside],
      total: 2,
      fetchStatsFn: async (params) => envelope(params.period === '4w' ? stats4w : statsFixture()),
    })

    fireEvent.change(screen.getByLabelText('Period'), { target: { value: '4w' } })
    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))

    await waitFor(() => expect(screen.queryByText('TRD-OUT')).not.toBeInTheDocument())
    // One row is inside the 4w window; the other is out of period, not hidden
    // by server truncation.
    expect(screen.getByText(/已平仓交易，1 条/)).toBeInTheDocument()
    expect(screen.queryByText(/共 2 条/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('journal-truncation-note')).not.toBeInTheDocument()
  })

  it('announces the applied and reset filter counts once', async () => {
    await renderList()

    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))
    await waitFor(() =>
      expect(screen.getByTestId('journal-filter-announcement')).toHaveTextContent(
        '筛选已应用：1 条已平仓交易',
      ),
    )

    fireEvent.click(screen.getByRole('button', { name: /清除筛选条件/ }))
    await waitFor(() =>
      expect(screen.getByTestId('journal-filter-announcement')).toHaveTextContent(
        '筛选已重置：1 条已平仓交易',
      ),
    )
  })

  it('associates data-quality reasons with the status chip', async () => {
    await renderList({
      items: [
        summaryFixture({
          data_quality: { status: 'PARTIAL', reasons: ['MAE/MFE not measured'] },
        }),
      ],
    })

    const chip = screen.getByTitle('MAE/MFE not measured')
    expect(chip).toHaveAccessibleDescription(/MAE\/MFE not measured/)
  })

  it('renders every documented closed-trade column', async () => {
    await renderList()

    const table = screen.getByRole('table', { name: /已平仓交易/ })
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((header) => header.textContent ?? '')
    for (const label of [
      'TRADE ID',
      'SYMBOL',
      'SIDE',
      'NET PNL',
      'R',
      'RETURN %',
      'DURATION',
      'OUTCOME',
      'DATA QUALITY',
    ]) {
      expect(headers).toContain(label)
    }
    const row = within(table).getByRole('row', { name: /TRD-1/ })
    expect(within(row).getByRole('link', { name: /TRD-1/ })).toHaveAttribute(
      'href',
      '/journal/TRD-1',
    )
    expect(within(row).getByText('▲ LONG')).toBeInTheDocument()
    expect(within(row).getByText('WIN')).toBeInTheDocument()
  })

  it('shows open positions as OPEN excluded, links them, and never folds them into net PnL', async () => {
    await renderList({ openPositions: [openPositionFixture()] })

    const row = screen.getByTestId('journal-open-row-POS-OPEN-1')
    expect(within(row).getByText('OPEN excluded')).toBeInTheDocument()
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0)
    expect(within(row).getByRole('link', { name: /POS-OPEN-1/ })).toHaveAttribute(
      'href',
      '/positions/POS-OPEN-1',
    )
    expect(screen.getByTestId('journal-exclusions')).toHaveTextContent('1')
  })

  it('degrades an open-position failure to a note without blanking the journal', async () => {
    await renderList({
      fetchOpenPositionsFn: async () => {
        throw new Error('open ledger down')
      },
    })

    expect(screen.getByTestId('journal-open-error')).toHaveTextContent(/open ledger down/)
    expect(screen.getByTestId('journal-stats')).toBeInTheDocument()
    expect(screen.getByRole('table', { name: /已平仓交易/ })).toBeInTheDocument()
  })

  it('shows the empty state without zeroed metrics and links to positions and paper', async () => {
    await renderList({
      stats: undefinedStats(),
      items: [],
      total: 0,
      openPositions: [],
    })

    expect(screen.getByRole('heading', { name: '还没有已平仓交易' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /查看持仓/ })).toHaveAttribute('href', '/positions')
    expect(screen.getByRole('link', { name: /Paper 账户/ })).toHaveAttribute('href', '/paper')
    expect(screen.queryByTestId('journal-stats')).not.toBeInTheDocument()
  })

  it('offers a filter reset when the filter yields no closed rows', async () => {
    await renderList()
    fireEvent.change(screen.getByLabelText('Side'), { target: { value: 'SHORT' } })
    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))

    await screen.findByRole('heading', { name: '没有匹配筛选条件的已平仓交易' })
    fireEvent.click(screen.getByRole('button', { name: /重置筛选/ }))
    await screen.findByText('BTCUSDT')
  })

  it('renders an error panel with retry and recovers', async () => {
    let calls = 0
    const fetchStatsFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('stats down')
      }
      return envelope(statsFixture())
    })
    render(
      <JournalPage
        refreshMs={60_000}
        fetchStatsFn={fetchStatsFn}
        fetchListFn={async () => listEnvelope([summaryFixture()])}
        fetchOpenPositionsFn={async () => ({
          ok: true,
          data: { items: [], total: 0 },
          error: null,
          meta,
        })}
      />,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/stats down/)
    fireEvent.click(within(alert).getByRole('button', { name: /重试加载交易日志/ }))
    await screen.findByRole('table', { name: /已平仓交易/ })
  })

  it('freezes polling while paused and revalidates once on resume', async () => {
    const fetchStatsFn = vi.fn(async () => envelope(statsFixture()))
    const fetchListFn = vi.fn(async () => listEnvelope([summaryFixture()]))
    const fetchOpenPositionsFn = vi.fn(async () => ({
      ok: true,
      data: { items: [], total: 0 },
      error: null,
      meta,
    }))
    const props = { fetchStatsFn, fetchListFn, fetchOpenPositionsFn }
    const { rerender } = render(<JournalPage {...props} refreshMs={20} paused />)

    await screen.findByRole('heading', { name: /交易日志与复盘/ })
    expect(screen.getByTestId('journal-paused')).toHaveTextContent(/实时更新已暂停/)
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)

    fireEvent(window, new Event('focus'))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)

    rerender(<JournalPage {...props} refreshMs={60_000} paused={false} />)
    await waitFor(() => expect(fetchStatsFn).toHaveBeenCalledTimes(2))
  })

  it('does not let a focus/timer tick discard the in-flight initial load', async () => {
    let settleStats: ((value: JournalEnvelope<JournalStatsData>) => void) | undefined
    let statsCalls = 0
    const fetchStatsFn = vi.fn(() => {
      statsCalls += 1
      if (statsCalls === 1) {
        return new Promise<JournalEnvelope<JournalStatsData>>((resolve) => {
          settleStats = resolve
        })
      }
      // A background revalidation must never be able to replace the initial
      // load; if it runs early it fails and would strand the page on loading.
      return Promise.reject(new Error('background down'))
    })
    render(
      <JournalPage
        refreshMs={20}
        fetchStatsFn={fetchStatsFn}
        fetchListFn={async () => listEnvelope([summaryFixture()])}
        fetchOpenPositionsFn={async () => ({
          ok: true,
          data: { items: [], total: 0 },
          error: null,
          meta,
        })}
      />,
    )

    expect(screen.getByText(/加载交易日志/)).toBeInTheDocument()
    fireEvent(window, new Event('focus'))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(statsCalls).toBe(1)
    expect(screen.getByText(/加载交易日志/)).toBeInTheDocument()

    await act(async () => {
      settleStats?.(envelope(statsFixture()))
    })
    expect(await screen.findByTestId('journal-stats')).toBeInTheDocument()
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
  })

  it('retains the last good data and warns when a background refresh fails', async () => {
    let calls = 0
    const fetchStatsFn = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        return envelope(statsFixture())
      }
      throw new Error('refresh down')
    })
    await renderList({ fetchStatsFn, refreshMs: 60_000 })
    await screen.findByTestId('journal-stats')

    fireEvent(window, new Event('focus'))

    expect(await screen.findByTestId('journal-refresh-warning')).toHaveTextContent(/refresh down/)
    expect(screen.getByTestId('journal-stats')).toBeInTheDocument()
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
  })

  it('treats a null quality row and an unreadable net as PARTIAL under Outcome=PARTIAL', async () => {
    const nullQuality = summaryFixture({
      trade_id: 'TRD-NULL',
      data_quality: null,
      net_pnl_usd: '482.16',
      outcome_tags: ['WIN'],
    })
    const unreadable = summaryFixture({
      trade_id: 'TRD-UNREADABLE',
      net_pnl_usd: 'not-a-number',
      data_quality: { status: 'COMPLETE', reasons: [] },
    })
    expect(outcomeOf(nullQuality)).toBe('PARTIAL')
    expect(outcomeOf(unreadable)).toBe('PARTIAL')
    await renderList({ items: [nullQuality, unreadable], total: 2 })

    fireEvent.change(screen.getByLabelText('Outcome'), { target: { value: 'PARTIAL' } })
    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))

    expect(await screen.findByText('TRD-NULL')).toBeInTheDocument()
    expect(screen.getByText('TRD-UNREADABLE')).toBeInTheDocument()
    const table = screen.getByRole('table', { name: /已平仓交易/ })
    expect(within(table).queryByText('WIN')).not.toBeInTheDocument()
  })

  it('shows only open rows under Outcome=OPEN even when closed rows exist', async () => {
    await renderList({
      items: [summaryFixture()],
      openPositions: [openPositionFixture()],
    })

    fireEvent.change(screen.getByLabelText('Outcome'), { target: { value: 'OPEN' } })
    fireEvent.click(screen.getByRole('button', { name: /应用筛选/ }))

    expect(screen.getByTestId('journal-open-row-POS-OPEN-1')).toBeInTheDocument()
    expect(screen.queryByText('TRD-1')).not.toBeInTheDocument()
  })

  it('pauses only the table panel and revalidates once on resume', async () => {
    const fetchStatsFn = vi.fn(async () => envelope(statsFixture()))
    await renderList({ fetchStatsFn, refreshMs: 20 })
    await waitFor(() => expect(fetchStatsFn.mock.calls.length).toBeGreaterThanOrEqual(2))

    fireEvent.click(screen.getByRole('button', { name: /暂停交易日志自动刷新/ }))
    const pausedAt = fetchStatsFn.mock.calls.length
    expect(screen.getByTestId('journal-panel-pause-stamp')).toHaveTextContent('PAUSED')
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchStatsFn).toHaveBeenCalledTimes(pausedAt)

    fireEvent.click(screen.getByRole('button', { name: /继续刷新：暂停交易日志自动刷新/ }))
    await waitFor(() => expect(fetchStatsFn.mock.calls.length).toBeGreaterThan(pausedAt))
    expect(screen.queryByTestId('journal-panel-pause-stamp')).not.toBeInTheDocument()
  })

  it('keeps the global pause dominant over the per-panel control', async () => {
    const fetchStatsFn = vi.fn(async () => envelope(statsFixture()))
    render(
      <JournalPage
        refreshMs={20}
        paused
        fetchStatsFn={fetchStatsFn}
        fetchListFn={async () => listEnvelope([summaryFixture()])}
        fetchOpenPositionsFn={async () => ({
          ok: true,
          data: { items: [], total: 0 },
          error: null,
          meta,
        })}
      />,
    )
    await screen.findByTestId('journal-stats')
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /暂停交易日志自动刷新/ }))
    fireEvent.click(screen.getByRole('button', { name: /继续刷新：暂停交易日志自动刷新/ }))
    fireEvent(window, new Event('focus'))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchStatsFn).toHaveBeenCalledTimes(1)
  })

  it('marks the English stat labels and table headers with lang="en"', async () => {
    await renderList()

    expect(screen.getByText('Net PnL')).toHaveAttribute('lang', 'en')
    expect(screen.getByText('Win rate')).toHaveAttribute('lang', 'en')
    expect(screen.getByText('Open excluded')).toHaveAttribute('lang', 'en')
    const table = screen.getByRole('table', { name: /已平仓交易/ })
    expect(within(table).getByText('WIN')).toHaveAttribute('lang', 'en')
  })

  it('is read-only at compact width', async () => {
    await renderList({ isCompact: true })

    expect(screen.getByText(/移动端只读/)).toBeInTheDocument()
  })

  it('downloads the server export through fetch and uses the filename header', async () => {
    const { fetchMock, createObjectURL, click } = stubDownloadEnvironment(
      'attachment; filename="journal-scope.csv"',
      new Blob(['trade_id\n']),
    )
    await renderList()

    fireEvent.click(screen.getByRole('button', { name: /导出 CSV/ }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/journal/export/file?format=csv&period=8w')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/CSV 公式前缀在服务端中和/)).toBeInTheDocument()
  })

  it.each([
    [401, 'AUTH_REQUIRED', 'authentication required'],
    [422, 'VALIDATION_FAILED', 'invalid export scope'],
    [500, 'INTERNAL_ERROR', 'export failed'],
  ])(
    'surfaces a %i export failure instead of downloading an error body',
    async (status, code, message) => {
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => ({
          ok: false,
          status,
          headers: { get: () => null },
          json: async () => ({
            ok: false,
            data: null,
            error: {
              code,
              message,
              category: 'SYSTEM',
              http_status: status,
              retryable: false,
              reference_id: 'REQ-1',
              details: [],
            },
            meta,
          }),
        })) as unknown as typeof fetch,
      )
      await renderList()

      fireEvent.click(screen.getByRole('button', { name: /导出 JSON/ }))
      const alert = await screen.findByTestId('journal-export-error')
      expect(alert).toHaveTextContent(message)
      expect(click).not.toHaveBeenCalled()
    },
  )
})

describe('JournalDetailPage', () => {
  it('renders the signal → trigger → decision → entry → management → exit timeline', async () => {
    await renderDetail()

    const timeline = screen.getByRole('list', { name: /交易时间线/ })
    expect(within(timeline).getByText('signal created')).toBeInTheDocument()
    expect(within(timeline).getByText('triggered')).toBeInTheDocument()
    expect(within(timeline).getByText(/LLM decision ENTER_LONG/)).toBeInTheDocument()
    expect(within(timeline).getByText('entry')).toBeInTheDocument()
    expect(within(timeline).getByText('management')).toBeInTheDocument()
    expect(within(timeline).getByText('exit')).toBeInTheDocument()
    expect(within(timeline).getByText(/PARTIAL_CLOSE/)).toBeInTheDocument()
    expect(within(timeline).getByText(/reduced size/)).toBeInTheDocument()
  })

  it('stamps the signal stage with the opportunity time and the decision with its persisted time', async () => {
    await renderDetail()

    const timeline = screen.getByRole('list', { name: /交易时间线/ })
    // Signal created comes from opportunity.created_at, not trade.opened_at.
    expect(within(timeline).getByText('2026-11-04T05:50:00.000Z UTC')).toBeInTheDocument()
    // Entry still uses trade.opened_at (a distinct, real timestamp).
    expect(within(timeline).getByText('2026-10-28T00:00:00.000Z UTC')).toBeInTheDocument()
    // The decision stage uses decision.persisted_at, not trigger.triggered_at.
    expect(within(timeline).getByText('2026-10-28T04:14:07.100Z UTC')).toBeInTheDocument()
    // The trigger time appears once (the trigger stage), never copied into the
    // decision stage.
    expect(within(timeline).getAllByText('2026-11-04T05:55:00.000Z UTC')).toHaveLength(1)
  })

  it('renders the signal stage as N/A with a reason when the opportunity is unavailable', async () => {
    await renderDetail({
      fetchOpportunityFn: async () => {
        throw new Error('opportunity down')
      },
    })

    const timeline = screen.getByRole('list', { name: /交易时间线/ })
    expect(within(timeline).getByText(/N\/A — opportunity down/)).toBeInTheDocument()
    // The entry stage still shows the real trade time; it is never reused.
    expect(within(timeline).getByText('2026-10-28T00:00:00.000Z UTC')).toBeInTheDocument()
  })

  it('renders the signal snapshot from the source opportunity and comparison', async () => {
    await renderDetail()

    const snapshot = screen.getByRole('region', { name: /Signal snapshot|信号快照/ })
    expect(within(snapshot).getByText(/3200\.00/)).toBeInTheDocument()
    expect(within(snapshot).getByText(/1\.4 bps/)).toBeInTheDocument()
    expect(within(snapshot).getByRole('link', { name: /RPT-1/ })).toHaveAttribute(
      'href',
      '/reports/RPT-1',
    )
  })

  it('renders the entry decision separately from the advisory review band', async () => {
    await renderDetail()

    expect(screen.getByRole('heading', { name: 'LLM 决策记录' })).toBeInTheDocument()
    expect(screen.getAllByText('ENTER_LONG').length).toBeGreaterThan(0)
    expect(screen.getByText('0.68')).toBeInTheDocument()
    expect(screen.getByText('中')).toBeInTheDocument()
    expect(screen.getByText('VALID')).toBeInTheDocument()
    expect(screen.getByText(/CVD 转正/)).toBeInTheDocument()
    expect(screen.getAllByText('stub').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/entry_recheck v2/).length).toBeGreaterThan(0)
  })

  it('renders missing stages as N/A instead of fake events', async () => {
    await renderDetail({
      fetchTriggerFn: async () => {
        throw new Error('trigger down')
      },
      fetchTriggerDecisionFn: async () => {
        throw new Error('decision down')
      },
      fetchOpportunityFn: async () => {
        throw new Error('opportunity down')
      },
      fetchHistoryFn: async () => {
        throw new Error('history down')
      },
    })

    expect(screen.getByRole('heading', { name: 'TRD-1' })).toBeInTheDocument()
    expect(screen.getAllByText(/N\/A/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/trigger down/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/decision down/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/opportunity down/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/history down/).length).toBeGreaterThan(0)
  })

  it('marks incomplete fills PARTIAL with the recorded reasons', async () => {
    await renderDetail({
      fetchTradeFn: async () =>
        envelope(
          tradeFixture({
            data_quality: { status: 'PARTIAL', reasons: ['MAE/MFE not measured'] },
          }),
        ),
    })

    const fills = screen.getByRole('region', { name: /Fills/ })
    expect(within(fills).getByText('PARTIAL')).toBeInTheDocument()
    expect(within(fills).getByText(/MAE\/MFE not measured/)).toBeInTheDocument()
    expect(within(fills).getByText(/139\.82/)).toBeInTheDocument()
  })

  it('renders the comparison and keeps it visible when a PARTIAL side exists', async () => {
    await renderDetail({
      fetchCompareFn: async () =>
        envelope(
          compareFixture({
            first_touch: {
              status: 'PARTIAL',
              reasons: ['AMBIGUOUS_BAR: one bar contains both levels'],
              kind: 'STOP',
              at: '2026-10-29T00:00:00.000Z',
              ambiguous: true,
            },
            data_quality: 'PARTIAL',
            reasons: ['AMBIGUOUS_BAR'],
          }),
        ),
    })

    expect(screen.getByRole('heading', { name: /Signal vs Outcome 对照/ })).toBeInTheDocument()
    expect(screen.getAllByText(/AMBIGUOUS_BAR/).length).toBeGreaterThan(0)
  })

  it('keeps metrics and comparison usable when the review read fails', async () => {
    await renderDetail({
      fetchReviewFn: async () => {
        throw new Error('review read down')
      },
    })

    expect(screen.getByText(/复盘读取失败：review read down/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Signal vs Outcome 对照/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'LLM 决策记录' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: /交易时间线/ })).toBeInTheDocument()
  })

  it('generates one advisory review and re-renders the linked decision', async () => {
    const requestReviewFn = vi.fn<
      (
        tradeId: string,
        options: { refresh?: boolean; idempotencyKey: string },
        signal?: AbortSignal,
      ) => Promise<JournalReviewEnvelope>
    >(async () =>
      envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-9',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: ['单样本不构成优势'],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: { provider: 'stub', model: 'fixture', prompt_id: 'review', prompt_version: 1 },
        reason: null,
        idempotent_replay: false,
      }),
    )
    const fetchReviewFn =
      vi.fn<(tradeId: string, signal?: AbortSignal) => Promise<JournalReviewEnvelope>>()
    fetchReviewFn
      .mockResolvedValueOnce(
        envelope({
          trade_id: 'TRD-1',
          decision_id: null,
          status: 'MISSING' as const,
          advisory: true,
          rationale: null,
          caveats: [],
          confidence: null,
          validation_status: null,
          provenance: null,
          reason: 'no advisory review is linked to this trade',
          idempotent_replay: false,
        }),
      )
      .mockResolvedValue(
        envelope({
          trade_id: 'TRD-1',
          decision_id: 'REV-9',
          status: 'FOUND' as const,
          advisory: true,
          rationale: '入场与信号一致。',
          caveats: ['单样本不构成优势'],
          confidence: 0.62,
          validation_status: 'VALID',
          provenance: {
            provider: 'stub',
            model: 'fixture',
            prompt_id: 'review',
            prompt_version: 1,
          },
          reason: null,
          idempotent_replay: false,
        }),
      )
    await renderDetail({ requestReviewFn, fetchReviewFn })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))

    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(1))
    const [, options] = requestReviewFn.mock.calls[0]
    expect(options.refresh).toBe(false)
    expect(options.idempotencyKey.length).toBeGreaterThan(0)
    await screen.findByText('REV-9')
    expect(screen.getByText('入场与信号一致。')).toBeInTheDocument()
  })

  it('retries a failed generation with the same idempotency key', async () => {
    const keys: string[] = []
    let call = 0
    const requestReviewFn = vi.fn(async (_tradeId: string, options: { idempotencyKey: string }) => {
      keys.push(options.idempotencyKey)
      call += 1
      if (call === 1) {
        throw new Error('network down')
      }
      return envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-9',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: [],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: { provider: 'stub', model: 'fixture', prompt_id: 'review', prompt_version: 1 },
        reason: null,
        idempotent_replay: false,
      })
    })
    await renderDetail({ requestReviewFn })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    const alert = await screen.findByTestId('journal-review-error')
    expect(alert).toHaveTextContent(/network down/)
    expect(alert).toHaveTextContent(/结果未知/)

    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))
    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(2))
    expect(keys[1]).toBe(keys[0])
  })

  it('clears a stale generation error when a refresh read finds the review', async () => {
    let stored = false
    const fetchReviewFn = vi.fn(async () =>
      stored ? foundReviewEnvelope() : missingReviewEnvelope(),
    )
    const requestReviewFn = vi.fn(async () => {
      // The server persisted the review even though the client saw a failure.
      stored = true
      throw new Error('network down')
    })
    await renderDetail({ requestReviewFn, fetchReviewFn, refreshMs: 100 })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    expect(await screen.findByTestId('journal-review-error')).toHaveTextContent(/network down/)

    // The background refresh read proves FOUND and must clear the stale error.
    await waitFor(() =>
      expect(screen.queryByTestId('journal-review-error')).not.toBeInTheDocument(),
    )
    expect(screen.getByText('REV-9')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重新生成/ })).toBeInTheDocument()
  })

  it('replays a failed generation with the stored key and no refresh after FOUND appears', async () => {
    const keys: string[] = []
    const refreshes: Array<boolean | undefined> = []
    let stored = false
    let posts = 0
    const fetchReviewFn = vi.fn(async () =>
      stored ? foundReviewEnvelope() : missingReviewEnvelope(),
    )
    const requestReviewFn = vi.fn(
      async (_tradeId: string, options: { refresh?: boolean; idempotencyKey: string }) => {
        posts += 1
        keys.push(options.idempotencyKey)
        refreshes.push(options.refresh)
        if (posts === 1) {
          // The server stored the review before the client's POST rejected.
          stored = true
          await new Promise((resolve) => setTimeout(resolve, 200))
          throw new Error('network down')
        }
        return foundReviewEnvelope()
      },
    )
    const props = detailProps({ requestReviewFn, fetchReviewFn, refreshMs: 10 })
    const { rerender } = render(<JournalDetailPage {...props} />)
    await screen.findByRole('heading', { name: 'TRD-1' })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    // The refresh read exposes FOUND before the POST rejects; pause so no later
    // poll clears the action error this test is about to retry.
    await screen.findByText('REV-9')
    rerender(<JournalDetailPage {...props} paused />)

    const alert = await screen.findByTestId('journal-review-error')
    expect(alert).toHaveTextContent(/network down/)
    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))

    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(2))
    // A retry replays the last attempt, not a new FOUND-derived refresh.
    expect(refreshes[1]).toBe(false)
    expect(keys[1]).toBe(keys[0])
  })

  it('replays a failed regeneration with the same key and refresh flag', async () => {
    const keys: string[] = []
    const refreshes: Array<boolean | undefined> = []
    let posts = 0
    const requestReviewFn = vi.fn(
      async (_tradeId: string, options: { refresh?: boolean; idempotencyKey: string }) => {
        posts += 1
        keys.push(options.idempotencyKey)
        refreshes.push(options.refresh)
        if (posts === 1) {
          throw new Error('network down')
        }
        return foundReviewEnvelope()
      },
    )
    await renderDetail({ requestReviewFn, fetchReviewFn: async () => foundReviewEnvelope() })

    fireEvent.click(screen.getByRole('button', { name: /重新生成/ }))
    await screen.findByTestId('journal-review-error')

    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))
    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(2))
    expect(refreshes[0]).toBe(true)
    // The idempotency hash binds `refresh`, so a replay must keep both.
    expect(refreshes[1]).toBe(true)
    expect(keys[1]).toBe(keys[0])
  })

  it('keeps the idempotency key for a 409 idempotency conflict', async () => {
    const keys: string[] = []
    let call = 0
    const requestReviewFn = vi.fn(async (_tradeId: string, options: { idempotencyKey: string }) => {
      keys.push(options.idempotencyKey)
      call += 1
      if (call === 1) {
        throw new JournalApiError(
          'A request with this Idempotency-Key is still running.',
          'IDEMPOTENCY_CONFLICT',
          { httpStatus: 409 },
        )
      }
      return envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-9',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: [],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: null,
        reason: null,
        idempotent_replay: false,
      })
    })
    await renderDetail({ requestReviewFn })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    const alert = await screen.findByTestId('journal-review-error')
    expect(alert).toHaveTextContent(/结果未知/)
    expect(alert).toHaveTextContent(/still running/)

    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))
    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(2))
    expect(keys[1]).toBe(keys[0])
  })

  it('clears the idempotency key for a definitive validation refusal', async () => {
    const keys: string[] = []
    let call = 0
    const requestReviewFn = vi.fn(async (_tradeId: string, options: { idempotencyKey: string }) => {
      keys.push(options.idempotencyKey)
      call += 1
      if (call === 1) {
        throw new JournalApiError('invalid review request', 'VALIDATION_FAILED', {
          httpStatus: 422,
        })
      }
      return envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-9',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: [],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: null,
        reason: null,
        idempotent_replay: false,
      })
    })
    await renderDetail({ requestReviewFn })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    const alert = await screen.findByTestId('journal-review-error')
    expect(alert).not.toHaveTextContent(/结果未知/)

    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))
    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(2))
    expect(keys[1]).not.toBe(keys[0])
  })

  it('sends one review mutation for a duplicate submit', async () => {
    const requestReviewFn = vi.fn(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
      return envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-9',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: [],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: null,
        reason: null,
        idempotent_replay: false,
      })
    })
    await renderDetail({ requestReviewFn })

    const button = screen.getByRole('button', { name: /生成复盘/ })
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(1))
  })

  it('aborts the review mutation on unmount without updating state', async () => {
    const deferred: { settle?: (value: JournalReviewEnvelope) => void } = {}
    let signal: AbortSignal | undefined
    const requestReviewFn = vi.fn(
      (
        _tradeId: string,
        _options: { refresh?: boolean; idempotencyKey: string },
        requestSignal?: AbortSignal,
      ) => {
        signal = requestSignal
        return new Promise<JournalReviewEnvelope>((resolve) => {
          deferred.settle = resolve
        })
      },
    )
    const { unmount } = render(<JournalDetailPage {...detailProps({ requestReviewFn })} />)
    await screen.findByRole('heading', { name: 'TRD-1' })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    await waitFor(() => expect(signal).toBeDefined())
    unmount()
    expect(signal?.aborted).toBe(true)

    deferred.settle?.(
      envelope({
        trade_id: 'TRD-1',
        decision_id: 'REV-9',
        status: 'FOUND' as const,
        advisory: true,
        rationale: '入场与信号一致。',
        caveats: [],
        confidence: 0.62,
        validation_status: 'VALID',
        provenance: null,
        reason: null,
        idempotent_replay: false,
      }),
    )
    await act(async () => {
      await Promise.resolve()
    })
  })

  it('reaches ready when a slow initial load overlaps a background refresh', async () => {
    const pending: { settle?: (value: JournalEnvelope<JournalTradeData>) => void } = {}
    const slowTrade = () =>
      new Promise<JournalEnvelope<JournalTradeData>>((resolve) => {
        pending.settle = resolve
      })
    render(<JournalDetailPage {...detailProps({ refreshMs: 20, fetchTradeFn: slowTrade })} />)

    expect(screen.getByText(/加载交易 TRD-1/)).toBeInTheDocument()
    // Let several fast poll ticks fire while the trade is still loading.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(screen.getByText(/加载交易 TRD-1/)).toBeInTheDocument()

    await act(async () => {
      pending.settle?.(envelope(tradeFixture()))
    })
    expect(await screen.findByRole('heading', { name: '时间线' })).toBeInTheDocument()
  })

  it('keeps the timeline and comparison when review generation fails', async () => {
    await renderDetail({
      requestReviewFn: async () => {
        throw new JournalApiError('schema validation failed', 'LLM_SCHEMA_INVALID', {
          httpStatus: 422,
        })
      },
    })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))

    const alert = await screen.findByTestId('journal-review-error')
    expect(alert).toHaveTextContent(/schema validation failed/)
    expect(alert).not.toHaveTextContent(/结果未知/)
    expect(screen.getByRole('list', { name: /交易时间线/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Signal vs Outcome 对照/ })).toBeInTheDocument()
  })

  it('notes an idempotent replay after a retried POST', async () => {
    await renderDetail({
      requestReviewFn: async () =>
        envelope(
          {
            trade_id: 'TRD-1',
            decision_id: 'REV-1',
            status: 'FOUND' as const,
            advisory: true,
            rationale: '入场与信号一致。',
            caveats: [],
            confidence: 0.62,
            validation_status: 'VALID',
            provenance: {
              provider: 'stub',
              model: 'fixture',
              prompt_id: 'review',
              prompt_version: 1,
            },
            reason: null,
            idempotent_replay: true,
          },
          { idempotent_replay: true },
        ),
    })

    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    expect(await screen.findByTestId('journal-review-replay')).toHaveTextContent(
      '幂等重放：复用已存结果',
    )
  })

  it('reserves not-found for a definitive 404 and links back to journal and positions', async () => {
    render(
      <JournalDetailPage
        {...detailProps({
          fetchTradeFn: async () => {
            throw new JournalApiError('trade missing', 'NOT_FOUND', { httpStatus: 404 })
          },
        })}
      />,
    )

    expect(
      await screen.findByRole('heading', { name: '未找到该交易，或交易尚未平仓' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /返回交易日志/ })).toHaveAttribute('href', '/journal')
    expect(screen.getByRole('link', { name: /返回持仓/ })).toHaveAttribute('href', '/positions')
  })

  it('renders a retryable error panel for a non-404 trade failure', async () => {
    let calls = 0
    render(
      <JournalDetailPage
        {...detailProps({
          fetchTradeFn: async () => {
            calls += 1
            if (calls === 1) {
              throw new JournalApiError('trade store down', undefined, { httpStatus: 500 })
            }
            return envelope(tradeFixture())
          },
        })}
      />,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/trade store down/)
    fireEvent.click(within(alert).getByRole('button', { name: /重试加载交易/ }))
    await screen.findByRole('heading', { name: 'TRD-1' })
  })

  it('is read-only at compact width and keeps the export controls usable', async () => {
    await renderDetail({ isCompact: true })

    expect(screen.getAllByText(/移动端只读/).length).toBeGreaterThan(0)
    const generate = screen.getByRole('button', { name: /生成复盘/ })
    expect(generate).toHaveAttribute('aria-disabled', 'true')
    expect(generate).not.toBeDisabled()
    expect(generate).toHaveAccessibleDescription(/移动端只读/)
    expect(screen.getByRole('button', { name: /导出该标的 CSV/ })).toBeInTheDocument()
  })

  it('freezes review/compare polling while paused and revalidates once on resume', async () => {
    const fetchCompareFn = vi.fn(async () => envelope(compareFixture()))
    const props = detailProps({ refreshMs: 20, paused: true, fetchCompareFn })
    const { rerender } = render(<JournalDetailPage {...props} />)

    await screen.findByRole('heading', { name: 'TRD-1' })
    expect(screen.getByTestId('journal-detail-paused')).toBeInTheDocument()
    expect(fetchCompareFn).toHaveBeenCalledTimes(1)

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchCompareFn).toHaveBeenCalledTimes(1)

    fireEvent(window, new Event('focus'))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(fetchCompareFn).toHaveBeenCalledTimes(1)

    rerender(<JournalDetailPage {...props} refreshMs={60_000} paused={false} />)
    await waitFor(() => expect(fetchCompareFn).toHaveBeenCalledTimes(2))
  })

  it('rejects the /journal/stats payload as not-found instead of crashing', async () => {
    render(
      <JournalDetailPage
        {...detailProps({
          fetchTradeFn: async () => envelope(statsFixture() as unknown as JournalTradeData),
        })}
      />,
    )

    expect(
      await screen.findByRole('heading', { name: '未找到该交易，或交易尚未平仓' }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '时间线' })).not.toBeInTheDocument()
  })

  it('rejects the /journal/export payload as not-found instead of crashing', async () => {
    const exportPayload = {
      format: 'csv',
      path: '/tmp/export.csv',
      rows: 1,
      bytes: 10,
      neutralized_cells: 0,
      skipped_unreadable: 0,
      data_quality: 'COMPLETE',
    } as unknown as JournalTradeData
    render(
      <JournalDetailPage {...detailProps({ fetchTradeFn: async () => envelope(exportPayload) })} />,
    )

    expect(
      await screen.findByRole('heading', { name: '未找到该交易，或交易尚未平仓' }),
    ).toBeInTheDocument()
  })

  it('validates the detail payload including the requested trade id', () => {
    expect(isJournalTradeData(tradeFixture(), 'TRD-1')).toBe(true)
    expect(isJournalTradeData(tradeFixture(), 'TRD-2')).toBe(false)
    expect(isJournalTradeData({ trade_id: 'TRD-1' }, 'TRD-1')).toBe(false)
    expect(isJournalTradeData({ ...tradeFixture(), symbol: 7 }, 'TRD-1')).toBe(false)
    expect(isJournalTradeData({ ...tradeFixture(), signal: null }, 'TRD-1')).toBe(true)
    expect(isJournalTradeData({ ...tradeFixture(), data_quality: null }, 'TRD-1')).toBe(true)
  })

  it('renders an undefined or null R multiple as an em dash', async () => {
    await renderDetail({
      fetchTradeFn: async () =>
        envelope(tradeFixture({ r_multiple: undefined as unknown as number | null })),
    })

    expect(screen.getByText(/R — · WIN/)).toBeInTheDocument()
  })

  it('renders an undefined review confidence as an em dash without throwing', async () => {
    await renderDetail({
      fetchReviewFn: async () =>
        envelope({
          ...foundReviewEnvelope().data!,
          confidence: undefined as unknown as number | null,
        }),
    })

    const band = screen.getByTestId('review-band')
    expect(within(band).getAllByText('—').length).toBeGreaterThan(0)
    expect(within(band).getByText('入场与信号一致。')).toBeInTheDocument()
  })

  it('rotates a fresh key for an explicit regenerate after an unknown outcome resolved by a read', async () => {
    const keys: string[] = []
    const refreshes: Array<boolean | undefined> = []
    let posts = 0
    const fetchReviewFn = vi.fn(async () => foundReviewEnvelope())
    const requestReviewFn = vi.fn(
      async (_tradeId: string, options: { refresh?: boolean; idempotencyKey: string }) => {
        posts += 1
        keys.push(options.idempotencyKey)
        refreshes.push(options.refresh)
        if (posts === 1) {
          // The refresh may have reached the server: unknown outcome.
          throw new Error('network down')
        }
        return foundReviewEnvelope()
      },
    )
    await renderDetail({ requestReviewFn, fetchReviewFn, refreshMs: 60_000 })

    fireEvent.click(screen.getByRole('button', { name: /重新生成/ }))
    expect(await screen.findByTestId('journal-review-error')).toHaveTextContent(/network down/)
    const staleKey = keys[0]
    expect(refreshes[0]).toBe(true)

    // A background read proves FOUND and clears the stale error.
    fireEvent(window, new Event('focus'))
    await waitFor(() =>
      expect(screen.queryByTestId('journal-review-error')).not.toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: /重新生成/ }))
    await waitFor(() => expect(requestReviewFn).toHaveBeenCalledTimes(2))
    expect(refreshes[1]).toBe(true)
    // The explicit regenerate must not replay the stale refresh key.
    expect(keys[1]).not.toBe(staleKey)
  })

  it('surfaces a detail export failure instead of downloading an error body', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 422,
        headers: { get: () => null },
        json: async () => ({
          ok: false,
          data: null,
          error: {
            code: 'VALIDATION_FAILED',
            message: 'invalid export symbol',
            category: 'SYSTEM',
            http_status: 422,
            retryable: false,
            reference_id: 'REQ-1',
            details: [],
          },
          meta,
        }),
      })) as unknown as typeof fetch,
    )
    await renderDetail()

    fireEvent.click(screen.getByRole('button', { name: /导出该标的 CSV/ }))

    expect(await screen.findByTestId('journal-detail-export-error')).toHaveTextContent(
      /invalid export symbol/,
    )
    expect(click).not.toHaveBeenCalled()
  })

  it('pauses only the review/compare panel and revalidates once on resume', async () => {
    const fetchCompareFn = vi.fn(async () => envelope(compareFixture()))
    await renderDetail({ refreshMs: 20, fetchCompareFn })
    await waitFor(() => expect(fetchCompareFn.mock.calls.length).toBeGreaterThanOrEqual(2))

    fireEvent.click(screen.getByRole('button', { name: /暂停复盘与对照自动刷新/ }))
    const pausedAt = fetchCompareFn.mock.calls.length
    expect(screen.getByTestId('journal-detail-panel-pause-stamp')).toHaveTextContent('PAUSED')
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    expect(fetchCompareFn).toHaveBeenCalledTimes(pausedAt)

    fireEvent.click(screen.getByRole('button', { name: /继续刷新：暂停复盘与对照自动刷新/ }))
    await waitFor(() => expect(fetchCompareFn.mock.calls.length).toBeGreaterThan(pausedAt))
  })

  it('marks the English detail labels with lang="en"', async () => {
    await renderDetail()

    expect(screen.getByText('Entry / Exit')).toHaveAttribute('lang', 'en')
    expect(screen.getByRole('heading', { name: 'Signal snapshot' })).toHaveAttribute('lang', 'en')
    expect(screen.getAllByText('Decision')[0]).toHaveAttribute('lang', 'en')
  })
})

describe('journal API helpers', () => {
  it('builds an encoded, server-generated export link with no path input', () => {
    expect(journalExportFileUrl({ format: 'csv', period: '8w', symbol: 'BTC/USDT' })).toBe(
      '/api/v1/journal/export/file?format=csv&period=8w&symbol=BTC%2FUSDT',
    )
    expect(journalExportFileUrl({ format: 'json' })).toBe('/api/v1/journal/export/file?format=json')
  })

  it('reads a review with an encoded trade id', async () => {
    const fetchMock =
      vi.fn<
        (
          input: string,
          init?: RequestInit,
        ) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>
      >()
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => envelope({ trade_id: 'TRD/1' } as never),
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchJournalReview('TRD/1')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/journal/TRD%2F1/review')
  })

  it('derives a safe export filename from the header or a stamp fallback', () => {
    expect(exportFilename('attachment; filename="journal-2026.csv"', 'csv')).toBe(
      'journal-2026.csv',
    )
    expect(exportFilename("attachment; filename*=UTF-8''journal%2Dx.json", 'json')).toBe(
      'journal-x.json',
    )
    // A path-shaped filename is reduced to its basename.
    expect(exportFilename('attachment; filename="../../etc/passwd"', 'csv')).toBe('passwd')
    expect(exportFilename(null, 'csv', new Date('2026-11-14T12:00:00.000Z'))).toBe(
      'journal-20261114t120000000z.csv',
    )
  })

  it('downloads an export blob and returns the filename from the header', async () => {
    const { fetchMock, createObjectURL, revokeObjectURL, click } = stubDownloadEnvironment(
      'attachment; filename="journal-scope.csv"',
      new Blob(['trade_id\n']),
    )

    const result = await downloadJournalExport({ format: 'csv', period: '8w' })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/journal/export/file?format=csv&period=8w')
    expect(result.filename).toBe('journal-scope.csv')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(revokeObjectURL).toHaveBeenCalledTimes(1)
  })

  it('throws a typed error with the envelope message, code, and status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 401,
        headers: { get: () => null },
        json: async () => ({
          ok: false,
          data: null,
          error: {
            code: 'AUTH_REQUIRED',
            message: 'authentication required',
            category: 'AUTH',
            http_status: 401,
            retryable: false,
            reference_id: 'REQ-1',
            details: [],
          },
          meta,
        }),
      })) as unknown as typeof fetch,
    )

    await expect(downloadJournalExport({ format: 'csv' })).rejects.toMatchObject({
      name: 'JournalApiError',
      message: 'authentication required',
      code: 'AUTH_REQUIRED',
      httpStatus: 401,
    })
  })

  it('posts one review with CSRF, Idempotency-Key, and the refresh query', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(
      async () =>
        ({
          ok: true,
          status: 200,
          json: async () =>
            envelope({
              trade_id: 'TRD-1',
              decision_id: 'REV-1',
              status: 'FOUND',
              advisory: true,
              rationale: 'ok',
              caveats: [],
              confidence: 0.5,
              validation_status: 'VALID',
              provenance: {},
              reason: null,
              idempotent_replay: false,
            }),
        }) as unknown as Response,
    )
    vi.stubGlobal('fetch', fetchMock)
    document.cookie = 'oft_csrf=csrf-token'

    await requestJournalReview('TRD-1', { refresh: true, idempotencyKey: 'review-key-1' })

    const [input, init] = fetchMock.mock.calls[0]
    expect(input).toBe('/api/v1/journal/TRD-1/review?refresh=true')
    expect((init as RequestInit).method).toBe('POST')
    expect((init as RequestInit).credentials).toBe('same-origin')
    expect((init as RequestInit).headers).toMatchObject({
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-CSRF-Token': 'csrf-token',
      'Idempotency-Key': 'review-key-1',
    })
  })

  it('classifies a non-allowlisted 5xx as unknown and a structured 4xx as definitive', async () => {
    const failure = (code: string, status: number) => ({
      ok: false,
      status,
      json: async () => ({
        ok: false,
        data: null,
        error: {
          code,
          message: 'failure',
          category: 'UPSTREAM',
          http_status: status,
          retryable: false,
          reference_id: 'REQ-1',
          details: [],
        },
        meta,
      }),
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => failure('UPSTREAM_UNAVAILABLE', 503)),
    )
    await expect(requestJournalReview('TRD-1', { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'unknown',
      httpStatus: 503,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => failure('LLM_SCHEMA_INVALID', 422)),
    )
    await expect(requestJournalReview('TRD-1', { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'definitive',
      httpStatus: 422,
    })

    // Status alone never decides the kind: DATA_UNAVAILABLE is a 503 but is
    // allowlisted as definitive (it may follow a link/audit failure, so a
    // fresh-key retry relies on the server's link/orphan replay).
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => failure('DATA_UNAVAILABLE', 503)),
    )
    await expect(requestJournalReview('TRD-1', { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'definitive',
      httpStatus: 503,
    })
  })

  it('classifies a 409 idempotency conflict as unknown and a validation refusal as definitive', async () => {
    const failure = (code: string, status: number) => ({
      ok: false,
      status,
      json: async () => ({
        ok: false,
        data: null,
        error: {
          code,
          message: 'failure',
          category: 'UPSTREAM',
          http_status: status,
          retryable: false,
          reference_id: 'REQ-1',
          details: [],
        },
        meta,
      }),
    })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => failure('IDEMPOTENCY_CONFLICT', 409)),
    )
    await expect(requestJournalReview('TRD-1', { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'unknown',
      httpStatus: 409,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => failure('CONFLICT', 409)),
    )
    await expect(requestJournalReview('TRD-1', { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'unknown',
      httpStatus: 409,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => failure('VALIDATION_FAILED', 422)),
    )
    await expect(requestJournalReview('TRD-1', { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'definitive',
      httpStatus: 422,
    })
  })

  it('fails a hung review with a typed TIMEOUT unknown outcome', async () => {
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

    const pending = requestJournalReview('TRD-1', { idempotencyKey: 'timeout-key' })
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'JournalApiError',
      code: 'TIMEOUT',
      kind: 'unknown',
    })
    await vi.advanceTimersByTimeAsync(REVIEW_TIMEOUT_MS)
    await assertion
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('persists one review key per trade until cleared', () => {
    window.sessionStorage.clear()
    clearReviewKey('TRD-A')
    clearReviewKey('TRD-B')

    const first = reviewKeyFor('TRD-A')
    expect(first.startsWith('web-review-TRD-A-')).toBe(true)
    expect(window.sessionStorage.getItem(`${REVIEW_KEY_PREFIX}TRD-A`)).toBe(first)
    expect(reviewKeyFor('TRD-A')).toBe(first)
    expect(storedReviewKey('TRD-A')).toBe(first)

    const other = reviewKeyFor('TRD-B')
    expect(other).not.toBe(first)
    const rotated = rotateReviewKey('TRD-A')
    expect(rotated).not.toBe(first)
    expect(storedReviewKey('TRD-B')).toBe(other)

    clearReviewKey('TRD-A')
    expect(storedReviewKey('TRD-A')).toBeNull()
  })

  it('derives outcome, R, and duration without inventing values', () => {
    expect(outcomeOf(summaryFixture())).toBe('WIN')
    expect(outcomeOf(summaryFixture({ net_pnl_usd: '-1.00' }))).toBe('LOSS')
    expect(outcomeOf(summaryFixture({ net_pnl_usd: '0.00' }))).toBe('TIE')
    expect(outcomeOf(summaryFixture({ data_quality: { status: 'PARTIAL', reasons: ['x'] } }))).toBe(
      'PARTIAL',
    )
    expect(outcomeOf(summaryFixture({ outcome_tags: ['DATA_PARTIAL'] }))).toBe('PARTIAL')

    expect(rLabel(summaryFixture())).toBe('1.42')
    expect(rLabel(summaryFixture({ r_multiple: null }))).toBe('—')
    expect(durationLabel('2026-10-28T00:00:00Z', '2026-10-30T08:16:02Z')).toBe('2d 08h')
    expect(durationLabel('bad', '2026-10-30T08:16:02Z')).toBe('—')
    expect(durationLabel('2026-10-28T00:00:00Z', 'bad')).toBe('—')
  })
})
