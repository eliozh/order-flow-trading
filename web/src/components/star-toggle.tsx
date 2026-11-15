import type { ReactNode } from 'react'

/** The constant accessible name every star control exposes for one symbol. */
export function starLabel(symbol: string): string {
  return `${symbol} in next scan`
}

export function starStateText(starred: boolean): string {
  return starred ? 'Included in next scan' : 'Not included in next scan'
}

interface StarToggleProps {
  symbol: string
  starred: boolean
  pending?: boolean
  /** Blocked direction (a non-TRADING row cannot be added from search). */
  disabled?: boolean
  describedBy?: string
  onToggle?: (next: boolean) => void
}

/**
 * Accessible star toggle.
 *
 * The button keeps a constant accessible name (``"<symbol> in next scan"``)
 * and exposes state through ``aria-pressed``; the visible ★/☆ glyph is
 * decorative and the state sentence is attached with ``aria-describedby``.
 *
 * ``aria-disabled`` (never native ``disabled``) marks a pending or blocked
 * toggle so the focused button stays in the tab order and focus is not dropped
 * to ``<body>``; a guarded click ignores the activation. A pending toggle is
 * ``aria-busy`` and describes itself as saving.
 */
export function StarToggle({
  symbol,
  starred,
  pending = false,
  disabled = false,
  describedBy,
  onToggle,
}: StarToggleProps): ReactNode {
  const stateId = `star-state-${symbol}`
  const savingId = `star-saving-${symbol}`
  const blocked = disabled || pending
  const description = [stateId, pending ? savingId : undefined, describedBy]
    .filter(Boolean)
    .join(' ')
  return (
    <button
      type="button"
      className="control-btn star-toggle"
      data-star-symbol={symbol}
      aria-label={starLabel(symbol)}
      aria-pressed={starred}
      aria-describedby={description || undefined}
      aria-busy={pending || undefined}
      aria-disabled={blocked || undefined}
      onClick={() => {
        if (blocked) {
          return
        }
        onToggle?.(!starred)
      }}
    >
      <span className="star-glyph" aria-hidden="true">
        {starred ? '★' : '☆'}
      </span>
      <span id={stateId} className="visually-hidden">
        {starStateText(starred)}
      </span>
      {pending && (
        <span id={savingId} className="visually-hidden">
          Saving
        </span>
      )}
      {pending && (
        <span className="star-pending" aria-hidden="true">
          •
        </span>
      )}
    </button>
  )
}

/** Read-only star state (no button) for the phone width. */
export function StarState({ starred }: { starred: boolean }): ReactNode {
  return (
    <span className="star-state">
      <span aria-hidden="true">{starred ? '★' : '☆'}</span>
      <span className="visually-hidden">{starStateText(starred)}</span>
    </span>
  )
}
