import { useCallback, useEffect, useRef, useState } from 'react'

import { errorMessage } from '../api/errors'
import { isMoneyValue } from '../api/budget'
import type {
  BudgetEnvelope,
  BudgetThresholds,
  BudgetUpdateEnvelope,
  BudgetUpdatePayload,
} from '../api/budget'
import type { ScanSettingsData, ScanSettingsEnvelope } from '../api/settings'
import { SettingsApiError } from '../api/settings'
import type { StarEnvelope, WatchlistEnvelope, WatchlistRow } from '../api/watchlist'
import { markNavFocus } from '../components/nav'
import { SourceChip } from '../components/settings-field'
import { settleOnboardingMutation } from './onboarding-idempotency'
import { useOnboardingLoad } from './use-onboarding-load'

/** A named, disclosed row cap; the server page may itself be a bounded subset. */
const VISIBLE_ROW_CAP = 12

type BudgetFieldName = 'daily_usd' | 'weekly_usd' | 'monthly_usd'
type ErrorField = 'watchlist' | 'period' | BudgetFieldName

const FIELD_LABELS: Record<ErrorField, string> = {
  watchlist: '首批标的',
  period: '扫描周期',
  daily_usd: '日预算',
  weekly_usd: '周预算',
  monthly_usd: '月预算',
}

const FIELD_TARGETS: Record<ErrorField, string> = {
  watchlist: 'onboarding-watchlist',
  period: 'onboarding-period-group',
  daily_usd: 'onboarding-budget-daily',
  weekly_usd: 'onboarding-budget-weekly',
  monthly_usd: 'onboarding-budget-monthly',
}

const BUDGET_FIELDS: readonly [BudgetFieldName, string, string][] = [
  ['daily_usd', '日预算（USD）', 'onboarding-budget-daily'],
  ['weekly_usd', '周预算（USD）', 'onboarding-budget-weekly'],
  ['monthly_usd', '月预算（USD）', 'onboarding-budget-monthly'],
]

interface FieldIssue {
  field: ErrorField
  message: string
}

interface WatchlistLoaded {
  rows: WatchlistRow[]
  total: number
  truncated: boolean
  dataQuality: string | null
  scan: ScanSettingsData
  budget: BudgetThresholds
}

export interface WatchlistStepProps {
  fetchWatchlistFn: (signal?: AbortSignal) => Promise<WatchlistEnvelope>
  setStarFn: (symbol: string, starred: boolean, signal?: AbortSignal) => Promise<StarEnvelope>
  fetchScanSettingsFn: (signal?: AbortSignal) => Promise<ScanSettingsEnvelope>
  updateScanPeriodFn: (
    period: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<ScanSettingsEnvelope>
  fetchBudgetFn: (signal?: AbortSignal) => Promise<BudgetEnvelope>
  updateBudgetThresholdsFn: (
    payload: BudgetUpdatePayload,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<BudgetUpdateEnvelope>
  idempotencyKeyFn: (label: string) => string
  clearIdempotencyKeyFn: (label: string) => void
  rotateIdempotencyKeyFn: (label: string) => string
  announce: (message: string) => void
  onFinish: () => Promise<boolean>
  finishing: boolean
  narrow: boolean
  /** The server says every active step is complete: render the review. */
  allComplete: boolean
}

function isErrorField(field: string): field is ErrorField {
  return field in FIELD_LABELS
}

function focusField(field: ErrorField): void {
  if (field === 'period') {
    document
      .getElementById('onboarding-period-group')
      ?.querySelector<HTMLInputElement>('input')
      ?.focus()
    return
  }
  document.getElementById(FIELD_TARGETS[field])?.focus()
}

function budgetDraftFrom(thresholds: BudgetThresholds): Record<BudgetFieldName, string> {
  return {
    daily_usd: thresholds.daily_usd,
    weekly_usd: thresholds.weekly_usd,
    monthly_usd: thresholds.monthly_usd,
  }
}

const EMPTY_BUDGET_DRAFT: Record<BudgetFieldName, string> = {
  daily_usd: '',
  weekly_usd: '',
  monthly_usd: '',
}

/**
 * Step 5: watchlist, scan period, budget, and the review.
 *
 * Reuses the real watchlist/scan/budget clients, persists only changed values,
 * discloses truncation and data quality, and focuses the review heading when the
 * wizard finishes. A background star failure is announced inline without
 * stealing focus; submit/validation failures focus the field-scoped summary.
 */
export function WatchlistStep({
  fetchWatchlistFn,
  setStarFn,
  fetchScanSettingsFn,
  updateScanPeriodFn,
  fetchBudgetFn,
  updateBudgetThresholdsFn,
  idempotencyKeyFn,
  clearIdempotencyKeyFn,
  rotateIdempotencyKeyFn,
  announce,
  onFinish,
  finishing,
  narrow,
  allComplete,
}: WatchlistStepProps) {
  const loader = useCallback(
    async (signal: AbortSignal): Promise<WatchlistLoaded> => {
      const [watchlist, scanEnvelopeValue, budgetEnvelopeValue] = await Promise.all([
        fetchWatchlistFn(signal),
        fetchScanSettingsFn(signal),
        fetchBudgetFn(signal),
      ])
      if (!watchlist.data || !scanEnvelopeValue.data || !budgetEnvelopeValue.data) {
        throw new Error('引导数据响应为空。')
      }
      return {
        rows: watchlist.data.rows,
        total: watchlist.data.total,
        truncated: watchlist.data.truncated,
        dataQuality: watchlist.meta.data_quality,
        scan: scanEnvelopeValue.data,
        budget: budgetEnvelopeValue.data.thresholds,
      }
    },
    [fetchWatchlistFn, fetchScanSettingsFn, fetchBudgetFn],
  )

  const load = useOnboardingLoad(loader)
  const { state } = load

  // Server data stays the render source; only the user's in-progress edits and
  // successful mutations are layered on top. Seeding these in a passive effect
  // would paint one frame of empty rows/budgets before the real data arrived.
  const [starOverrides, setStarOverrides] = useState<
    Record<string, { starred: boolean; starred_at: string | null }>
  >({})
  const [scanOverride, setScanOverride] = useState<ScanSettingsData | null>(null)
  const [budgetOverride, setBudgetOverride] = useState<BudgetThresholds | null>(null)
  const [periodEdit, setPeriodEdit] = useState<string | null>(null)
  const [budgetEdits, setBudgetEdits] = useState<Partial<Record<BudgetFieldName, string>>>({})
  const [pendingStars, setPendingStars] = useState<ReadonlySet<string>>(new Set())
  const [savingSettings, setSavingSettings] = useState(false)
  const [issues, setIssues] = useState<FieldIssue[]>([])
  const [summaryLead, setSummaryLead] = useState('无法完成引导')
  const [summaryMessage, setSummaryMessage] = useState('')
  const [summaryNonce, setSummaryNonce] = useState(0)
  const [starError, setStarError] = useState<string | null>(null)
  const [finished, setFinished] = useState(false)
  const summaryRef = useRef<HTMLDivElement>(null)
  const reviewRef = useRef<HTMLHeadingElement>(null)

  const data = load.data
  const rows = (data?.rows ?? []).map((row) => {
    const override = starOverrides[row.symbol]
    return override ? { ...row, ...override } : row
  })
  const total = data?.total ?? 0
  const truncated = data?.truncated ?? false
  const dataQuality = data?.dataQuality ?? null
  const scan = scanOverride ?? data?.scan ?? null
  const budget = budgetOverride ?? data?.budget ?? null
  const periodDraft = periodEdit ?? scan?.value ?? ''
  const budgetDraft = {
    ...(budget ? budgetDraftFrom(budget) : EMPTY_BUDGET_DRAFT),
    ...budgetEdits,
  }

  useEffect(() => {
    if (state === 'ready') {
      announce('Watchlist、周期与预算已加载。')
    } else if (state === 'error') {
      announce('引导数据不可用。')
    }
  }, [state, announce])

  // A completed wizard (immediate finish or an all-complete reload) renders a
  // read-only review: no editable control may imply a save path that is gone.
  const reviewLocked = finished || allComplete
  useEffect(() => {
    if (finished) {
      reviewRef.current?.focus()
    }
  }, [finished])

  // A submit/validation failure (including a repeated identical one) moves focus
  // to the summary. Background star failures never increment the nonce.
  useEffect(() => {
    if (summaryNonce > 0) {
      summaryRef.current?.focus()
    }
  }, [summaryNonce])

  const starredCount = rows.filter((row) => row.starred).length
  const visibleRows = rows.slice(0, VISIBLE_ROW_CAP)
  const rowsDisclosed = rows.length > VISIBLE_ROW_CAP || truncated || total > rows.length
  const periodInvalid = issues.some((issue) => issue.field === 'period')
  const busy = finishing || savingSettings

  function issueFor(field: ErrorField): string | null {
    return issues.find((issue) => issue.field === field)?.message ?? null
  }

  function clearIssue(field: ErrorField): void {
    setIssues((current) => current.filter((issue) => issue.field !== field))
  }

  async function toggleStar(row: WatchlistRow): Promise<void> {
    if (narrow || reviewLocked || pendingStars.has(row.symbol)) {
      return
    }
    setPendingStars((current) => new Set(current).add(row.symbol))
    try {
      const envelope = await setStarFn(row.symbol, !row.starred)
      const result = envelope.data
      if (!result) {
        throw new Error('星标响应为空。')
      }
      if (result.symbol !== row.symbol) {
        throw new Error('星标响应与请求的标的并不一致；结果未知。')
      }
      setStarOverrides((current) => ({
        ...current,
        [row.symbol]: { starred: result.starred, starred_at: result.starred_at },
      }))
      // Successful recovery clears the star error and the star-validation issue.
      setStarError(null)
      clearIssue('watchlist')
      setSummaryMessage('')
      announce(`${row.symbol} ${result.starred ? '已加入' : '已移出'}下次扫描。`)
    } catch (cause: unknown) {
      // Announced inline and politely; never steal focus from the symbol list.
      setStarError(`无法更新 ${row.symbol}：${errorMessage(cause)}`)
    } finally {
      setPendingStars((current) => {
        const next = new Set(current)
        next.delete(row.symbol)
        return next
      })
    }
  }

  function validateDraft(): FieldIssue[] {
    const found: FieldIssue[] = []
    if (starredCount < 1) {
      found.push({ field: 'watchlist', message: '至少星标 1 个标的，下一次扫描才会运行。' })
    }
    for (const [field, label] of BUDGET_FIELDS) {
      if (!isMoneyValue(budgetDraft[field].trim())) {
        found.push({
          field,
          message: `${label.replace('（USD）', '')}必须是非负十进制金额（例如 10.00）。`,
        })
      }
    }
    return found
  }

  function serverIssues(cause: unknown): FieldIssue[] {
    if (cause instanceof SettingsApiError) {
      return cause.details
        .filter(
          (detail): detail is { field: ErrorField; message?: string } =>
            typeof detail.field === 'string' && isErrorField(detail.field),
        )
        .map((detail) => ({
          field: detail.field,
          message: detail.message ?? cause.message,
        }))
    }
    return []
  }

  async function finish(): Promise<void> {
    if (narrow || reviewLocked || savingSettings || finishing || state !== 'ready') {
      return
    }
    const found = validateDraft()
    if (found.length > 0) {
      setIssues(found)
      setSummaryLead('无法完成引导')
      setSummaryMessage('请修正标出的字段。')
      setSummaryNonce((value) => value + 1)
      return
    }
    setIssues([])
    setSummaryMessage('')
    setStarError(null)
    setSavingSettings(true)
    let label = 'scan.period'
    try {
      if (scan && periodDraft !== scan.value) {
        const envelope = await updateScanPeriodFn(periodDraft, idempotencyKeyFn(label))
        if (!envelope.data) {
          throw new Error('扫描周期响应为空。')
        }
        clearIdempotencyKeyFn(label)
        setScanOverride(envelope.data)
        setPeriodEdit(null)
      }
      label = 'budget.thresholds'
      if (budget) {
        const payload: BudgetUpdatePayload = {}
        for (const [field] of BUDGET_FIELDS) {
          if (budgetDraft[field] !== budget[field]) {
            payload[field] = budgetDraft[field]
          }
        }
        if (Object.keys(payload).length > 0) {
          const envelope = await updateBudgetThresholdsFn(payload, idempotencyKeyFn(label))
          if (!envelope.data) {
            throw new Error('预算响应为空。')
          }
          clearIdempotencyKeyFn(label)
          setBudgetOverride(envelope.data.thresholds)
          setBudgetEdits({})
        }
      }
      const ok = await onFinish()
      if (ok) {
        setFinished(true)
        announce('引导已完成；当前为 Paper。')
      }
    } catch (cause: unknown) {
      const failure = settleOnboardingMutation(cause, label, {
        rotate: rotateIdempotencyKeyFn,
        clear: clearIdempotencyKeyFn,
      })
      const foundIssues = serverIssues(cause)
      setIssues(foundIssues)
      setSummaryLead(failure.unknown ? '保存结果未知' : '保存被拒绝')
      setSummaryMessage(failure.message)
      setSummaryNonce((value) => value + 1)
    } finally {
      setSavingSettings(false)
    }
  }

  const narrowDisabled = narrow || busy

  return (
    <div className="stack">
      {state === 'loading' && (
        <p className="status-message" role="status">
          加载引导数据…
        </p>
      )}
      {state === 'error' && (
        <>
          <p className="status-message status-message--error" role="alert">
            引导数据不可用：{load.message ?? '未知错误'}
          </p>
          <div className="settings-actions">
            <button type="button" className="control-btn" onClick={load.retry}>
              重试
            </button>
          </div>
        </>
      )}

      {(issues.length > 0 || summaryMessage !== '') && (
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
                      focusField(issue.field)
                    }}
                  >
                    {FIELD_LABELS[issue.field]}：{issue.message}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {starError !== null && (
        <p
          className="status-message status-message--error"
          role="status"
          data-testid="onboarding-star-error"
        >
          {starError}
        </p>
      )}

      {state === 'ready' && (
        <form
          className="stack"
          aria-label="Watchlist、周期与预算"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (!reviewLocked) {
              void finish()
            }
          }}
        >
          <section className="panel" aria-labelledby="onboarding-watchlist-heading">
            <div className="panel-head">
              <h2 id="onboarding-watchlist-heading">首批标的</h2>
            </div>
            <p className="status-message" data-testid="watchlist-source">
              source: symbol registry（服务器返回） · data_quality: {dataQuality ?? '—'}
            </p>
            <p
              className={
                starredCount > 0 ? 'status-message' : 'status-message status-message--error'
              }
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
                      disabled={narrow || reviewLocked}
                      aria-disabled={pending || undefined}
                      aria-busy={pending || undefined}
                      aria-pressed={row.starred}
                      onClick={() => void toggleStar(row)}
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

          <section className="panel" aria-labelledby="onboarding-schedule-heading">
            <div className="panel-head">
              <h2 id="onboarding-schedule-heading">扫描周期</h2>
              {scan && <SourceChip source={scan.source} testId="scan-source" />}
            </div>
            <p className="status-message">
              当前：<strong data-testid="scan-effective">{scan?.value ?? '—'}</strong> · 下一个 UTC
              边界{' '}
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
                    disabled={narrowDisabled || reviewLocked}
                    onChange={() => {
                      setPeriodEdit(period)
                      clearIssue('period')
                    }}
                  />{' '}
                  <span className="mono">{period}</span>
                </label>
              ))}
            </fieldset>
          </section>

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
                      disabled={narrowDisabled || reviewLocked}
                      aria-invalid={invalid ? true : undefined}
                      aria-describedby={
                        invalid ? `${id}-error onboarding-finish-error` : `${id}-hint`
                      }
                      onChange={(event) => {
                        setBudgetEdits((current) => ({
                          ...current,
                          [field]: event.target.value,
                        }))
                        clearIssue(field)
                      }}
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

          <section className="panel" aria-labelledby="onboarding-env-heading">
            <div className="panel-head">
              <h2 id="onboarding-env-heading">环境</h2>
            </div>
            <p className="status-message">
              <span className="chip chip--ok">PAPER</span> Live 未启用；引导不提供 Live 选项。
            </p>
          </section>

          {!reviewLocked && (
            <div className="settings-actions">
              <button type="submit" className="control-btn" disabled={narrowDisabled}>
                {busy ? '正在完成…' : '完成引导并查看首次扫描'}
              </button>
              <span className="hint">
                完成后下一次扫描运行；等同 CLI <code>oft scan run</code>。
              </span>
            </div>
          )}

          {reviewLocked && scan && budget && (
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
          )}
        </form>
      )}
    </div>
  )
}
