import type { ReactNode } from 'react'

import { riskVerdictChip, type RiskVerdict } from '../api/triggers'

interface RiskPanelProps {
  verdict: RiskVerdict | null
  symbol?: string | null
}

function value(value: string | number | null | undefined, suffix = ''): string {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return `${value}${suffix}`
}

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${value.toFixed(2)}%`
}

function staleInputs(verdict: RiskVerdict | null): string[] {
  const values = verdict?.stale_inputs
  return Array.isArray(values) ? values.filter((item) => typeof item === 'string') : []
}

function Row({ cap, children }: { cap: string; children: ReactNode }) {
  return (
    <tr>
      <th scope="row">{cap}</th>
      {children}
    </tr>
  )
}

/**
 * The CALC risk gate: requested vs allowed caps, the final size, and which cap
 * bound the decision. Values are rendered exactly as persisted, never inferred.
 */
export function RiskPanel({ verdict, symbol }: RiskPanelProps) {
  if (!verdict) {
    return (
      <div>
        <p className="status-message" role="status">
          风险门尚未评估。运行 LLM 复核后显示每个 cap 的请求值与允许值。
        </p>
      </div>
    )
  }

  const requested = verdict.requested ?? {}
  const allowed = verdict.allowed ?? {}
  const chip = riskVerdictChip(verdict)
  const stale = staleInputs(verdict)
  const reasons = Array.isArray(verdict.reasons) ? verdict.reasons : []
  const finalSize =
    allowed.quantity === null || allowed.quantity === undefined
      ? '—'
      : `${allowed.quantity}${symbol ? ` ${symbol}` : ''}`

  return (
    <div>
      <div className="action-row" style={{ marginBottom: 8 }}>
        <span className={chip.className}>{chip.label}</span>
        {stale.length > 0 && <span className="chip chip--warn">PARTIAL</span>}
        <span className="mono-muted">requested vs allowed · stricter side wins</span>
      </div>
      <div className="table-wrap">
        <table className="data-table">
          <caption className="visually-hidden">Risk gate caps, requested versus allowed</caption>
          <thead>
            <tr>
              <th scope="col">Cap</th>
              <th scope="col">Requested</th>
              <th scope="col">Allowed</th>
            </tr>
          </thead>
          <tbody>
            <Row cap="单笔风险">
              <td className="mono">请求 {percent(requested.risk_pct)}</td>
              <td className="mono">{value(allowed.risk_amount_usd, ' USDT')}</td>
            </Row>
            <Row cap="最大名义">
              <td className="mono">请求 {value(requested.notional_usd, ' USDT')}</td>
              <td className="mono">{value(allowed.notional_usd, ' USDT')}</td>
            </Row>
            <Row cap="杠杆">
              <td className="mono">请求 {value(requested.leverage, 'x')}</td>
              <td className="mono">allowed {value(allowed.leverage, 'x')}</td>
            </Row>
            <Row cap="入场 / 止损">
              <td className="mono">entry {value(requested.entry_price)}</td>
              <td className="mono">stop {value(requested.stop_price)}</td>
            </Row>
            <Row cap="最终规模">
              <td className="mono">allowed {value(allowed.quantity)}</td>
              <td className="mono">{finalSize}</td>
            </Row>
            <tr data-testid="binding-cap">
              <th scope="row">绑定上限</th>
              <td className="mono" colSpan={2}>
                {verdict.binding_cap ?? 'N/A'}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      {stale.length > 0 && (
        <p className="panel-foot">
          PARTIAL — 未测量的输入：{stale.join(', ')}。这些输入不会被当作零。
        </p>
      )}
      {reasons.length > 0 && <p className="panel-foot">reasons: {reasons.join('; ')}</p>}
      <p className="panel-foot">
        最终规模由 LLM 提议与配置上限中更严格的一方决定；绑定的 cap 是「
        {verdict.binding_cap ?? 'N/A'}」。LLM 不能绕过任何风控上限。
      </p>
    </div>
  )
}
