import { useEffect, useRef, useState } from 'react'

import { useLiveUpdates } from '../hooks/use-live-updates'

interface StatusBarProps {
  env?: string
  daemon?: 'UP' | 'OFFLINE'
  version?: string
  readiness?: 'READY' | 'NOT_READY' | 'UNKNOWN'
}

/**
 * The global status bar: environment, daemon, version, and readiness facts only.
 * Focusable chrome controls are rendered after it (kill switch, then pause) so
 * the first three tab stops are the two skip links and the kill switch
 * (DESIGN A2.2 / A12 #7).
 */
export function StatusBar({
  env = 'PAPER',
  daemon = 'OFFLINE',
  version = 'UNKNOWN',
  readiness = 'UNKNOWN',
}: StatusBarProps) {
  const daemonOnline = daemon === 'UP'

  return (
    <header className="statusbar" role="region" aria-label="System status bar">
      <span className="sb-brand">
        <span className="mark" aria-hidden="true">
          ◈
        </span>
        OFT
      </span>
      <span className="sb-sep" aria-hidden="true">
        /
      </span>
      <span className={`sb-item sb-env sb-env--${env.toLowerCase()}`}>{env}</span>
      <span className="sb-item">
        daemon{' '}
        <b className={daemonOnline ? 'sb-ok' : 'sb-offline'}>
          <span aria-hidden="true">●</span> {daemon}
        </b>
      </span>
      <span className="sb-item">
        version <b>{version}</b>
      </span>
      <span className="sb-item">
        readiness <b className={readiness === 'READY' ? 'sb-ok' : 'sb-warn'}>{readiness}</b>
      </span>
    </header>
  )
}

/**
 * The DESIGN A6 global "暂停实时更新" control (WCAG 2.2.2). It is rendered after
 * the kill switch in the shell chrome so the kill switch is the third tab stop.
 * It keeps a real toggle button with ``aria-pressed``, a visible, flipping
 * label, a visible PAUSED stamp, and a separate polite live region that
 * announces each state change exactly once.
 */
export function PauseControl() {
  const { paused, toggle } = useLiveUpdates()
  const [announcement, setAnnouncement] = useState('')
  const previousPaused = useRef(paused)

  useEffect(() => {
    if (previousPaused.current === paused) {
      return
    }
    previousPaused.current = paused
    setAnnouncement(paused ? '实时更新已暂停' : '实时更新已恢复')
  }, [paused])

  return (
    <div className="pause-bar">
      {paused && (
        <span className="stamp stamp--stale" data-testid="statusbar-paused" lang="en">
          PAUSED
        </span>
      )}
      <button type="button" className="control-btn sb-pause" aria-pressed={paused} onClick={toggle}>
        {paused ? '继续实时更新' : '暂停实时更新'}
      </button>
      <span className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </span>
    </div>
  )
}
