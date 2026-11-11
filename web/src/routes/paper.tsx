import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

import { isAbortError } from '../api/abort'
import {
  fetchPaperAccount,
  fetchPaperFills,
  fetchPaperOrders,
  type PaperAccountData,
  type PaperAccountEnvelope,
  type PaperFill,
  type PaperFillsEnvelope,
  type PaperOrder,
  type PaperOrdersEnvelope,
} from '../api/paper'
import { PaperFillsTable, PaperOrdersTable, PaperPositionsTable } from '../components/paper-tables'
import { useCompact } from '../hooks/use-compact'

interface PaperPageProps {
  fetchAccountFn?: (signal?: AbortSignal) => Promise<PaperAccountEnvelope>
  fetchOrdersFn?: (signal?: AbortSignal) => Promise<PaperOrdersEnvelope>
  fetchFillsFn?: (signal?: AbortSignal) => Promise<PaperFillsEnvelope>
  isCompact?: boolean
}

interface PaperState {
  state: 'loading' | 'ready' | 'error'
  account?: PaperAccountData
  orders: PaperOrder[]
  ordersTotal: number
  fills: PaperFill[]
  fillsTotal: number
  message?: string
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
      </div>
      <div style={{ padding: 16 }}>{children}</div>
    </section>
  )
}

/**
 * The paper account: equity, orders, fills, and open positions read directly
 * from the CALC read models. There is no paper reset, no live toggle, and no
 * position-management action on this surface.
 */
export function PaperPage({
  fetchAccountFn = fetchPaperAccount,
  fetchOrdersFn = fetchPaperOrders,
  fetchFillsFn = fetchPaperFills,
  isCompact,
}: PaperPageProps) {
  const [load, setLoad] = useState<PaperState>({
    state: 'loading',
    orders: [],
    ordersTotal: 0,
    fills: [],
    fillsTotal: 0,
  })
  const compact = useCompact(isCompact)

  useEffect(() => {
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    async function run(): Promise<void> {
      const [account, orders, fills] = await Promise.all([
        fetchAccountFn(controller.signal),
        fetchOrdersFn(controller.signal),
        fetchFillsFn(controller.signal),
      ])
      if (controller.signal.aborted) {
        return
      }
      if (!account.data) {
        throw new Error('The paper account response was empty.')
      }
      setLoad({
        state: 'ready',
        account: account.data,
        orders: orders.data?.items ?? [],
        ordersTotal: orders.data?.total ?? 0,
        fills: fills.data?.items ?? [],
        fillsTotal: fills.data?.total ?? 0,
      })
    }
    run().catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      setLoad({
        state: 'error',
        orders: [],
        ordersTotal: 0,
        fills: [],
        fillsTotal: 0,
        message: errorMessage(error),
      })
    })
    return () => {
      controller.abort()
    }
  }, [fetchAccountFn, fetchOrdersFn, fetchFillsFn])

  const hasTrades =
    load.orders.length > 0 || load.fills.length > 0 || (load.account?.positions.length ?? 0) > 0
  const stale = load.account?.stale_inputs ?? []

  return (
    <section className="system-page" aria-labelledby="paper-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">
            <span lang="en">Paper</span> / 账户
          </p>
          <h1 id="paper-heading">Paper 交易账户</h1>
          <p className="page-sub">
            模拟余额、订单、成交与持仓。用于至少 4 周的闭环验证；本页只读，不含重置或 Live
            开关。货币 <span className="mono">USDT</span>。
          </p>
        </div>
        <div className="report-stamps">
          <span className="chip chip--calc">CALC</span>
          <span className="env-chip env-chip--paper">PAPER</span>
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：Paper 账户在 375px 不可编辑。Kill switch 仍可用。
        </p>
      )}

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          加载 Paper 账户…
        </p>
      )}

      {load.state === 'error' && (
        <div className="panel" role="alert">
          <div className="panel-head">
            <h2>paper 引擎不可用</h2>
          </div>
          <p className="panel-foot">
            {load.message} — 显示失败，不伪造余额或成交；Kill switch 仍可用。
          </p>
        </div>
      )}

      {load.state === 'ready' && load.account && (
        <>
          <dl className="stat-grid">
            <div className="stat">
              <dt lang="en">Equity</dt>
              <dd className="mono">{load.account.equity}</dd>
              <dd className="stat-note" lang="en">
                cash + Σ(size × last mark)
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Cash balance</dt>
              <dd className="mono">{load.account.cash_balance}</dd>
              <dd className="stat-note" lang="en">
                paper balance
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Marked value</dt>
              <dd className="mono">{load.account.unrealized_at_last_mark}</dd>
              <dd className="stat-note" lang="en">
                at last mark · not a PnL number
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Open positions</dt>
              <dd className="mono">{load.account.open_positions} 个未平仓</dd>
              <dd className="stat-note">{load.account.account_id}</dd>
            </div>
          </dl>

          {stale.length > 0 && (
            <div className="panel" role="status">
              <div className="panel-head">
                <h2>部分快照不完整</h2>
                <span className="chip chip--warn">STALE</span>
              </div>
              <p className="panel-foot">
                mark 不可用：{stale.join(', ')}。相关 uPnL 保持 N/A，不写零；不重写历史成交。
              </p>
            </div>
          )}

          {!hasTrades && (
            <div className="panel" role="status">
              <div className="panel-head">
                <h2>模拟账户尚未产生交易</h2>
              </div>
              <p className="panel-foot">
                初始余额 {load.account.initial_balance}{' '}
                USDT。运行一次扫描并接受触发的入场，才会产生模拟订单与成交；不生成占位成交。Paper
                reset 不在本切片提供。
              </p>
              <div className="action-row" style={{ padding: '0 16px 16px' }}>
                <a className="btn" href="/scans">
                  运行扫描
                </a>
                <a className="btn" href="/opportunities">
                  查看机会
                </a>
              </div>
            </div>
          )}

          <Panel title="持仓">
            <PaperPositionsTable positions={load.account.positions} />
            <p className="panel-foot">
              mark 来自本地 paper 引擎；mark 缺失时标 STALE，不伪造 PnL。
            </p>
          </Panel>

          <Panel title="订单">
            <PaperOrdersTable orders={load.orders} total={load.ordersTotal} />
            <p className="panel-foot">
              模拟订单；每笔订单绑定一个风险 verdict，且只允许一个 idempotency key。
            </p>
          </Panel>

          <Panel title="成交">
            <PaperFillsTable fills={load.fills} total={load.fillsTotal} />
            <p className="panel-foot">
              模拟 fill price 与 mark 的差异保留；缺失费用显示 N/A，不计为 0。
            </p>
          </Panel>
        </>
      )}
    </section>
  )
}
