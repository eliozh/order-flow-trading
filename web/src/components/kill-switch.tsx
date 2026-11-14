import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  activateKillSwitch,
  deactivateKillSwitch,
  fetchKillSwitch,
  type KillSwitchEnvelope,
  type KillSwitchStatus,
} from '../api/paper'

interface KillSwitchBarProps {
  fetchStatusFn?: () => Promise<KillSwitchEnvelope>
  activateFn?: () => Promise<KillSwitchEnvelope>
  deactivateFn?: (token: string) => Promise<KillSwitchEnvelope>
}

const REFRESH_MS = 15000

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

function focusables(panel: HTMLElement | null): HTMLElement[] {
  if (!panel) {
    return []
  }
  return Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
}

/**
 * Modal focus contract: initial focus, Tab/Shift+Tab trap, Escape from
 * anywhere, inert background, locked body scroll, and focus restoration.
 */
function useDialogFocus(open: boolean, onClose: () => void) {
  const panelRef = useRef<HTMLDivElement>(null)
  const restoreRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open || typeof document === 'undefined') {
      return
    }
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current
    const initial =
      panel?.querySelector<HTMLElement>('[data-autofocus]') ?? focusables(panel)[0] ?? panel
    initial?.focus()

    const shell = document.querySelector('.app-shell')
    const shellWasInert = shell?.hasAttribute('inert') ?? false
    shell?.setAttribute('inert', '')
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') {
        return
      }
      const items = focusables(panel)
      if (items.length === 0) {
        event.preventDefault()
        panel?.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (
        event.shiftKey &&
        (document.activeElement === first || document.activeElement === panel)
      ) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = previousOverflow
      if (!shellWasInert) {
        shell?.removeAttribute('inert')
      }
      const target = restoreRef.current
      if (target && target.isConnected) {
        target.focus()
      } else {
        document.getElementById('kill-switch-heading')?.focus()
      }
    }
  }, [open, onClose])

  return panelRef
}

/**
 * The global kill switch. Stopping is always allowed and immediate; resuming
 * requires the typed RESUME token. It is never hidden at 375px: stopping must
 * never be blocked by a read-only or narrow surface.
 */
export function KillSwitchBar({
  fetchStatusFn = fetchKillSwitch,
  activateFn = activateKillSwitch,
  deactivateFn = deactivateKillSwitch,
}: KillSwitchBarProps) {
  const [status, setStatus] = useState<KillSwitchStatus | null>(null)
  const [probeError, setProbeError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [token, setToken] = useState('')
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const probe = useCallback(async (): Promise<void> => {
    try {
      const envelope = await fetchStatusFn()
      if (!mounted.current) {
        return
      }
      if (envelope.data) {
        setStatus(envelope.data)
        setProbeError(null)
      } else {
        setProbeError('The kill-switch status response was empty.')
      }
    } catch (error: unknown) {
      if (mounted.current) {
        setProbeError(message(error))
      }
    }
  }, [fetchStatusFn])

  useEffect(() => {
    void probe()
  }, [probe])

  // Revalidate on an interval and whenever the window regains focus, so a CLI
  // `oft kill on` is reflected without a reload.
  useEffect(() => {
    const interval = window.setInterval(() => {
      void probe()
    }, REFRESH_MS)
    const onFocus = (): void => {
      void probe()
    }
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', onFocus)
    }
  }, [probe])

  async function activate(): Promise<void> {
    setActing(true)
    setActionError(null)
    try {
      const envelope = await activateFn()
      if (envelope.data) {
        setStatus(envelope.data)
        setProbeError(null)
        setAnnouncement('Kill switch 已启用：新订单已停止。')
      }
    } catch (error: unknown) {
      const errorText = message(error)
      setActionError(errorText)
      setAnnouncement(`Kill switch 启用失败：${errorText}`)
    } finally {
      setActing(false)
    }
  }

  async function resume(): Promise<void> {
    setActing(true)
    setActionError(null)
    try {
      const envelope = await deactivateFn(token)
      if (envelope.data) {
        setStatus(envelope.data)
        setAnnouncement('Kill switch 已关闭：可发送新订单。')
      }
      setOpen(false)
      setToken('')
    } catch (error: unknown) {
      // Keep the dialog open and show the failure next to the token input.
      const errorText = message(error)
      setActionError(errorText)
      setAnnouncement(`Kill switch 恢复失败：${errorText}`)
    } finally {
      setActing(false)
    }
  }

  const closeDialog = useCallback((): void => {
    setOpen(false)
    setToken('')
    setActionError(null)
  }, [])

  // A resume dialog must never stack on another modal (for example the
  // management dialog that stays open while a mutation settles). The management
  // trap never includes this control, and this click guard is the last line.
  function openResumeDialog(): void {
    if (
      typeof document !== 'undefined' &&
      document.querySelector('[role="dialog"][aria-modal="true"]')
    ) {
      setAnnouncement('另一个弹窗已打开：请先关闭它，再恢复订单发送。')
      return
    }
    setActionError(null)
    setToken('')
    setOpen(true)
  }

  const panelRef = useDialogFocus(open, closeDialog)
  const active = status?.active === true
  const unknown = status === null
  const chipClass = unknown ? 'chip' : active ? 'chip chip--danger' : 'chip chip--ok'

  return (
    <section
      className="kill-switch-bar"
      aria-label="Kill switch"
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 12,
        padding: '8px 16px',
        borderBottom: '1px solid var(--oft-border-default)',
        background: active ? '#2a0e12' : 'var(--oft-bg-panel)',
        color: 'var(--oft-text-primary)',
        fontFamily: 'var(--oft-font-mono)',
        fontSize: 11,
      }}
    >
      <span id="kill-switch-heading" className="visually-hidden" tabIndex={-1}>
        Kill switch
      </span>
      <span className={chipClass} aria-live="assertive" aria-atomic="true">
        {unknown ? (
          '状态未知'
        ) : active ? (
          <span lang="en">KILL SWITCH ON</span>
        ) : (
          <span lang="en">KILL SWITCH OFF</span>
        )}
      </span>
      <span className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </span>

      {probeError !== null && (
        <span role="alert" className="value-down">
          Kill switch 状态不可用：{probeError}
        </span>
      )}
      {actionError !== null && !open && (
        <span role="alert" className="value-down">
          {actionError}
        </span>
      )}

      {active ? (
        <button
          type="button"
          className="btn"
          data-kill-switch-action="resume"
          aria-label="Kill switch 开启，可停止新订单"
          disabled={acting}
          onClick={openResumeDialog}
        >
          Kill switch 开启，可停止新订单
        </button>
      ) : (
        <button
          type="button"
          className="btn"
          data-kill-switch-action="activate"
          aria-label="启用 Kill switch（立即停止新订单）"
          disabled={acting}
          onClick={() => {
            void activate()
          }}
        >
          启用 Kill switch（立即停止新订单）
        </button>
      )}

      <span className="mono-muted">
        {active ? 'no new orders will be sent · 停止无需确认' : '关闭需输入 RESUME；移动端仍可用'}
      </span>

      {open &&
        active &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            className="dialog-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="kill-switch-off-title"
            aria-describedby={
              actionError === null
                ? 'kill-switch-off-consequences'
                : 'kill-switch-off-consequences kill-switch-action-error'
            }
          >
            <div className="dialog-panel dialog-panel--narrow" ref={panelRef} tabIndex={-1}>
              <form
                onSubmit={(event) => {
                  event.preventDefault()
                  void resume()
                }}
              >
                <h2 id="kill-switch-off-title" style={{ marginTop: 0 }}>
                  关闭 <span lang="en">Kill switch</span>
                </h2>
                <p className="status-message" id="kill-switch-off-consequences">
                  恢复订单发送会让新的 Paper 订单进入模拟引擎。输入 RESUME 继续。
                </p>
                <label
                  htmlFor="kill-switch-token"
                  style={{ display: 'block', margin: '12px 0 6px', fontSize: 12 }}
                >
                  输入 <span lang="en">RESUME</span> 以恢复订单发送
                </label>
                <input
                  id="kill-switch-token"
                  className="mono"
                  value={token}
                  data-autofocus
                  autoCapitalize="off"
                  spellCheck={false}
                  aria-invalid={actionError !== null}
                  aria-describedby={
                    actionError === null
                      ? 'kill-switch-off-consequences'
                      : 'kill-switch-action-error'
                  }
                  onChange={(event) => setToken(event.target.value)}
                  style={{
                    width: '100%',
                    minHeight: 44,
                    padding: '4px 10px',
                    border: '1px solid var(--oft-border-control)',
                    background: 'var(--oft-bg-inset, #07080a)',
                    color: 'var(--oft-text-primary)',
                  }}
                />
                {actionError !== null && (
                  <p
                    id="kill-switch-action-error"
                    role="alert"
                    className="status-message--error"
                    style={{ marginTop: 8 }}
                  >
                    恢复失败：{actionError}
                  </p>
                )}
                <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
                  <button type="button" className="btn" onClick={closeDialog}>
                    取消
                  </button>
                  <button type="submit" className="btn" disabled={token !== 'RESUME' || acting}>
                    输入 RESUME 并关闭
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
