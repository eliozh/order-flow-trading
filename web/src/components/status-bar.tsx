interface StatusBarProps {
  env?: string
  daemon?: 'UP' | 'OFFLINE'
  version?: string
  readiness?: 'READY' | 'NOT_READY' | 'UNKNOWN'
}

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
      <button
        type="button"
        className="kill-btn"
        title="Kill switch is not wired in the walking skeleton"
        aria-label="Kill switch (not available in this build)"
      >
        Kill switch <span className="dot" aria-hidden="true" />
      </button>
    </header>
  )
}
