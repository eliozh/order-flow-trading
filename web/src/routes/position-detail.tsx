import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { isAbortError } from '../api/abort'
import { fetchKillSwitch, type KillSwitchEnvelope } from '../api/paper'
import {
  fetchOpportunity,
  type OpportunityData,
  type OpportunityEnvelope,
} from '../api/opportunities'
import { fetchReport, utcLabel, type ReportData, type ReportEnvelope } from '../api/reports'
import { getTrigger, type TriggerDetailEnvelope, type TriggerPayload } from '../api/triggers'
import {
  dash,
  fetchPosition,
  fetchPositionHistory,
  formatSignedMoney,
  formatSignedPercent,
  managementIdempotencyKey,
  markState,
  netPnl,
  pnlPct,
  POSITION_REFRESH_MS,
  rMultiple,
  requestManagement,
  type PositionData,
  type PositionDetailEnvelope,
  type PositionHistoryData,
  type PositionHistoryEnvelope,
  type PositionManageData,
  type PositionManageEnvelope,
} from '../api/positions'
import { Chart } from '../components/chart'
import { DataQualityBadge } from '../components/data-quality'
import { ManagementTimeline } from '../components/management-timeline'
import { PnlBreakdown } from '../components/pnl-breakdown'
import { ProvenanceStamp } from '../components/provenance'
import { RiskPanel } from '../components/risk-panel'
import { useNarrowCompact } from '../hooks/use-compact'

interface PositionDetailPageProps {
  positionId: string
  fetchPositionFn?: (positionId: string, signal?: AbortSignal) => Promise<PositionDetailEnvelope>
  fetchHistoryFn?: (positionId: string, signal?: AbortSignal) => Promise<PositionHistoryEnvelope>
  fetchTriggerFn?: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
  fetchOpportunityFn?: (opportunityId: string, signal?: AbortSignal) => Promise<OpportunityEnvelope>
  fetchReportFn?: (reportId: string, signal?: AbortSignal) => Promise<ReportEnvelope>
  fetchKillSwitchFn?: (signal?: AbortSignal) => Promise<KillSwitchEnvelope>
  requestManagementFn?: (
    positionId: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<PositionManageEnvelope>
  isNarrow?: boolean
  /** Deterministic clock for age and mark freshness (defaults to Date.now()). */
  now?: number
  /** Revalidation cadence for position, history, and kill switch (default 5s). */
  refreshMs?: number
}

interface DetailState {
  state: 'loading' | 'ready' | 'error'
  position?: PositionData
  history?: PositionHistoryData
  trigger?: TriggerPayload | null
  opportunity?: OpportunityData | null
  report?: ReportData | null
  linkError?: string | null
  killSwitch?: boolean | null
  message?: string
}

/** The immutable trigger -> opportunity -> report chain for one position. */
interface LinkChain {
  trigger: TriggerPayload | null
  opportunity: OpportunityData | null
  report: ReportData | null
  error: string | null
}

const EMPTY_CHAIN: LinkChain = { trigger: null, opportunity: null, report: null, error: null }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

/**
 * Whether a polled position snapshot may replace the rendered one. A snapshot
 * whose version (or, when version is absent, whose ``updated_at``) is older may
 * have been captured before a management mutation and must not regress it.
 */
function positionSupersedes(incoming: PositionData, previous: PositionData | undefined): boolean {
  if (!previous) {
    return true
  }
  if (typeof incoming.version === 'number' && typeof previous.version === 'number') {
    return incoming.version >= previous.version
  }
  const incomingAt = Date.parse(incoming.updated_at)
  const previousAt = Date.parse(previous.updated_at)
  if (!Number.isNaN(incomingAt) && !Number.isNaN(previousAt)) {
    return incomingAt >= previousAt
  }
  return true
}

function recordField(
  record: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const value = record?.[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : null
}

function focusables(panel: HTMLElement | null): HTMLElement[] {
  if (!panel) {
    return []
  }
  return Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
}

/**
 * Modal focus contract: initial focus, Tab trap with the global kill switch as
 * the final stop (DESIGN A2.4), Escape, inert page content, and focus restore.
 * Only the page content is inert; the kill-switch bar stays reachable so
 * stopping is never blocked by a dialog.
 */
function useDialogFocus(open: boolean, onClose: () => void) {
  const panelRef = useRef<HTMLDivElement>(null)
  const restoreRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open || typeof document === 'undefined') {
      return
    }
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current
    const initial =
      panel?.querySelector<HTMLElement>('[data-autofocus]') ?? focusables(panel)[0] ?? panel
    initial?.focus()

    const shell = document.querySelector('.app-shell')
    const inertTargets = shell
      ? Array.from(shell.children).filter(
          (child): child is HTMLElement =>
            child instanceof HTMLElement && !child.classList.contains('kill-switch-bar'),
        )
      : []
    const inertWasSet = inertTargets.map((node) => node.hasAttribute('inert'))
    inertTargets.forEach((node) => node.setAttribute('inert', ''))
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    function killSwitchAction(): HTMLElement | null {
      // Only the ACTIVATE control may join the cycle. When the switch is active
      // its bar control opens the RESUME dialog and must never be a tab stop of
      // an already-open modal (that would stack two aria-modal dialogs).
      const control = document.querySelector<HTMLElement>('[data-kill-switch-action="activate"]')
      if (!control || control.hasAttribute('disabled') || control.closest('[inert]')) {
        return null
      }
      return control
    }

    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') {
        return
      }
      const dialogItems = focusables(panel)
      if (dialogItems.length === 0) {
        event.preventDefault()
        panel?.focus()
        return
      }
      const first = dialogItems[0]
      const lastDialog = dialogItems[dialogItems.length - 1]
      const killSwitch = killSwitchAction()
      const active = document.activeElement
      if (event.shiftKey) {
        if (active === killSwitch) {
          event.preventDefault()
          lastDialog.focus()
        } else if (active === first || active === panel) {
          event.preventDefault()
          ;(killSwitch ?? lastDialog).focus()
        }
      } else if (active === killSwitch) {
        event.preventDefault()
        first.focus()
      } else if (active === lastDialog) {
        event.preventDefault()
        ;(killSwitch ?? first).focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = previousOverflow
      inertTargets.forEach((node, index) => {
        if (!inertWasSet[index]) {
          node.removeAttribute('inert')
        }
      })
      const target = restoreRef.current
      const focusableTarget =
        target !== null &&
        target.isConnected &&
        !(target instanceof HTMLButtonElement && target.disabled) &&
        target.getAttribute('aria-disabled') !== 'true'
      if (focusableTarget) {
        target.focus()
      }
      if (!focusableTarget || document.activeElement !== target) {
        document.getElementById('position-detail-heading')?.focus()
      }
    }
  }, [open, onClose])

  return panelRef
}

/**
 * The one reason a management request is unavailable, or null when it can run.
 * The kill switch is tri-state: only an explicit `false` (inactive) enables the
 * action; an unknown state fails closed.
 */
function managementBlockedReason(
  position: PositionData,
  mark: ReturnType<typeof markState>,
  killSwitch: boolean | null,
  narrow: boolean,
): string | null {
  if (position.state !== 'OPEN') {
    return `持仓状态为 ${position.state}：只有 OPEN 持仓可以请求管理决策。`
  }
  if (mark === 'MISSING') {
    return 'mark 价格缺失（STALE）：管理动作已禁用，不伪造 PnL 或风控输入。'
  }
  if (mark === 'STALE') {
    return 'mark 价格已过期（STALE）：管理动作已禁用，等待新的 recorded mark。'
  }
  if (killSwitch === null) {
    return 'Kill switch 状态未知：管理动作已禁用，等待 kill-switch 状态确认。'
  }
  if (killSwitch) {
    return 'Kill switch 已启用：新订单与 LLM 管理执行已被阻断。'
  }
  if (narrow) {
    return '375px 只读：请在更宽的窗口请求管理决策。'
  }
  return null
}

/**
 * One position: PnL breakdown, entry caps, management timeline, linked signal
 * chain, and the paper-only management request (CSRF + idempotency). Every link
 * in the chain degrades to N/A instead of breaking the page.
 */
export function PositionDetailPage({
  positionId,
  fetchPositionFn = fetchPosition,
  fetchHistoryFn = fetchPositionHistory,
  fetchTriggerFn = getTrigger,
  fetchOpportunityFn = fetchOpportunity,
  fetchReportFn = fetchReport,
  fetchKillSwitchFn = fetchKillSwitch,
  requestManagementFn = requestManagement,
  isNarrow,
  now,
  refreshMs = POSITION_REFRESH_MS,
}: PositionDetailPageProps) {
  const [load, setLoad] = useState<DetailState>({ state: 'loading' })
  const [dialogOpen, setDialogOpen] = useState(false)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [linkRefreshError, setLinkRefreshError] = useState<string | null>(null)
  const [dialogClosedReason, setDialogClosedReason] = useState<string | null>(null)
  const [result, setResult] = useState<PositionManageData | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const narrow = useNarrowCompact(isNarrow)
  const inFlight = useRef(false)
  const keyRef = useRef<string | null>(null)
  const actionController = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  /** The last successfully loaded link chain (fetched once per position). */
  const chainRef = useRef<LinkChain | null>(null)
  /** Incremented whenever a management result is applied, invalidating polls. */
  const mutationEpoch = useRef(0)
  const clock = now ?? Date.now()

  // Abort the mutation only on unmount. A request in flight must be allowed to
  // settle so its definitive result can be surfaced, and its key retained.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      actionController.current?.abort()
    }
  }, [])

  const fetchChain = useCallback(
    async (sourceTriggerId: string | null, signal: AbortSignal): Promise<LinkChain> => {
      let trigger: TriggerPayload | null = null
      let opportunity: OpportunityData | null = null
      let report: ReportData | null = null
      let error: string | null = null
      if (sourceTriggerId) {
        try {
          trigger = (await fetchTriggerFn(sourceTriggerId, signal)).data
        } catch (caught: unknown) {
          if (signal.aborted || isAbortError(caught)) {
            throw caught
          }
          error = errorMessage(caught)
        }
        const opportunityId = trigger?.opportunity_id ?? null
        if (opportunityId) {
          try {
            opportunity = (await fetchOpportunityFn(opportunityId, signal)).data
          } catch (caught: unknown) {
            if (signal.aborted || isAbortError(caught)) {
              throw caught
            }
            error ??= errorMessage(caught)
          }
        }
        const reportId = opportunity?.source?.report_id ?? null
        if (reportId) {
          try {
            report = (await fetchReportFn(reportId, signal)).data
          } catch (caught: unknown) {
            if (signal.aborted || isAbortError(caught)) {
              throw caught
            }
            error ??= errorMessage(caught)
          }
        }
      }
      return { trigger, opportunity, report, error }
    },
    [fetchTriggerFn, fetchOpportunityFn, fetchReportFn],
  )

  const runLoad = useCallback(
    async (signal: AbortSignal, initial: boolean, refreshLinks = false): Promise<void> => {
      if (initial) {
        setLoad({ state: 'loading' })
      }
      const epochAtStart = mutationEpoch.current
      const killPromise = fetchKillSwitchFn(signal)
        .then((envelope) => (envelope.data ? envelope.data.active === true : null))
        .catch(() => null)
      const [positionEnvelope, historyEnvelope] = await Promise.all([
        fetchPositionFn(positionId, signal),
        fetchHistoryFn(positionId, signal),
      ])
      const position = positionEnvelope.data
      const history = historyEnvelope.data
      if (!position) {
        throw new Error('The position response was empty.')
      }
      if (!history) {
        throw new Error('The position history response was empty.')
      }

      // The immutable chain is loaded once per position. A manual revalidation
      // retries it, but a failure keeps the last successful chain (and chart)
      // and surfaces a non-blocking warning instead of blanking it.
      if (initial || refreshLinks || chainRef.current === null) {
        const chain = await fetchChain(position.source_trigger_id, signal)
        if (signal.aborted) {
          return
        }
        const previousChain = chainRef.current
        if (chain.error && !initial && previousChain && previousChain.error === null) {
          setLinkRefreshError(chain.error)
        } else {
          chainRef.current = chain
          setLinkRefreshError(null)
        }
      }

      const killSwitch = await killPromise
      if (signal.aborted) {
        return
      }
      // A poll that started before a management mutation may carry a pre-trade
      // snapshot; never let it regress the applied result for a cycle.
      if (epochAtStart !== mutationEpoch.current) {
        return
      }
      const chain = chainRef.current ?? EMPTY_CHAIN
      setLoad((previous) => {
        if (previous.state === 'ready' && !positionSupersedes(position, previous.position)) {
          return previous
        }
        return {
          state: 'ready',
          position,
          history,
          trigger: chain.trigger,
          opportunity: chain.opportunity,
          report: chain.report,
          linkError: chain.error,
          killSwitch,
        }
      })
      setRefreshError(null)
    },
    [positionId, fetchPositionFn, fetchHistoryFn, fetchChain, fetchKillSwitchFn],
  )

  useEffect(() => {
    const controller = new AbortController()
    runLoad(controller.signal, true).catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      setLoad({ state: 'error', message: errorMessage(error) })
    })
    return () => {
      controller.abort()
    }
  }, [runLoad])

  // Revalidate position/history/kill switch on a 5s cadence. A manual focus or
  // visibility revalidation also retries the immutable link chain. A failure
  // keeps the last values and shows a non-blocking warning instead of blanking.
  useEffect(() => {
    let controller: AbortController | null = null
    const revalidate = (refreshLinks: boolean): void => {
      controller?.abort()
      const next = new AbortController()
      controller = next
      runLoad(next.signal, false, refreshLinks).catch((error: unknown) => {
        if (next.signal.aborted || isAbortError(error)) {
          return
        }
        setRefreshError(errorMessage(error))
      })
    }
    const interval = window.setInterval(() => revalidate(false), refreshMs)
    const onFocus = (): void => {
      revalidate(true)
    }
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') {
        revalidate(true)
      }
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
      controller?.abort()
    }
  }, [runLoad, refreshMs])

  // A second aria-modal dialog must never stack on the management dialog. When
  // the kill switch turns active or unknown, close it with a visible, announced
  // reason. An in-flight mutation is allowed to settle first (H2 behavior).
  useEffect(() => {
    if (!dialogOpen || acting || inFlight.current || load.state !== 'ready') {
      return
    }
    const killSwitch = load.killSwitch ?? null
    if (killSwitch !== true && killSwitch !== null) {
      return
    }
    const reason = killSwitch === null ? 'Kill switch 状态未知' : 'Kill switch 已启用'
    const text = `${reason}：管理决策弹窗已自动关闭；不会叠加开启 Kill switch 的恢复弹窗。`
    setDialogOpen(false)
    setActionError(null)
    setDialogClosedReason(text)
    setAnnouncement(text)
  }, [dialogOpen, acting, load.state, load.killSwitch])

  const closeDialog = useCallback((): void => {
    // Never cancel an in-flight mutation: the server may already have executed
    // it. The dialog stays open (Cancel is disabled) until it settles.
    if (inFlight.current) {
      return
    }
    setDialogOpen(false)
    setActionError(null)
  }, [])
  const panelRef = useDialogFocus(dialogOpen, closeDialog)

  function openDialog(): void {
    setActionError(null)
    setDialogClosedReason(null)
    // One idempotency key per position until a definitive management result.
    keyRef.current ??= managementIdempotencyKey(`web-manage-${positionId}`)
    setDialogOpen(true)
  }

  async function confirmManagement(): Promise<void> {
    // A duplicate click must not send a second mutation, even before React
    // flushes the aria-disabled state.
    if (inFlight.current) {
      return
    }
    if (load.state !== 'ready' || !load.position) {
      return
    }
    // Recompute the gate at submit time so a mark that aged since the dialog
    // opened cannot be submitted.
    const submitMark = markState(load.position, now ?? Date.now())
    const submitBlocked = managementBlockedReason(
      load.position,
      submitMark,
      load.killSwitch ?? null,
      narrow,
    )
    if (submitBlocked !== null) {
      setActionError(submitBlocked)
      setAnnouncement(`管理请求已阻止：${submitBlocked}`)
      return
    }
    inFlight.current = true
    setActing(true)
    setActionError(null)
    const controller = new AbortController()
    actionController.current = controller
    const key = keyRef.current ?? managementIdempotencyKey(`web-manage-${positionId}`)
    keyRef.current = key
    try {
      const envelope = await requestManagementFn(positionId, key, controller.signal)
      if (controller.signal.aborted) {
        return
      }
      const data = envelope.data
      if (!data) {
        throw new Error('The management response was empty.')
      }
      setResult(data)
      // Any poll that started before this definitive result is now suspect.
      mutationEpoch.current += 1
      // A blocked response may carry `position: null`; treat that as unchanged
      // so the loaded position, binding cap, refusals, and timeline stay.
      setLoad((previous) =>
        previous.state === 'ready'
          ? { ...previous, position: data.position ?? previous.position }
          : previous,
      )
      setDialogOpen(false)
      // A definitive result releases the key; the next logical request rotates.
      keyRef.current = null
      setAnnouncement(`管理决策已返回：${data.action} / ${data.status}。`)
    } catch (error: unknown) {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      const message = errorMessage(error)
      setActionError(message)
      setAnnouncement(`管理请求失败：${message}`)
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setActing(false)
      }
    }
  }

  if (load.state === 'loading') {
    return (
      <section
        className="system-page"
        aria-labelledby="position-detail-heading"
        data-readonly={narrow}
      >
        <p className="status-message" role="status">
          加载持仓 {positionId}…
        </p>
      </section>
    )
  }

  if (load.state === 'error' || !load.position || !load.history) {
    return (
      <section className="system-page" aria-labelledby="position-detail-heading">
        <div className="panel" role="alert">
          <div className="panel-head">
            <h1 id="position-detail-heading" className="mono" tabIndex={-1}>
              未找到该持仓
            </h1>
          </div>
          <p className="panel-foot">
            {positionId}：{load.message ?? '持仓不存在或数据不可用。'}
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            <a className="btn" href="/positions">
              返回持仓列表
            </a>
          </div>
        </div>
      </section>
    )
  }

  const { position, history, trigger, opportunity, report } = load
  const mark = markState(position, clock)
  const net = netPnl(position)
  const pct = pnlPct(position)
  const r = rMultiple(position)
  const blockedReason = managementBlockedReason(position, mark, load.killSwitch ?? null, narrow)
  const riskVerdict = trigger?.risk_verdict ?? null
  const reportId = opportunity?.source?.report_id ?? null
  const orderId = recordField(result?.order, 'order_id')
  const fillId = recordField(result?.fill, 'fill_id')
  const pnlQuality = history.pnl.data_quality
  const fundingCount = history.funding.filter((item) => item.accrued).length
  // PARTIAL means the position data quality is not COMPLETE. A stale mark is a
  // separate state (its own chip); a stale-but-COMPLETE position is not PARTIAL.
  const positionPartial = position.data_quality.status !== 'COMPLETE'
  const entryNumber = Number(position.avg_entry_price)
  const entryMarkerClaim = Number.isFinite(entryNumber)
    ? `entry marker @ ${position.avg_entry_price} · 无新增指标`
    : 'entry marker 不可用 · 无新增指标'

  return (
    <section
      className="system-page"
      aria-labelledby="position-detail-heading"
      data-readonly={narrow}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <a className="rowlink" href="/positions">
              持仓
            </a>{' '}
            / 详情
          </p>
          <h1 id="position-detail-heading" className="mono" tabIndex={-1}>
            {position.position_id}
          </h1>
          <p className="page-sub">
            {position.symbol} · {position.side} · <span lang="en">state</span>{' '}
            <span lang="en">{position.state}</span> · <span lang="en">environment</span>{' '}
            <span lang="en">{position.environment}</span>
          </p>
        </div>
        <div className="report-stamps">
          <ProvenanceStamp kind="CALC" />
          <DataQualityBadge
            status={position.data_quality.status}
            reasons={position.data_quality.reasons}
          />
          {mark !== 'FRESH' && <span className="chip chip--warn">STALE</span>}
          {positionPartial && <span className="chip chip--warn">PARTIAL</span>}
        </div>
      </div>

      {narrow && (
        <p className="readonly-note" role="note">
          移动端只读：持仓详情在 375px 不可编辑。Kill switch 仍可用。
        </p>
      )}

      <p className="visually-hidden" role="status" aria-live="polite" data-testid="manage-live">
        {announcement}
      </p>

      {refreshError !== null && (
        <p className="status-message" role="status" data-testid="refresh-warning">
          自动刷新失败：{refreshError} — 保留上次数据，未清空页面。
        </p>
      )}

      {linkRefreshError !== null && (
        <p className="status-message" role="status" data-testid="link-refresh-warning">
          关联信号刷新失败：{linkRefreshError} — 保留上次的图表与链接。
        </p>
      )}

      <p className="mono-muted" style={{ fontSize: 11 }}>
        自动刷新：每 5s 轮询 position / history / kill switch；窗口聚焦或标签页可见时刷新关联信号。
      </p>

      <dl className="stat-grid">
        <div className="stat">
          <dt>
            {position.symbol} {position.side}
          </dt>
          <dd className="mono">{position.size}</dd>
          <dd className="stat-note">entry {position.avg_entry_price}</dd>
        </div>
        <div className="stat">
          <dt lang="en">Mark / uPnL</dt>
          <dd className="mono">
            {position.mark_price === null ? (
              <span className="chip chip--warn">STALE</span>
            ) : (
              position.mark_price
            )}
          </dd>
          <dd className="stat-note">
            {net === null ? '—' : formatSignedMoney(net)} ·{' '}
            {pct === null ? '—' : formatSignedPercent(pct)}
          </dd>
        </div>
        <div className="stat">
          <dt lang="en">R multiple</dt>
          <dd className="mono">{r === null ? '—' : r.toFixed(2)}</dd>
          <dd className="stat-note">risk 1.0R = {position.risk_amount_usd} USDT</dd>
        </div>
        <div className="stat">
          <dt lang="en">Stop / target</dt>
          <dd className="mono">
            {dash(position.stop_price)} /{' '}
            {position.target_prices.length > 0 ? position.target_prices.join(' / ') : '—'}
          </dd>
          <dd className="stat-note">risk {position.risk_pct.toFixed(2)}%</dd>
        </div>
      </dl>

      {positionPartial && (
        <div className="panel" role="status">
          <div className="panel-head">
            <h2>部分数据缺失</h2>
            <span className="chip chip--warn">PARTIAL</span>
          </div>
          <p className="panel-foot">
            PnL data quality {pnlQuality.status}；funding 已记录 {fundingCount} 笔。管理决策仍可见，
            但风险门可能 PARTIAL；未测量的项不会被当作零。mark 状态 {mark} 单独展示（STALE 不等于
            PARTIAL）。
          </p>
        </div>
      )}

      <section className="panel" aria-labelledby="pnl-heading">
        <div className="panel-head">
          <h2 id="pnl-heading">PnL 分解</h2>
          <span className="mono-muted">basis {history.pnl.basis}</span>
        </div>
        <PnlBreakdown position={position} pnl={history.pnl} />
      </section>

      <section className="panel" aria-labelledby="risk-heading">
        <div className="panel-head">
          <h2 id="risk-heading">入场时应用的风险上限</h2>
          <span className="mono-muted">stricter side wins</span>
        </div>
        {riskVerdict ? (
          <RiskPanel verdict={riskVerdict} symbol={position.symbol} />
        ) : (
          <>
            <dl className="kv">
              <dt>单笔风险</dt>
              <dd className="mono">
                {position.risk_pct.toFixed(2)}% · {position.risk_amount_usd} USDT
              </dd>
              <dt>杠杆</dt>
              <dd className="mono">{position.leverage}x</dd>
              <dt>名义</dt>
              <dd className="mono">{position.notional_usd} USDT</dd>
              <dt>保证金</dt>
              <dd className="mono">{position.margin_usd} USDT</dd>
            </dl>
            <p className="panel-foot">
              入场 risk verdict 不可用（历史记录缺失）；以上为持仓自身数值，不推断 caps。
            </p>
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="chart-heading">
        <div className="panel-head">
          <h2 id="chart-heading">图表</h2>
          <span className="mono-muted">{entryMarkerClaim}</span>
        </div>
        {report ? (
          <Chart
            symbol={report.symbol}
            timeframe={report.timeframe}
            bars={report.snapshot.bars}
            metrics={report.metrics}
            quality={report.data_quality}
            qualityReasons={report.metrics.quality_reasons}
            entryPrice={position.avg_entry_price}
          />
        ) : (
          <div role="note" aria-label="Chart unavailable" style={{ padding: 16 }}>
            <h3>Chart unavailable</h3>
            <p className="panel-foot" style={{ borderTop: 0, padding: '8px 0' }}>
              报告或快照不可用：不绘制虚构的图表或 entry marker。
              {load.linkError ? ` 原因：${load.linkError}` : ''}
            </p>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="links-heading">
        <div className="panel-head">
          <h2 id="links-heading">关联信号</h2>
        </div>
        <dl className="kv">
          <dt>Trigger</dt>
          <dd className="mono">
            {trigger ? (
              <a
                className="rowlink mono"
                href={`/triggers/${encodeURIComponent(trigger.trigger_id)}`}
              >
                {trigger.trigger_id}
              </a>
            ) : (
              'N/A'
            )}
          </dd>
          <dt>Opportunity</dt>
          <dd className="mono">
            {trigger?.opportunity_id || opportunity?.opportunity_id ? (
              <a
                className="rowlink mono"
                href={`/opportunities/${encodeURIComponent(
                  trigger?.opportunity_id ?? opportunity?.opportunity_id ?? '',
                )}`}
              >
                {trigger?.opportunity_id ?? opportunity?.opportunity_id}
              </a>
            ) : (
              'N/A'
            )}
          </dd>
          <dt>Report</dt>
          <dd className="mono">
            {reportId ? (
              <a className="rowlink mono" href={`/reports/${encodeURIComponent(reportId)}`}>
                {reportId}
              </a>
            ) : (
              'N/A'
            )}
          </dd>
        </dl>
      </section>

      <section className="panel" aria-labelledby="quality-heading">
        <div className="panel-head">
          <h2 id="quality-heading">数据质量</h2>
          <DataQualityBadge status={pnlQuality.status} reasons={pnlQuality.reasons} />
        </div>
        <dl className="kv">
          <dt>Mark freshness</dt>
          <dd className="mono">
            {mark} · updated {utcLabel(position.updated_at)} UTC
          </dd>
          <dt>PnL data quality</dt>
          <dd className="mono">
            {pnlQuality.status}
            {pnlQuality.reasons.length > 0 ? ` — ${pnlQuality.reasons.join('; ')}` : ''}
          </dd>
          <dt>Funding accruals</dt>
          <dd className="mono">{fundingCount} recorded settlements</dd>
          <dt>Funding quality</dt>
          <dd className="mono">
            {history.funding_reasons.length > 0
              ? history.funding_reasons.join('; ')
              : 'no unaccrued recorded settlements'}
          </dd>
        </dl>
      </section>

      <section className="panel" aria-labelledby="manage-heading">
        <div className="panel-head">
          <h2 id="manage-heading">管理决策</h2>
          <span className="chip chip--calc" lang="en">
            PAPER
          </span>
        </div>
        <div style={{ padding: 16 }}>
          <div className="action-row" style={{ marginTop: 0 }}>
            <button
              type="button"
              className={blockedReason !== null ? 'btn btn--muted' : 'btn'}
              onClick={() => {
                if (blockedReason !== null || acting) {
                  return
                }
                openDialog()
              }}
              disabled={acting}
              aria-disabled={blockedReason !== null || undefined}
              aria-describedby={blockedReason ? 'manage-blocked-reason' : undefined}
            >
              {acting ? '请求进行中…' : '请求管理决策'}
            </button>
            <span className="mono-muted">仅在 Paper 中由代码校验后执行；不会发送到交易所。</span>
          </div>
          {blockedReason !== null && (
            <p id="manage-blocked-reason" className="panel-foot" role="note">
              {blockedReason}
            </p>
          )}
          {actionError !== null && (
            <p role="alert" className="status-message--error" style={{ marginTop: 12 }}>
              管理请求失败：{actionError} — 未创建任何订单，持仓未改变。使用同一 Idempotency-Key
              重试是安全的。
            </p>
          )}
          {dialogClosedReason !== null && (
            <p className="status-message" role="status" data-testid="manage-dialog-closed-reason">
              {dialogClosedReason}
            </p>
          )}
          {(result || position.management_history.length > 0) && (
            <ManagementTimeline entries={position.management_history} result={result} />
          )}
          {result && result.executed && (
            <p className="panel-foot">
              已执行：订单 {orderId ?? '—'} · fill {fillId ?? '—'}。绑定上限{' '}
              {result.risk?.binding_cap ?? 'N/A'}；LLM 不能绕过任何风险上限。
            </p>
          )}
          <p className="panel-foot">
            LLM 只提议，CALC 校验并执行。确认后使用一个 Idempotency-Key 与 CSRF token；重复点击不会
            创建第二笔订单。
          </p>
        </div>
      </section>

      {dialogOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            className="dialog-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby="manage-title"
            aria-describedby="manage-consequence manage-killswitch-note"
          >
            <div className="dialog-panel" ref={panelRef} tabIndex={-1}>
              <form
                aria-busy={acting}
                onSubmit={(event) => {
                  event.preventDefault()
                  void confirmManagement()
                }}
              >
                <h2 id="manage-title" style={{ marginTop: 0 }}>
                  请求管理决策（Paper）
                </h2>
                <p className="status-message" id="manage-consequence">
                  管理决策在 Paper 中由代码校验后执行：LLM 只提议，CALC
                  校验并执行；不会发送到交易所， 也不会绕过风险上限。重复提交使用同一个
                  Idempotency-Key，不会创建第二笔订单。
                </p>
                <dl className="kv" style={{ padding: 0 }}>
                  <dt>持仓</dt>
                  <dd className="mono">
                    {position.position_id} · {position.symbol} {position.side}
                  </dd>
                  <dt>Mark</dt>
                  <dd className="mono">{position.mark_price ?? '—'}</dd>
                  <dt>Bindings</dt>
                  <dd className="mono">CSRF + Idempotency-Key</dd>
                </dl>
                <p id="manage-killswitch-note" className="visually-hidden">
                  Kill switch 在弹窗之外仍可用，并作为 Tab 循环的最后一个停留点。
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
                    data-autofocus
                    disabled={acting}
                    onClick={closeDialog}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className={acting ? 'btn btn--muted' : 'btn'}
                    aria-disabled={acting || undefined}
                    aria-busy={acting}
                  >
                    {acting ? '请求中…' : '确认请求管理'}
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
