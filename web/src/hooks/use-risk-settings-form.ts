import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, RefObject } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  clearRiskSettingsKey,
  fetchRiskSettings,
  type RiskCapField,
  RISK_CAP_FIELDS,
  type RiskCaps,
  type RiskSettingsData,
  type RiskSettingsEnvelope,
  riskSettingsKey,
  rotateRiskSettingsKey,
  SettingsApiError,
  type SettingsFieldDetail,
  updateRiskSettings,
} from '../api/settings'
import type { ConfirmDiffRow } from '../components/confirm-dialog'
import {
  draftFromCaps,
  RISK_CAP_LABELS,
  RISK_CAP_META,
  type RiskDraft,
  type RiskErrors,
  validateRiskDraft,
} from '../components/risk-cap-form'
import { useSettingsReadonly } from '../components/settings-field'

export interface RiskLoad {
  state: 'loading' | 'ready' | 'error'
  settings: RiskSettingsData | null
  message?: string
}

export interface UseRiskSettingsFormOptions {
  fetchRiskSettingsFn?: (signal?: AbortSignal) => Promise<RiskSettingsEnvelope>
  updateRiskSettingsFn?: (
    caps: Partial<RiskCaps>,
    confirm: boolean,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<RiskSettingsEnvelope>
  idempotencyKeyFn?: () => string
  rotateIdempotencyKeyFn?: () => string
  clearIdempotencyKeyFn?: () => void
  isCompact?: boolean
}

export interface RiskSettingsForm {
  readonly: boolean
  load: RiskLoad
  draft: RiskDraft
  errors: RiskErrors
  saving: boolean
  statusMessage: string
  liveMessage: string
  errorText: string
  confirmOpen: boolean
  pendingRows: ConfirmDiffRow[]
  unknownOutcome: boolean
  unknownOutcomeConfirmed: boolean
  editable: boolean
  invalidFields: RiskCapField[]
  saveButtonRef: RefObject<HTMLButtonElement | null>
  errorSummaryRef: RefObject<HTMLDivElement | null>
  change: (field: RiskCapField, value: string) => void
  retry: () => void
  save: (event: FormEvent<HTMLFormElement>) => Promise<void>
  confirmLoosen: () => Promise<void>
  cancelLoosen: () => void
}

/** Zeroed shell used only until the first response initializes the draft. */
const EMPTY_CAPS: RiskCaps = {
  per_trade_risk_pct: 0,
  max_concurrent_positions: 0,
  daily_loss_pct: 0,
  max_leverage: 0,
  max_notional_pct: 0,
  max_slippage_bps: 0,
  max_spread_bps: 0,
  stale_mark_seconds: 0,
}

export function formatCapValue(value: number): string {
  return String(value)
}

function isRiskCapField(field: string | null | undefined): field is RiskCapField {
  return (
    field !== null && field !== undefined && (RISK_CAP_FIELDS as readonly string[]).includes(field)
  )
}

function fieldErrorsFromDetails(details: readonly SettingsFieldDetail[]): RiskErrors {
  const errors: RiskErrors = {}
  for (const detail of details) {
    if (isRiskCapField(detail.field) && detail.message) {
      errors[detail.field] = detail.message
    }
  }
  return errors
}

/**
 * The confirmation diff comes from the server's ``confirmation_required``
 * details, never from client state: the server is the only authority on which
 * caps actually loosen and on the committed ``before`` values. A detail without
 * ``before``/``after`` (older server) falls back to the mounted caps and the
 * submitted patch.
 */
function confirmationRows(
  details: readonly SettingsFieldDetail[],
  patch: Partial<RiskCaps>,
  caps: RiskCaps,
): ConfirmDiffRow[] {
  const flagged = new Set<RiskCapField>()
  for (const detail of details) {
    if (detail.code === 'confirmation_required' && isRiskCapField(detail.field)) {
      flagged.add(detail.field)
    }
  }
  return RISK_CAP_META.filter((meta) => flagged.has(meta.field)).map((meta) => {
    const detail = details.find(
      (candidate) => candidate.code === 'confirmation_required' && candidate.field === meta.field,
    )
    return {
      field: meta.field,
      label: meta.label,
      before: formatCapValue(detail?.before ?? caps[meta.field]),
      after: formatCapValue(detail?.after ?? patch[meta.field] ?? caps[meta.field]),
    }
  })
}

/**
 * Load/save/reconcile state for the `/settings/risk` form.
 *
 * The page stays a thin composition; this hook owns the idempotency lifecycle,
 * the confirmation dialog's pending patch and server-sourced diff, the
 * unknown-outcome reconciliation, and the post-settle focus moves.
 */
export function useRiskSettingsForm({
  fetchRiskSettingsFn = fetchRiskSettings,
  updateRiskSettingsFn = updateRiskSettings,
  idempotencyKeyFn = riskSettingsKey,
  rotateIdempotencyKeyFn = rotateRiskSettingsKey,
  clearIdempotencyKeyFn = clearRiskSettingsKey,
  isCompact,
}: UseRiskSettingsFormOptions = {}): RiskSettingsForm {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<RiskLoad>({ state: 'loading', settings: null })
  const [draft, setDraft] = useState<RiskDraft>(() => draftFromCaps(EMPTY_CAPS))
  const [errors, setErrors] = useState<RiskErrors>({})
  const [saving, setSaving] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorText, setErrorText] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pendingPatch, setPendingPatch] = useState<Partial<RiskCaps> | null>(null)
  const [pendingRows, setPendingRows] = useState<ConfirmDiffRow[]>([])
  const [unknownOutcome, setUnknownOutcome] = useState(false)
  const [unknownOutcomeConfirmed, setUnknownOutcomeConfirmed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [focusTick, setFocusTick] = useState(0)
  const inFlight = useRef(false)
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

  /**
   * Request a focus move that lands after the next render.
   *
   * The effect keys on both ``saving`` and ``focusTick`` so the client-side
   * invalid path (which never toggles ``saving``) still focuses the summary.
   */
  function requestFocus(target: 'save' | 'summary'): void {
    settleFocusRef.current = target
    setFocusTick((value) => value + 1)
  }

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchRiskSettingsFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', settings: null, message: '设置响应为空。' })
          return
        }
        setLoad({ state: 'ready', settings: data })
        setDraft(draftFromCaps(data.caps))
        setErrors({})
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setLoad({ state: 'error', settings: null, message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchRiskSettingsFn, attempt])

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

  const settings = load.settings
  const editable = load.state === 'ready' && settings !== null && !readonly
  const invalidFields = RISK_CAP_FIELDS.filter((field) => errors[field] !== undefined)

  const cancelLoosen = useCallback((): void => {
    setConfirmOpen(false)
    setPendingPatch(null)
    setPendingRows([])
    // The refused initial PUT was a definitive no-write refusal, so its
    // pending key must not bind a later, different patch to a spurious 409.
    clearIdempotencyKeyFn()
    const message = '已取消放宽；未写入任何变更。'
    setStatusMessage(message)
    setLiveMessage(message)
  }, [clearIdempotencyKeyFn])

  function retry(): void {
    setAttempt((value) => value + 1)
  }

  function change(field: RiskCapField, value: string): void {
    if (inFlight.current) {
      return
    }
    setDraft((previous) => ({ ...previous, [field]: value }))
    if (errors[field] !== undefined) {
      setErrors((previous) => {
        const next = { ...previous }
        delete next[field]
        return next
      })
    }
    setErrorText('')
  }

  function applySaved(data: RiskSettingsData, before: RiskCaps): void {
    setLoad({ state: 'ready', settings: data })
    setDraft(draftFromCaps(data.caps))
    setErrors({})
    setConfirmOpen(false)
    setPendingPatch(null)
    setPendingRows([])
    setUnknownOutcome(false)
    setUnknownOutcomeConfirmed(false)
    const changed = RISK_CAP_FIELDS.filter((field) => data.caps[field] !== before[field])
    const message =
      changed.length === 0
        ? '已保存：风险上限未变更，未写入新 audit。'
        : `已保存：${changed
            .map(
              (field) =>
                `${RISK_CAP_LABELS[field]} ${formatCapValue(before[field])} → ${formatCapValue(
                  data.caps[field],
                )}`,
            )
            .join('；')}。audit 已写入。`
    setStatusMessage(message)
    setLiveMessage(message)
    requestFocus('save')
  }

  function handleSaveError(
    error: unknown,
    patch: Partial<RiskCaps>,
    before: RiskCaps,
    confirmed: boolean,
  ): void {
    if (!mounted.current || isAbortError(error)) {
      return
    }
    const message = errorMessage(error)
    setLiveMessage('')
    if (error instanceof SettingsApiError) {
      const confirmation = error.details.some((detail) => detail.code === 'confirmation_required')
      if (confirmation) {
        // A confirmation-required refusal is definitive and wrote nothing, so
        // retire the pending key; the confirmed retry rotates a fresh one.
        clearIdempotencyKeyFn()
        setPendingPatch(patch)
        setPendingRows(confirmationRows(error.details, patch, before))
        setConfirmOpen(true)
        setErrorText('')
        setStatusMessage('')
        setLiveMessage('放宽上限需要确认；尚未写入任何变更。')
        settleFocusRef.current = null
        return
      }
      if (error.kind === 'refusal') {
        const mapped = fieldErrorsFromDetails(error.details)
        setErrors(mapped)
        if (error.code === 'IDEMPOTENCY_CONFLICT') {
          rotateIdempotencyKeyFn()
          setErrorText(
            `请求冲突；已生成新的 Idempotency-Key，可直接重试。${message ? ` ${message}` : ''}`,
          )
        } else {
          setErrorText(message)
          clearIdempotencyKeyFn()
        }
        setUnknownOutcome(false)
        setUnknownOutcomeConfirmed(false)
        requestFocus('summary')
        return
      }
    }
    if (confirmed) {
      // The confirmed PUT may have committed. Replaying the form's plain save
      // would resend this key with ``confirm: false`` (same key + different
      // body → guaranteed 409), so rotate now: compare-and-write makes a
      // retried write a safe no-op, and the guidance must not promise a replay.
      rotateIdempotencyKeyFn()
      setUnknownOutcome(true)
      setUnknownOutcomeConfirmed(true)
      setErrorText(
        `上一次确认保存结果未知；已生成新的 Idempotency-Key。请重新保存并在确认对话框中再次确认。${
          message ? ` ${message}` : ''
        }`,
      )
    } else {
      setUnknownOutcome(true)
      setUnknownOutcomeConfirmed(false)
      setErrorText(`结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`)
    }
    requestFocus('summary')
  }

  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (!editable || settings === null || inFlight.current) {
      return
    }
    const outcome = validateRiskDraft(draft, settings.caps)
    setErrors(outcome.errors)
    if (!outcome.valid) {
      // The focused assertively-announced alert below is the single
      // announcement; clearing the polite region avoids a duplicate reading of
      // the same error (the server-refusal path does the same).
      setErrorText('无法保存：请修正标出的字段。')
      setLiveMessage('')
      setStatusMessage('')
      requestFocus('summary')
      return
    }
    if (Object.keys(outcome.patch).length === 0) {
      const message = '未变更：没有保存任何值，未写入新 audit。'
      setStatusMessage(message)
      setLiveMessage(message)
      setErrorText('')
      return
    }
    inFlight.current = true
    setSaving(true)
    setStatusMessage('')
    setErrorText('')
    setLiveMessage('保存中…')
    const before = settings.caps
    try {
      const envelope = await updateRiskSettingsFn(outcome.patch, false, idempotencyKeyFn())
      if (envelope.data === null) {
        throw new Error('设置响应为空。')
      }
      // Success is mount-independent: always retire the key, even if the page
      // unmounted while the PUT was in flight.
      clearIdempotencyKeyFn()
      if (!mounted.current) {
        return
      }
      applySaved(envelope.data, before)
    } catch (error: unknown) {
      handleSaveError(error, outcome.patch, before, false)
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setSaving(false)
      }
    }
  }

  async function confirmLoosen(): Promise<void> {
    if (settings === null || pendingPatch === null || inFlight.current) {
      return
    }
    inFlight.current = true
    setSaving(true)
    setLiveMessage('确认保存中…')
    const before = settings.caps
    try {
      const envelope = await updateRiskSettingsFn(pendingPatch, true, rotateIdempotencyKeyFn())
      if (envelope.data === null) {
        throw new Error('设置响应为空。')
      }
      clearIdempotencyKeyFn()
      if (!mounted.current) {
        return
      }
      applySaved(envelope.data, before)
    } catch (error: unknown) {
      if (mounted.current) {
        setConfirmOpen(false)
      }
      handleSaveError(error, pendingPatch, before, true)
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
    statusMessage,
    liveMessage,
    errorText,
    confirmOpen,
    pendingRows,
    unknownOutcome,
    unknownOutcomeConfirmed,
    editable,
    invalidFields,
    saveButtonRef,
    errorSummaryRef,
    change,
    retry,
    save,
    confirmLoosen,
    cancelLoosen,
  }
}
