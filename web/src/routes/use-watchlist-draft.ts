import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import type { RefObject } from 'react'

import type {
  BudgetEnvelope,
  BudgetThresholds,
  BudgetUpdateEnvelope,
  BudgetUpdatePayload,
} from '../api/budget'
import { isMoneyValue } from '../api/budget'
import { errorMessage } from '../api/errors'
import type { OnboardingProgressData } from '../api/onboarding'
import type { ScanSettingsData, ScanSettingsEnvelope } from '../api/settings'
import { SettingsApiError } from '../api/settings'
import type { StarEnvelope, WatchlistEnvelope, WatchlistRow } from '../api/watchlist'
import { settleOnboardingMutation } from './onboarding-idempotency'
import { useOnboardingLoad } from './use-onboarding-load'

/** A named, disclosed row cap; the server page may itself be a bounded subset. */
export const VISIBLE_ROW_CAP = 12

export type BudgetFieldName = 'daily_usd' | 'weekly_usd' | 'monthly_usd'
export type ErrorField = 'watchlist' | 'period' | BudgetFieldName

export const FIELD_LABELS: Record<ErrorField, string> = {
  watchlist: '首批标的',
  period: '扫描周期',
  daily_usd: '日预算',
  weekly_usd: '周预算',
  monthly_usd: '月预算',
}

export const FIELD_TARGETS: Record<ErrorField, string> = {
  watchlist: 'onboarding-watchlist',
  period: 'onboarding-period-group',
  daily_usd: 'onboarding-budget-daily',
  weekly_usd: 'onboarding-budget-weekly',
  monthly_usd: 'onboarding-budget-monthly',
}

export const BUDGET_FIELDS: readonly [BudgetFieldName, string, string][] = [
  ['daily_usd', '日预算（USD）', 'onboarding-budget-daily'],
  ['weekly_usd', '周预算（USD）', 'onboarding-budget-weekly'],
  ['monthly_usd', '月预算（USD）', 'onboarding-budget-monthly'],
]

export interface FieldIssue {
  field: ErrorField
  message: string
}

export interface StarOverride {
  starred: boolean
  starred_at: string | null
}

interface IssuesState {
  issues: FieldIssue[]
  summaryLead: string
  summaryMessage: string
}

const INITIAL_ISSUES_STATE: IssuesState = {
  issues: [],
  summaryLead: '无法完成引导',
  summaryMessage: '',
}

type IssuesAction =
  | { type: 'setIssues'; issues: FieldIssue[]; lead: string; message: string }
  | { type: 'clearIssue'; field: ErrorField }

/**
 * Issues and the assertive summary are one atomic state.
 *
 * An async star continuation must filter the issues that exist *now*, never a
 * render-time snapshot, so a concurrent submit's errors cannot be wiped. The
 * summary is dropped only when no issue remains; a still-flagged field keeps
 * the summary it explains.
 */
function issuesReducer(state: IssuesState, action: IssuesAction): IssuesState {
  if (action.type === 'setIssues') {
    return {
      issues: action.issues,
      summaryLead: action.lead,
      summaryMessage: action.message,
    }
  }
  const issues = state.issues.filter((issue) => issue.field !== action.field)
  if (issues.length === 0) {
    return { issues, summaryLead: '无法完成引导', summaryMessage: '' }
  }
  return { ...state, issues }
}

interface WatchlistLoaded {
  rows: WatchlistRow[]
  total: number
  truncated: boolean
  dataQuality: string | null
  /** The server's authoritative starred total (``next_scan_count``). */
  starredTotal: number
  scan: ScanSettingsData
  budget: BudgetThresholds
}

export function isErrorField(field: string): field is ErrorField {
  return field in FIELD_LABELS
}

export function focusField(field: ErrorField): void {
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
 * The server's starred total adjusted by local deltas only.
 *
 * ``total`` covers the whole registry (a starred symbol may sit outside the
 * bounded page), so it is the base truth. Each successful local toggle adds
 * +/-1 relative to the original row's server state; the result never goes
 * below zero.
 */
export function adjustStarredCount(
  total: number,
  rows: readonly WatchlistRow[],
  overrides: Readonly<Record<string, StarOverride>>,
): number {
  let delta = 0
  for (const row of rows) {
    const override = overrides[row.symbol]
    if (override === undefined) {
      continue
    }
    delta += (override.starred ? 1 : 0) - (row.starred ? 1 : 0)
  }
  return Math.max(0, total + delta)
}

export interface WatchlistDraftDeps {
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
  onFinish: () => Promise<OnboardingProgressData | null>
  finishing: boolean
  narrow: boolean
  allComplete: boolean
}

export interface WatchlistDraft {
  state: 'loading' | 'ready' | 'error'
  loadMessage: string | null
  retry: () => void
  rows: WatchlistRow[]
  visibleRows: WatchlistRow[]
  total: number
  truncated: boolean
  dataQuality: string | null
  scan: ScanSettingsData | null
  budget: BudgetThresholds | null
  periodDraft: string
  budgetDraft: Record<BudgetFieldName, string>
  starredCount: number
  rowsDisclosed: boolean
  periodInvalid: boolean
  issues: FieldIssue[]
  summaryLead: string
  summaryMessage: string
  summaryNonce: number
  starError: string | null
  pendingStars: ReadonlySet<string>
  busy: boolean
  narrowDisabled: boolean
  reviewLocked: boolean
  savedResumedStep: number | null
  summaryRef: RefObject<HTMLDivElement | null>
  reviewRef: RefObject<HTMLHeadingElement | null>
  issueFor: (field: ErrorField) => string | null
  clearIssue: (field: ErrorField) => void
  toggleStar: (row: WatchlistRow) => Promise<void>
  finish: () => Promise<void>
  changePeriod: (period: string) => void
  changeBudget: (field: BudgetFieldName, value: string) => void
}

/**
 * Step 5's draft, mutation, and completion state.
 *
 * Server data stays the render source; only the user's in-progress edits and
 * successful mutations are layered on top. Starred counts come from the
 * server's registry-wide total, not the bounded page.
 */
export function useWatchlistDraft(deps: WatchlistDraftDeps): WatchlistDraft {
  const {
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
  } = deps

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
        starredTotal: watchlist.data.next_scan_count,
        scan: scanEnvelopeValue.data,
        budget: budgetEnvelopeValue.data.thresholds,
      }
    },
    [fetchWatchlistFn, fetchScanSettingsFn, fetchBudgetFn],
  )

  const load = useOnboardingLoad(loader)
  const { state } = load
  const data = load.data

  const [starOverrides, setStarOverrides] = useState<Record<string, StarOverride>>({})
  const [scanOverride, setScanOverride] = useState<ScanSettingsData | null>(null)
  const [budgetOverride, setBudgetOverride] = useState<BudgetThresholds | null>(null)
  const [periodEdit, setPeriodEdit] = useState<string | null>(null)
  const [budgetEdits, setBudgetEdits] = useState<Partial<Record<BudgetFieldName, string>>>({})
  const [pendingStars, setPendingStars] = useState<ReadonlySet<string>>(new Set())
  const [savingSettings, setSavingSettings] = useState(false)
  const [issuesState, dispatchIssues] = useReducer(issuesReducer, INITIAL_ISSUES_STATE)
  const { issues, summaryLead, summaryMessage } = issuesState
  const [summaryNonce, setSummaryNonce] = useState(0)
  const [starError, setStarError] = useState<string | null>(null)
  const [justFinished, setJustFinished] = useState(false)
  const [savedResumedStep, setSavedResumedStep] = useState<number | null>(null)
  const summaryRef = useRef<HTMLDivElement>(null)
  const reviewRef = useRef<HTMLHeadingElement>(null)

  // A refetch replaces the local deltas: the fresh server total already
  // includes every successful toggle, so keeping overrides would double-count.
  useEffect(() => {
    setStarOverrides({})
  }, [data])

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
  const starredCount = adjustStarredCount(data?.starredTotal ?? 0, data?.rows ?? [], starOverrides)
  const visibleRows = rows.slice(0, VISIBLE_ROW_CAP)
  const rowsDisclosed = rows.length > VISIBLE_ROW_CAP || truncated || total > rows.length
  const periodInvalid = issues.some((issue) => issue.field === 'period')
  const busy = finishing || savingSettings
  const narrowDisabled = narrow || busy
  // The review is the server's claim: only `all_complete` locks editing.
  const reviewLocked = allComplete

  useEffect(() => {
    if (state === 'ready') {
      announce('Watchlist、周期与预算已加载。')
    } else if (state === 'error') {
      announce('引导数据不可用。')
    }
  }, [state, announce])

  // Focus the review only when this finish produced it, not on an all-complete
  // reload (the step heading keeps focus then).
  useEffect(() => {
    if (justFinished && reviewLocked) {
      reviewRef.current?.focus()
    }
  }, [justFinished, reviewLocked])

  // A submit/validation failure (including a repeated identical one) moves focus
  // to the summary. Background star failures never increment the nonce.
  useEffect(() => {
    if (summaryNonce > 0) {
      summaryRef.current?.focus()
    }
  }, [summaryNonce])

  function issueFor(field: ErrorField): string | null {
    return issues.find((issue) => issue.field === field)?.message ?? null
  }

  function clearIssue(field: ErrorField): void {
    // Reducer transition, not a render-time snapshot: a corrected last field
    // also drops the stale assertive summary, while other issues keep it.
    dispatchIssues({ type: 'clearIssue', field })
  }

  async function toggleStar(row: WatchlistRow): Promise<void> {
    if (narrow || reviewLocked || busy || pendingStars.has(row.symbol)) {
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
      dispatchIssues({
        type: 'setIssues',
        issues: found,
        lead: '无法完成引导',
        message: '请修正标出的字段。',
      })
      setSummaryNonce((value) => value + 1)
      return
    }
    dispatchIssues({ type: 'setIssues', issues: [], lead: '无法完成引导', message: '' })
    setStarError(null)
    setSavedResumedStep(null)
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
      // The server decides whether the wizard is actually complete; a step-5
      // save with earlier steps open must not render the wizard-complete review.
      const result = await onFinish()
      if (result?.all_complete) {
        setJustFinished(true)
        announce('引导已完成；当前为 Paper。')
      } else if (result) {
        setSavedResumedStep(result.resumed_step)
        announce('步骤 5 已保存；继续完成剩余步骤。')
      }
    } catch (cause: unknown) {
      const failure = settleOnboardingMutation(cause, label, {
        rotate: rotateIdempotencyKeyFn,
        clear: clearIdempotencyKeyFn,
      })
      const foundIssues = serverIssues(cause)
      dispatchIssues({
        type: 'setIssues',
        issues: foundIssues,
        lead: failure.unknown ? '保存结果未知' : '保存被拒绝',
        message: failure.message,
      })
      setSummaryNonce((value) => value + 1)
    } finally {
      setSavingSettings(false)
    }
  }

  function changePeriod(period: string): void {
    setPeriodEdit(period)
    clearIssue('period')
  }

  function changeBudget(field: BudgetFieldName, value: string): void {
    setBudgetEdits((current) => ({ ...current, [field]: value }))
    clearIssue(field)
  }

  return {
    state,
    loadMessage: load.message,
    retry: load.retry,
    rows,
    visibleRows,
    total,
    truncated,
    dataQuality,
    scan,
    budget,
    periodDraft,
    budgetDraft,
    starredCount,
    rowsDisclosed,
    periodInvalid,
    issues,
    summaryLead,
    summaryMessage,
    summaryNonce,
    starError,
    pendingStars,
    busy,
    narrowDisabled,
    reviewLocked,
    savedResumedStep,
    summaryRef,
    reviewRef,
    issueFor,
    clearIssue,
    toggleStar,
    finish,
    changePeriod,
    changeBudget,
  }
}
