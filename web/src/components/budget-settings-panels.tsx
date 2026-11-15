import { createPortal } from 'react-dom'
import type { FormEvent, ReactNode, RefObject } from 'react'

import type {
  BudgetAlert,
  BudgetData,
  BudgetField,
  BudgetPeriod,
  BudgetPeriodRow,
} from '../api/budget'
import {
  BUDGET_FIELD_IDS,
  BUDGET_FIELDS,
  BUDGET_LABELS,
  type BudgetDraft,
  type BudgetErrors,
} from '../hooks/use-budget-settings-form'
import { DataQualityBadge } from './data-quality'

const PERIOD_LABELS: Record<BudgetPeriod, string> = {
  day: '今日',
  week: '本周',
  month: '本月',
}

const USAGE_PERIOD_LABELS: Record<BudgetPeriod, string> = {
  day: 'Today',
  week: 'This week',
  month: 'This month',
}

/** Wrap an English term on the zh-CN page so AT switches language (WCAG 3.1.2). */
function LangTerm({ children }: { children: ReactNode }) {
  return <span lang="en">{children}</span>
}

function thresholdFor(
  thresholds: BudgetData['thresholds'],
  period: BudgetPeriod,
): string {
  return period === 'day'
    ? thresholds.daily_usd
    : period === 'week'
      ? thresholds.weekly_usd
      : thresholds.monthly_usd
}

function money(value: string): string {
  return `$${value}`
}

function percentOf(spend: string, threshold: string): number {
  const spendValue = Number(spend)
  const thresholdValue = Number(threshold)
  if (!Number.isFinite(spendValue) || !Number.isFinite(thresholdValue) || thresholdValue <= 0) {
    return 0
  }
  return Math.round((spendValue / thresholdValue) * 100)
}

export function BudgetLoadStatus({ loading }: { loading: boolean }) {
  return (
    <p
      role="status"
      className="visually-hidden"
      id="settings-budget-load-status"
      data-testid="settings-budget-load-status"
    >
      {loading ? '加载预算与用量…' : ''}
    </p>
  )
}

export function BudgetLoadErrorPanel({
  message,
  onRetry,
}: {
  message: string | undefined
  onRetry: () => void
}) {
  return (
    <div className="panel" role="alert">
      <div className="panel-head">
        <h2>预算设置不可用</h2>
      </div>
      <p className="panel-foot">{message ?? '未知错误'}</p>
      <div className="action-row">
        <button type="button" className="control-btn" onClick={onRetry}>
          重试
        </button>
      </div>
    </div>
  )
}

interface UsageMeterProps {
  period: BudgetPeriod
  row: BudgetPeriodRow
  thresholdUsd: string
}

function UsageMeter({ period, row, thresholdUsd }: UsageMeterProps) {
  const label = `${PERIOD_LABELS[period]}花费`
  const spend = Number(row.spend_usd)
  const threshold = Number(thresholdUsd)
  const safeSpend = Number.isFinite(spend) ? spend : 0
  const safeThreshold = Number.isFinite(threshold) && threshold > 0 ? threshold : 0
  const valueNow = safeThreshold > 0 ? Math.max(0, Math.min(safeSpend, safeThreshold)) : 0
  const percentage =
    safeThreshold > 0 ? Math.max(0, Math.min(100, (safeSpend / safeThreshold) * 100)) : 0
  return (
    <div
      className={`budget-meter budget-meter--${row.budget_state.toLowerCase()}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeThreshold}
      aria-valuenow={valueNow}
      aria-valuetext={`${label}：${money(row.spend_usd)} / ${money(
        thresholdUsd,
      )}；状态 ${row.budget_state}；调用 ${row.call_count} 次；unpriced ${
        row.unpriced_count
      } 次`}
      data-testid={`budget-meter-${period}`}
    >
      <span className="budget-meter-head">
        <span>{label}</span>
        <span className="mono">
          {money(row.spend_usd)} / {money(thresholdUsd)}
        </span>
      </span>
      <span className="budget-meter-track" aria-hidden="true">
        <span className="budget-meter-fill" style={{ width: `${percentage}%` }} />
      </span>
      <span className="hint">
        状态 <strong>{row.budget_state}</strong> · 调用 {row.call_count} 次 ·{' '}
        <LangTerm>unpriced</LangTerm> {row.unpriced_count} 次
      </span>
    </div>
  )
}

interface BudgetUsagePanelProps {
  data: BudgetData | null
  reason?: string
  stale: boolean
  refreshing: boolean
  notice: string
  onRefresh: () => void
  saving?: boolean
  hardError?: boolean
}

function MissingUsage({ reason }: { reason: string }) {
  const rows: { label: string; testId: string }[] = [
    { label: 'Spend', testId: 'budget-usage-spend' },
    { label: 'State', testId: 'budget-usage-state' },
    { label: 'Projected month-end', testId: 'budget-usage-projection' },
    { label: 'Unpriced calls', testId: 'budget-usage-unpriced' },
  ]
  return (
    <div data-testid="budget-usage-missing">
      <dl className="stat-grid">
        {rows.map((row) => (
          <div className="stat" key={row.testId}>
            <dt>
              <LangTerm>{row.label}</LangTerm>
            </dt>
            <dd className="mono" data-testid={row.testId}>
              <LangTerm>N/A</LangTerm>
            </dd>
          </div>
        ))}
      </dl>
      <p className="panel-foot">{reason}</p>
    </div>
  )
}

export function BudgetUsagePanel({
  data,
  reason,
  stale,
  refreshing,
  notice,
  onRefresh,
  saving = false,
  hardError = false,
}: BudgetUsagePanelProps) {
  const degraded = data?.usage_quality === 'UNAVAILABLE'
  if (data === null || data.periods === null) {
    return (
      <section
        className="panel"
        aria-labelledby="budget-usage-heading"
        aria-busy={refreshing}
        data-testid="budget-usage"
      >
        <div className="panel-head">
          <h2 id="budget-usage-heading">用量（只读）</h2>
          <div className="action-row">
            <span className="chip chip--muted">
              <LangTerm>N/A</LangTerm>
            </span>
            {degraded && (
              <span className="chip chip--warn" data-testid="budget-usage-degraded">
                <LangTerm>degraded</LangTerm>
              </span>
            )}
            {!hardError && (
              <button
                type="button"
                className="control-btn"
                onClick={onRefresh}
                disabled={refreshing || saving}
                aria-busy={refreshing}
              >
                {refreshing ? '刷新中…' : '重试用量'}
              </button>
            )}
          </div>
        </div>
        <MissingUsage reason={reason ?? '用量不可用；不会以 0 代替缺失数据。'} />
        <p className="panel-foot">
          {degraded
            ? '阈值仍可编辑；'
            : '阈值不可编辑，直到重新加载成功；'}
          <LangTerm>unpriced</LangTerm> 调用绝不静默归零。
        </p>
      </section>
    )
  }

  const rows: { period: BudgetPeriod; testId: string }[] = [
    { period: 'day', testId: 'budget-today-spend' },
    { period: 'week', testId: 'budget-weekly-spend' },
    { period: 'month', testId: 'budget-monthly-spend' },
  ]
  const periods = data.periods
  const month = periods.month
  return (
    <section
      className="panel"
      aria-labelledby="budget-usage-heading"
      aria-busy={refreshing}
      data-testid="budget-usage"
    >
      <div className="panel-head">
        <h2 id="budget-usage-heading">用量（只读）</h2>
        <div className="action-row">
          {stale && <DataQualityBadge status="STALE" />}
          <button
            type="button"
            className="control-btn"
            onClick={onRefresh}
            disabled={refreshing || saving}
            aria-busy={refreshing}
          >
            {refreshing ? '刷新中…' : '刷新用量'}
          </button>
        </div>
      </div>
      <dl className="stat-grid">
        {rows.map(({ period, testId }) => {
          const row = periods[period]
          const threshold = thresholdFor(data.thresholds, period)
          return (
            <div className="stat" key={period}>
              <dt>
                <LangTerm>{USAGE_PERIOD_LABELS[period]}</LangTerm>
              </dt>
              <dd className="mono">
                <span data-testid={testId}>{money(row.spend_usd)}</span>
                <small className="stat-note">
                  {percentOf(row.spend_usd, threshold)}% of {money(threshold)} ·{' '}
                  <LangTerm>{row.budget_state}</LangTerm>
                  {stale ? (
                    <>
                      {' · '}
                      <LangTerm>STALE</LangTerm>
                    </>
                  ) : null}
                </small>
              </dd>
            </div>
          )
        })}
        <div className="stat">
          <dt>
            <LangTerm>Projected month-end</LangTerm>
          </dt>
          <dd className="mono">
            <span data-testid="budget-projection">
              {data.projected_month_end_usd === null
                ? 'N/A'
                : money(data.projected_month_end_usd)}
            </span>
            {data.projection_basis !== null && data.projected_month_end_usd !== null ? (
              <small className="stat-note">
                {percentOf(data.projected_month_end_usd, data.thresholds.monthly_usd)}% of{' '}
                {money(data.thresholds.monthly_usd)} · elapsed{' '}
                {(data.projection_basis.elapsed_fraction * 100).toFixed(2)}%
                {stale ? (
                  <>
                    {' · '}
                    <LangTerm>STALE</LangTerm>
                  </>
                ) : null}
              </small>
            ) : null}
          </dd>
        </div>
        <div className="stat">
          <dt>
            <LangTerm>Unpriced calls</LangTerm> (month)
          </dt>
          <dd className="mono">
            <span data-testid="budget-unpriced-count">{month.unpriced_count}</span>
            <small className="stat-note">价格未知的调用单独计数，绝不静默归零</small>
          </dd>
        </div>
      </dl>
      <div className="budget-meters">
        {rows.map(({ period }) => (
          <UsageMeter
            key={period}
            period={period}
            row={periods[period]}
            thresholdUsd={thresholdFor(data.thresholds, period)}
          />
        ))}
      </div>
      {month.unpriced_count > 0 && (
        <p className="status-message status-message--warn" role="note" data-testid="budget-unpriced-banner">
          <span aria-hidden="true">⚠</span> 存在 {month.unpriced_count} 次 <LangTerm>unpriced</LangTerm>{' '}
          调用：模型价格未知，预算总额可能低估；<LangTerm>unpriced</LangTerm> 调用绝不静默归零。
        </p>
      )}
      {notice !== '' && (
        <p className="status-message" role="status" data-testid="budget-refresh-notice">
          {notice}
        </p>
      )}
      <p className="panel-foot">
        阈值只在达到 warn {data.thresholds.warn_pct}% 与 100% 时本地告警；绝不停止或阻断调用；不会通过
        Telegram 投递。成本按 token 与模型价格记账。
      </p>
    </section>
  )
}

export function BudgetLastAlertPanel({
  alert,
  degraded = false,
}: {
  alert: BudgetAlert | null
  degraded?: boolean
}) {
  return (
    <section className="panel" aria-labelledby="budget-alert-heading">
      <div className="panel-head">
        <h2 id="budget-alert-heading">最近一次阈值告警</h2>
        <span className="chip chip--muted">
          <LangTerm>alert only</LangTerm>
        </span>
      </div>
      {degraded ? (
        <p className="panel-foot" data-testid="budget-last-alert">
          用量读取失败（<LangTerm>degraded</LangTerm>），最近告警暂不可读；阈值仍可编辑。
        </p>
      ) : alert === null ? (
        <p className="panel-foot" data-testid="budget-last-alert">
          尚无阈值告警（尚未跨过 warn 或 100%）。告警不会停止调用。
        </p>
      ) : (
        <div className="panel-foot" data-testid="budget-last-alert">
          <p className="mono">
            {alert.occurred_at} · {alert.period} · <LangTerm>{alert.state}</LangTerm> ·{' '}
            {money(alert.spend_usd)} / {money(alert.threshold_usd)} · {alert.provider}{' '}
            {alert.model}
          </p>
          <p className="hint">
            该告警仅为本地审计记录；阈值达到 warn 或 100% 时不会停止、阻断或拒绝任何调用。
          </p>
        </div>
      )}
    </section>
  )
}

interface BudgetThresholdsPanelProps {
  draft: BudgetDraft
  errors: BudgetErrors
  warnPct: number
  readOnly: boolean
  saving: boolean
  onChange: (field: BudgetField, value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  saveButtonRef: RefObject<HTMLButtonElement | null>
}

export function BudgetThresholdsPanel({
  draft,
  errors,
  warnPct,
  readOnly,
  saving,
  onChange,
  onSubmit,
  saveButtonRef,
}: BudgetThresholdsPanelProps) {
  return (
    <section className="panel" aria-labelledby="budget-thresholds-heading">
      <div className="panel-head">
        <h2 id="budget-thresholds-heading">阈值</h2>
        <span className="chip chip--muted">
          <LangTerm>alert only · no hard cap</LangTerm>
        </span>
      </div>
      <form
        className="budget-form"
        aria-label="修改预算阈值"
        aria-busy={saving}
        onSubmit={onSubmit}
      >
        <div className="budget-threshold-grid">
          {BUDGET_FIELDS.map((field) => {
            const id = BUDGET_FIELD_IDS[field]
            const error = errors[field]
            const hint =
              field === 'warn_pct'
                ? '1–100；达到该百分比时告警'
                : `warn ${warnPct}% · alert 100%`
            return (
              <div className="field" key={field}>
                <label className="field-label" htmlFor={id}>
                  <LangTerm>{BUDGET_LABELS[field]}</LangTerm>
                </label>
                <input
                  id={id}
                  className="settings-input"
                  type="text"
                  inputMode="decimal"
                  value={draft[field]}
                  disabled={readOnly}
                  aria-invalid={error !== undefined ? 'true' : undefined}
                  aria-describedby={error ? `${id}-hint ${id}-error` : `${id}-hint`}
                  onChange={(event) => onChange(field, event.target.value)}
                />
                <p className="hint" id={`${id}-hint`}>
                  {hint}
                </p>
                {error !== undefined && (
                  <p className="field-error" id={`${id}-error`}>
                    {error}
                  </p>
                )}
              </div>
            )
          })}
        </div>
        <div className="settings-actions">
          <button
            ref={saveButtonRef}
            type="submit"
            className="btn primary"
            disabled={readOnly || saving}
          >
            {saving ? '保存中…' : '保存阈值'}
          </button>
          <span className="hint">
            仅在实际变更时写入一条 BUDGET_THRESHOLDS_UPDATED <LangTerm>audit</LangTerm>
            ；无硬性上限，绝不停止调用。
          </span>
        </div>
      </form>
    </section>
  )
}

/** Polite refresh announcements; kept outside the panel's ``aria-busy`` subtree. */
export function BudgetUsageStatus({ message }: { message: string }) {
  return (
    <p
      role="status"
      className="visually-hidden"
      data-testid="budget-usage-status"
    >
      {message}
    </p>
  )
}

/** The single polite save announcer, portalled outside the app shell. */
export function BudgetSaveStatus({ message }: { message: string }) {
  if (typeof document === 'undefined') {
    return null
  }
  return createPortal(
    <p
      role="status"
      className="visually-hidden"
      id="settings-budget-save-status"
      data-testid="settings-budget-save-status"
    >
      {message}
    </p>,
    document.body,
  )
}

export function BudgetErrorSummary({
  errorText,
  invalidFields,
  summaryRef,
}: {
  errorText: string
  invalidFields: readonly BudgetField[]
  summaryRef: RefObject<HTMLDivElement | null>
}) {
  if (errorText === '') {
    return null
  }
  return (
    <div
      className="status-message status-message--error"
      role="alert"
      tabIndex={-1}
      ref={summaryRef}
      data-testid="settings-budget-error"
    >
      <span aria-hidden="true">⚠</span> 错误：{errorText}
      {invalidFields.map((field) => (
        <span key={field}>
          {' '}
          <a
            href={`#${BUDGET_FIELD_IDS[field]}`}
            aria-label={`转到${BUDGET_LABELS[field]}`}
          >
            转到<LangTerm>{BUDGET_LABELS[field]}</LangTerm>
          </a>
        </span>
      ))}
    </div>
  )
}

export function BudgetSaveVisible({ message }: { message: string }) {
  if (message === '') {
    return null
  }
  return (
    <p className="status-message">
      <span data-testid="settings-budget-save-visible">{message}</span>
    </p>
  )
}

export function BudgetUnknownOutcomeHint() {
  return (
    <p className="hint">
      上一次保存结果未知；重试会使用同一 Idempotency-Key 重放，不会重复写入。
    </p>
  )
}
