import { useEffect, useState } from 'react'

import { fetchStatus, type StatusEnvelope } from '../api/status'

type StatusFetcher = () => Promise<StatusEnvelope>

interface LoadState {
  state: 'loading' | 'ready' | 'error'
  envelope?: StatusEnvelope
  message?: string
}

interface StatusPanelProps {
  fetcher?: StatusFetcher
}

export function StatusPanel({ fetcher = fetchStatus }: StatusPanelProps) {
  const [load, setLoad] = useState<LoadState>({ state: 'loading' })

  useEffect(() => {
    let active = true
    setLoad({ state: 'loading' })
    fetcher()
      .then((envelope) => {
        if (active) {
          setLoad({ state: 'ready', envelope })
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setLoad({
            state: 'error',
            message: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      })
    return () => {
      active = false
    }
  }, [fetcher])

  if (load.state === 'loading') {
    return (
      <p className="status-message" role="status">
        Loading system status…
      </p>
    )
  }

  if (load.state === 'error') {
    return (
      <p className="status-message status-message--error" role="alert">
        Status unavailable: {load.message}
      </p>
    )
  }

  const data = load.envelope?.data
  if (!data) {
    return (
      <p className="status-message status-message--error" role="alert">
        Status unavailable: empty response
      </p>
    )
  }

  return (
    <section className="status-panel" aria-labelledby="status-heading">
      <h2 id="status-heading">System status</h2>
      <dl className="status-grid">
        <div>
          <dt>Environment</dt>
          <dd>
            <span className={`env-chip env-chip--${data.env}`}>{data.env.toUpperCase()}</span>
          </dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>{data.version}</dd>
        </div>
        <div>
          <dt>API bind</dt>
          <dd>
            {data.host}:{data.port}
          </dd>
        </div>
        <div>
          <dt>Data directory</dt>
          <dd>
            <code>{data.data_dir}</code>
          </dd>
        </div>
        <div>
          <dt>Symbols</dt>
          <dd>{data.universe.count}</dd>
        </div>
        <div>
          <dt>Generated</dt>
          <dd>{data.generated_at}</dd>
        </div>
      </dl>
      <table className="symbol-table">
        <caption>Fixture symbol universe</caption>
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Contract</th>
            <th scope="col">TradFi</th>
          </tr>
        </thead>
        <tbody>
          {data.universe.symbols.map((symbol) => (
            <tr key={symbol.symbol}>
              <td>{symbol.symbol}</td>
              <td>{symbol.contract_type}</td>
              <td>{symbol.tradifi ? 'yes' : 'no'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
