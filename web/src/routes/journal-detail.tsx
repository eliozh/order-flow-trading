import { useCallback, useEffect, useRef, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  clearReviewKey,
  durationLabel,
  fetchJournalCompare,
  fetchJournalReview,
  fetchJournalTrade,
  fetchTriggerDecision,
  isJournalTradeData,
  JOURNAL_REFRESH_MS,
  JournalApiError,
  numberOrNull,
  requestJournalReview,
  reviewKeyFor,
  rotateReviewKey,
  type EntryDecisionData,
  type EntryDecisionEnvelope,
  type JournalCompareData,
  type JournalCompareEnvelope,
  type JournalReviewData,
  type JournalReviewEnvelope,
  type JournalTradeData,
  type JournalTradeEnvelope,
} from '../api/journal'
import {
  fetchOpportunity,
  type OpportunityData,
  type OpportunityEnvelope,
} from '../api/opportunities'
import {
  dash,
  fetchPosition,
  fetchPositionHistory,
  type PositionData,
  type PositionDetailEnvelope,
  type PositionHistoryData,
  type PositionHistoryEnvelope,
} from '../api/positions'
import { getTrigger, type TriggerDetailEnvelope, type TriggerPayload } from '../api/triggers'
import { DataQualityBadge } from '../components/data-quality'
import {
  JournalDecisionPanel,
  JournalExportPanel,
  JournalFills,
  JournalLinks,
  JournalTimeline,
  SignalSnapshot,
} from '../components/journal-detail-panels'
import { PausedPanel, PausedStamp } from '../components/paused-state'
import { PanelPause } from '../components/panel-pause'
import { ProvenanceStamp } from '../components/provenance'
import { ReviewBand } from '../components/review-band'
import { SignalComparison } from '../components/signal-comparison'
import { useCompact } from '../hooks/use-compact'
import { useJournalExport } from '../hooks/use-journal-export'
import { useLiveUpdates } from '../hooks/use-live-updates'
import { usePolling } from '../hooks/use-polling'
import { useRunGeneration } from '../hooks/use-run-generation'

interface Settled<T> {
  value: T | null
  error: string | null
}

async function settle<T>(task: Promise<{ data: T | null }> | null): Promise<Settled<T>> {
  if (task === null) {
    return { value: null, error: null }
  }
  try {
    const envelope = await task
    return { value: envelope.data, error: null }
  } catch (error: unknown) {
    return { value: null, error: errorMessage(error) }
  }
}

interface DetailLoad {
  state: 'loading' | 'ready' | 'error' | 'not-found'
  trade: JournalTradeData | null
  compare: JournalCompareData | null
  compareError: string | null
  review: JournalReviewData | null
  reviewError: string | null
  trigger: TriggerPayload | null
  triggerError: string | null
  entryDecision: EntryDecisionData | null
  entryDecisionError: string | null
  opportunity: OpportunityData | null
  opportunityError: string | null
  position: PositionData | null
  positionError: string | null
  history: PositionHistoryData | null
  historyError: string | null
  message?: string
}

function emptyLoad(): DetailLoad {
  return {
    state: 'loading',
    trade: null,
    compare: null,
    compareError: null,
    review: null,
    reviewError: null,
    trigger: null,
    triggerError: null,
    entryDecision: null,
    entryDecisionError: null,
    opportunity: null,
    opportunityError: null,
    position: null,
    positionError: null,
    history: null,
    historyError: null,
  }
}

interface JournalDetailPageProps {
  tradeId: string
  fetchTradeFn?: (tradeId: string, signal?: AbortSignal) => Promise<JournalTradeEnvelope>
  fetchCompareFn?: (tradeId: string, signal?: AbortSignal) => Promise<JournalCompareEnvelope>
  fetchReviewFn?: (tradeId: string, signal?: AbortSignal) => Promise<JournalReviewEnvelope>
  fetchTriggerFn?: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
  fetchTriggerDecisionFn?: (
    triggerId: string,
    signal?: AbortSignal,
  ) => Promise<EntryDecisionEnvelope>
  fetchOpportunityFn?: (opportunityId: string, signal?: AbortSignal) => Promise<OpportunityEnvelope>
  fetchPositionFn?: (positionId: string, signal?: AbortSignal) => Promise<PositionDetailEnvelope>
  fetchHistoryFn?: (positionId: string, signal?: AbortSignal) => Promise<PositionHistoryEnvelope>
  requestReviewFn?: (
    tradeId: string,
    options: { refresh?: boolean; idempotencyKey: string },
    signal?: AbortSignal,
  ) => Promise<JournalReviewEnvelope>
  isCompact?: boolean
  /** Revalidation cadence for the review read and comparison (default 15s). */
  refreshMs?: number
  /** Override the global pause state (unit tests); defaults to the context. */
  paused?: boolean
}

/**
 * `/journal/:tradeId`: the closed-trade summary, the signal → trigger →
 * decision → entry → management → exit timeline, the signal snapshot, the
 * persisted entry decision, fills, the signal-vs-outcome comparison, the
 * advisory LLM review band, and server-generated export links.
 *
 * Trade/compare/review/trigger/decision/opportunity/position/history load
 * independently: one failure renders N/A for that section and never blanks the
 * page. Review generation is CSRF + Idempotency-Key protected, advisory only,
 * and has no rule-apply action anywhere.
 */
export function JournalDetailPage({
  tradeId,
  fetchTradeFn = fetchJournalTrade,
  fetchCompareFn = fetchJournalCompare,
  fetchReviewFn = fetchJournalReview,
  fetchTriggerFn = getTrigger,
  fetchTriggerDecisionFn = fetchTriggerDecision,
  fetchOpportunityFn = fetchOpportunity,
  fetchPositionFn = fetchPosition,
  fetchHistoryFn = fetchPositionHistory,
  requestReviewFn = requestJournalReview,
  isCompact,
  refreshMs = JOURNAL_REFRESH_MS,
  paused,
}: JournalDetailPageProps) {
  const [load, setLoad] = useState<DetailLoad>(emptyLoad)
  const [retryToken, setRetryToken] = useState(0)
  const [generating, setGenerating] = useState(false)
  const [reviewAnnouncement, setReviewAnnouncement] = useState<string | null>(null)
  const [actionError, setActionError] = useState<{
    message: string
    kind: 'definitive' | 'unknown'
  } | null>(null)
  const [replayNote, setReplayNote] = useState(false)
  // The pending key of an explicit refresh attempt that may have reached the
  // server. It is replayed only by a retry of that same attempt, never by a new
  // explicit regenerate.
  const pendingRefreshRef = useRef<string | null>(null)
  // Whether the most recent review attempt was an explicit refresh. A retry
  // must replay that exact request: the idempotency hash binds `refresh`, so
  // deriving it from the visible review status would start a new generation.
  const lastAttemptRefreshRef = useRef(false)
  const [panelPaused, setPanelPaused] = useState(false)
  const { exporting, exportError, runExport } = useJournalExport()
  const compact = useCompact(isCompact)
  const live = useLiveUpdates()
  const isPaused = paused ?? live.paused
  const { begin, isCurrent } = useRunGeneration()
  // A background refresh must never invalidate an in-flight initial load.
  const loadStateRef = useRef(load.state)
  const inFlight = useRef(false)
  const actionController = useRef<AbortController | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    loadStateRef.current = load.state
  }, [load.state])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      // The server call is allowed to settle; aborting is cleanup only.
      actionController.current?.abort()
    }
  }, [])

  const runInitial = useCallback(
    async (signal: AbortSignal, initial: boolean, generation: number): Promise<void> => {
      if (initial) {
        setLoad((prev) => ({ ...prev, state: 'loading' }))
      }
      let trade: JournalTradeData | null = null
      try {
        const envelope = await fetchTradeFn(tradeId, signal)
        trade = envelope.data
      } catch (error: unknown) {
        if (signal.aborted || !isCurrent(generation)) {
          return
        }
        if (
          error instanceof JournalApiError &&
          (error.httpStatus === 404 || error.code === 'NOT_FOUND')
        ) {
          setLoad({ ...emptyLoad(), state: 'not-found' })
          return
        }
        setLoad({ ...emptyLoad(), state: 'error', message: errorMessage(error) })
        return
      }
      if (signal.aborted || !isCurrent(generation)) {
        return
      }
      // `/journal/stats` and `/journal/export` are reserved API paths that return
      // `ok: true` with a non-trade payload. Validate the contract (including the
      // requested trade id) before dereferencing any field; a mismatch is the
      // same not-found panel as a 404 and never renders the foreign payload.
      if (!isJournalTradeData(trade, tradeId)) {
        setLoad({ ...emptyLoad(), state: 'not-found' })
        return
      }
      const refs = trade.signal
      const triggerId = refs?.trigger_id ?? null
      const opportunityId = refs?.opportunity_id ?? null
      const positionId = trade.position_id || null
      const [compare, review, trigger, decision, opportunity, position, history] =
        await Promise.all([
          settle(fetchCompareFn(tradeId, signal)),
          settle(fetchReviewFn(tradeId, signal)),
          settle(triggerId ? fetchTriggerFn(triggerId, signal) : null),
          settle(triggerId ? fetchTriggerDecisionFn(triggerId, signal) : null),
          settle(opportunityId ? fetchOpportunityFn(opportunityId, signal) : null),
          settle(positionId ? fetchPositionFn(positionId, signal) : null),
          settle(positionId ? fetchHistoryFn(positionId, signal) : null),
        ])
      if (signal.aborted || !isCurrent(generation)) {
        return
      }
      setLoad({
        state: 'ready',
        trade,
        compare: compare.value,
        compareError: compare.error,
        review: review.value,
        reviewError: review.error,
        trigger: trigger.value,
        triggerError: trigger.error,
        entryDecision: decision.value,
        entryDecisionError: decision.error,
        opportunity: opportunity.value,
        opportunityError: opportunity.error,
        position: position.value,
        positionError: position.error,
        history: history.value,
        historyError: history.error,
      })
    },
    [
      tradeId,
      fetchTradeFn,
      fetchCompareFn,
      fetchReviewFn,
      fetchTriggerFn,
      fetchTriggerDecisionFn,
      fetchOpportunityFn,
      fetchPositionFn,
      fetchHistoryFn,
      isCurrent,
    ],
  )

  useEffect(() => {
    const controller = new AbortController()
    const generation = begin()
    runInitial(controller.signal, true, generation).catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      if (!isCurrent(generation)) {
        return
      }
      setLoad({ ...emptyLoad(), state: 'error', message: errorMessage(error) })
    })
    return () => {
      controller.abort()
    }
  }, [runInitial, retryToken, begin, isCurrent])

  const refreshLive = useCallback(
    async (signal: AbortSignal, generation: number): Promise<void> => {
      const [compare, review] = await Promise.all([
        settle(fetchCompareFn(tradeId, signal)),
        settle(fetchReviewFn(tradeId, signal)),
      ])
      if (signal.aborted || !isCurrent(generation)) {
        return
      }
      if (review.value?.status === 'FOUND') {
        // A confirmed review invalidates any stale "outcome unknown" error.
        setActionError(null)
      }
      setLoad((prev) =>
        prev.state === 'ready'
          ? {
              ...prev,
              compare: compare.value ?? prev.compare,
              compareError: compare.error,
              review: review.value ?? prev.review,
              reviewError: review.error,
            }
          : prev,
      )
    },
    [tradeId, fetchCompareFn, fetchReviewFn, isCurrent],
  )

  const { revalidate } = usePolling(
    (signal) => {
      // Until the page is ready the initial load owns the generation; a poll
      // (or focus refresh) must not invalidate it.
      if (loadStateRef.current !== 'ready') {
        return
      }
      const generation = begin()
      return refreshLive(signal, generation)
    },
    { refreshMs, paused: isPaused || panelPaused },
  )

  const regenerate = useCallback(
    async (intent: 'generate' | 'regenerate' | 'retry'): Promise<void> => {
      // A duplicate click must not send a second mutation, even before React
      // flushes the aria-disabled state.
      if (compact || inFlight.current) {
        return
      }
      inFlight.current = true
      setGenerating(true)
      setReviewAnnouncement('正在生成复盘…')
      setActionError(null)
      const controller = new AbortController()
      actionController.current = controller
      // A retry replays the failed attempt with its own key and refresh flag.
      // An explicit regenerate is a deliberate new provider generation: it must
      // never reuse a pending refresh key left behind by an unknown outcome
      // (a later read may have found the stored review, but the user asked for
      // a fresh one), so it rotates a new key.
      const refresh = intent === 'retry' ? lastAttemptRefreshRef.current : intent === 'regenerate'
      lastAttemptRefreshRef.current = refresh
      let key: string
      if (intent === 'retry') {
        key = refresh
          ? (pendingRefreshRef.current ?? rotateReviewKey(tradeId))
          : reviewKeyFor(tradeId)
        if (refresh) {
          pendingRefreshRef.current = key
        }
      } else if (intent === 'regenerate') {
        pendingRefreshRef.current = null
        key = rotateReviewKey(tradeId)
        pendingRefreshRef.current = key
      } else {
        key = reviewKeyFor(tradeId)
      }
      try {
        const result = await requestReviewFn(
          tradeId,
          { refresh, idempotencyKey: key },
          controller.signal,
        )
        if (controller.signal.aborted) {
          return
        }
        clearReviewKey(tradeId)
        pendingRefreshRef.current = null
        setActionError(null)
        setReplayNote(Boolean(result.data?.idempotent_replay || result.meta?.idempotent_replay))
        if (result.data) {
          setLoad((prev) =>
            prev.state === 'ready' ? { ...prev, review: result.data, reviewError: null } : prev,
          )
        }
        const decisionId = result.data?.decision_id ?? null
        setReviewAnnouncement(decisionId ? `复盘已生成：${decisionId}` : '复盘已生成')
        const generation = begin()
        try {
          const read = await fetchReviewFn(tradeId, controller.signal)
          if (controller.signal.aborted || !isCurrent(generation)) {
            return
          }
          if (read.data?.status === 'FOUND') {
            setLoad((prev) =>
              prev.state === 'ready' ? { ...prev, review: read.data, reviewError: null } : prev,
            )
          }
        } catch {
          // The accepted POST result remains visible.
        }
      } catch (error: unknown) {
        if (controller.signal.aborted || isAbortError(error)) {
          return
        }
        const kind = error instanceof JournalApiError ? error.kind : 'unknown'
        if (kind === 'definitive') {
          clearReviewKey(tradeId)
          pendingRefreshRef.current = null
        }
        if (mounted.current) {
          setReviewAnnouncement(null)
          setActionError({ message: errorMessage(error), kind })
        }
      } finally {
        inFlight.current = false
        if (mounted.current) {
          setGenerating(false)
        }
      }
    },
    [compact, tradeId, requestReviewFn, fetchReviewFn, begin, isCurrent],
  )

  if (load.state === 'loading') {
    return (
      <section
        className="system-page"
        aria-labelledby="journal-detail-heading"
        data-readonly={compact}
      >
        <div className="page-head">
          <div>
            <p className="kicker">
              <a className="rowlink" href="/journal">
                复盘
              </a>{' '}
              / 交易详情
            </p>
            <h1 id="journal-detail-heading" className="mono" tabIndex={-1}>
              {tradeId}
            </h1>
          </div>
        </div>
        <p className="status-message" role="status">
          加载交易 {tradeId}…
        </p>
      </section>
    )
  }

  if (load.state === 'not-found') {
    return (
      <section className="system-page" aria-labelledby="journal-detail-heading">
        <div className="panel" role="alert">
          <div className="panel-head">
            <h1 id="journal-detail-heading" className="mono" tabIndex={-1}>
              未找到该交易，或交易尚未平仓
            </h1>
          </div>
          <p className="panel-foot">
            {tradeId}：该交易不存在，或尚未平仓；复盘在平仓后生成。不显示虚构的 outcome。
          </p>
          <div className="action-row journal-panel-actions">
            <a className="btn" href="/journal">
              返回交易日志
            </a>
            <a className="btn" href="/positions">
              返回持仓
            </a>
          </div>
        </div>
      </section>
    )
  }

  if (load.state === 'error' || !load.trade) {
    return (
      <section className="system-page" aria-labelledby="journal-detail-heading">
        <div className="panel" role="alert">
          <div className="panel-head">
            <h1 id="journal-detail-heading" className="mono" tabIndex={-1}>
              交易数据不可用
            </h1>
          </div>
          <p className="panel-foot">
            {tradeId}：{load.message ?? '交易请求失败或其响应为空。'}
          </p>
          <div className="action-row journal-panel-actions">
            <button
              type="button"
              className="btn"
              onClick={() => {
                setRetryToken((value) => value + 1)
              }}
            >
              重试加载交易
            </button>
            <a className="btn" href="/journal">
              返回交易日志
            </a>
          </div>
        </div>
      </section>
    )
  }

  const trade = load.trade
  const refs = trade.signal
  const outcomeTags = trade.outcome_tags ?? []
  const quality = trade.data_quality ?? { status: 'MISSING', reasons: [] }
  const partial = quality.status !== 'COMPLETE'
  const netValue = numberOrNull(trade.net_pnl_usd)
  const netClass =
    netValue === null
      ? 'mono'
      : netValue > 0
        ? 'mono value-up'
        : netValue < 0
          ? 'mono value-down'
          : 'mono value-flat'
  const rValue = numberOrNull(trade.r_multiple)
  const rText = rValue === null ? '—' : rValue.toFixed(2)

  const decisionConfidence =
    load.entryDecision?.confidence ?? load.entryDecision?.decision?.confidence ?? null
  const decisionText = load.entryDecision?.decision?.entry_recheck?.decision ?? null
  const decisionRationale = load.entryDecision?.decision?.rationale ?? null
  const decisionId = load.entryDecision?.entry_decision_id ?? refs?.decision_id ?? null
  const decisionValidation =
    load.entryDecision?.validation_status ?? load.entryDecision?.decision?.validation_status ?? null
  const decisionProvenance =
    load.entryDecision?.provenance ?? load.entryDecision?.decision?.provenance ?? null

  const triggerId = refs?.trigger_id ?? null
  const opportunityId = refs?.opportunity_id ?? null
  const reportId = refs?.report_id ?? null

  const triggerReason =
    load.triggerError ?? (triggerId ? 'trigger 读取失败' : '交易未记录 trigger 引用')
  const decisionReason =
    load.entryDecisionError ??
    load.entryDecision?.reasons?.join('; ') ??
    (triggerId ? '没有可用的入场决策' : '交易未记录 trigger 引用')
  const managementEvents = load.history?.events ?? []
  const managementReason =
    load.historyError ??
    (load.history
      ? managementEvents.length === 0
        ? '没有管理事件'
        : null
      : trade.position_id
        ? 'position history 不可用'
        : '交易未记录持仓引用')

  const zoneLow = load.opportunity?.zone.low ?? load.compare?.zone.zone_low ?? null
  const zoneHigh = load.opportunity?.zone.high ?? load.compare?.zone.zone_high ?? null
  const zoneText = zoneLow && zoneHigh ? `${zoneLow} – ${zoneHigh}` : 'N/A'
  const slippage = load.compare?.slippage ?? null
  const slippageBps = numberOrNull(slippage?.bps ?? null)
  const slippageText =
    slippage?.status === 'OK' && slippageBps !== null
      ? `${slippageBps} bps · ref ${dash(slippage.reference_price)}`
      : `N/A — ${slippage?.reasons.join('; ') || 'slippage 不可用'}`
  const invalidationText =
    load.opportunity && load.opportunity.invalidation_conditions.length > 0
      ? load.opportunity.invalidation_conditions.map((condition) => condition.type).join(', ')
      : `N/A — ${load.opportunityError ?? '没有失效条件'}`

  const signalTime = load.opportunity?.created_at ? load.opportunity.created_at : null
  const signalReason =
    load.opportunityError ??
    (opportunityId ? 'opportunity 读取失败' : '交易未记录 opportunity 引用')
  const decisionTime = load.entryDecision?.decision?.persisted_at
    ? load.entryDecision.decision.persisted_at
    : null

  return (
    <section
      className="system-page"
      aria-labelledby="journal-detail-heading"
      data-readonly={compact}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/journal">
              复盘
            </a>{' '}
            / 交易详情
          </p>
          <h1 id="journal-detail-heading" className="mono" tabIndex={-1}>
            {trade.trade_id}
          </h1>
          <p className="page-sub" lang="en">
            {trade.symbol} · {trade.side} · closed {trade.closed_at} UTC · {trade.environment}
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="CALC" />
          <DataQualityBadge status={quality.status} reasons={quality.reasons} />
          {isPaused && <PausedStamp testId="journal-detail-paused-stamp" />}
        </div>
      </div>

      {compact && (
        <p className="readonly-note" role="note">
          移动端只读：复盘生成请在桌面端或 CLI 完成。Kill switch 仍可用。
        </p>
      )}

      {isPaused && <PausedPanel testId="journal-detail-paused" />}

      {load.reviewError !== null && (
        <p className="status-message" role="status" data-testid="journal-review-refresh-warning">
          复盘刷新失败：{load.reviewError} — 保留上次的复盘与对照。
        </p>
      )}

      {load.compareError !== null && (
        <p className="status-message" role="status" data-testid="journal-compare-refresh-warning">
          comparison 刷新失败：{load.compareError} — 保留上次的对照。
        </p>
      )}

      <div className="action-row">
        <PanelPause
          paused={panelPaused}
          onToggle={() => setPanelPaused((value) => !value)}
          label="暂停复盘与对照自动刷新"
          testId="journal-detail-panel-pause"
        />
      </div>

      <p className="mono-muted">
        {isPaused
          ? '实时更新已暂停：不轮询 review / comparison，也不因窗口聚焦刷新。'
          : panelPaused
            ? '本面板自动刷新已暂停：保留已加载的复盘与对照；恢复后立即重新校验一次。'
            : `自动刷新：每 ${Math.round(refreshMs / 1000)}s 轮询 review 与 comparison；窗口聚焦或标签页可见时刷新。`}
      </p>

      <div className="detail-cols">
        <div className="stack-sm">
          <dl className="stat-grid">
            <div className="stat">
              <dt lang="en">
                {trade.symbol} {trade.side}
              </dt>
              <dd className={netClass}>{trade.net_pnl_usd}</dd>
              <dd className="stat-note" lang="en">
                R {rText} · {outcomeTags.join(' / ') || '—'}
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Entry / Exit</dt>
              <dd className="mono">
                {dash(trade.entry_avg_price)} / {dash(trade.exit_avg_price)}
              </dd>
              <dd className="stat-note" lang="en">
                size {dash(trade.size)} · tick precision kept
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Duration</dt>
              <dd className="mono">{durationLabel(trade.opened_at, trade.closed_at)}</dd>
              <dd className="stat-note" lang="en">
                {trade.environment} · {trade.closed_at} UTC
              </dd>
            </div>
            <div className="stat">
              <dt lang="en">Fees / Funding</dt>
              <dd className="mono">
                {dash(trade.fees_usd)} / {dash(trade.funding_usd)}
              </dd>
              <dd className="stat-note" lang="en">
                net USDT · CALC
              </dd>
            </div>
          </dl>

          <JournalTimeline
            trade={trade}
            signalTime={signalTime}
            signalReason={signalReason}
            trigger={load.trigger}
            triggerReason={triggerReason}
            decisionText={decisionText}
            decisionConfidence={decisionConfidence}
            decisionId={decisionId}
            decisionProvenance={decisionProvenance}
            decisionTime={decisionTime}
            decisionReason={decisionReason}
            managementEvents={managementEvents}
            managementReason={managementReason}
            netValue={netValue}
          />

          <SignalSnapshot
            opportunityId={opportunityId}
            reportId={reportId}
            zoneText={zoneText}
            entryAvgPrice={trade.entry_avg_price}
            slippageText={slippageText}
            invalidationText={invalidationText}
          />

          <ReviewBand
            review={load.review}
            loading={false}
            readError={load.reviewError}
            generating={generating}
            announcement={reviewAnnouncement}
            actionError={actionError?.message ?? null}
            actionErrorKind={actionError?.kind ?? null}
            readonly={compact}
            onGenerate={() => {
              void regenerate(
                actionError !== null
                  ? 'retry'
                  : load.review?.status === 'FOUND'
                    ? 'regenerate'
                    : 'generate',
              )
            }}
            onRetryRead={() => revalidate(true)}
          />

          {replayNote && load.review?.idempotent_replay !== true && (
            <p className="band-note" data-testid="journal-review-replay">
              幂等重放：复用已存结果
            </p>
          )}
        </div>

        <div className="stack-sm">
          <SignalComparison compare={load.compare} error={load.compareError} />

          <JournalDecisionPanel
            entryDecision={load.entryDecision}
            decisionText={decisionText}
            decisionConfidence={decisionConfidence}
            decisionId={decisionId}
            decisionValidation={decisionValidation}
            decisionRationale={decisionRationale}
            decisionProvenance={decisionProvenance}
            decisionReason={decisionReason}
          />

          <JournalFills trade={trade} partial={partial} qualityReasons={quality.reasons ?? []} />

          <JournalLinks
            triggerId={triggerId}
            opportunityId={opportunityId}
            positionId={trade.position_id}
            reportId={reportId}
          />

          <JournalExportPanel
            headingId="journal-detail-export-heading"
            csvLabel="导出该标的 CSV"
            jsonLabel="导出该标的 JSON"
            exporting={exporting}
            exportError={exportError}
            errorTestId="journal-detail-export-error"
            onExport={(format) => {
              void runExport(format, { symbol: trade.symbol })
            }}
          />
        </div>
      </div>
    </section>
  )
}
