/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- the queue table is a keyboard-scrollable region */
import { useEffect, useState } from 'react'

import { isAbortError } from '../api/abort'
import { fetchOpportunity, type OpportunityEnvelope } from '../api/opportunities'
import { confidenceBand, utcLabel } from '../api/reports'
import {
  fetchTriggers,
  formatAge,
  getTrigger,
  isPendingStatus,
  type TriggerDetailEnvelope,
  type TriggerListEnvelope,
  type TriggerPayload,
  type TriggerSummary,
  riskVerdictChip,
} from '../api/triggers'
import { useCompact } from '../hooks/use-compact'

interface TriggersPageProps {
  fetchTriggersFn?: (
    options: { limit: number },
    signal?: AbortSignal,
  ) => Promise<TriggerListEnvelope>
  fetchTriggerDetailFn?: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
  fetchOpportunityFn?: (opportunityId: string, signal?: AbortSignal) => Promise<OpportunityEnvelope>
  nowMs?: number
  isCompact?: boolean
}

interface QueueRow {
  summary: TriggerSummary
  detail: TriggerPayload | null
  confidence: number | null
}

interface QueueState {
  state: 'loading' | 'ready' | 'error'
  rows: QueueRow[]
  message?: string
}

function stateChip(status: string): { label: string; className: string } {
  if (isPendingStatus(status)) {
    return { label: 'PENDING', className: 'chip chip--warn' }
  }
  if (status === 'ACCEPTED') {
    return { label: 'ACCEPTED', className: 'chip chip--ok' }
  }
  if (status === 'REJECTED' || status === 'BLOCKED') {
    return { label: status, className: 'chip chip--danger' }
  }
  if (status === 'EXPIRED') {
    return { label: 'EXPIRED', className: 'chip' }
  }
  return { label: status, className: 'chip' }
}

/** Pending human decisions first, then newest triggers. */
function orderRows(rows: QueueRow[]): QueueRow[] {
  return [...rows].sort((left, right) => {
    const leftPending = isPendingStatus(left.summary.status) ? 0 : 1
    const rightPending = isPendingStatus(right.summary.status) ? 0 : 1
    if (leftPending !== rightPending) {
      return leftPending - rightPending
    }
    return Date.parse(right.summary.triggered_at) - Date.parse(left.summary.triggered_at)
  })
}

export function TriggersPage({
  fetchTriggersFn = fetchTriggers,
  fetchTriggerDetailFn = getTrigger,
  fetchOpportunityFn = fetchOpportunity,
  nowMs,
  isCompact,
}: TriggersPageProps) {
  const [queue, setQueue] = useState<QueueState>({ state: 'loading', rows: [] })
  const compact = useCompact(isCompact)

  useEffect(() => {
    const controller = new AbortController()
    setQueue({ state: 'loading', rows: [] })
    async function load(): Promise<void> {
      const envelope = await fetchTriggersFn({ limit: 50 }, controller.signal)
      const items = envelope.data?.items ?? []
      if (controller.signal.aborted) {
        return
      }
      // Render the summary rows immediately; details hydrate progressively.
      setQueue({
        state: 'ready',
        rows: orderRows(
          items.map((summary) => ({
            summary,
            detail: null,
            confidence: null,
          })),
        ),
      })
      // Resolved rows need no detail fan-out; their state is in the summary.
      const pending = items.filter((summary) => isPendingStatus(summary.status))
      await Promise.all(
        pending.map(async (summary) => {
          const [detail, opportunity] = await Promise.allSettled([
            fetchTriggerDetailFn(summary.trigger_id, controller.signal),
            fetchOpportunityFn(summary.opportunity_id, controller.signal),
          ])
          if (controller.signal.aborted) {
            return
          }
          setQueue((previous) => ({
            ...previous,
            rows: previous.rows.map((row) =>
              row.summary.trigger_id === summary.trigger_id
                ? {
                    ...row,
                    detail: detail.status === 'fulfilled' ? detail.value.data : null,
                    confidence:
                      opportunity.status === 'fulfilled'
                        ? (opportunity.value.data?.confidence ?? null)
                        : null,
                  }
                : row,
            ),
          }))
        }),
      )
    }
    load().catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      setQueue({
        state: 'error',
        rows: [],
        message: error instanceof Error ? error.message : 'Unknown error',
      })
    })
    return () => {
      controller.abort()
    }
  }, [fetchTriggersFn, fetchTriggerDetailFn, fetchOpportunityFn])

  const pending = queue.rows.filter((row) => isPendingStatus(row.summary.status)).length
  const resolved = queue.rows.length - pending
  const clock = nowMs ?? Date.now()

  return (
    <section className="system-page" aria-labelledby="triggers-heading" data-readonly={compact}>
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/opportunities">
              机会台账
            </a>{' '}
            / 入场复核
          </p>
          <h1 id="triggers-heading">入场复核队列</h1>
          <p className="page-sub">
            待处理决策优先。接受/拒绝只发生在详情；风险 verdict 由代码计算。环境为 PAPER。
          </p>
        </div>
        <div className="report-stamps">
          <span className="chip chip--calc">CALC</span>
          <span className="chip">PAPER</span>
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：接受/拒绝请在桌面端或 CLI 处理。Kill switch 仍可用。
        </p>
      )}

      {queue.state === 'loading' && queue.rows.length === 0 && (
        <p className="status-message" role="status">
          加载入场复核队列…
        </p>
      )}

      {queue.state === 'error' && (
        <div className="panel" role="alert">
          <div className="panel-head">
            <h2>队列加载失败</h2>
          </div>
          <p className="panel-foot">
            无法读取决策队列：{queue.message}。Kill switch 仍在页面顶部可用。
          </p>
        </div>
      )}

      {queue.state === 'ready' && queue.rows.length === 0 && (
        <div className="panel" role="status">
          <div className="panel-head">
            <h2>没有待处理的入场复核</h2>
          </div>
          <p className="panel-foot">
            还没有触发事件。机会由扫描提出，触发由代码判定；新的触发会在这里出现。
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <a className="btn" href="/opportunities">
              打开机会台账
            </a>
            <a className="btn" href="/scans">
              运行扫描
            </a>
          </div>
        </div>
      )}

      {queue.rows.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <h2>入场复核队列</h2>
            <span className="mono-muted">risk verdict 由代码计算</span>
          </div>
          <div className="table-wrap" role="region" tabIndex={0} aria-label="入场复核队列表">
            <table className="data-table">
              <caption className="visually-hidden">
                入场复核队列 · 待处理 {pending} · 已处理 {resolved}
              </caption>
              <thead lang="en">
                <tr>
                  <th scope="col">Decision</th>
                  <th scope="col">Symbol</th>
                  <th scope="col">Side</th>
                  <th scope="col">Age</th>
                  <th scope="col">Conf</th>
                  <th scope="col">Risk</th>
                  <th scope="col">Env</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {queue.rows.map((row) => {
                  const status = row.detail?.status ?? row.summary.status
                  const state = stateChip(status)
                  const verdict = row.detail?.risk_verdict ?? null
                  const risk =
                    row.detail === null
                      ? isPendingStatus(status)
                        ? { label: 'PENDING', className: 'chip' }
                        : { label: '—', className: 'chip' }
                      : riskVerdictChip(verdict)
                  const band = row.confidence === null ? null : confidenceBand(row.confidence)
                  return (
                    <tr key={row.summary.trigger_id}>
                      <td>
                        <a
                          className="rowlink mono"
                          href={`/triggers/${encodeURIComponent(row.summary.trigger_id)}`}
                        >
                          {row.summary.trigger_id}
                        </a>
                      </td>
                      <td className="mono">{row.summary.symbol}</td>
                      <td className={row.summary.side === 'SHORT' ? 'value-down' : 'value-up'}>
                        {row.summary.side === 'SHORT' ? '▼ SHORT' : '▲ LONG'}
                      </td>
                      <td className="mono">{formatAge(row.summary.triggered_at, clock)}</td>
                      <td>
                        <span className="mono">
                          {row.confidence === null ? '—' : row.confidence}
                        </span>{' '}
                        {band !== null && <span className="chip">{band}</span>}
                      </td>
                      <td>
                        <span className={risk.className}>{risk.label}</span>
                      </td>
                      <td>
                        <span className="env-chip env-chip--paper" lang="en">
                          {row.detail?.environment ?? '—'}
                        </span>
                      </td>
                      <td>
                        <span className={state.className}>{state.label}</span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="panel-foot">
            共 {queue.rows.length} 条 · 年龄按 triggered_at 计算 · 全部时间以 UTC 显示（参考{' '}
            {utcLabel(new Date(clock).toISOString())} UTC）。
          </p>
        </div>
      )}
    </section>
  )
}
