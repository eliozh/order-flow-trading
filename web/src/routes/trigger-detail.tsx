import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

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
import { errorMessage } from '../api/errors'
import { recordField } from '../api/records'
import { useNarrowCompact } from '../hooks/use-compact'
import { useDialogFocus } from '../hooks/use-dialog-focus'

interface TriggerDetailPageProps {
  triggerId: string
  getTriggerFn?: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
  recheckFn?: (
    triggerId: string,
    key: string,
    signal?: AbortSignal,
  ) => Promise<DecisionCardEnvelope>
  acceptFn?: (
    triggerId: string,
    key: string,
    signal?: AbortSignal,
  ) => Promise<TriggerAcceptEnvelope>
  rejectFn?: (
    triggerId: string,
    reason: string,
    key: string,
    signal?: AbortSignal,
  ) => Promise<TriggerRejectEnvelope>
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

const RECHECK_STORAGE_PREFIX = 'oft.entry.recheck.'
const memoryRecheckKeys = new Map<string, string>()

function sessionStore(): Storage | null {
  if (typeof window === 'undefined') {
    return null
  }
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function storedRecheckKey(triggerId: string): string | null {
  const store = sessionStore()
  if (store) {
    return store.getItem(`${RECHECK_STORAGE_PREFIX}${triggerId}`)
  }
  return memoryRecheckKeys.get(triggerId) ?? null
}

/** One recheck key per trigger for the browser session, so a refresh replays. */
function recheckKeyFor(triggerId: string): string {
  return storedRecheckKey(triggerId) ?? rotateRecheckKey(triggerId)
}

/** A deliberate new re-check gets a fresh key and becomes the session key. */
function rotateRecheckKey(triggerId: string): string {
  const key = idempotencyKey(`web-recheck-${triggerId}`)
  const store = sessionStore()
  if (store) {
    store.setItem(`${RECHECK_STORAGE_PREFIX}${triggerId}`, key)
  } else {
    memoryRecheckKeys.set(triggerId, key)
  }
  return key
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
  const [acceptOpen, setAcceptOpen] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string | null>(null)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [accepted, setAccepted] = useState<TriggerAcceptData | null>(null)
  const [rejected, setRejected] = useState<TriggerRejectData | null>(null)
  const [rechecking, setRechecking] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const compact = useNarrowCompact(isCompact)

  // Keys are lazy-initialised: `idempotencyKey` never runs during render.
  const acceptKeyRef = useRef<string | null>(null)
  const rejectKeyRef = useRef<string | null>(null)
  const actionController = useRef<AbortController | null>(null)
  const recheckController = useRef<AbortController | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      actionController.current?.abort()
      recheckController.current?.abort()
    }
  }, [])

  function acceptKey(): string {
    acceptKeyRef.current ??= idempotencyKey(`web-accept-${triggerId}`)
    return acceptKeyRef.current
  }

  function rejectKey(): string {
    rejectKeyRef.current ??= idempotencyKey(`web-reject-${triggerId}`)
    return rejectKeyRef.current
  }

  useEffect(() => {
    const controller = new AbortController()
    setLoad({ state: 'loading' })
    async function run(): Promise<void> {
      const triggerEnvelope = await getTriggerFn(triggerId, controller.signal)
      const trigger = triggerEnvelope.data
      if (!trigger) {
        throw new Error('The trigger response was empty.')
      }
      // A paid re-check runs only for a fresh pending trigger with no stored
      // entry decision, and never on a read-only/compact surface.
      const autoRecheck =
        trigger.status === 'PENDING_RECHECK' && !trigger.entry_decision_id && !compact
      let card: DecisionCardData
      let recheckError: string | null = null
      if (autoRecheck) {
        try {
          const envelope = await recheckFn(triggerId, recheckKeyFor(triggerId), controller.signal)
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
      if (autoRecheck) {
        setAnnouncement(
          recheckError === null ? 'LLM 复核已完成。' : `LLM 复核失败：${recheckError}`,
        )
      }
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
  }, [triggerId, getTriggerFn, recheckFn, fetchOpportunityFn, compact])

  const closeAccept = useCallback((): void => {
    actionController.current?.abort()
    setAcceptOpen(false)
  }, [])
  const closeReject = useCallback((): void => {
    actionController.current?.abort()
    setRejectOpen(false)
  }, [])
  const acceptPanelRef = useDialogFocus(acceptOpen, closeAccept, {
    fallbackFocusId: 'trigger-detail-heading',
  })
  const rejectPanelRef = useDialogFocus(rejectOpen, closeReject, {
    fallbackFocusId: 'trigger-detail-heading',
  })

  async function manualRecheck(): Promise<void> {
    const controller = new AbortController()
    recheckController.current = controller
    setRechecking(true)
    setActionError(null)
    try {
      const envelope = await recheckFn(triggerId, rotateRecheckKey(triggerId), controller.signal)
      if (controller.signal.aborted) {
        return
      }
      const card = envelope.data
      if (!card) {
        throw new Error('The re-check response was empty.')
      }
      setLoad((previous) =>
        previous.state === 'ready' ? { ...previous, card, recheckError: null } : previous,
      )
      setAnnouncement('LLM 重新复核已完成。')
    } catch (error: unknown) {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      const errorText = errorMessage(error)
      setLoad((previous) =>
        previous.state === 'ready' ? { ...previous, recheckError: errorText } : previous,
      )
      setAnnouncement(`LLM 复核失败：${errorText}`)
    } finally {
      if (mounted.current) {
        setRechecking(false)
      }
    }
  }

  async function confirmAccept(): Promise<void> {
    const controller = new AbortController()
    actionController.current = controller
    setActing(true)
    setActionError(null)
    try {
      const envelope = await acceptFn(triggerId, acceptKey(), controller.signal)
      if (controller.signal.aborted) {
        return
      }
      const data = envelope.data
      if (!data) {
        throw new Error('The accept response was empty.')
      }
      setAccepted(data)
      setAcceptOpen(false)
      setAnnouncement(`已接受（Paper）：服务端状态 ${data.status}。`)
    } catch (error: unknown) {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      const errorText = errorMessage(error)
      setActionError(errorText)
      setAnnouncement(`接受失败：${errorText}`)
    } finally {
      if (mounted.current) {
        setActing(false)
      }
    }
  }

  async function confirmReject(): Promise<void> {
    if (reason.trim().length === 0) {
      setReasonError('错误：请输入拒绝理由。')
      return
    }
    const controller = new AbortController()
    actionController.current = controller
    setActing(true)
    setActionError(null)
    setReasonError(null)
    try {
      const envelope = await rejectFn(triggerId, reason.trim(), rejectKey(), controller.signal)
      if (controller.signal.aborted) {
        return
      }
      const data = envelope.data
      if (!data) {
        throw new Error('The reject response was empty.')
      }
      setRejected(data)
      setRejectOpen(false)
      setAnnouncement('已拒绝：机会转为 INVALIDATED，拒绝理由已记录。')
    } catch (error: unknown) {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      const errorText = errorMessage(error)
      setActionError(errorText)
      setAnnouncement(`拒绝失败：${errorText}`)
    } finally {
      if (mounted.current) {
        setActing(false)
      }
    }
  }

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
            <h1 id="trigger-detail-heading" className="mono" tabIndex={-1}>
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
            <h1 id="trigger-detail-heading" className="mono" tabIndex={-1}>
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
        decision_id: accepted.decision_id ?? load.card.decision_id,
        status: accepted.status,
        risk: accepted.risk_verdict ?? load.card.risk,
        human_action: { action: 'ACCEPT', reason: null },
      }
    : rejected
      ? {
          ...load.card,
          status: rejected.status ?? load.card.status,
          human_action: rejected.human_action ?? {
            action: 'REJECT',
            reason: rejected.rejection_reason ?? reason,
          },
        }
      : load.card
  const actionable =
    !accepted &&
    !rejected &&
    isPendingStatus(displayCard.status ?? trigger.status) &&
    displayCard.human_action === null

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
          <h1 id="trigger-detail-heading" className="mono" tabIndex={-1}>
            {triggerId}
          </h1>
          <p className="page-sub">
            {trigger.symbol} · {trigger.side} · <span lang="en">status</span>{' '}
            <span lang="en">{displayCard.status ?? trigger.status}</span> ·{' '}
            <span lang="en">environment</span> <span lang="en">{trigger.environment}</span>
          </p>
        </div>
        <div className="report-stamps">
          <span className="chip chip--calc" lang="en">
            CALC
          </span>
          <span className="chip" lang="en">
            LLM
          </span>
          <span className="env-chip env-chip--paper" lang="en">
            {trigger.environment}
          </span>
          {!compact && isPendingStatus(trigger.status) && (
            <button
              type="button"
              className="control-btn"
              disabled={rechecking}
              onClick={() => {
                void manualRecheck()
              }}
            >
              {rechecking ? '重新复核中…' : '重新复核'}
            </button>
          )}
        </div>
      </div>

      <p className="visually-hidden" role="status" aria-live="polite" data-testid="action-live">
        {announcement}
      </p>

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
              disabled={rechecking}
              onClick={() => {
                void manualRecheck()
              }}
            >
              重试 LLM 复核
            </button>
          </div>
        </div>
      )}

      {actionError !== null && !acceptOpen && !rejectOpen && (
        <div className="panel" role="alert">
          <div className="panel-head">
            <h2>操作失败</h2>
          </div>
          <p className="panel-foot">{actionError} — 未创建任何订单，也未改变决策状态。</p>
        </div>
      )}

      {accepted && (
        <div className="panel" role="status">
          <div className="panel-head">
            <h2>已接受（Paper）</h2>
            <span className="chip chip--ok" lang="en">
              {accepted.status}
            </span>
          </div>
          <p className="panel-foot">
            订单 {orderId ?? '—'} · <span lang="en">fill</span> {fillId ?? '—'} ·{' '}
            <span lang="en">position</span> {positionId ?? '—'} · 最终规模 {finalSize ?? '—'}{' '}
            {trigger.symbol}。风控上限已应用；LLM 不能绕过风险上限。状态与审计时间以服务端返回为准。
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
            <span className="chip chip--danger" lang="en">
              INVALIDATED
            </span>
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

      {acceptOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            className="dialog-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="accept-title"
            aria-describedby="accept-consequences"
          >
            <div className="dialog-panel" ref={acceptPanelRef} tabIndex={-1}>
              <form
                onSubmit={(event) => {
                  event.preventDefault()
                  void confirmAccept()
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
                <p
                  className="panel-foot"
                  id="accept-consequences"
                  style={{ borderTop: 0, padding: 0 }}
                >
                  最终规模取 LLM 提议与配置上限中的更严者；LLM 不能绕过风险上限。接受会向 Paper
                  模拟引擎提交订单，不会发送到 Binance。
                </p>
                {actionError !== null && (
                  <p role="alert" className="status-message--error" style={{ marginTop: 12 }}>
                    {actionError}
                  </p>
                )}
                <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
                  <button type="button" className="btn" data-autofocus onClick={closeAccept}>
                    取消
                  </button>
                  <button type="submit" className="btn" disabled={acting}>
                    确认接受（Paper）
                  </button>
                </div>
              </form>
            </div>
          </div>,
          document.body,
        )}

      {rejectOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            className="dialog-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="reject-title"
            aria-describedby="reject-consequences"
          >
            <div className="dialog-panel" ref={rejectPanelRef} tabIndex={-1}>
              <form
                onSubmit={(event) => {
                  event.preventDefault()
                  void confirmReject()
                }}
              >
                <h2 id="reject-title" style={{ marginTop: 0 }}>
                  拒绝入场复核
                </h2>
                <p className="status-message" id="reject-consequences">
                  机会转为 <span lang="en">INVALIDATED</span>
                  ，拒绝理由写入台账；LLM 决策保留供复盘。不会触发任何订单。
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
                  data-autofocus
                  required
                  aria-required="true"
                  aria-invalid={reasonError !== null}
                  aria-describedby={
                    reasonError === null ? 'reject-reason-hint' : 'reject-reason-error'
                  }
                  autoCapitalize="off"
                  spellCheck={false}
                  onChange={(event) => setReason(event.target.value)}
                  onInvalid={(event) => {
                    event.preventDefault()
                    setReasonError('错误：请输入拒绝理由。')
                  }}
                  style={{
                    width: '100%',
                    minHeight: 44,
                    padding: '4px 10px',
                    border: '1px solid var(--oft-border-control)',
                    background: 'var(--oft-bg-inset, #07080a)',
                    color: 'var(--oft-text-primary)',
                  }}
                />
                <p
                  className="panel-foot"
                  id="reject-reason-hint"
                  style={{ borderTop: 0, padding: '6px 0 0' }}
                >
                  可使用自由文本；不会触发任何订单。
                </p>
                {reasonError !== null && (
                  <p id="reject-reason-error" role="alert" className="status-message--error">
                    {reasonError}
                  </p>
                )}
                {actionError !== null && (
                  <p role="alert" className="status-message--error">
                    {actionError}
                  </p>
                )}
                <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
                  <button type="button" className="btn" onClick={closeReject}>
                    取消
                  </button>
                  <button
                    type="submit"
                    className="btn"
                    disabled={acting}
                    style={{
                      borderColor: 'var(--oft-status-danger)',
                      color: 'var(--oft-status-danger)',
                    }}
                  >
                    拒绝并标记 <span lang="en">invalidated</span>
                  </button>
                </div>
              </form>
            </div>
          </div>,
          document.body,
        )}
    </section>
  )
}
