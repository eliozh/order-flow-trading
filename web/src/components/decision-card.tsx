import { useId } from 'react'

import type { DecisionCard as DecisionCardData, TriggerConditionResult } from '../api/triggers'
import { acceptBlockedReason, isStaleMark, riskVerdictChip } from '../api/triggers'
import { confidenceBand } from '../api/reports'
import { ProvenanceStamp } from './provenance'
import { RiskPanel } from './risk-panel'

/** The linked analysis decision's provenance (never the raw prompt body). */
export interface AnalysisProvenance {
  provider?: string
  model?: string
  prompt_id?: string
  prompt_version?: number
  cost?: { amount_usd?: string | null; unpriced?: boolean }
  usage?: { total_tokens?: number }
}

interface DecisionCardProps {
  card: DecisionCardData
  analysis?: AnalysisProvenance | null
  environment?: string
  compact?: boolean
  onAccept?: () => void
  onReject?: () => void
  acceptDisabledReason?: string | null
  acting?: boolean
}

function metadata(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return 'N/A'
  }
  return String(value)
}

function promptLabel(
  provider: AnalysisProvenance | null | undefined,
  cardLlm: DecisionCardData['llm'],
): string {
  const promptId = cardLlm.prompt_id ?? provider?.prompt_id
  const version = cardLlm.prompt_version ?? provider?.prompt_version
  if (!promptId) {
    return 'N/A'
  }
  return `${promptId} v${version ?? '—'}`
}

function costLabel(
  provider: AnalysisProvenance | null | undefined,
  cardLlm: DecisionCardData['llm'],
): string {
  const cost = cardLlm.cost ?? provider?.cost
  if (!cost) {
    return 'N/A'
  }
  if (cost.amount_usd === null || cost.amount_usd === undefined) {
    return cost.unpriced ? 'unpriced · N/A' : 'N/A'
  }
  return cost.unpriced ? `$${cost.amount_usd} (unpriced)` : `$${cost.amount_usd}`
}

function tokensLabel(
  provider: AnalysisProvenance | null | undefined,
  cardLlm: DecisionCardData['llm'],
): string {
  const tokens = cardLlm.usage?.total_tokens ?? provider?.usage?.total_tokens
  return tokens === undefined ? 'N/A' : tokens.toLocaleString('en-US')
}

function conditionLine(condition: TriggerConditionResult): string {
  const observed = condition.observed_value ?? '—'
  const threshold = condition.threshold ?? '—'
  return `${condition.type} · observed ${observed} · threshold ${threshold}`
}

function SideLabel({ side }: { side: string | null }) {
  if (side === 'SHORT') {
    return <span className="value-down">▼ SHORT</span>
  }
  if (side === 'LONG') {
    return <span className="value-up">▲ LONG</span>
  }
  return <span>—</span>
}

function Confidence({ confidence }: { confidence: number | null | undefined }) {
  if (confidence === null || confidence === undefined) {
    return <span className="mono">N/A</span>
  }
  const band = confidenceBand(confidence)
  return <span className="mono">{band === null ? confidence : `${confidence} · ${band}`}</span>
}

/**
 * The ordered decision card (DESIGN §14, F06):
 * Trigger (CALC) -> LLM decision -> Risk gate (CALC) -> Human action.
 *
 * The narrative is a React text node only. No raw HTML, prompt body, provider
 * key, or credential can reach this surface.
 */
export function DecisionCard({
  card,
  analysis = null,
  environment = 'PAPER',
  compact = false,
  onAccept,
  onReject,
  acceptDisabledReason,
  acting = false,
}: DecisionCardProps) {
  const triggerId = useId()
  const llmId = useId()
  const riskId = useId()
  const humanId = useId()
  const acceptWhyId = useId()

  const risk = card.risk
  const verdictChip = riskVerdictChip(risk)
  const blockedReason = acceptDisabledReason ?? acceptBlockedReason(risk)
  const humanAction = card.human_action
  const status = card.status ?? card.trigger.status ?? null
  const actionable = status === 'PENDING_RECHECK' || status === 'RECHECKED'
  const narrative =
    typeof card.llm.rationale === 'string' && card.llm.rationale.length > 0
      ? card.llm.rationale
      : null
  const finalSize = risk?.allowed?.quantity ?? null
  const providerName = metadata(card.llm.provider ?? analysis?.provider)
  const modelName = metadata(card.llm.model ?? analysis?.model)
  const metadataFromAnalysis =
    !card.llm.provider && !card.llm.model && analysis !== null && analysis !== undefined

  return (
    <div className="stack-sm">
      <section className="band" aria-labelledby={triggerId}>
        <div className="band-head">
          <ProvenanceStamp kind="CALC" />
          <h2 id={triggerId}>1 · Trigger（代码判定）</h2>
          <span className="chip chip--ok">CODE</span>
        </div>
        <dl className="kv">
          <dt>Opportunity</dt>
          <dd>
            {card.trigger.opportunity_id ? (
              <a
                className="rowlink mono"
                href={`/opportunities/${encodeURIComponent(card.trigger.opportunity_id)}`}
              >
                {card.trigger.opportunity_id}
              </a>
            ) : (
              'N/A'
            )}
          </dd>
          <dt>Symbol / side</dt>
          <dd className="mono">
            {card.trigger.symbol ?? '—'} · <SideLabel side={card.trigger.side} />
          </dd>
          <dt>Triggered at</dt>
          <dd className="mono">{card.trigger.triggered_at ?? '—'}</dd>
          <dt>Snapshot</dt>
          <dd className="mono">
            {card.trigger.snapshot?.market_snapshot_id ?? 'N/A'} ·{' '}
            {card.trigger.snapshot?.data_quality ?? 'N/A'}
          </dd>
          <dt>Condition values</dt>
          <dd>
            {card.trigger.condition_results.length === 0 ? (
              '—'
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {card.trigger.condition_results.map((condition) => (
                  <li key={condition.condition_id} className="mono">
                    {conditionLine(condition)} · {condition.met ? 'MET' : 'NOT MET'}
                  </li>
                ))}
              </ul>
            )}
          </dd>
          <dt>Data quality</dt>
          <dd className="mono">
            {card.data_quality ?? card.trigger.snapshot?.data_quality ?? 'N/A'}
            {risk && isStaleMark(risk) ? ' · STALE' : ''}
          </dd>
        </dl>
      </section>

      <section className="band band--llm" aria-labelledby={llmId}>
        <div className="band-head">
          <ProvenanceStamp kind="LLM" />
          <h2 id={llmId}>2 · LLM 入场判断</h2>
          <span className="chip chip--calc">{card.llm.decision ?? 'N/A'}</span>
          <span className="chip">
            confidence <Confidence confidence={card.llm.confidence} />
          </span>
        </div>
        {narrative ? (
          <>
            <p className="prose">{narrative}</p>
            <p className="band-note">
              Untrusted model text, not a measurement. It cannot change a threshold or bypass the
              CALC risk gate.
            </p>
          </>
        ) : (
          <p className="llm-empty">No rationale was produced for this decision.</p>
        )}
        <dl className="band-meta">
          <div>
            <dt>Provider</dt>
            <dd>{providerName}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>{modelName}</dd>
          </div>
          <div>
            <dt>Prompt</dt>
            <dd>{promptLabel(analysis, card.llm)}</dd>
          </div>
          <div>
            <dt>Tokens</dt>
            <dd>{tokensLabel(analysis, card.llm)}</dd>
          </div>
          <div>
            <dt>Cost</dt>
            <dd>{costLabel(analysis, card.llm)}</dd>
          </div>
        </dl>
        <p className="panel-foot">
          Entry decision <span className="mono">{card.decision_id ?? 'N/A'}</span> · validation{' '}
          {card.llm.validation_status ?? 'N/A'}.
          {metadataFromAnalysis
            ? ' Provider/model/prompt/cost are read from the linked analysis decision; the entry re-check read model does not re-serve them. No raw prompt is rendered.'
            : ' No raw prompt or provider key is rendered.'}
        </p>
      </section>

      <section className="band" aria-labelledby={riskId}>
        <div className="band-head">
          <ProvenanceStamp kind="CALC" />
          <h2 id={riskId}>3 · Risk gate（取更严者）</h2>
          <span className={verdictChip.className}>{verdictChip.label}</span>
        </div>
        <RiskPanel verdict={risk} symbol={card.trigger.symbol} />
      </section>

      <section className="panel" aria-labelledby={humanId}>
        <div className="panel-head">
          <h2 id={humanId}>4 · Human action</h2>
          <span className="env-chip env-chip--paper">environment {environment}</span>
        </div>
        <div style={{ padding: 16 }}>
          <p className="panel-foot" style={{ borderTop: 0, padding: 0 }}>
            接受会在 Paper 模拟引擎提交；Live
            未启用，按钮不会假装发送真实订单。拒绝需要理由，机会转为 INVALIDATED 并保留决策。LLM
            不能绕过风险上限。
          </p>

          {humanAction ? (
            <div className="action-row" style={{ marginTop: 12 }}>
              <span
                className={humanAction.action === 'REJECT' ? 'chip chip--danger' : 'chip chip--ok'}
              >
                {humanAction.action ?? 'RECORDED'}
              </span>
              <span className="mono">{humanAction.at ?? '—'} · </span>
              <span>{humanAction.reason ?? 'accepted'}</span>
            </div>
          ) : compact ? (
            <p className="readonly-note" role="note" style={{ marginTop: 12 }}>
              移动端只读：接受/拒绝请在桌面端或 CLI 处理。Kill switch 仍可用。
            </p>
          ) : actionable ? (
            <div className="action-row" style={{ marginTop: 12, flexWrap: 'wrap' }}>
              <button
                type="button"
                className="btn"
                style={{
                  minHeight: 44,
                  minWidth: 88,
                  borderColor: 'var(--oft-status-danger)',
                  color: 'var(--oft-status-danger)',
                }}
                disabled={acting}
                onClick={onReject}
              >
                拒绝
              </button>
              <button
                type="button"
                className="btn"
                style={{
                  minHeight: 44,
                  minWidth: 88,
                  borderColor: 'var(--oft-accent)',
                  color: 'var(--oft-bg-canvas)',
                  background: 'var(--oft-accent)',
                }}
                disabled={blockedReason !== null || acting}
                aria-describedby={acceptWhyId}
                onClick={onAccept}
              >
                {acting ? '处理中…' : '接受（Paper）'}
              </button>
              <span className="panel-foot" id={acceptWhyId} style={{ borderTop: 0, padding: 0 }}>
                {blockedReason ??
                  `最终规模 ${finalSize ?? '—'} ${card.trigger.symbol ?? ''} · 风控绑定 ${
                    risk?.binding_cap ?? 'N/A'
                  }。`}
              </span>
            </div>
          ) : (
            <p className="status-message" style={{ marginTop: 12 }}>
              该决策已进入终态（{status ?? 'UNKNOWN'}），不可再接受或拒绝。
            </p>
          )}

          <p className="panel-foot" style={{ borderTop: 0, padding: '12px 0 0' }}>
            Live gate：Paper 未满 4 周，Live 未启用。即使启用，accept 也需类型化 LIVE
            确认；当前唯一可执行路径是 Paper。
          </p>
        </div>
      </section>
    </div>
  )
}
