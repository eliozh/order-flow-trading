import { useCallback, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

import { useDialogFocus } from '../hooks/use-dialog-focus'

export interface ConfirmDiffRow {
  field: string
  label: string
  before: string
  after: string
}

interface ConfirmDialogProps {
  open: boolean
  title: string
  description: string
  rows: ConfirmDiffRow[]
  confirmLabel?: string
  cancelLabel?: string
  busy?: boolean
  /** Stable focus target when the invoker was disabled (e.g. the save button). */
  restoreFocusId?: string
  onCancel: () => void
  onConfirm: () => void
}

const TITLE_ID = 'risk-confirm-title'
const DESCRIPTION_ID = 'risk-confirm-description'

/**
 * The reusable before/after confirmation dialog. Initial focus is the least
 * destructive control (取消), Escape closes through ``useDialogFocus``, and the
 * panel is a named modal with its consequence described.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  rows,
  confirmLabel = '确认放宽',
  cancelLabel = '取消',
  busy = false,
  restoreFocusId,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  // Keep the close callback identity stable across ``busy`` and parent
  // re-renders. ``useDialogFocus`` keys its whole focus effect on the callback,
  // so a new identity while the confirmed PUT is in flight would tear the trap
  // down, restore focus to the invoker outside the modal, and leave focus on
  // ``<body>``. Refs let Escape still observe the latest busy/onCancel values.
  const busyRef = useRef(busy)
  const onCancelRef = useRef(onCancel)
  useEffect(() => {
    busyRef.current = busy
    onCancelRef.current = onCancel
  }, [busy, onCancel])
  // Escape must not close the dialog while a confirmed save is in flight: the
  // PUT may still write, and closing would claim "nothing was written".
  const requestClose = useCallback(() => {
    if (busyRef.current) {
      return
    }
    onCancelRef.current()
  }, [])
  const panelRef = useDialogFocus(open, requestClose, {
    // Keep the kill switch outside the inert set and in the tab cycle so
    // stopping is never blocked by a confirmation (DESIGN A2.4/A10).
    inert: 'shell-except-kill-switch',
    includeKillSwitch: true,
    fallbackFocusId: restoreFocusId,
  })

  if (!open || typeof document === 'undefined') {
    return null
  }

  return createPortal(
    <div
      className="dialog-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby={TITLE_ID}
      aria-describedby={DESCRIPTION_ID}
      data-testid="risk-confirm-dialog"
    >
      <div className="dialog-panel" ref={panelRef} tabIndex={-1}>
        <h2 id={TITLE_ID} style={{ marginTop: 0 }}>
          {title}
        </h2>
        <p className="status-message" id={DESCRIPTION_ID}>
          {description}
        </p>
        <div className="table-wrap risk-diff-wrap">
          <table className="data risk-diff" aria-label="上限变更前后对比">
            <thead>
              <tr>
                <th scope="col">上限</th>
                <th scope="col" className="num">
                  旧值
                </th>
                <th scope="col" className="num">
                  新值
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.field}>
                  <th scope="row">{row.label}</th>
                  <td className="num mono">{row.before}</td>
                  <td className="num mono">{row.after}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="hint">
          放宽上限会让代码强制的上限更宽松；确认后写入一条 <span lang="en">RISK_CAPS_UPDATED</span>{' '}
          audit，记录 before/after。取消不会写入任何变更。
        </p>
        <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
          <button type="button" className="btn" data-autofocus disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={onConfirm}>
            {busy ? '保存中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
