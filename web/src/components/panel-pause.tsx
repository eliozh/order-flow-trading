import { PausedStamp } from './paused-state'

interface PanelPauseProps {
  /** Whether this panel's own auto-refresh is paused. */
  paused: boolean
  onToggle: () => void
  /** The accessible action name, e.g. "暂停交易日志自动刷新". */
  label: string
  testId?: string
}

/**
 * A per-panel pause control (DESIGN A6/A12).
 *
 * It is deliberately independent of the global live-updates pause: a viewer may
 * freeze one panel while other surfaces keep updating. The caller ORs this
 * state with the global pause when gating polling, so the global pause always
 * dominates. The visible label flips and a PAUSED stamp is shown while frozen.
 */
export function PanelPause({ paused, onToggle, label, testId }: PanelPauseProps) {
  return (
    <span className="panel-pause" data-testid={testId}>
      <button type="button" className="btn btn--sm" aria-pressed={paused} onClick={onToggle}>
        {paused ? `继续刷新：${label}` : label}
      </button>
      {paused && <PausedStamp testId={testId ? `${testId}-stamp` : undefined} />}
    </span>
  )
}
