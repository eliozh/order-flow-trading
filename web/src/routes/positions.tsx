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
import { PositionTable } from '../components/position-table'
import { useCompact } from '../hooks/use-compact'

interface PositionsPageProps {
  fetchPositionsFn?: (signal?: AbortSignal) => Promise<PositionListEnvelope>
  isCompact?: boolean
  /** Deterministic clock for age and mark freshness (defaults to Date.now()). */
  now?: number
  /** Revalidation cadence (default 5s). */
  refreshMs?: number
}

interface PositionsState {
  state: 'loading' | 'ready' | 'error'
  items: PositionData[]
  total: number
  message?: string
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

/**
 * The open-position ledger: exposure, aggregate uPnL, risk utilization, margin,
 * and every documented column. A missing mark is excluded from the aggregate
 * (never treated as zero) and the summary is labelled PARTIAL. There is no
 * mutating control here; management is requested from the detail surface.
 */
export function PositionsPage({
  fetchPositionsFn = fetchPositions,
  isCompact,
  now,
  refreshMs = POSITION_REFRESH_MS,
}: PositionsPageProps) {
  const [load, setLoad] = useState<PositionsState>({
    state: 'loading',
    items: [],
    total: 0,
  })
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const compact = useCompact(isCompact)
  const clock = now ?? Date.now()

  const runLoad = useCallback(
    async (signal: AbortSignal, initial: boolean): Promise<void> => {
      if (initial) {
        setLoad({ state: 'loading', items: [], total: 0 })
      }
      const envelope = await fetchPositionsFn(signal)
      if (signal.aborted) {
        return
      }
      const data = envelope.data
      if (!data) {
        throw new Error('The positions response was empty.')
      }
      setLoad({ state: 'ready', items: data.items, total: data.total })
      setRefreshError(null)
    },
    [fetchPositionsFn],
  )

  useEffect(() => {
    const controller = new AbortController()
    runLoad(controller.signal, true).catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      setLoad({ state: 'error', items: [], total: 0, message: errorMessage(error) })
    })
    return () => {
      controller.abort()
    }
  }, [runLoad])

  // Revalidate on a 5s cadence and immediately when the window regains focus or
  // the tab becomes visible. A refresh failure keeps the last values and shows
  // a non-blocking warning instead of blanking the table.
  useEffect(() => {
    let controller: AbortController | null = null
    const revalidate = (): void => {
      controller?.abort()
      const next = new AbortController()
      controller = next
      runLoad(next.signal, false).catch((error: unknown) => {
        if (next.signal.aborted || isAbortError(error)) {
          return
        }
        setRefreshError(errorMessage(error))
      })
    }
    const interval = window.setInterval(revalidate, refreshMs)
    const onFocus = (): void => {
      revalidate()
    }
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') {
        revalidate()
      }
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
      controller?.abort()
    }
  }, [runLoad, refreshMs])

  const aggregate = aggregateNetPnl(load.items, clock)
  const notional = sumNotional(load.items)
  const margin = sumMargin(load.items)
  const riskPct = sumRiskPct(load.items)
  const openCount = load.state === 'ready' ? Math.max(load.total, load.items.length) : 0
  const partial = aggregate.missing > 0
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
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：持仓列表在 768px 及以下不可编辑。管理动作在持仓详情发起；Kill switch 仍可用。
        </p>
      )}

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
                  <span lang="en">net = price − fees + funding · CALC</span>
                )}
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Notional</dt>
              <dd className="mono">{formatMoney(notional)}</dd>
              <dd className="stat-note">USDT · {load.items.length} 个持仓 · Σ notional_usd</dd>
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
                <h2>部分 mark 不完整</h2>
                <span className={partial ? 'chip chip--warn' : 'chip'}>
                  {partial ? 'PARTIAL' : 'STALE'}
                </span>
              </div>
              <p className="panel-foot">
                {partial
                  ? `${aggregate.missing} 个持仓缺少 mark：其 uPnL 不计入合计，绝不当作 0。`
                  : '存在已过期的 mark。'}
                {stale ? ` ${aggregate.stale} 个 mark 已过期并标 STALE，保留最后值。` : ''}{' '}
                管理动作可能被保守的风险门阻断。
              </p>
            </div>
          )}

          <section className="panel" aria-labelledby="positions-table-heading">
            <div className="panel-head">
              <h2 id="positions-table-heading">持仓列表</h2>
              <span className="mono-muted">
                {load.items.length} / {load.total} · 每 5s 自动刷新；窗口聚焦时立即刷新
              </span>
            </div>
            <PositionTable positions={load.items} now={clock} />
            <p className="panel-foot">
              uPnL 由 mark、已记录费用与已记录资金费计算（CALC）；mark 缺失时标 STALE 且不写零。
              风险上限由服务端强制，本页不自行推断上限分母。
            </p>
          </section>
        </>
      )}
    </section>
  )
}
