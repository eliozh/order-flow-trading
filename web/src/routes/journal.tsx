import { Fragment, useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'

import { isAbortError } from '../api/abort'
import {
  fetchJournalList,
  fetchJournalStats,
  JOURNAL_REFRESH_MS,
  journalExportFileUrl,
  numberOrNull,
  type JournalStatsData,
  type JournalStatsEnvelope,
  type JournalPeriod,
  type TradeLogListEnvelope,
  type TradeLogSummary,
} from '../api/journal'
import { errorMessage } from '../api/errors'
import { fetchPositions, type PositionData, type PositionListEnvelope } from '../api/positions'
import { DataQualityBadge } from '../components/data-quality'
import { JournalTable } from '../components/journal-table'
import { PausedPanel, PausedStamp } from '../components/paused-state'
import { ProvenanceStamp } from '../components/provenance'
import { useCompact } from '../hooks/use-compact'
import { useLiveUpdates } from '../hooks/use-live-updates'
import { usePolling } from '../hooks/use-polling'
import { useRunGeneration } from '../hooks/use-run-generation'

type PeriodKey = '8w' | '4w' | 'all'
type SideFilter = 'ALL' | 'LONG' | 'SHORT'
type OutcomeFilter = 'ALL' | 'WIN' | 'LOSS' | 'TIE' | 'PARTIAL' | 'OPEN'

interface Filters {
  period: PeriodKey
  symbol: string
  side: SideFilter
  outcome: OutcomeFilter
}

const DEFAULT_FILTERS: Filters = {
  period: '8w',
  symbol: '',
  side: 'ALL',
  outcome: 'ALL',
}

function periodParam(period: PeriodKey): string | undefined {
  return period === 'all' ? undefined : period
}

function periodLabel(period: PeriodKey): string {
  if (period === '8w') {
    return '最近 8 周'
  }
  if (period === '4w') {
    return '最近 4 周'
  }
  return '全部'
}

function hasFilterValue(filters: Filters): boolean {
  return filters.symbol !== '' || filters.side !== 'ALL' || filters.outcome !== 'ALL'
}

/**
 * The period window the table must share with the server stats. `all` has no
 * window (the stats response carries `period: null`).
 */
function periodWindowOf(
  filters: Filters,
  stats: JournalStatsData | null,
): JournalPeriod | null {
  if (filters.period === 'all') {
    return null
  }
  return stats?.period ?? null
}

/** A row is inside the window only when its `closed_at` is comparable. */
function withinPeriod(trade: TradeLogSummary, period: JournalPeriod | null): boolean {
  if (period === null) {
    return true
  }
  const closed = Date.parse(trade.closed_at)
  const start = Date.parse(period.start)
  const end = Date.parse(period.end)
  if (Number.isNaN(closed) || Number.isNaN(start) || Number.isNaN(end)) {
    return false
  }
  return start <= closed && closed <= end
}

/**
 * The closed rows the table shows: the same period window as the stats, then
 * the side/outcome filters. Server stats stay authoritative for every metric.
 */
function filterClosedRows(
  items: TradeLogSummary[],
  filters: Filters,
  period: JournalPeriod | null,
): TradeLogSummary[] {
  return items.filter((trade) => {
    if (!withinPeriod(trade, period)) {
      return false
    }
    if (filters.side !== 'ALL' && trade.side.toUpperCase() !== filters.side) {
      return false
    }
    if (filters.outcome === 'ALL' || filters.outcome === 'OPEN') {
      return filters.outcome === 'ALL'
    }
    const quality = trade.data_quality?.status ?? 'COMPLETE'
    const tags = trade.outcome_tags ?? []
    if (filters.outcome === 'PARTIAL') {
      return quality !== 'COMPLETE' || tags.some((tag) => /PARTIAL/i.test(tag))
    }
    if (quality !== 'COMPLETE' || tags.some((tag) => /PARTIAL/i.test(tag))) {
      return false
    }
    const net = numberOrNull(trade.net_pnl_usd)
    if (net === null) {
      return false
    }
    return filters.outcome === 'WIN' ? net > 0 : filters.outcome === 'LOSS' ? net < 0 : net === 0
  })
}

interface JournalPageProps {
  fetchStatsFn?: (
    params: { period?: string; symbol?: string },
    signal?: AbortSignal,
  ) => Promise<JournalStatsEnvelope>
  fetchListFn?: (
    params: { symbol?: string; limit?: number; offset?: number },
    signal?: AbortSignal,
  ) => Promise<TradeLogListEnvelope>
  fetchOpenPositionsFn?: (signal?: AbortSignal) => Promise<PositionListEnvelope>
  isCompact?: boolean
  /** Revalidation cadence (default 15s). */
  refreshMs?: number
  /** Override the global pause state (unit tests); defaults to the context. */
  paused?: boolean
}

interface JournalLoad {
  state: 'loading' | 'ready' | 'error'
  stats: JournalStatsData | null
  items: TradeLogSummary[]
  total: number
  openPositions: PositionData[]
  openError: string | null
  message?: string
}

/**
 * `/journal`: deterministic statistics with their definitions and exclusions,
 * the closed-trade table, the OPEN rows that are excluded from stats, explicit
 * filters, and the server-generated export links. There is no mutating control
 * and no rule-apply action.
 */
export function JournalPage({
  fetchStatsFn = fetchJournalStats,
  fetchListFn = fetchJournalList,
  fetchOpenPositionsFn = fetchPositions,
  isCompact,
  refreshMs = JOURNAL_REFRESH_MS,
  paused,
}: JournalPageProps) {
  const [load, setLoad] = useState<JournalLoad>({
    state: 'loading',
    stats: null,
    items: [],
    total: 0,
    openPositions: [],
    openError: null,
  })
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [retryToken, setRetryToken] = useState(0)
  const [applied, setApplied] = useState<Filters>(DEFAULT_FILTERS)
  const [draft, setDraft] = useState<Filters>(DEFAULT_FILTERS)
  const [pendingFilterAnnouncement, setPendingFilterAnnouncement] = useState<
    'apply' | 'reset' | null
  >(null)
  const [filterAnnouncement, setFilterAnnouncement] = useState('')
  // Only period + symbol change the server scope; side/outcome filter the
  // already-fetched table, so they never trigger a stats/list refetch.
  const [serverScope, setServerScope] = useState<{ period: PeriodKey; symbol: string }>({
    period: DEFAULT_FILTERS.period,
    symbol: DEFAULT_FILTERS.symbol,
  })
  const compact = useCompact(isCompact)
  const live = useLiveUpdates()
  const isPaused = paused ?? live.paused
  const { begin, isCurrent } = useRunGeneration()

  const runLoad = useCallback(
    async (signal: AbortSignal, initial: boolean, generation: number): Promise<void> => {
      if (initial) {
        setLoad((prev) => ({ ...prev, state: 'loading' }))
      }
      const [statsResult, listResult, openResult] = await Promise.allSettled([
        fetchStatsFn(
          { period: periodParam(serverScope.period), symbol: serverScope.symbol || undefined },
          signal,
        ),
        fetchListFn({ symbol: serverScope.symbol || undefined, limit: 500, offset: 0 }, signal),
        fetchOpenPositionsFn(signal),
      ])
      if (signal.aborted || !isCurrent(generation)) {
        return
      }
      const coreError =
        statsResult.status === 'rejected'
          ? statsResult.reason
          : listResult.status === 'rejected'
            ? listResult.reason
            : null
      if (statsResult.status === 'rejected' || listResult.status === 'rejected' || coreError) {
        if (initial) {
          setLoad((prev) => ({
            ...prev,
            state: 'error',
            message: errorMessage(coreError),
          }))
        } else {
          setRefreshError(errorMessage(coreError))
        }
        return
      }
      const stats = statsResult.value.data
      const list = listResult.value.data
      if (!stats || !list) {
        const message = 'The journal response was empty.'
        if (initial) {
          setLoad((prev) => ({ ...prev, state: 'error', message }))
        } else {
          setRefreshError(message)
        }
        return
      }
      setLoad((prev) => ({
        state: 'ready',
        stats,
        items: list.items,
        total: list.total,
        openPositions:
          openResult.status === 'fulfilled'
            ? (openResult.value.data?.items ?? [])
            : prev.openPositions,
        openError: openResult.status === 'rejected' ? errorMessage(openResult.reason) : null,
      }))
      setRefreshError(null)
    },
    [serverScope, fetchStatsFn, fetchListFn, fetchOpenPositionsFn, isCurrent],
  )

  // The initial load and every explicit filter apply run even while paused.
  useEffect(() => {
    const controller = new AbortController()
    const generation = begin()
    runLoad(controller.signal, true, generation).catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      if (!isCurrent(generation)) {
        return
      }
      setLoad((prev) => ({ ...prev, state: 'error', message: errorMessage(error) }))
    })
    return () => {
      controller.abort()
    }
  }, [runLoad, retryToken, begin, isCurrent])

  usePolling(
    (signal) => {
      const generation = begin()
      return runLoad(signal, false, generation).catch((error: unknown) => {
        if (!isCurrent(generation)) {
          return
        }
        throw error
      })
    },
    {
      refreshMs,
      paused: isPaused,
      onError: (error) => setRefreshError(errorMessage(error)),
    },
  )

  const stats = load.stats
  const defined = stats?.status === 'DEFINED'
  const filtered = hasFilterValue(applied)
  const appliedPeriod = periodWindowOf(applied, stats)
  const closedRows = filterClosedRows(load.items, applied, appliedPeriod)
  const openRows = load.openPositions.filter((position) => {
    if (applied.symbol !== '' && position.symbol.toUpperCase() !== applied.symbol.toUpperCase()) {
      return false
    }
    if (applied.side !== 'ALL' && position.side.toUpperCase() !== applied.side) {
      return false
    }
    return applied.outcome === 'ALL' || applied.outcome === 'OPEN'
  })
  const hasAnyRows = closedRows.length + openRows.length > 0
  // `load.total` counts the unfiltered server list. The table caption may only
  // report it when the server actually truncated the fetched window; otherwise
  // the caption reports the client-filtered row count (closedRows.length).
  const serverTruncated = load.total > load.items.length
  // No closed trades were ever recorded (filters aside): show the explicit
  // empty panel, but never hide OPEN rows that reach the table.
  const globalEmpty =
    load.state === 'ready' && load.items.length === 0 && !filtered && stats?.status === 'UNDEFINED'
  const filterEmpty = load.state === 'ready' && closedRows.length === 0 && !globalEmpty
  const showStats = load.state === 'ready' && !globalEmpty

  // Announce the applied/reset result exactly once, after the load that
  // produced the matching period window has settled.
  useEffect(() => {
    if (pendingFilterAnnouncement === null || load.state !== 'ready' || !stats) {
      return
    }
    const periodMatches =
      applied.period === 'all' ? stats.period === null : stats.period?.label === applied.period
    if (!periodMatches) {
      return
    }
    const count = filterClosedRows(load.items, applied, periodWindowOf(applied, stats)).length
    const action = pendingFilterAnnouncement === 'apply' ? '筛选已应用' : '筛选已重置'
    setFilterAnnouncement(`${action}：${count} 条已平仓交易`)
    setPendingFilterAnnouncement(null)
  }, [pendingFilterAnnouncement, load.state, load.items, applied, stats])

  function applyServerScope(next: Filters): void {
    setServerScope((prev) =>
      prev.period === next.period && prev.symbol === next.symbol
        ? prev
        : { period: next.period, symbol: next.symbol },
    )
  }

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    const next: Filters = {
      ...draft,
      symbol: draft.symbol.trim().toUpperCase(),
    }
    setDraft(next)
    setApplied(next)
    setPendingFilterAnnouncement('apply')
    applyServerScope(next)
  }

  function resetFilters(): void {
    setDraft(DEFAULT_FILTERS)
    setApplied(DEFAULT_FILTERS)
    setPendingFilterAnnouncement('reset')
    applyServerScope(DEFAULT_FILTERS)
  }

  const csvHref = journalExportFileUrl({
    format: 'csv',
    period: periodParam(applied.period),
    symbol: applied.symbol || undefined,
  })
  const jsonHref = journalExportFileUrl({
    format: 'json',
    period: periodParam(applied.period),
    symbol: applied.symbol || undefined,
  })

  return (
    <section className="system-page" aria-labelledby="journal-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">复盘 / 全部</p>
          <h1 id="journal-heading">交易日志与复盘</h1>
          <p className="page-sub">
            已平仓交易、净 PnL、胜率与 R multiple。定义写在界面上；开放交易与不完整数据不计入
            统计，并显示排除计数。
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="CALC" />
          {stats && <DataQualityBadge status={stats.data_quality} />}
          {isPaused && <PausedStamp testId="journal-paused-stamp" />}
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：交易日志为只读视图；筛选与导出可用，复盘生成请在桌面端或 CLI 完成。 Kill
          switch 仍可用。
        </p>
      )}

      {isPaused && <PausedPanel testId="journal-paused" />}

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          加载交易日志…
        </p>
      )}

      {load.state === 'error' && (
        <div className="panel" role="alert">
          <div className="panel-head">
            <h2>交易存储不可用</h2>
          </div>
          <p className="panel-foot">
            {load.message ?? '无法读取交易与统计。'} — 不伪造统计或交易行；Kill switch 仍可用。
          </p>
          <div className="action-row journal-panel-actions">
            <button
              type="button"
              className="btn"
              onClick={() => {
                setRetryToken((value) => value + 1)
              }}
            >
              重试加载交易日志
            </button>
          </div>
        </div>
      )}

      {refreshError !== null && load.state === 'ready' && (
        <p className="status-message" role="status" data-testid="journal-refresh-warning">
          自动刷新失败：{refreshError} — 保留上次数据，未清空页面。
        </p>
      )}

      {load.openError !== null && (
        <p className="status-message" role="status" data-testid="journal-open-error">
          开放持仓读取失败：{load.openError} — 统计中的 open 排除计数仍来自服务端；不伪造开放行。
        </p>
      )}

      {showStats && stats && (
        <dl className="stat-grid" data-testid="journal-stats">
          <div className="stat">
            <dt>Net PnL</dt>
            <dd
              className={
                defined
                  ? (numberOrNull(stats.totals.net_pnl_usd) ?? 0) < 0
                    ? 'value-down'
                    : 'value-up'
                  : ''
              }
            >
              {defined ? (stats.totals.net_pnl_usd ?? '—') : '—'}
            </dd>
            <dd className="stat-note">
              {defined ? 'USDT · net of fees/funding' : 'metrics undefined'}
            </dd>
          </div>
          <div className="stat">
            <dt>Win rate</dt>
            <dd>
              {defined && stats.win_rate !== null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—'}
            </dd>
            <dd className="stat-note">
              {defined
                ? `${stats.wins} win / ${stats.sample_size} closed · open excluded`
                : 'metrics undefined'}
            </dd>
          </div>
          <div className="stat">
            <dt>Expectancy</dt>
            <dd>{defined ? (stats.expectancy_usd ?? '—') : '—'}</dd>
            <dd className="stat-note">
              {defined ? 'USDT · per closed trade' : 'metrics undefined'}
            </dd>
          </div>
          <div className="stat">
            <dt>Avg R</dt>
            <dd>{defined && stats.avg_r.value !== null ? stats.avg_r.value.toFixed(2) : '—'}</dd>
            <dd className="stat-note">
              {defined
                ? `${stats.avg_r.samples} 样本 · ${stats.avg_r.excluded_null_r} 个 null R 排除`
                : 'metrics undefined'}
            </dd>
          </div>
          <div className="stat">
            <dt>Sample</dt>
            <dd>{defined ? `${stats.sample_size} closed` : '0 closed'}</dd>
            <dd className="stat-note">
              {`${stats.exclusions.open_positions} open excluded · ${stats.exclusions.partial_trades} PARTIAL excluded`}
            </dd>
          </div>
          <div className="stat">
            <dt>Period</dt>
            <dd className="mono">{stats.period?.label ?? periodLabel(applied.period)}</dd>
            <dd className="stat-note">
              {stats.period ? `${stats.period.start} → ${stats.period.end} UTC` : '全部已平仓交易'}
            </dd>
          </div>
        </dl>
      )}

      {showStats && stats?.status === 'UNDEFINED' && (
        <div className="panel" role="status" data-testid="journal-metrics-undefined">
          <div className="panel-head">
            <h2>metrics undefined</h2>
            <span className="chip chip--warn">UNDEFINED</span>
          </div>
          <p className="panel-foot">
            没有可计入的已平仓交易：{stats.reason ?? 'no sample'}。指标不显示为 0——没有样本时
            无定义。开放交易与 PARTIAL 交易分别计数，从不写入零值。
          </p>
        </div>
      )}

      {showStats && stats && (
        <section className="panel" aria-labelledby="journal-definitions-heading">
          <div className="panel-head">
            <h2 id="journal-definitions-heading">定义</h2>
            <span className="mono-muted">definitions 由服务端提供，逐字展示</span>
          </div>
          <dl className="kv journal-definitions" data-testid="journal-definitions">
            {Object.entries(stats.definitions).map(([name, value]) => (
              <Fragment key={name}>
                <dt className="mono">{name}</dt>
                <dd>{value}</dd>
              </Fragment>
            ))}
          </dl>
          <div className="panel-foot">
            <dl className="kv journal-definitions" data-testid="journal-exclusions">
              <dt>Open excluded</dt>
              <dd className="mono">{stats.exclusions.open_positions}</dd>
              <dt>PARTIAL excluded</dt>
              <dd className="mono">{stats.exclusions.partial_trades}</dd>
              <dt>Unreadable excluded</dt>
              <dd className="mono">{stats.exclusions.unreadable_trades}</dd>
              <dt>Outside period</dt>
              <dd className="mono">{stats.exclusions.outside_period}</dd>
            </dl>
          </div>
        </section>
      )}

      <form
        className="panel journal-filters"
        onSubmit={submit}
        aria-labelledby="journal-filters-heading"
      >
        <div className="panel-head">
          <h2 id="journal-filters-heading">筛选</h2>
          <span className="mono-muted">显式提交，变更不自动应用</span>
        </div>
        <div className="journal-filter-grid">
          <div className="journal-field">
            <label htmlFor="journal-period">Period</label>
            <select
              id="journal-period"
              value={draft.period}
              onChange={(event) => setDraft({ ...draft, period: event.target.value as PeriodKey })}
            >
              <option value="8w">最近 8 周</option>
              <option value="4w">最近 4 周</option>
              <option value="all">全部</option>
            </select>
          </div>
          <div className="journal-field">
            <label htmlFor="journal-symbol">Symbol</label>
            <input
              id="journal-symbol"
              value={draft.symbol}
              placeholder="全部"
              onChange={(event) => setDraft({ ...draft, symbol: event.target.value })}
            />
          </div>
          <div className="journal-field">
            <label htmlFor="journal-side">Side</label>
            <select
              id="journal-side"
              value={draft.side}
              onChange={(event) => setDraft({ ...draft, side: event.target.value as SideFilter })}
            >
              <option value="ALL">全部</option>
              <option value="LONG">LONG</option>
              <option value="SHORT">SHORT</option>
            </select>
          </div>
          <div className="journal-field">
            <label htmlFor="journal-outcome">Outcome</label>
            <select
              id="journal-outcome"
              value={draft.outcome}
              onChange={(event) =>
                setDraft({ ...draft, outcome: event.target.value as OutcomeFilter })
              }
            >
              <option value="ALL">全部</option>
              <option value="WIN">WIN</option>
              <option value="LOSS">LOSS</option>
              <option value="TIE">TIE</option>
              <option value="PARTIAL">PARTIAL</option>
              <option value="OPEN">OPEN</option>
            </select>
          </div>
        </div>
        <div className="action-row journal-filter-actions">
          <button type="submit" className="btn">
            应用筛选
          </button>
          <button type="button" className="btn" onClick={resetFilters}>
            清除筛选条件
          </button>
        </div>
        <p className="panel-foot">
          统计与表格使用同一 period + symbol 范围：统计由服务端计算，表格按 period 窗口与 symbol
          过滤已平仓交易；side/outcome 进一步筛选表格。period 与 symbol 也决定导出范围。
        </p>
      </form>

      <p
        className="visually-hidden"
        role="status"
        data-testid="journal-filter-announcement"
      >
        {filterAnnouncement}
      </p>

      {globalEmpty && (
        <div className="panel empty-panel" role="status">
          <p className="kicker" lang="en">
            0 CLOSED
          </p>
          <h2>还没有已平仓交易</h2>
          <p className="panel-foot journal-empty-text">
            Paper 交易平仓后会出现在这里。统计不显示为 0——没有样本时指标无定义。
          </p>
          <div className="action-row journal-empty-actions">
            <a className="btn" href="/positions">
              查看持仓
            </a>
            <a className="btn" href="/paper">
              Paper 账户
            </a>
          </div>
        </div>
      )}

      {filterEmpty && (
        <div className="panel empty-panel" role="status">
          <p className="kicker" lang="en">
            0 MATCHED
          </p>
          <h2>没有匹配筛选条件的已平仓交易</h2>
          <p className="panel-foot journal-empty-text">
            当前 period / side / outcome 筛选没有匹配的已平仓交易。统计仍按 period + symbol
            由服务端计算；开放交易只在 outcome 为 ALL/OPEN 时显示。
          </p>
          <div className="action-row journal-empty-actions">
            <button type="button" className="btn" onClick={resetFilters}>
              重置筛选
            </button>
          </div>
        </div>
      )}

      {load.state === 'ready' && hasAnyRows && (
        <section className="panel" aria-labelledby="journal-table-heading">
          <div className="panel-head">
            <h2 id="journal-table-heading">已平仓交易</h2>
            <span className="mono-muted">
              {isPaused
                ? '实时更新已暂停'
                : `每 ${Math.round(refreshMs / 1000)}s 自动刷新；窗口聚焦时立即刷新`}
            </span>
          </div>
          <JournalTable
            trades={closedRows}
            openPositions={openRows}
            total={serverTruncated ? load.total : closedRows.length}
            captionNote="表格与统计使用同一 period 窗口，并按 symbol 过滤"
          />
          <p className="panel-foot">
            净 PnL 与 R 由代码计算（CALC）；R 为 null 时显示 —。开放交易标记 OPEN excluded 并
            单独计数，不写入统计。列表接口只返回已平仓交易。
          </p>
        </section>
      )}

      {load.state === 'ready' && serverTruncated && (
        <p className="status-message" data-testid="journal-truncation-note">
          仅显示前 {load.items.length} 条，共 {load.total} 条；调整 symbol/period 缩小范围。
        </p>
      )}

      {load.state !== 'error' && (
        <section className="panel" aria-labelledby="journal-export-heading">
          <div className="panel-head">
            <h2 id="journal-export-heading">导出</h2>
            <span className="mono-muted">服务端生成 · CSV formula 中和在服务端完成</span>
          </div>
          <div className="action-row journal-filter-actions">
            <a className="btn" href={csvHref} download>
              导出 CSV
            </a>
            <a className="btn" href={jsonHref} download>
              导出 JSON
            </a>
          </div>
          <p className="panel-foot">
            导出文件由 API 生成并下载；客户端不生成文件。CSV 公式前缀在服务端中和。
          </p>
        </section>
      )}
    </section>
  )
}
