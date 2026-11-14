import { utcLabel } from '../api/reports'
import type { PositionManageData, PositionManagementEntry } from '../api/positions'
import { dash } from '../api/positions'
import { recordField } from '../api/records'

interface ManagementTimelineProps {
  /** Persisted ``management_history`` entries from the position payload. */
  entries?: PositionManagementEntry[]
  /** The validated result of the current management request, when one ran. */
  result?: PositionManageData | null
}

function confidenceText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return value.toFixed(2)
}

/**
 * The management timeline: the persisted history plus, when a request ran, the
 * three explicit stages (LLM proposal -> CALC validation -> execution). The
 * rationale is rendered as a text node only; no prompt or raw HTML is shown.
 */
export function ManagementTimeline({ entries = [], result = null }: ManagementTimelineProps) {
  const orderId = recordField(result?.order, 'order_id')
  const fillId = recordField(result?.fill, 'fill_id')
  const risk = result?.risk ?? null

  return (
    <div className="mgmt-timeline">
      {entries.length > 0 && (
        // Safari/VoiceOver drops list semantics for `list-style: none`, so the
        // explicit role is intentional (WCAG 1.3.1).
        // eslint-disable-next-line jsx-a11y/no-redundant-roles
        <ol className="timeline" role="list" aria-label="管理历史">
          {entries.map((entry, index) => (
            <li key={`${entry.decision_id ?? entry.action}-${entry.at}-${index}`}>
              <div className="tl-time">{utcLabel(entry.at)} UTC</div>
              <div className="tl-title">{entry.action}</div>
              <div className="tl-desc mono">
                decision {dash(entry.decision_id)} · order {dash(entry.executed_order_id)}
              </div>
              {entry.note && <div className="tl-desc">{entry.note}</div>}
            </li>
          ))}
        </ol>
      )}

      {result && (
        <section aria-labelledby="mgmt-stages-heading">
          <h3 id="mgmt-stages-heading" className="mono-muted">
            管理决策阶段
          </h3>
          {/*
            Safari/VoiceOver drops list semantics for `list-style: none`, so the
            explicit role is intentional (WCAG 1.3.1).
          */}
          {/* eslint-disable-next-line jsx-a11y/no-redundant-roles */}
          <ol className="timeline timeline--stages" role="list">
            <li className="acc">
              <div className="tl-title">LLM 提议</div>
              <div className="tl-desc">
                <span className="stamp stamp--llm" lang="en">
                  LLM
                </span>{' '}
                action <span className="mono">{result.action}</span> · confidence{' '}
                <span className="mono">{confidenceText(result.llm?.confidence)}</span>
                {result.llm?.provenance?.provider && result.llm?.provenance?.model && (
                  <>
                    {' '}
                    · {result.llm.provenance.provider} / {result.llm.provenance.model}
                  </>
                )}
              </div>
              {result.llm?.rationale && <p className="tl-desc">{result.llm.rationale}</p>}
            </li>

            <li className={risk?.verdict === 'BLOCK' ? 'failed' : 'ok'}>
              <div className="tl-title">CALC 校验</div>
              <div className="tl-desc">
                <span className="stamp stamp--calc" lang="en">
                  CALC
                </span>{' '}
                status <span className="mono">{result.status}</span> · reason_code{' '}
                <span className="mono">{result.reason_code}</span>
              </div>
              <div className="tl-desc">
                风险门 <span className="mono">{risk?.verdict ?? 'N/A'}</span> · 绑定上限{' '}
                <span className="mono">{risk?.binding_cap ?? 'N/A'}</span>
              </div>
              {(risk?.stale_inputs?.length ?? 0) > 0 && (
                <div className="tl-desc">
                  PARTIAL — 未测量的风险输入：{risk?.stale_inputs?.join(', ')}
                </div>
              )}
              {(risk?.reasons?.length ?? 0) > 0 && (
                <div className="tl-desc">reasons: {risk?.reasons?.join('; ')}</div>
              )}
              {result.reason && <p className="tl-desc">{result.reason}</p>}
            </li>

            <li className={result.executed ? 'ok' : ''}>
              <div className="tl-title">执行</div>
              <div className="tl-desc">
                {result.executed ? (
                  <>
                    订单 <span className="mono">{orderId ?? '—'}</span> · fill{' '}
                    <span className="mono">{fillId ?? '—'}</span> · status{' '}
                    <span className="mono">{result.status}</span>
                  </>
                ) : (
                  <>
                    无订单（{result.action} / {result.status}）— 未向 Paper 引擎提交任何订单。
                  </>
                )}
              </div>
              {result.idempotent_replay && (
                <div className="tl-desc">idempotent replay：同一 Idempotency-Key 的已存结果。</div>
              )}
            </li>
          </ol>

          {result.refusals.length > 0 && (
            // Safari/VoiceOver drops list semantics for `list-style: none`.
            // eslint-disable-next-line jsx-a11y/no-redundant-roles
            <ul className="refusal-list" role="list" aria-label="管理拒绝项">
              {result.refusals.map((refusal) => (
                <li key={`${refusal.code}-${refusal.field}`} className="tl-desc">
                  <span className="mono">{refusal.code}</span> ·{' '}
                  <span className="mono">{refusal.field}</span>: {refusal.message}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
