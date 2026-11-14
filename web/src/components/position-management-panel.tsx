import { createPortal } from 'react-dom'
import type { RefObject } from 'react'

import type { PositionData, PositionManageData } from '../api/positions'
import { resolvedMark } from '../api/positions'
import { recordField } from '../api/records'
import { ManagementTimeline } from './management-timeline'
import { managementErrorText, type ManagementError } from '../hooks/use-position-management'

interface PositionManagementPanelProps {
  position: PositionData
  blockedReason: string | null
  acting: boolean
  actionError: ManagementError | null
  dialogOpen: boolean
  dialogClosedReason: string | null
  result: PositionManageData | null
  announcement: string
  panelRef: RefObject<HTMLDivElement | null>
  onOpenDialog: () => void
  onCloseDialog: () => void
  onConfirm: () => void
}

/**
 * The paper-only management surface and its confirmation dialog.
 *
 * The status region is portaled to ``document.body`` so announcements fire
 * while the page content is inert under an open dialog (DESIGN A2.4).
 */
export function PositionManagementPanel({
  position,
  blockedReason,
  acting,
  actionError,
  dialogOpen,
  dialogClosedReason,
  result,
  announcement,
  panelRef,
  onOpenDialog,
  onCloseDialog,
  onConfirm,
}: PositionManagementPanelProps) {
  const orderId = recordField(result?.order, 'order_id')
  const fillId = recordField(result?.fill, 'fill_id')
  const errorText = actionError ? managementErrorText(actionError) : null

  return (
    <section className="panel" aria-labelledby="manage-heading">
      <div className="panel-head">
        <h2 id="manage-heading">管理决策</h2>
        <span className="chip chip--calc" lang="en">
          PAPER
        </span>
      </div>
      <div style={{ padding: 16 }}>
        <div className="action-row" style={{ marginTop: 0 }}>
          <button
            type="button"
            className={blockedReason !== null ? 'btn btn--muted' : 'btn'}
            onClick={() => {
              if (blockedReason !== null || acting) {
                return
              }
              onOpenDialog()
            }}
            disabled={acting}
            aria-disabled={blockedReason !== null || undefined}
            aria-describedby={blockedReason ? 'manage-blocked-reason' : undefined}
          >
            {acting ? '请求进行中…' : '请求管理决策'}
          </button>
          <span className="mono-muted">仅在 Paper 中由代码校验后执行；不会发送到交易所。</span>
        </div>
        {blockedReason !== null && (
          <p id="manage-blocked-reason" className="panel-foot" role="note">
            {blockedReason}
          </p>
        )}
        {actionError !== null && errorText !== null && (
          <p
            role="alert"
            className="status-message--error"
            data-testid="manage-error"
            data-kind={actionError.kind}
            style={{ marginTop: 12 }}
          >
            {errorText}
          </p>
        )}
        {dialogClosedReason !== null && (
          <p className="status-message" role="status" data-testid="manage-dialog-closed-reason">
            {dialogClosedReason}
          </p>
        )}
        {(result || position.management_history.length > 0) && (
          <ManagementTimeline entries={position.management_history} result={result} />
        )}
        {result && result.executed && (
          <p className="panel-foot">
            已执行：订单 {orderId ?? '—'} · fill {fillId ?? '—'}。绑定上限{' '}
            {result.risk?.binding_cap ?? 'N/A'}；LLM 不能绕过任何风险上限。
          </p>
        )}
        <p className="panel-foot">
          LLM 只提议，CALC 校验并执行。确认后使用一个 Idempotency-Key 与 CSRF token；重复点击不会
          创建第二笔订单。该 key 按持仓保存在当前浏览器会话中，直到出现确定结果。
        </p>
      </div>

      {typeof document !== 'undefined' &&
        createPortal(
          <p className="visually-hidden" role="status" aria-live="polite" data-testid="manage-live">
            {announcement}
          </p>,
          document.body,
        )}

      {dialogOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            className="dialog-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="manage-title"
            aria-describedby="manage-consequence manage-killswitch-note"
          >
            <div className="dialog-panel" ref={panelRef} tabIndex={-1}>
              <form
                aria-busy={acting}
                onSubmit={(event) => {
                  event.preventDefault()
                  onConfirm()
                }}
              >
                <h2 id="manage-title" style={{ marginTop: 0 }}>
                  请求管理决策（Paper）
                </h2>
                <p className="status-message" id="manage-consequence">
                  管理决策在 Paper 中由代码校验后执行：LLM 只提议，CALC
                  校验并执行；不会发送到交易所， 也不会绕过风险上限。重复提交使用同一个
                  Idempotency-Key，不会创建第二笔订单。
                </p>
                <dl className="kv" style={{ padding: 0 }}>
                  <dt>持仓</dt>
                  <dd className="mono">
                    {position.position_id} · {position.symbol} {position.side}
                  </dd>
                  <dt>Mark</dt>
                  <dd className="mono">{resolvedMark(position) ?? '—'}</dd>
                  <dt>Bindings</dt>
                  <dd className="mono">CSRF + Idempotency-Key</dd>
                </dl>
                <p id="manage-killswitch-note" className="visually-hidden">
                  Kill switch 在弹窗之外仍可用，并作为 Tab 循环的最后一个停留点。
                </p>
                {actionError !== null && errorText !== null && (
                  <p
                    role="alert"
                    className="status-message--error"
                    data-testid="manage-dialog-error"
                    data-kind={actionError.kind}
                    style={{ marginTop: 12 }}
                  >
                    {errorText}
                  </p>
                )}
                <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
                  <button
                    type="button"
                    className="btn"
                    data-autofocus
                    disabled={acting}
                    onClick={onCloseDialog}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className={acting ? 'btn btn--muted' : 'btn'}
                    aria-disabled={acting || undefined}
                    aria-busy={acting}
                  >
                    {acting ? '请求中…' : '确认请求管理'}
                  </button>
                </div>
              </form>
            </div>
          </div>,
          document.body,
        )}
    </section>
  )
}
