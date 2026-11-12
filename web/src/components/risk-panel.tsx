/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable risk table */
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

function Row({ cap, testId, children }: { cap: string; testId?: string; children: ReactNode }) {
  return (
    <tr data-testid={testId}>
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

  const requestedQuantity =
    requested.quantity === null || requested.quantity === undefined
      ? '—'
      : `${requested.quantity}${symbol ? ` ${symbol}` : ''}`

  return (
    <div>
      <div className="action-row" style={{ marginBottom: 8 }}>
        <span className={chip.className}>{chip.label}</span>
        {stale.length > 0 && <span className="chip chip--warn">PARTIAL</span>}
        <span className="mono-muted">requested vs allowed · stricter side wins</span>
      </div>
      <div className="table-wrap" role="region" tabIndex={0} aria-label="Risk gate caps table">
        <table className="data-table">
          <caption className="visually-hidden">Risk gate caps, requested versus allowed</caption>
          <thead lang="en">
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
            <Row cap="入场价" testId="cap-entry">
              <td className="mono">请求 {value(requested.entry_price)}</td>
              <td className="mono">—</td>
            </Row>
            <Row cap="止损价" testId="cap-stop">
              <td className="mono">请求 {value(requested.stop_price)}</td>
              <td className="mono">—</td>
            </Row>
            <Row cap="最终规模" testId="cap-final-size">
              <td className="mono">请求 {requestedQuantity}</td>
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
      <p className="panel-foot">
        入场价与止损价是请求值（没有 allowed 对应项）；最终规模只取 requested 与 allowed
        中更严格的一方。列上方标题为 <span lang="en">Requested / Allowed</span>。
      </p>
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
