import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, RefObject } from 'react'

import { isAbortError } from '../api/abort'
import {
  type BudgetAlert,
  type BudgetData,
  type BudgetEnvelope,
  type BudgetField,
  type BudgetThresholds,
  type BudgetUpdateData,
  type BudgetUpdateEnvelope,
  type BudgetUpdatePayload,
  budgetSettingsKey,
  clearBudgetSettingsKey,
  fetchBudget,
  rotateBudgetSettingsKey,
  updateBudgetThresholds,
} from '../api/budget'
import { errorMessage } from '../api/errors'
import { SettingsApiError, type SettingsFieldDetail } from '../api/settings'
import { useSettingsReadonly } from '../components/settings-field'

export type BudgetDraft = Record<BudgetField, string>
export type BudgetErrors = Partial<Record<BudgetField, string>>

export const BUDGET_FIELDS: readonly BudgetField[] = [
  'daily_usd',
  'weekly_usd',
  'monthly_usd',
  'warn_pct',
]

export const BUDGET_LABELS: Record<BudgetField, string> = {
  daily_usd: 'Daily USD',
  weekly_usd: 'Weekly USD',
  monthly_usd: 'Monthly USD',
  warn_pct: '警告阈值 %',
}

export const BUDGET_FIELD_IDS: Record<BudgetField, string> = {
  daily_usd: 'budget-daily',
  weekly_usd: 'budget-weekly',
  monthly_usd: 'budget-monthly',
  warn_pct: 'budget-warn',
}

type MoneyField = 'daily_usd' | 'weekly_usd' | 'monthly_usd'

const MONEY_FIELDS: readonly MoneyField[] = ['daily_usd', 'weekly_usd', 'monthly_usd']

/**
 * Editing one field invalidates the ordering errors that involve it: the
 * validator pairs daily/weekly and weekly/monthly, so an error on a neighbour
 * would otherwise stay stale after the operator fixes the draft.
 */
const COUPLED_ERROR_FIELDS: Record<BudgetField, readonly BudgetField[]> = {
  daily_usd: ['daily_usd', 'weekly_usd'],
  weekly_usd: ['weekly_usd', 'daily_usd', 'monthly_usd'],
  monthly_usd: ['monthly_usd', 'weekly_usd'],
  warn_pct: ['warn_pct'],
}

export function draftFromThresholds(thresholds: BudgetThresholds): BudgetDraft {
  return {
    daily_usd: thresholds.daily_usd,
    weekly_usd: thresholds.weekly_usd,
    monthly_usd: thresholds.monthly_usd,
    warn_pct: String(thresholds.warn_pct),
  }
}

function parseMoney(raw: string, label: string): { value?: string; error?: string } {
  const trimmed = raw.trim()
  if (trimmed === '') {
    return { error: `${label} 必填` }
  }
  if (!/^\d+(?:\.\d+)?$/.test(trimmed)) {
    return { error: `${label} 必须是正的十进制金额` }
  }
  const value = Number(trimmed)
  if (!Number.isFinite(value) || value <= 0) {
    return { error: `${label} 必须大于 0` }
  }
  return { value: trimmed }
}

function parseWarn(raw: string): { value?: number; error?: string } {
  const trimmed = raw.trim()
  if (trimmed === '') {
    return { error: '警告阈值必填' }
  }
  const value = Number(trimmed)
  if (!Number.isFinite(value)) {
    return { error: '警告阈值必须是有限数字' }
  }
  if (value <= 0 || value > 100) {
    return { error: '警告阈值必须大于 0 且不超过 100' }
  }
  return { value }
}

export interface BudgetValidation {
  errors: BudgetErrors
  payload: BudgetUpdatePayload
  valid: boolean
}

/**
 * Mirrors the server Web update rule: positive money, warn greater than 0 and
 * at most 100, and daily ≤ weekly ≤ monthly. Like ``candidate_thresholds``, an
 * ordering error is reported only for a crossing the draft *introduces*
 * relative to ``current`` (the loaded server thresholds), so a CLI-created
 * crossed set stays editable for unrelated fields. Omitting ``current`` treats
 * the prior set as uncrossed, preserving the pure validator's behavior.
 */
export function validateBudgetDraft(
  draft: BudgetDraft,
  current?: BudgetThresholds,
): BudgetValidation {
  const errors: BudgetErrors = {}
  const payload: BudgetUpdatePayload = {}
  for (const field of MONEY_FIELDS) {
    const parsed = parseMoney(draft[field], BUDGET_LABELS[field])
    if (parsed.error !== undefined) {
      errors[field] = parsed.error
    } else if (parsed.value !== undefined) {
      payload[field] = parsed.value
    }
  }
  const warn = parseWarn(draft.warn_pct)
  if (warn.error !== undefined) {
    errors.warn_pct = warn.error
  } else if (warn.value !== undefined) {
    payload.warn_pct = warn.value
  }
  if (errors.daily_usd === undefined && errors.weekly_usd === undefined) {
    const weekly = Number(payload.weekly_usd)
    const daily = Number(payload.daily_usd)
    const currentCrossed =
      current !== undefined && Number(current.weekly_usd) < Number(current.daily_usd)
    if (weekly < daily && !currentCrossed) {
      errors.weekly_usd = 'Weekly 必须 ≥ Daily'
    }
  }
  if (errors.weekly_usd === undefined && errors.monthly_usd === undefined) {
    const monthly = Number(payload.monthly_usd)
    const weekly = Number(payload.weekly_usd)
    const currentCrossed =
      current !== undefined && Number(current.monthly_usd) < Number(current.weekly_usd)
    if (monthly < weekly && !currentCrossed) {
      errors.monthly_usd = 'Monthly 必须 ≥ Weekly'
    }
  }
  for (const field of BUDGET_FIELDS) {
    if (errors[field] !== undefined) {
      delete payload[field]
    }
  }
  return { errors, payload, valid: Object.keys(errors).length === 0 }
}

function isBudgetField(value: string | null | undefined): value is BudgetField {
  return (
    value !== null && value !== undefined && (BUDGET_FIELDS as readonly string[]).includes(value)
  )
}

function fieldErrorsFromDetails(details: readonly SettingsFieldDetail[]): BudgetErrors {
  const errors: BudgetErrors = {}
  for (const detail of details) {
    if (isBudgetField(detail.field) && detail.message) {
      errors[detail.field] = detail.message
    }
  }
  return errors
}

export interface BudgetLoad {
  state: 'loading' | 'ready' | 'error'
  data: BudgetData | null
  message?: string
}

export interface UseBudgetSettingsFormOptions {
  fetchBudgetFn?: (signal?: AbortSignal) => Promise<BudgetEnvelope>
  updateBudgetFn?: (
    payload: BudgetUpdatePayload,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<BudgetUpdateEnvelope>
  idempotencyKeyFn?: () => string
  rotateIdempotencyKeyFn?: () => string
  clearIdempotencyKeyFn?: () => void
  isCompact?: boolean
}

export interface BudgetSettingsForm {
  readonly: boolean
  load: BudgetLoad
  draft: BudgetDraft
  errors: BudgetErrors
  saving: boolean
  refreshing: boolean
  usageStale: boolean
  usageNotice: string
  usageLive: string
  statusMessage: string
  liveMessage: string
  errorText: string
  editable: boolean
  invalidFields: BudgetField[]
  unknownOutcome: boolean
  saveButtonRef: RefObject<HTMLButtonElement | null>
  errorSummaryRef: RefObject<HTMLDivElement | null>
  change: (field: BudgetField, value: string) => void
  retry: () => void
  refreshUsage: () => Promise<void>
  save: (event: FormEvent<HTMLFormElement>) => Promise<void>
}

export function useBudgetSettingsForm({
  fetchBudgetFn = fetchBudget,
  updateBudgetFn = updateBudgetThresholds,
  idempotencyKeyFn = budgetSettingsKey,
  rotateIdempotencyKeyFn = rotateBudgetSettingsKey,
  clearIdempotencyKeyFn = clearBudgetSettingsKey,
  isCompact,
}: UseBudgetSettingsFormOptions = {}): BudgetSettingsForm {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<BudgetLoad>({ state: 'loading', data: null })
  const [draft, setDraft] = useState<BudgetDraft>(() =>
    draftFromThresholds({
      daily_usd: '10.00',
      weekly_usd: '50.00',
      monthly_usd: '150.00',
      warn_pct: 80,
    }),
  )
  const [errors, setErrors] = useState<BudgetErrors>({})
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [usageStale, setUsageStale] = useState(false)
  const [usageNotice, setUsageNotice] = useState('')
  const [usageLive, setUsageLive] = useState('')
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorText, setErrorText] = useState('')
  const [unknownOutcome, setUnknownOutcome] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [focusTick, setFocusTick] = useState(0)
  const inFlight = useRef(false)
  const refreshingRef = useRef(false)
  // Incremented whenever a save replaces the displayed snapshot. A refresh
  // captures the current value and drops its response when a save superseded
  // it, so a slow refresh can never restore older thresholds.
  const snapshotGeneration = useRef(0)
  const mounted = useRef(true)
  const saveButtonRef = useRef<HTMLButtonElement>(null)
  const errorSummaryRef = useRef<HTMLDivElement>(null)
  const settleFocusRef = useRef<'save' | 'summary' | null>(null)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  function requestFocus(target: 'save' | 'summary'): void {
    settleFocusRef.current = target
    setFocusTick((value) => value + 1)
  }

  /**
   * The single place a server snapshot reaches the screen.
   *
   * Initial load, usage refresh, save success, and the post-failure reconcile
   * all funnel through it, so the displayed snapshot, the usage STALE marker,
   * the notice, and the live announcement cannot diverge. A server snapshot
   * also finalizes any read it superseded, so callers that need a specific
   * announcement set ``usageLive`` explicitly afterwards.
   */
  const applySnapshot = useCallback(
    (snapshot: BudgetData, options: { resetDraft?: boolean } = {}): void => {
      setLoad({ state: 'ready', data: snapshot })
      if (options.resetDraft === true) {
        setDraft(draftFromThresholds(snapshot.thresholds))
        setErrors({})
      }
      setUsageStale(false)
      setUsageNotice('')
      setUsageLive('')
    },
    [],
  )

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchBudgetFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', data: null, message: '预算响应为空。' })
          return
        }
        applySnapshot(data, { resetDraft: true })
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setLoad({ state: 'error', data: null, message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchBudgetFn, attempt, applySnapshot])

  useEffect(() => {
    if (saving) {
      return
    }
    const target = settleFocusRef.current
    if (target === null) {
      return
    }
    settleFocusRef.current = null
    if (target === 'summary') {
      errorSummaryRef.current?.focus()
    } else {
      saveButtonRef.current?.focus()
    }
  }, [saving, focusTick])

  const data = load.data
  const editable = load.state === 'ready' && data !== null && !readonly
  const invalidFields = BUDGET_FIELDS.filter((field) => errors[field] !== undefined)

  const retry = useCallback((): void => {
    setAttempt((value) => value + 1)
  }, [])

  const change = useCallback((field: BudgetField, value: string): void => {
    if (inFlight.current) {
      return
    }
    setDraft((previous) => ({ ...previous, [field]: value }))
    setErrors((previous) => {
      const coupled = COUPLED_ERROR_FIELDS[field]
      if (coupled.every((name) => previous[name] === undefined)) {
        return previous
      }
      const next = { ...previous }
      for (const name of coupled) {
        delete next[name]
      }
      return next
    })
    setErrorText('')
  }, [])

  const refreshUsage = useCallback(async (): Promise<void> => {
    if (refreshingRef.current || inFlight.current) {
      return
    }
    // The degraded panel shows N/A with no STALE badge, so it must not claim a
    // last-good value that the operator cannot see.
    const degraded = data === null || data.periods === null
    const requestGeneration = snapshotGeneration.current
    refreshingRef.current = true
    setRefreshing(true)
    setUsageNotice('')
    setUsageLive(
      degraded ? '正在刷新用量；用量当前不可用（N/A）。' : '正在刷新用量；显示上次值并标记 STALE。',
    )
    // Keep the last good values on screen; only mark them stale while the read
    // is in flight. The degraded branch ignores the marker and shows N/A.
    setUsageStale(true)
    try {
      const envelope = await fetchBudgetFn()
      if (!mounted.current) {
        return
      }
      if (snapshotGeneration.current !== requestGeneration) {
        // A save committed while this refresh was in flight and already wrote
        // the authoritative snapshot; never clobber it with an older read.
        return
      }
      if (envelope.data === null) {
        setUsageLive('')
        setUsageNotice(
          degraded
            ? '刷新失败：响应为空；用量仍不可用（N/A），阈值可编辑。'
            : '刷新失败：响应为空；保留上次成功值并标记 STALE。',
        )
        return
      }
      applySnapshot(envelope.data)
      setUsageLive(
        envelope.data.usage_quality === 'UNAVAILABLE'
          ? '用量刷新后仍不可用（N/A）；阈值可编辑。'
          : '用量已刷新。',
      )
    } catch (error: unknown) {
      if (!mounted.current || isAbortError(error)) {
        return
      }
      if (snapshotGeneration.current !== requestGeneration) {
        // A save (commit or unknown-outcome reconcile) already applied a newer
        // authoritative snapshot; this older failed read must not stamp STALE
        // or a failure notice over it.
        return
      }
      const failure = degraded
        ? `刷新失败：${errorMessage(error)}；用量仍不可用（N/A），阈值可编辑。`
        : `刷新失败：${errorMessage(error)}。保留上次成功值并标记 STALE。`
      // Keep the visible notice and announce the failure through the single
      // polite status region instead of clearing it.
      setUsageStale(true)
      setUsageNotice(failure)
      setUsageLive(failure)
    } finally {
      refreshingRef.current = false
      if (mounted.current) {
        setRefreshing(false)
      }
    }
  }, [fetchBudgetFn, applySnapshot, data])

  function applySaved(snapshot: BudgetUpdateData, before: BudgetThresholds): void {
    // The PUT returns the authoritative server snapshot (thresholds, usage,
    // projection, last alert), so no client-side state math remains.
    snapshotGeneration.current += 1
    applySnapshot(snapshot, { resetDraft: true })
    const message = snapshot.changed
      ? `已保存：日 ${before.daily_usd} → ${snapshot.thresholds.daily_usd}、周 ${before.weekly_usd} → ${snapshot.thresholds.weekly_usd}、月 ${before.monthly_usd} → ${snapshot.thresholds.monthly_usd}；audit 已写入。达到 warn ${snapshot.thresholds.warn_pct}% 与 100% 时仅告警，绝不停止或阻断调用；不会通过 Telegram 投递。`
      : `已保存：阈值未变更，未写入新 audit。阈值仅告警，绝不停止或阻断调用；不会通过 Telegram 投递。`
    setStatusMessage(message)
    setLiveMessage(message)
    setUnknownOutcome(false)
    requestFocus('save')
  }

  function handleSaveError(error: unknown): void {
    if (!mounted.current || isAbortError(error)) {
      return
    }
    const message = errorMessage(error)
    setLiveMessage('')
    if (error instanceof SettingsApiError && error.kind === 'refusal') {
      setErrors(fieldErrorsFromDetails(error.details))
      setUnknownOutcome(false)
      if (error.code === 'IDEMPOTENCY_CONFLICT') {
        rotateIdempotencyKeyFn()
        setErrorText(
          `请求冲突；已生成新的 Idempotency-Key，可直接重试。${message ? ` ${message}` : ''}`,
        )
      } else {
        setErrorText(message)
        clearIdempotencyKeyFn()
      }
      requestFocus('summary')
      return
    }
    // A timeout/transport failure may have committed. Keep the key so a retry
    // replays the original payload, and reconcile the displayed snapshot.
    // Advancing the generation drops a refresh that was in flight before the
    // save, so it can never restore the pre-save thresholds. The reconcile
    // itself captures that generation: if a retry save commits first, this
    // older read must not overwrite the newer authoritative snapshot.
    snapshotGeneration.current += 1
    const reconcileGeneration = snapshotGeneration.current
    setUnknownOutcome(true)
    setErrorText(`结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`)
    requestFocus('summary')
    void fetchBudgetFn()
      .then((envelope) => {
        if (
          !mounted.current ||
          envelope.data === null ||
          snapshotGeneration.current !== reconcileGeneration
        ) {
          return
        }
        // Applying this snapshot also finalizes the refresh that was in flight
        // when the save failed: it returns early on the generation change and
        // would otherwise leave STALE and "正在刷新用量" on screen.
        applySnapshot(envelope.data)
      })
      .catch(() => {
        // Keep the last good snapshot; the operator can retry safely.
      })
  }

  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (!editable || data === null || inFlight.current) {
      return
    }
    const outcome = validateBudgetDraft(draft, data.thresholds)
    setErrors(outcome.errors)
    if (!outcome.valid) {
      setUnknownOutcome(false)
      setErrorText('无法保存：请修正标出的字段。')
      setLiveMessage('')
      setStatusMessage('')
      requestFocus('summary')
      return
    }
    inFlight.current = true
    setSaving(true)
    setStatusMessage('')
    setErrorText('')
    setLiveMessage('保存中…')
    const before = data.thresholds
    try {
      const envelope = await updateBudgetFn(outcome.payload, idempotencyKeyFn())
      const result = envelope.data
      if (result === null) {
        throw new Error('预算响应为空。')
      }
      clearIdempotencyKeyFn()
      if (!mounted.current) {
        return
      }
      applySaved(result, before)
    } catch (error: unknown) {
      handleSaveError(error)
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setSaving(false)
      }
    }
  }

  return {
    readonly,
    load,
    draft,
    errors,
    saving,
    refreshing,
    usageStale,
    usageNotice,
    usageLive,
    statusMessage,
    liveMessage,
    errorText,
    editable,
    invalidFields,
    unknownOutcome,
    saveButtonRef,
    errorSummaryRef,
    change,
    retry,
    refreshUsage,
    save,
  }
}

export type { BudgetAlert }
