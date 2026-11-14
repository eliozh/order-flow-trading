import { useCallback, useEffect, useRef, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  clearManagementKey,
  managementKeyFor,
  markState,
  PositionApiError,
  type PositionData,
  type PositionManageData,
  type PositionManageEnvelope,
} from '../api/positions'
import { useDialogFocus } from './use-dialog-focus'
import { managementBlockedReason, type DetailState } from '../routes/position-detail-model'

export type ManagementErrorKind = 'refusal' | 'unknown' | 'blocked'

export interface ManagementError {
  kind: ManagementErrorKind
  message: string
  httpStatus?: number
}

/**
 * The user-facing consequence copy. Only an allowlisted pre-execution refusal
 * (``error.kind === 'refusal'``) may state that no order was created; every
 * other failure — including 409/transport/timeout — must say the result is
 * unknown, that the server may have executed, and that retrying with the same
 * Idempotency-Key replays the stored result (never a second order).
 */
export function managementErrorText(error: ManagementError): string {
  if (error.kind === 'blocked') {
    return `管理请求已阻止：${error.message}`
  }
  if (error.kind === 'refusal') {
    const status = error.httpStatus !== undefined ? `（HTTP ${error.httpStatus}）` : ''
    return (
      `管理请求失败：${error.message}${status} — 服务端拒绝了该请求，` +
      '未创建任何订单，持仓未改变。使用同一 Idempotency-Key 重试是安全的。'
    )
  }
  return (
    `管理请求失败：${error.message} — 结果未知：服务器可能已执行。` +
    '使用同一 Idempotency-Key 重试会重放已存结果，不会创建第二笔订单。'
  )
}

interface UsePositionManagementOptions {
  positionId: string
  load: DetailState
  now?: number
  narrow: boolean
  requestManagementFn: (
    positionId: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<PositionManageEnvelope>
  /** Commit a definitive result's position (null keeps the loaded one). */
  onPositionApplied: (position: PositionData | null) => void
  /** An unknown outcome: re-poll the position/history immediately. */
  onUnknownOutcome: () => void
}

export interface PositionManagement {
  dialogOpen: boolean
  acting: boolean
  actionError: ManagementError | null
  dialogClosedReason: string | null
  result: PositionManageData | null
  announcement: string
  panelRef: ReturnType<typeof useDialogFocus>
  openDialog: () => void
  closeDialog: () => void
  confirmManagement: () => Promise<void>
}

/**
 * The paper-only management request lifecycle: per-position key persistence,
 * submit-time re-gating, refusal/unknown classification, and the dialog's
 * focus contract. A definitive success or an allowlisted pre-execution refusal
 * clears the pending key; every unknown outcome (409 conflict, transport,
 * timeout) keeps it and triggers an immediate re-poll.
 */
export function usePositionManagement({
  positionId,
  load,
  now,
  narrow,
  requestManagementFn,
  onPositionApplied,
  onUnknownOutcome,
}: UsePositionManagementOptions): PositionManagement {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<ManagementError | null>(null)
  const [dialogClosedReason, setDialogClosedReason] = useState<string | null>(null)
  const [result, setResult] = useState<PositionManageData | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const inFlight = useRef(false)
  const actionController = useRef<AbortController | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      // The request is allowed to settle so its definitive result is surfaced;
      // aborting on unmount is cleanup only.
      actionController.current?.abort()
    }
  }, [])

  const closeDialog = useCallback((): void => {
    // Never cancel an in-flight mutation: the server may already have executed
    // it. The dialog stays open (Cancel is disabled) until it settles.
    if (inFlight.current) {
      return
    }
    setDialogOpen(false)
    setActionError(null)
  }, [])

  const panelRef = useDialogFocus(dialogOpen, closeDialog, {
    inert: 'shell-except-kill-switch',
    includeKillSwitch: true,
    fallbackFocusId: 'position-detail-heading',
    skipDisabledInvoker: true,
  })

  function openDialog(): void {
    setActionError(null)
    setDialogClosedReason(null)
    setDialogOpen(true)
  }

  // A second aria-modal dialog must never stack on the management dialog. When
  // the kill switch turns active or unknown, close it with a visible, announced
  // reason. An in-flight mutation is allowed to settle first. A safety error
  // already shown (unknown/refusal copy) is preserved: closing the dialog must
  // not erase the warning the operator needs to act on.
  useEffect(() => {
    if (!dialogOpen || acting || inFlight.current || load.state !== 'ready') {
      return
    }
    const killSwitch = load.killSwitch ?? null
    if (killSwitch !== true && killSwitch !== null) {
      return
    }
    const reason = killSwitch === null ? 'Kill switch 状态未知' : 'Kill switch 已启用'
    const text = `${reason}：管理决策弹窗已自动关闭；不会叠加开启 Kill switch 的恢复弹窗。`
    setDialogOpen(false)
    setDialogClosedReason(text)
    setAnnouncement(text)
  }, [dialogOpen, acting, load.state, load.killSwitch])

  async function confirmManagement(): Promise<void> {
    // A duplicate click must not send a second mutation, even before React
    // flushes the aria-disabled state.
    if (inFlight.current) {
      return
    }
    if (load.state !== 'ready' || !load.position) {
      return
    }
    // Recompute the gate at submit time so a mark that aged (or a kill switch
    // that changed) since the dialog opened cannot be submitted.
    const submitMark = markState(load.position, now ?? Date.now())
    const submitBlocked = managementBlockedReason(
      load.position,
      submitMark,
      load.killSwitch ?? null,
      narrow,
    )
    if (submitBlocked !== null) {
      setActionError({ kind: 'blocked', message: submitBlocked })
      setAnnouncement(`管理请求已阻止：${submitBlocked}`)
      return
    }
    inFlight.current = true
    setActing(true)
    setActionError(null)
    const controller = new AbortController()
    actionController.current = controller
    // The stored per-position key is reused until a definitive outcome.
    const key = managementKeyFor(positionId)
    try {
      const envelope = await requestManagementFn(positionId, key, controller.signal)
      if (controller.signal.aborted) {
        return
      }
      const data = envelope.data
      if (!data) {
        throw new Error('The management response was empty.')
      }
      setResult(data)
      onPositionApplied(data.position ?? null)
      setDialogOpen(false)
      // A definitive result releases the key; the next request rotates.
      clearManagementKey(positionId)
      setAnnouncement(`管理决策已返回：${data.action} / ${data.status}。`)
    } catch (error: unknown) {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      const definitiveRefusal = error instanceof PositionApiError && error.kind === 'refusal'
      const classified: ManagementError = {
        kind: definitiveRefusal ? 'refusal' : 'unknown',
        message: errorMessage(error),
        httpStatus: error instanceof PositionApiError ? error.httpStatus : undefined,
      }
      setActionError(classified)
      if (definitiveRefusal) {
        // An allowlisted pre-execution rejection wrote nothing: rotate for the
        // next try.
        clearManagementKey(positionId)
        setAnnouncement(`管理请求被拒绝：${classified.message}`)
      } else {
        // Keep the key so a retry replays the stored result; re-poll now.
        setAnnouncement(
          `管理结果未知：${classified.message} — 服务器可能已执行；` +
            '使用同一 Idempotency-Key 重试会重放已存结果。',
        )
        onUnknownOutcome()
      }
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setActing(false)
      }
    }
  }

  return {
    dialogOpen,
    acting,
    actionError,
    dialogClosedReason,
    result,
    announcement,
    panelRef,
    openDialog,
    closeDialog,
    confirmManagement,
  }
}
