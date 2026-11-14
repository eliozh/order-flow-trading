import { fetchKillSwitch, type KillSwitchEnvelope } from '../api/paper'
import { fetchOpportunity, type OpportunityEnvelope } from '../api/opportunities'
import { fetchReport, utcLabel, type ReportEnvelope } from '../api/reports'
import { getTrigger, type TriggerDetailEnvelope } from '../api/triggers'
import {
  dash,
  fetchPosition,
  fetchPositionHistory,
  formatSignedMoney,
  formatSignedPercent,
  markAsOf,
  markState,
  netPnl,
  pnlPct,
  POSITION_REFRESH_MS,
  requestManagement,
  resolvedMark,
  rMultiple,
  type PositionDetailEnvelope,
  type PositionHistoryEnvelope,
  type PositionManageEnvelope,
} from '../api/positions'
import { Chart } from '../components/chart'
import { DataQualityBadge } from '../components/data-quality'
import { PausedPanel, PausedStamp } from '../components/paused-state'
import { PositionManagementPanel } from '../components/position-management-panel'
import { PnlBreakdown } from '../components/pnl-breakdown'
import { RiskPanel } from '../components/risk-panel'
import { ProvenanceStamp } from '../components/provenance'
import { useNarrowCompact } from '../hooks/use-compact'
import { useLiveUpdates } from '../hooks/use-live-updates'
import { usePositionDetailData } from '../hooks/use-position-detail-data'
import { usePositionManagement } from '../hooks/use-position-management'
import { managementBlockedReason } from './position-detail-model'

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
  /** Override the global pause state (unit tests); defaults to the context. */
  paused?: boolean
}

/**
 * One position: PnL breakdown, entry caps, management timeline, linked signal
 * chain, and the paper-only management request (CSRF + idempotency). Every link
 * in the chain degrades to N/A instead of breaking the page.
 *
 * Position and history are independent: a history-only failure renders the
 * position with a "history unavailable" panel instead of "not found".
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
  paused,
}: PositionDetailPageProps) {
  const narrow = useNarrowCompact(isNarrow)
  const live = useLiveUpdates()
  const isPaused = paused ?? live.paused
  const clock = now ?? Date.now()

  const data = usePositionDetailData({
    positionId,
    fetchPositionFn,
    fetchHistoryFn,
    fetchTriggerFn,
    fetchOpportunityFn,
    fetchReportFn,
    fetchKillSwitchFn,
    refreshMs,
    paused: isPaused,
  })
  const management = usePositionManagement({
    positionId,
    load: data.load,
    now,
    narrow,
    requestManagementFn,
    onPositionApplied: data.applyManagementPosition,
    onUnknownOutcome: data.revalidate,
  })

  if (data.load.state === 'loading') {
    return (
      <section
        className="system-page"
        aria-labelledby="position-detail-heading"
        data-readonly={narrow}
      >
        <div className="page-head">
          <div>
            <p className="kicker">持仓 / 详情</p>
            <h1 id="position-detail-heading" className="mono" tabIndex={-1}>
              {positionId}
            </h1>
          </div>
        </div>
        <p className="status-message" role="status">
          加载持仓 {positionId}…
        </p>
      </section>
    )
  }

  if (data.load.state === 'error' || !data.load.position) {
    const notFound = data.load.notFound === true
    return (
      <section className="system-page" aria-labelledby="position-detail-heading">
        <div className="panel" role="alert">
          <div className="panel-head">
            <h1 id="position-detail-heading" className="mono" tabIndex={-1}>
              {notFound ? '未找到该持仓' : '持仓数据不可用'}
            </h1>
          </div>
          <p className="panel-foot">
            {positionId}：
            {data.load.message ??
              (notFound ? '持仓不存在或数据不可用。' : '持仓请求失败或其响应为空。')}
          </p>
          <div className="action-row" style={{ padding: '0 16px 16px' }}>
            {!notFound && (
              <button type="button" className="btn" onClick={data.retryInitial}>
                重试加载持仓
              </button>
            )}
            <a className="btn" href="/positions">
              返回持仓列表
            </a>
          </div>
        </div>
      </section>
    )
  }

  const { position, trigger, opportunity, report } = data.load
  const history = data.load.history ?? null
  const mark = markState(position, clock)
  const net = netPnl(position)
  const pct = pnlPct(position)
  const r = rMultiple(position)
  const blockedReason = managementBlockedReason(
    position,
    mark,
    data.load.killSwitch ?? null,
    narrow,
  )
  const riskVerdict = trigger?.risk_verdict ?? null
  const reportId = opportunity?.source?.report_id ?? null
  const pnlQuality = history?.pnl.data_quality ?? null
  const fundingCount = history ? history.funding.filter((item) => item.accrued).length : 0
  // PARTIAL means the position data quality is not COMPLETE. A stale mark is a
  // separate state (its own chip); a stale-but-COMPLETE position is not PARTIAL.
  const positionPartial = position.data_quality.status !== 'COMPLETE'
  const entryNumber = Number(position.avg_entry_price)
  const entryMarkerClaim = Number.isFinite(entryNumber)
    ? `entry marker @ ${position.avg_entry_price} · 无新增指标`
    : 'entry marker 不可用 · 无新增指标'
  const resolved = resolvedMark(position)
  const asOf = markAsOf(position)

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
          {isPaused && <PausedStamp testId="position-detail-paused-stamp" />}
        </div>
      </div>

      {narrow && (
        <p className="readonly-note" role="note">
          移动端只读：持仓详情在 375px 不可编辑。Kill switch 仍可用。
        </p>
      )}

      {isPaused && <PausedPanel testId="position-detail-paused" />}

      {data.refreshError !== null && (
        <p className="status-message" role="status" data-testid="refresh-warning">
          自动刷新失败：{data.refreshError} — 保留上次数据，未清空页面。
        </p>
      )}

      {history !== null && data.load.historyError !== null && (
        <p className="status-message" role="status" data-testid="history-refresh-warning">
          历史刷新失败：{data.load.historyError} — 保留上次的 PnL 分解与时间线。
        </p>
      )}

      {data.linkRefreshError !== null && (
        <p className="status-message" role="status" data-testid="link-refresh-warning">
          关联信号刷新失败：{data.linkRefreshError} — 保留上次的图表与链接。
        </p>
      )}

      <p className="mono-muted" style={{ fontSize: 11 }}>
        {isPaused
          ? '实时更新已暂停：不轮询 position / history / kill switch，也不因窗口聚焦刷新。'
          : '自动刷新：每 5s 轮询 position / history / kill switch；窗口聚焦或标签页可见时刷新关联信号。'}
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
            {resolved === null ? <span className="chip chip--warn">STALE</span> : resolved}
          </dd>
          <dd className="stat-note">
            {net === null ? '—' : formatSignedMoney(net)} ·{' '}
            {pct === null ? '—' : formatSignedPercent(pct)} · as of {utcLabel(asOf)} UTC
            {position.resolved_mark_source ? ` · ${position.resolved_mark_source}` : ''}
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

      {history === null && (
        <div className="panel" role="alert" data-testid="history-unavailable">
          <div className="panel-head">
            <h2>历史数据不可用</h2>
          </div>
          <p className="panel-foot">
            {data.load.historyError ?? 'position history 请求失败'} — 持仓本身仍按服务端返回展示；
            不伪造 PnL 分解、资金费或数据质量。
          </p>
        </div>
      )}

      {positionPartial && (
        <div className="panel" role="status">
          <div className="panel-head">
            <h2>部分数据缺失</h2>
            <span className="chip chip--warn">PARTIAL</span>
          </div>
          <p className="panel-foot">
            {history && pnlQuality
              ? `PnL data quality ${pnlQuality.status}；funding 已记录 ${fundingCount} 笔。`
              : 'history 不可用：不展示 PnL/funding 质量。'}{' '}
            管理决策仍可见，但风险门可能 PARTIAL；未测量的项不会被当作零。mark 状态 {mark}{' '}
            单独展示（STALE 不等于 PARTIAL）。
          </p>
        </div>
      )}

      {history && (
        <section className="panel" aria-labelledby="pnl-heading">
          <div className="panel-head">
            <h2 id="pnl-heading">PnL 分解</h2>
            <span className="mono-muted">basis {history.pnl.basis}</span>
          </div>
          <PnlBreakdown position={position} pnl={history.pnl} />
        </section>
      )}

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
              {data.load.linkError ? ` 原因：${data.load.linkError}` : ''}
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
          {pnlQuality && (
            <DataQualityBadge status={pnlQuality.status} reasons={pnlQuality.reasons} />
          )}
        </div>
        <dl className="kv">
          <dt>Mark freshness</dt>
          <dd className="mono">
            {mark} · mark as of {utcLabel(asOf)} UTC
            {position.resolved_mark_source ? ` · ${position.resolved_mark_source}` : ''}
          </dd>
          {history && pnlQuality && (
            <>
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
            </>
          )}
        </dl>
      </section>

      <PositionManagementPanel
        position={position}
        blockedReason={blockedReason}
        acting={management.acting}
        actionError={management.actionError}
        dialogOpen={management.dialogOpen}
        dialogClosedReason={management.dialogClosedReason}
        result={management.result}
        announcement={management.announcement}
        panelRef={management.panelRef}
        onOpenDialog={management.openDialog}
        onCloseDialog={management.closeDialog}
        onConfirm={() => {
          void management.confirmManagement()
        }}
      />
    </section>
  )
}
