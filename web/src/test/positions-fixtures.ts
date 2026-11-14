import type { ApiMeta } from '../api/status'
import type { OpportunityEnvelope } from '../api/opportunities'
import type { ReportData, ReportEnvelope } from '../api/reports'
import type { KillSwitchEnvelope } from '../api/paper'
import type { TriggerDetailEnvelope } from '../api/triggers'
import type {
  PositionData,
  PositionEnvelope,
  PositionHistoryData,
  PositionHistoryEnvelope,
  PositionListEnvelope,
  PositionManageData,
  PositionManageEnvelope,
  PositionPnlData,
} from '../api/positions'

/**
 * The one shared positions fixture module. Every positions test builds its
 * rows from here so a new field (for example the resolved mark) never drifts
 * between suites.
 */

export const meta: ApiMeta = {
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

/** A fresh resolved mark observed five seconds before NOW. */
export const FRESH_MARK_AS_OF = '2026-11-06T11:59:55.000Z'
export const STALE_MARK_AS_OF = '2026-11-06T10:00:00.000Z'

export function positionFixture(overrides: Partial<PositionData> = {}): PositionData {
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
    resolved_mark_price: '3284.55',
    resolved_mark_as_of: FRESH_MARK_AS_OF,
    resolved_mark_source: 'BARS',
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

export function historyFixture(overrides: Partial<PositionHistoryData> = {}): PositionHistoryData {
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
    pnl: pnlFixture(),
    ...overrides,
  }
}

export function pnlFixture(overrides: Partial<PositionPnlData> = {}): PositionPnlData {
  return {
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
    ...overrides,
  }
}

export function listEnvelope(items: PositionData[]): PositionListEnvelope {
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

/** A paginated list envelope with an explicit server total/has_next. */
export function paginatedListEnvelope(
  items: PositionData[],
  total: number,
  hasNext: boolean,
): PositionListEnvelope {
  return {
    ok: true,
    data: { items, total, counts: { OPEN: total } },
    error: null,
    meta: {
      ...meta,
      pagination: { limit: items.length, cursor: '0', has_next: hasNext, next_cursor: null, total },
    },
  }
}

export function positionEnvelope(data: PositionData): PositionEnvelope<PositionData> {
  return { ok: true, data, error: null, meta }
}

export function historyEnvelope(data: PositionHistoryData): PositionHistoryEnvelope {
  return { ok: true, data, error: null, meta }
}

export function triggerEnvelope(): TriggerDetailEnvelope {
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

export function opportunityEnvelope(): OpportunityEnvelope {
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

export function reportEnvelope(): ReportEnvelope {
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

export function killSwitchEnvelope(active: boolean): KillSwitchEnvelope {
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
    position: positionFixture(),
    trade_log: null,
    account: null,
    refusals: [],
    idempotent_replay: false,
    due_conditions: [],
    ...overrides,
  }
}

export function manageEnvelope(data: PositionManageData): PositionManageEnvelope {
  return { ok: true, data, error: null, meta }
}
