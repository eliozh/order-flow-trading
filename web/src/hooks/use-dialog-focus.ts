import { useEffect, useRef } from 'react'

/** The tabbable controls inside one dialog panel, in DOM order. */
export function focusables(panel: HTMLElement | null): HTMLElement[] {
  if (!panel) {
    return []
  }
  return Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
}

export interface DialogFocusOptions {
  /**
   * ``shell`` inerts the whole app shell. ``shell-except-kill-switch`` inerts
   * every shell child except the kill-switch bar so stopping is never blocked.
   */
  inert?: 'shell' | 'shell-except-kill-switch'
  /** Append the kill switch ACTIVATE control as the final Tab stop. */
  includeKillSwitch?: boolean
  /** Heading id to focus when the invoker no longer exists. */
  fallbackFocusId?: string
  /** Treat a disabled invoker as gone instead of trying to focus it. */
  skipDisabledInvoker?: boolean
}

/**
 * The one modal focus contract (DESIGN A2.4): initial focus on
 * ``[data-autofocus]`` or the first focusable, Tab/Shift+Tab trap, Escape from
 * anywhere, inert background, locked body scroll, and focus restoration.
 *
 * Callers choose whether the global kill switch stays outside the inert set
 * and joins the cycle, and which heading receives focus when the invoker is
 * gone.
 */
export function useDialogFocus(
  open: boolean,
  onClose: () => void,
  options: DialogFocusOptions = {},
) {
  const {
    inert = 'shell',
    includeKillSwitch = false,
    fallbackFocusId,
    skipDisabledInvoker = false,
  } = options
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
    let shellWasInert = false
    const inertTargets: HTMLElement[] = []
    if (shell) {
      if (inert === 'shell') {
        shellWasInert = shell.hasAttribute('inert')
        shell.setAttribute('inert', '')
      } else {
        inertTargets.push(
          ...Array.from(shell.children).filter(
            (child): child is HTMLElement =>
              child instanceof HTMLElement && !child.classList.contains('kill-switch-bar'),
          ),
        )
      }
    }
    const inertWasSet = inertTargets.map((node) => node.hasAttribute('inert'))
    inertTargets.forEach((node) => node.setAttribute('inert', ''))
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    function killSwitchAction(): HTMLElement | null {
      // Only the ACTIVATE control may join the cycle. When the switch is active
      // its bar control opens the RESUME dialog and must never be a tab stop of
      // an already-open modal (that would stack two aria-modal dialogs).
      const control = document.querySelector<HTMLElement>('[data-kill-switch-action="activate"]')
      if (!control || control.hasAttribute('disabled') || control.closest('[inert]')) {
        return null
      }
      return control
    }

    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') {
        return
      }
      const dialogItems = focusables(panel)
      if (dialogItems.length === 0) {
        event.preventDefault()
        panel?.focus()
        return
      }
      const first = dialogItems[0]
      const lastDialog = dialogItems[dialogItems.length - 1]
      const killSwitch = includeKillSwitch ? killSwitchAction() : null
      const active = document.activeElement
      if (event.shiftKey) {
        if (killSwitch !== null && active === killSwitch) {
          event.preventDefault()
          lastDialog.focus()
        } else if (active === first || active === panel) {
          event.preventDefault()
          ;(killSwitch ?? lastDialog).focus()
        }
      } else if (killSwitch !== null && active === killSwitch) {
        event.preventDefault()
        first.focus()
      } else if (active === lastDialog) {
        event.preventDefault()
        ;(killSwitch ?? first).focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = previousOverflow
      if (shell && inert === 'shell') {
        if (!shellWasInert) {
          shell.removeAttribute('inert')
        }
      }
      inertTargets.forEach((node, index) => {
        if (!inertWasSet[index]) {
          node.removeAttribute('inert')
        }
      })
      const target = restoreRef.current
      const focusableTarget =
        target !== null &&
        target.isConnected &&
        (!skipDisabledInvoker ||
          (!(target instanceof HTMLButtonElement && target.disabled) &&
            target.getAttribute('aria-disabled') !== 'true'))
      if (focusableTarget) {
        target.focus()
      }
      if (!focusableTarget || document.activeElement !== target) {
        if (fallbackFocusId) {
          document.getElementById(fallbackFocusId)?.focus()
        }
      }
    }
  }, [open, onClose, inert, includeKillSwitch, fallbackFocusId, skipDisabledInvoker])

  return panelRef
}
