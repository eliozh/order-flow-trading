import { useCallback, useEffect, useState } from 'react'

import { isAbortError } from '../api/abort'
import {
  aggregateNetPnl,
  fetchPositions,
  formatMoney,
  formatSignedMoney,
  POSITION_REFRESH_MS,
  sumMargin,
  sumNotional,
  sumRiskPct,
  type PositionData,
  type PositionListEnvelope,
} from '../api/positions'
import { errorMessage } from '../api/errors'
import type { ApiMeta } from '../api/status'
import { PositionTable } from '../components/position-table'
import { PausedPanel, PausedStamp } from '../components/paused-state'
import { useCompact } from '../hooks/use-compact'
import { useLiveUpdates } from '../hooks/use-live-updates'
import { usePolling } from '../hooks/use-polling'
import { useRunGeneration } from '../hooks/use-run-generation'

interface PositionsPageProps {
  fetchPositionsFn?: (signal?: AbortSignal) => Promise<PositionListEnvelope>
  isCompact?: boolean
  /** Deterministic clock for age and mark freshness (defaults to Date.now()). */
  now?: number
  /** Revalidation cadence (default 5s). */
  refreshMs?: number
  /** Override the global pause state (unit tests); defaults to the context. */
  paused?: boolean
}

interface PositionsState {
  state: 'loading' | 'ready' | 'error'
  items: PositionData[]
  total: number
  hasNext: boolean
  message?: string
}

interface PaginationMeta {
  has_next?: boolean
  total?: number
}

/** Read the additive pagination block without trusting its shape. */
function readPagination(meta: ApiMeta | null | undefined): PaginationMeta {
  const pagination = meta?.pagination
  if (typeof pagination !== 'object' || pagination === null) {
    return {}
  }
  const record = pagination as Record<string, unknown>
  const total = typeof record.total === 'number' ? record.total : undefined
  const hasNext = typeof record.has_next === 'boolean' ? record.has_next : undefined
  return { has_next: hasNext, total }
}

/**
 * The open-position ledger: exposure, aggregate uPnL, risk utilization, margin,
 * and every documented column. A missing mark is excluded from the aggregate
 * (never treated as zero) and the summary is labelled PARTIAL. An incomplete
 * page (``has_next`` or ``items.length < total``) is also PARTIAL: the fetched
 * page is never presented as the whole ledger. There is no mutating control
 * here; management is requested from the detail surface.
 */
export function PositionsPage({
  fetchPositionsFn = fetchPositions,
  isCompact,
  now,
  refreshMs = POSITION_REFRESH_MS,
  paused,
}: PositionsPageProps) {
  const [load, setLoad] = useState<PositionsState>({
    state: 'loading',
    items: [],
    total: 0,
    hasNext: false,
  })
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [retryToken, setRetryToken] = useState(0)
  const compact = useCompact(isCompact)
  const live = useLiveUpdates()
  const isPaused = paused ?? live.paused
  const clock = now ?? Date.now()
  const { begin, isCurrent } = useRunGeneration()

  const runLoad = useCallback(
    async (signal: AbortSignal, initial: boolean, generation: number): Promise<void> => {
      if (initial) {
        setLoad({ state: 'loading', items: [], total: 0, hasNext: false })
      }
      const envelope = await fetchPositionsFn(signal)
      // Only the most recently started run may apply: a late initial rejection
      // must not replace a ready page, and a stale poll must not regress a
      // newer initial/retry result.
      if (signal.aborted || !isCurrent(generation)) {
        return
      }
      const data = envelope.data
      if (!data) {
        throw new Error('The positions response was empty.')
      }
      const pagination = readPagination(envelope.meta)
      const total = pagination.total ?? data.total
      const hasNext = pagination.has_next ?? data.items.length < total
      setLoad({ state: 'ready', items: data.items, total, hasNext })
      setRefreshError(null)
    },
    [fetchPositionsFn, isCurrent],
  )

  // The initial load always runs, including while paused.
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
      setLoad({
        state: 'error',
        items: [],
        total: 0,
        hasNext: false,
        message: errorMessage(error),
      })
    })
    return () => {
      controller.abort()
    }
  }, [runLoad, retryToken, begin, isCurrent])

  // Freeze the 5s cadence and the focus/visibility revalidation while paused;
  // resuming performs exactly one immediate revalidation.
  usePolling(
    (signal) => {
      const generation = begin()
      return runLoad(signal, false, generation).catch((error: unknown) => {
        // A stale poll's failure is not the current page state.
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

  const aggregate = aggregateNetPnl(load.items, clock)
  const notional = sumNotional(load.items)
  const margin = sumMargin(load.items)
  const riskPct = sumRiskPct(load.items)
  const openCount = load.state === 'ready' ? Math.max(load.total, load.items.length) : 0
  const truncated = load.hasNext || load.items.length < load.total
  const partial = aggregate.missing > 0 || truncated
  const stale = aggregate.stale > 0

  return (
    <section className="system-page" aria-labelledby="positions-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">
            <span lang="en">Positions</span> / 未平仓
          </p>
          <h1 id="positions-heading">未平仓持仓</h1>
          <p className="page-sub">
            当前敞口、风险利用率、uPnL、R
            multiple、止损/目标与杠杆。管理动作从持仓详情发起，由代码校验后执行。
          </p>
        </div>
        <div className="report-stamps">
          <span className="chip chip--calc" lang="en">
            CALC
          </span>
          <span className="env-chip env-chip--paper" lang="en">
            PAPER
          </span>
          {isPaused && <PausedStamp testId="positions-paused-stamp" />}
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：持仓列表在 768px 及以下不可编辑。管理动作在持仓详情发起；Kill switch 仍可用。
        </p>
      )}

      {isPaused && <PausedPanel testId="positions-paused" />}

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          加载未平仓持仓…
        </p>
      )}

      {load.state === 'error' && (
        <div className="panel" role="alert">
          <div className="panel-head">
            <h2>持仓数据不可用</h2>
          </div>
          <p className="panel-foot">
            {load.message} — 显示失败，不伪造任何 PnL 或 mark；Kill switch 仍可用。
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setRetryToken((value) => value + 1)
              }}
            >
              重试加载持仓
            </button>
          </div>
        </div>
      )}

      {refreshError !== null && load.state === 'ready' && (
        <p className="status-message" role="status" data-testid="refresh-warning">
          自动刷新失败：{refreshError} — 保留上次数据，未清空表格。
        </p>
      )}

      {load.state === 'ready' && (
        <>
          <dl className="stat-grid">
            <div className="stat">
              <dt lang="en">Total uPnL (net)</dt>
              <dd
                className={
                  aggregate.value !== null && aggregate.value < 0
                    ? 'mono value-down'
                    : 'mono value-up'
                }
              >
                {aggregate.value === null ? '—' : formatSignedMoney(aggregate.value)}
              </dd>
              <dd className="stat-note">
                {partial ? (
                  <span className="chip chip--warn">PARTIAL</span>
                ) : (
                  <span lang="en">price + accumulated gross − fees + funding · CALC</span>
                )}
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Notional</dt>
              <dd className="mono">{formatMoney(notional)}</dd>
              <dd className="stat-note">
                USDT · {load.items.length} / {load.total} 个持仓 · Σ notional_usd
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Risk utilization</dt>
              <dd className="mono">
                {openCount} 个持仓 · Σ risk {riskPct.toFixed(2)}%
              </dd>
              <dd className="stat-note" lang="en">
                CALC · caps enforced server-side
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Margin used</dt>
              <dd className="mono">{formatMoney(margin)}</dd>
              <dd className="stat-note">USDT · Σ margin_usd</dd>
            </div>
          </dl>

          {(partial || stale) && (
            <div className="panel" role="status">
              <div className="panel-head">
                <h2>部分 mark 不完整或列表未完整返回</h2>
                <span className={partial ? 'chip chip--warn' : 'chip'}>
                  {partial ? 'PARTIAL' : 'STALE'}
                </span>
              </div>
              <p className="panel-foot">
                {truncated
                  ? `列表已分页：本页 ${load.items.length} / ${load.total} 个持仓，汇总只覆盖本页。`
                  : ''}
                {aggregate.missing > 0
                  ? ` ${aggregate.missing} 个持仓缺少 mark：其 uPnL 不计入合计，绝不当作 0。`
                  : ''}
                {stale ? ` ${aggregate.stale} 个 mark 已过期并标 STALE，保留最后值。` : ''}{' '}
                管理动作可能被保守的风险门阻断。
              </p>
            </div>
          )}

          <section className="panel" aria-labelledby="positions-table-heading">
            <div className="panel-head">
              <h2 id="positions-table-heading">持仓列表</h2>
              <span className="mono-muted">
                {load.items.length} / {load.total} ·{' '}
                {isPaused ? '实时更新已暂停' : '每 5s 自动刷新；窗口聚焦时立即刷新'}
              </span>
            </div>
            <PositionTable
              positions={load.items}
              now={clock}
              total={load.total}
              truncated={truncated}
            />
            <p className="panel-foot">
              uPnL 由 resolved mark、累计已实现 gross、已记录费用与已记录资金费计算（CALC）； mark
              缺失时标 STALE 且不写零。mark 的 as-of 单独展示。风险上限由服务端强制，本页不
              自行推断上限分母。
            </p>
          </section>
        </>
      )}
    </section>
  )
}
