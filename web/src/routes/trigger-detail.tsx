/* eslint-disable jsx-a11y/no-autofocus -- focus starts on the least destructive control (cancel or reason) */
/* eslint-disable jsx-a11y/no-noninteractive-element-interactions -- role=dialog closes on Escape */
import { useEffect, useRef, useState } from 'react'

import { isAbortError } from '../api/abort'
import {
  fetchOpportunity,
  type OpportunityData,
  type OpportunityEnvelope,
} from '../api/opportunities'
import {
  acceptTrigger,
  getTrigger,
  idempotencyKey,
  isPendingStatus,
  recheckTrigger,
  rejectTrigger,
  type DecisionCard as DecisionCardData,
  type DecisionCardEnvelope,
  type TriggerAcceptData,
  type TriggerAcceptEnvelope,
  type TriggerDetailEnvelope,
  type TriggerPayload,
  type TriggerRejectData,
  type TriggerRejectEnvelope,
} from '../api/triggers'
import { DecisionCard, type AnalysisProvenance } from '../components/decision-card'
import { useCompact } from '../hooks/use-compact'

interface TriggerDetailPageProps {
  triggerId: string
  getTriggerFn?: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
  recheckFn?: (
    triggerId: string,
    key: string,
    signal?: AbortSignal,
  ) => Promise<DecisionCardEnvelope>
  acceptFn?: (triggerId: string, key: string) => Promise<TriggerAcceptEnvelope>
  rejectFn?: (triggerId: string, reason: string, key: string) => Promise<TriggerRejectEnvelope>
  fetchOpportunityFn?: (opportunityId: string, signal?: AbortSignal) => Promise<OpportunityEnvelope>
  isCompact?: boolean
}

interface DetailState {
  state: 'loading' | 'ready' | 'error'
  trigger?: TriggerPayload
  card?: DecisionCardData
  opportunity?: OpportunityData | null
  recheckError?: string | null
  message?: string
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

function analysisOf(opportunity: OpportunityData | null | undefined): AnalysisProvenance | null {
  const llm = opportunity?.provenance?.llm
  if (!llm) {
    return null
  }
  return {
    provider: llm.provider,
    model: llm.model,
    prompt_id: llm.prompt_id,
    prompt_version: llm.prompt_version,
    cost: llm.cost,
    usage: llm.usage,
  }
}

/**
 * The stored trigger payload is a trigger-event contract; it cannot carry the
 * full decision card. Build the card from the persisted trigger + verdict when
 * no re-check can run (terminal states), and never invent LLM values.
 */
function cardFromTrigger(trigger: TriggerPayload, recheckError?: string | null): DecisionCardData {
  return {
    decision_id: trigger.entry_decision_id,
    trigger_id: trigger.trigger_id,
    status: trigger.status,
    rejection_reason: trigger.rejection_reason,
    data_quality: trigger.snapshot?.data_quality ?? null,
    error_code: recheckError ?? null,
    stage_order: ['TRIGGER', 'LLM', 'RISK', 'HUMAN'],
    trigger: {
      trigger_id: trigger.trigger_id,
      opportunity_id: trigger.opportunity_id,
      symbol: trigger.symbol,
      side: trigger.side,
      status: trigger.status,
      trigger_source: trigger.trigger_source,
      triggered_at: trigger.triggered_at,
      snapshot: trigger.snapshot,
      condition_results: trigger.condition_results,
      provenance: 'CALC',
    },
    llm: {
      decision_id: trigger.entry_decision_id,
      decision: null,
      validation_status: null,
      confidence: null,
      rationale: null,
      reasons: trigger.rejection_reason ? [trigger.rejection_reason] : [],
      provenance: 'LLM',
    },
    risk: trigger.risk_verdict,
    human_action: trigger.human_action,
  }
}

function recordField(
  record: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const value = record?.[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : null
}

export function TriggerDetailPage({
  triggerId,
  getTriggerFn = getTrigger,
  recheckFn = recheckTrigger,
  acceptFn = acceptTrigger,
  rejectFn = rejectTrigger,
  fetchOpportunityFn = fetchOpportunity,
  isCompact,
}: TriggerDetailPageProps) {
  const [load, setLoad] = useState<DetailState>({ state: 'loading' })
  const [reload, setReload] = useState(0)
  const [acceptOpen, setAcceptOpen] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [accepted, setAccepted] = useState<TriggerAcceptData | null>(null)
  const [rejected, setRejected] = useState<TriggerRejectData | null>(null)
  const compact = useCompact(isCompact)

  const recheckKey = useRef(idempotencyKey(`web-recheck-${triggerId}`))
  const acceptKey = useRef(idempotencyKey(`web-accept-${triggerId}`))
  const rejectKey = useRef(idempotencyKey(`web-reject-${triggerId}`))

  useEffect(() => {
    const controller = new AbortController()
    setLoad({ state: 'loading' })
    async function run(): Promise<void> {
      const triggerEnvelope = await getTriggerFn(triggerId, controller.signal)
      const trigger = triggerEnvelope.data
      if (!trigger) {
        throw new Error('The trigger response was empty.')
      }
      if (reload > 0) {
        recheckKey.current = idempotencyKey(`web-recheck-${triggerId}`)
      }
      let card: DecisionCardData
      let recheckError: string | null = null
      if (isPendingStatus(trigger.status)) {
        try {
          const envelope = await recheckFn(triggerId, recheckKey.current, controller.signal)
          if (!envelope.data) {
            throw new Error('The re-check response was empty.')
          }
          card = envelope.data
        } catch (error: unknown) {
          if (controller.signal.aborted || isAbortError(error)) {
            throw error
          }
          recheckError = errorMessage(error)
          card = cardFromTrigger(trigger, recheckError)
        }
      } else {
        card = cardFromTrigger(trigger)
      }
      let opportunity: OpportunityData | null = null
      try {
        const envelope = await fetchOpportunityFn(trigger.opportunity_id, controller.signal)
        opportunity = envelope.data
      } catch (error: unknown) {
        if (controller.signal.aborted || isAbortError(error)) {
          throw error
        }
        opportunity = null
      }
      if (controller.signal.aborted) {
        return
      }
      setLoad({ state: 'ready', trigger, card, opportunity, recheckError })
    }
    run().catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      setLoad({ state: 'error', message: errorMessage(error) })
    })
    return () => {
      controller.abort()
    }
  }, [triggerId, getTriggerFn, recheckFn, fetchOpportunityFn, reload])

  if (load.state === 'loading') {
    return (
      <section
        className="system-page"
        aria-labelledby="trigger-detail-heading"
        data-readonly={compact}
      >
        <div className="page-head">
          <div>
            <p className="kicker">入场复核 / 决策卡</p>
            <h1 id="trigger-detail-heading" className="mono">
              {triggerId}
            </h1>
          </div>
        </div>
        <p className="status-message" role="status">
          LLM 复核中… 阶段：schema validation → risk gate。完成前不展示旧决策。
        </p>
      </section>
    )
  }

  if (load.state === 'error' || !load.trigger || !load.card) {
    return (
      <section className="system-page" aria-labelledby="trigger-detail-heading">
        <div className="panel" role="alert">
          <div className="panel-head">
            <h1 id="trigger-detail-heading" className="mono">
              未找到该决策
            </h1>
          </div>
          <p className="panel-foot">
            {triggerId}：{load.message ?? '决策不存在，或已不在可处理状态。'}
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <a className="btn" href="/triggers">
              返回队列
            </a>
          </div>
        </div>
      </section>
    )
  }

  const { trigger, opportunity } = load
  const displayCard: DecisionCardData = accepted
    ? {
        ...load.card,
        status: 'ACCEPTED',
        human_action: {
          action: 'ACCEPT',
          at: new Date().toISOString(),
          reason: null,
        },
      }
    : rejected
      ? {
          ...load.card,
          status: 'REJECTED',
          human_action: rejected.human_action ?? {
            action: 'REJECT',
            at: new Date().toISOString(),
            reason: rejected.rejection_reason ?? reason,
          },
        }
      : load.card
  const actionable =
    !accepted &&
    !rejected &&
    isPendingStatus(displayCard.status ?? trigger.status) &&
    displayCard.human_action === null

  async function confirmAccept(): Promise<void> {
    setActing(true)
    setActionError(null)
    try {
      const envelope = await acceptFn(triggerId, acceptKey.current)
      if (!envelope.data) {
        throw new Error('The accept response was empty.')
      }
      setAccepted(envelope.data)
      setAcceptOpen(false)
    } catch (error: unknown) {
      setActionError(errorMessage(error))
    } finally {
      setActing(false)
    }
  }

  async function confirmReject(): Promise<void> {
    if (reason.trim().length === 0) {
      setReasonError('错误：请输入拒绝理由。')
      return
    }
    setActing(true)
    setActionError(null)
    setReasonError(null)
    try {
      const envelope = await rejectFn(triggerId, reason.trim(), rejectKey.current)
      if (!envelope.data) {
        throw new Error('The reject response was empty.')
      }
      setRejected(envelope.data)
      setRejectOpen(false)
    } catch (error: unknown) {
      setActionError(errorMessage(error))
    } finally {
      setActing(false)
    }
  }

  const orderId = recordField(accepted?.order, 'order_id')
  const fillId = recordField(accepted?.fill, 'fill_id')
  const positionId = recordField(accepted?.position, 'position_id')
  const finalSize = displayCard.risk?.allowed?.quantity ?? null

  return (
    <section
      className="system-page"
      aria-labelledby="trigger-detail-heading"
      data-readonly={compact}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/triggers">
              入场复核
            </a>{' '}
            / 决策卡
          </p>
          <h1 id="trigger-detail-heading" className="mono">
            {triggerId}
          </h1>
          <p className="page-sub">
            {trigger.symbol} · {trigger.side} · status {displayCard.status ?? trigger.status} ·
            environment {trigger.environment}
          </p>
        </div>
        <div className="report-stamps">
          <span className="chip chip--calc">CALC</span>
          <span className="chip">LLM</span>
          <span className="env-chip env-chip--paper">{trigger.environment}</span>
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：决策与风险为只读视图。Kill switch 仍可用。
        </p>
      )}

      {load.recheckError && (
        <div className="panel" role="alert">
          <div className="panel-head">
            <h2>LLM 复核失败</h2>
          </div>
          <p className="panel-foot">
            {load.recheckError} — 未执行任何订单。旧决策不会被冒充为当前决策。
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setReload((value) => value + 1)
              }}
            >
              重试 LLM 复核
            </button>
          </div>
        </div>
      )}

      {accepted && (
        <div className="panel" role="status">
          <div className="panel-head">
            <h2>已接受（Paper）</h2>
            <span className="chip chip--ok">ACCEPTED</span>
          </div>
          <p className="panel-foot">
            订单 {orderId ?? '—'} · fill {fillId ?? '—'} · position {positionId ?? '—'} · 最终规模{' '}
            {finalSize ?? '—'} {trigger.symbol}。风控上限已应用；LLM 不能绕过风险上限。
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <a className="btn" href="/paper">
              查看 Paper 账户
            </a>
          </div>
        </div>
      )}

      {rejected && (
        <div className="panel" role="status">
          <div className="panel-head">
            <h2>已拒绝</h2>
            <span className="chip chip--danger">INVALIDATED</span>
          </div>
          <p className="panel-foot">
            机会已转为失效状态；拒绝理由已记录，LLM 决策保留供复盘。未发送任何订单。
          </p>
        </div>
      )}

      <DecisionCard
        card={displayCard}
        analysis={analysisOf(opportunity)}
        environment={trigger.environment}
        compact={compact}
        acting={acting}
        onAccept={
          actionable && !compact
            ? () => {
                setActionError(null)
                setAcceptOpen(true)
              }
            : undefined
        }
        onReject={
          actionable && !compact
            ? () => {
                setActionError(null)
                setReason('')
                setReasonError(null)
                setRejectOpen(true)
              }
            : undefined
        }
      />

      {acceptOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="accept-title"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 20,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 16,
            background: '#07080ab8',
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setAcceptOpen(false)
            }
          }}
        >
          <div
            style={{
              width: 'min(560px, 100%)',
              border: '1px solid var(--oft-border-strong)',
              background: 'var(--oft-bg-raised)',
              padding: 16,
            }}
          >
            <h2 id="accept-title" style={{ marginTop: 0 }}>
              接受（Paper）确认
            </h2>
            <dl className="kv" style={{ padding: 0 }}>
              <dt>最终规模</dt>
              <dd className="mono">
                {finalSize ?? '—'} {trigger.symbol}
              </dd>
              <dt>绑定上限</dt>
              <dd className="mono">{displayCard.risk?.binding_cap ?? 'N/A'}</dd>
              <dt>单笔风险</dt>
              <dd className="mono">
                请求 {displayCard.risk?.requested?.risk_pct ?? '—'}% · allowed{' '}
                {displayCard.risk?.allowed?.risk_amount_usd ?? '—'} USDT
              </dd>
              <dt>杠杆</dt>
              <dd className="mono">
                请求 {displayCard.risk?.requested?.leverage ?? '—'}x · allowed{' '}
                {displayCard.risk?.allowed?.leverage ?? '—'}x
              </dd>
            </dl>
            <p className="panel-foot" style={{ borderTop: 0, padding: 0 }}>
              最终规模取 LLM 提议与配置上限中的更严者；LLM 不能绕过风险上限。接受会向 Paper
              模拟引擎提交订单，不会发送到 Binance。
            </p>
            {actionError !== null && (
              <p role="alert" className="status-message--error" style={{ marginTop: 12 }}>
                {actionError}
              </p>
            )}
            <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
              <button
                type="button"
                className="btn"
                autoFocus
                onClick={() => {
                  setAcceptOpen(false)
                  setActionError(null)
                }}
              >
                取消
              </button>
              <button
                type="button"
                className="btn"
                disabled={acting}
                onClick={() => {
                  void confirmAccept()
                }}
              >
                确认接受（Paper）
              </button>
            </div>
          </div>
        </div>
      )}

      {rejectOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="reject-title"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 20,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 16,
            background: '#07080ab8',
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setRejectOpen(false)
            }
          }}
        >
          <div
            style={{
              width: 'min(560px, 100%)',
              border: '1px solid var(--oft-border-strong)',
              background: 'var(--oft-bg-raised)',
              padding: 16,
            }}
          >
            <h2 id="reject-title" style={{ marginTop: 0 }}>
              拒绝入场复核
            </h2>
            <p className="status-message">
              机会转为 INVALIDATED，拒绝理由写入台账；LLM 决策保留供复盘。不会触发任何订单。
            </p>
            <label
              htmlFor="reject-reason"
              style={{ display: 'block', margin: '12px 0 6px', fontSize: 12 }}
            >
              拒绝理由（必填）
            </label>
            <input
              id="reject-reason"
              className="mono"
              value={reason}
              autoFocus
              required
              aria-required="true"
              autoCapitalize="off"
              spellCheck={false}
              onChange={(event) => setReason(event.target.value)}
              style={{
                width: '100%',
                minHeight: 32,
                padding: '4px 10px',
                border: '1px solid var(--oft-border-control)',
                background: 'var(--oft-bg-inset, #07080a)',
                color: 'var(--oft-text-primary)',
              }}
            />
            <p className="panel-foot" style={{ borderTop: 0, padding: '6px 0 0' }}>
              可使用自由文本；不会触发任何订单。
            </p>
            {reasonError !== null && (
              <p role="alert" className="status-message--error">
                {reasonError}
              </p>
            )}
            {actionError !== null && (
              <p role="alert" className="status-message--error">
                {actionError}
              </p>
            )}
            <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setRejectOpen(false)
                  setActionError(null)
                }}
              >
                取消
              </button>
              <button
                type="button"
                className="btn"
                disabled={acting}
                style={{
                  borderColor: 'var(--oft-status-danger)',
                  color: 'var(--oft-status-danger)',
                }}
                onClick={() => {
                  void confirmReject()
                }}
              >
                拒绝并标记 invalidated
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
