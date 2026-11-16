import type { RefObject } from 'react'

import type { BudgetThresholds } from '../api/budget'
import type { ScanSettingsData } from '../api/settings'
import type { WatchlistRow } from '../api/watchlist'
import { markNavFocus } from '../components/nav'
import { SourceChip } from '../components/settings-field'
import {
  BUDGET_FIELDS,
  FIELD_LABELS,
  FIELD_TARGETS,
} from './use-watchlist-draft'
import type { BudgetFieldName, ErrorField, FieldIssue } from './use-watchlist-draft'

// -- error summary ----------------------------------------------------------

interface IssueSummaryProps {
  issues: FieldIssue[]
  summaryLead: string
  summaryMessage: string
  summaryRef: RefObject<HTMLDivElement | null>
  onFocusField: (field: ErrorField) => void
}

/** The assertive submit/validation summary, with focusable field links. */
export function IssueSummary({
  issues,
  summaryLead,
  summaryMessage,
  summaryRef,
  onFocusField,
}: IssueSummaryProps) {
  return (
    <div
      ref={summaryRef}
      role="alert"
      tabIndex={-1}
      id="onboarding-finish-error"
      data-testid="onboarding-finish-error"
      className="status-message status-message--error"
    >
      <p>
        {summaryLead}：{summaryMessage || '请修正标出的字段。'}
      </p>
      {issues.length > 0 && (
        <ul className="onboarding-error-list">
          {issues.map((issue) => (
            <li key={issue.field}>
              <a
                href={`#${FIELD_TARGETS[issue.field]}`}
                onClick={(event) => {
                  event.preventDefault()
                  onFocusField(issue.field)
                }}
              >
                {FIELD_LABELS[issue.field]}：{issue.message}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// -- star list --------------------------------------------------------------

interface StarListProps {
  visibleRows: WatchlistRow[]
  pendingStars: ReadonlySet<string>
  /** Narrow or busy: the star set must not change mid-finish. */
  disabled: boolean
  reviewLocked: boolean
  dataQuality: string | null
  starredCount: number
  rowsDisclosed: boolean
  total: number
  truncated: boolean
  onToggle: (row: WatchlistRow) => void
}

export function StarList({
  visibleRows,
  pendingStars,
  disabled,
  reviewLocked,
  dataQuality,
  starredCount,
  rowsDisclosed,
  total,
  truncated,
  onToggle,
}: StarListProps) {
  return (
    <section className="panel" aria-labelledby="onboarding-watchlist-heading">
      <div className="panel-head">
        <h2 id="onboarding-watchlist-heading">首批标的</h2>
      </div>
      <p className="status-message" data-testid="watchlist-source">
        source: symbol registry（服务器返回） · data_quality: {dataQuality ?? '—'}
      </p>
      <p
        className={starredCount > 0 ? 'status-message' : 'status-message status-message--error'}
        data-testid="watchlist-validation"
      >
        已星标 {starredCount} 个 · 验证：至少 1 个标的才会运行下一次扫描
      </p>
      {/* Safari/VoiceOver drops list semantics for `list-style: none`, so
          the explicit role is intentional (WCAG 1.3.1). */}
      {/* eslint-disable-next-line jsx-a11y/no-redundant-roles */}
      <ul
        className="stack onboarding-pad onboarding-symbol-list"
        role="list"
        id="onboarding-watchlist"
        tabIndex={-1}
      >
        {visibleRows.map((row) => {
          const pending = pendingStars.has(row.symbol)
          return (
            <li key={row.symbol}>
              <button
                type="button"
                className="control-btn onboarding-star-btn"
                disabled={disabled || reviewLocked}
                aria-disabled={pending || undefined}
                aria-busy={pending || undefined}
                aria-pressed={row.starred}
                onClick={() => onToggle(row)}
              >
                {row.starred ? `取消星标 ${row.symbol}` : `星标 ${row.symbol}`}
              </button>{' '}
              <span className="mono">{row.symbol}</span>{' '}
              <span className="mono-muted">{row.contract_type}</span>
            </li>
          )
        })}
      </ul>
      {rowsDisclosed && (
        <p className="hint" data-testid="watchlist-truncation">
          显示前 {visibleRows.length} 个，共 {total} 个
          {truncated ? '（服务器返回的是有界分页）' : ''}。
          <a href="/watchlist" onClick={markNavFocus}>
            在 /watchlist 查看完整列表
          </a>
        </p>
      )}
    </section>
  )
}

// -- scan period ------------------------------------------------------------

interface SchedulePanelProps {
  scan: ScanSettingsData | null
  periodDraft: string
  periodInvalid: boolean
  disabled: boolean
  reviewLocked: boolean
  onChange: (period: string) => void
}

export function SchedulePanel({
  scan,
  periodDraft,
  periodInvalid,
  disabled,
  reviewLocked,
  onChange,
}: SchedulePanelProps) {
  return (
    <section className="panel" aria-labelledby="onboarding-schedule-heading">
      <div className="panel-head">
        <h2 id="onboarding-schedule-heading">扫描周期</h2>
        {scan && <SourceChip source={scan.source} testId="scan-source" />}
      </div>
      <p className="status-message">
        当前：<strong data-testid="scan-effective">{scan?.value ?? '—'}</strong> · 下一个 UTC 边界{' '}
        <span className="mono" data-testid="scan-next-run">
          {scan?.next_run ?? '—'}
        </span>
      </p>
      <fieldset
        className="settings-radio-group onboarding-pad"
        id="onboarding-period-group"
        aria-invalid={periodInvalid || undefined}
        aria-describedby={periodInvalid ? 'onboarding-finish-error' : undefined}
      >
        <legend>扫描周期</legend>
        {(scan?.supported ?? ['4h', '8h']).map((period) => (
          <label key={period} className="check">
            <input
              type="radio"
              name="onboarding-period"
              value={period}
              checked={periodDraft === period}
              disabled={disabled || reviewLocked}
              onChange={() => onChange(period)}
            />{' '}
            <span className="mono">{period}</span>
          </label>
        ))}
      </fieldset>
    </section>
  )
}

// -- budget ----------------------------------------------------------------

interface BudgetPanelProps {
  budgetDraft: Record<BudgetFieldName, string>
  issueFor: (field: ErrorField) => string | null
  disabled: boolean
  reviewLocked: boolean
  onChange: (field: BudgetFieldName, value: string) => void
}

export function BudgetPanel({
  budgetDraft,
  issueFor,
  disabled,
  reviewLocked,
  onChange,
}: BudgetPanelProps) {
  return (
    <section className="panel" aria-labelledby="onboarding-budget-heading">
      <div className="panel-head">
        <h2 id="onboarding-budget-heading">预算阈值</h2>
      </div>
      <div className="settings-readonly-grid">
        {BUDGET_FIELDS.map(([field, label, id]) => {
          const fieldIssue = issueFor(field)
          const invalid = fieldIssue !== null
          return (
            <div className="field" key={field}>
              <label className="field-label" htmlFor={id}>
                {label}
              </label>
              <input
                id={id}
                className="settings-input num-input"
                type="number"
                min="0"
                step="0.01"
                inputMode="decimal"
                value={budgetDraft[field]}
                disabled={disabled || reviewLocked}
                aria-invalid={invalid ? true : undefined}
                aria-describedby={
                  invalid ? `${id}-error onboarding-finish-error` : `${id}-hint`
                }
                onChange={(event) => onChange(field, event.target.value)}
              />
              <p className="hint" id={`${id}-hint`}>
                仅告警，无硬性上限。
              </p>
              {invalid && (
                <p className="field-error" id={`${id}-error`}>
                  {fieldIssue}
                </p>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

// -- results ---------------------------------------------------------------

interface ReviewPanelProps {
  scan: ScanSettingsData
  budget: BudgetThresholds
  starredCount: number
  reviewRef: RefObject<HTMLHeadingElement | null>
}

/** The wizard-complete review; rendered only when the server says complete. */
export function ReviewPanel({ scan, budget, starredCount, reviewRef }: ReviewPanelProps) {
  return (
    <section
      className="panel"
      data-testid="onboarding-review"
      aria-labelledby="onboarding-review-heading"
    >
      <div className="panel-head">
        <h2 id="onboarding-review-heading" ref={reviewRef} tabIndex={-1}>
          引导完成 · 复盘
        </h2>
        <span className="chip chip--ok">PAPER</span>
      </div>
      <dl className="kv onboarding-pad">
        <dt>环境</dt>
        <dd>Paper · Live 未启用（需 4 周 paper 后手动开启）</dd>
        <dt>标的</dt>
        <dd>{starredCount} 个已星标</dd>
        <dt>扫描周期</dt>
        <dd>
          {scan.value} · source: {scan.source} · next {scan.next_run}
        </dd>
        <dt>预算阈值</dt>
        <dd>
          日 ${budget.daily_usd} · 周 ${budget.weekly_usd} · 月 ${budget.monthly_usd}
        </dd>
      </dl>
      <div className="settings-actions">
        <a className="btn" href="/scans" onClick={markNavFocus}>
          打开 /scans 观察首次扫描
        </a>
        <span className="hint">
          CLI 等价：<code>oft scan run</code>
        </span>
      </div>
    </section>
  )
}

interface Step5SavedPanelProps {
  resumedStep: number
}

/**
 * Step 5 saved while earlier active steps remain open.
 *
 * Deliberately not the wizard-complete review: it must not claim "引导完成".
 */
export function Step5SavedPanel({ resumedStep }: Step5SavedPanelProps) {
  return (
    <section
      className="panel"
      data-testid="onboarding-step5-saved"
      aria-labelledby="onboarding-step5-saved-heading"
    >
      <div className="panel-head">
        <h2 id="onboarding-step5-saved-heading">步骤 5 已保存</h2>
      </div>
      <p className="status-message">步骤 5 已保存；继续完成剩余步骤。</p>
      <div className="settings-actions">
        <a className="btn" href={`/onboarding/${resumedStep}`} onClick={markNavFocus}>
          继续步骤 {resumedStep}
        </a>
      </div>
    </section>
  )
}
