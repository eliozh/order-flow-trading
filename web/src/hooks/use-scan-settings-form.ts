import { useEffect, useRef, useState } from 'react'
import type { FormEvent, RefObject } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  clearScanPeriodKey,
  fetchScanSettings,
  rotateScanPeriodKey,
  scanPeriodKey,
  type ScanSettingsData,
  type ScanSettingsEnvelope,
  SettingsApiError,
  updateScanPeriod,
} from '../api/settings'
import { useSettingsReadonly } from '../components/settings-field'

export interface ScanLoad {
  state: 'loading' | 'ready' | 'error'
  settings: ScanSettingsData | null
  message?: string
}

type SettleFocus = 'save' | 'summary' | null

export interface UseScanSettingsFormOptions {
  fetchScanSettingsFn?: (signal?: AbortSignal) => Promise<ScanSettingsEnvelope>
  updateScanPeriodFn?: (
    period: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<ScanSettingsEnvelope>
  isCompact?: boolean
  idempotencyKeyFn?: () => string
  clearIdempotencyKeyFn?: () => void
  rotateIdempotencyKeyFn?: () => string
}

export interface ScanSettingsForm {
  readonly: boolean
  load: ScanLoad
  selected: string
  saving: boolean
  statusMessage: string
  liveMessage: string
  errorMessageText: string
  fieldError: string
  editable: boolean
  saveError: string
  describedBy: string
  saveButtonRef: RefObject<HTMLButtonElement | null>
  errorSummaryRef: RefObject<HTMLDivElement | null>
  selectPeriod: (period: string) => void
  retry: () => void
  save: (event: FormEvent<HTMLFormElement>) => Promise<void>
}

/**
 * Load/save/reconcile state for the `/settings/scan` form.
 *
 * The page stays a thin composition; this hook owns the idempotency lifecycle,
 * unknown-outcome reconciliation, and the post-settle focus move.
 */
export function useScanSettingsForm({
  fetchScanSettingsFn = fetchScanSettings,
  updateScanPeriodFn = updateScanPeriod,
  isCompact,
  idempotencyKeyFn = scanPeriodKey,
  clearIdempotencyKeyFn = clearScanPeriodKey,
  rotateIdempotencyKeyFn = rotateScanPeriodKey,
}: UseScanSettingsFormOptions = {}): ScanSettingsForm {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<ScanLoad>({ state: 'loading', settings: null })
  const [selected, setSelected] = useState('')
  const [saving, setSaving] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorMessageText, setErrorMessageText] = useState('')
  const [fieldError, setFieldError] = useState('')
  const [unknownOutcome, setUnknownOutcome] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const inFlight = useRef(false)
  const mounted = useRef(true)
  const saveButtonRef = useRef<HTMLButtonElement>(null)
  const errorSummaryRef = useRef<HTMLDivElement>(null)
  const submittedValueRef = useRef<string | null>(null)
  const settleFocusRef = useRef<SettleFocus>(null)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchScanSettingsFn(controller.signal)
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
        setSelected(data.value)
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
  }, [fetchScanSettingsFn, attempt])

  // Move focus once the save settles. The summary is rendered by then.
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
  }, [saving])

  const settings = load.settings
  const editable = load.state === 'ready' && settings !== null && !readonly
  const saveError = fieldError || errorMessageText
  const describedBy = fieldError ? 'scan-period-hint scan-period-error' : 'scan-period-hint'

  function selectPeriod(period: string): void {
    if (inFlight.current) {
      return
    }
    if (unknownOutcome && period !== submittedValueRef.current) {
      // The previous attempt may have committed; a new logical save must not
      // replay its idempotency key with a different payload.
      rotateIdempotencyKeyFn()
      setUnknownOutcome(false)
      setErrorMessageText('')
    }
    // A valid change invalidates the field-scoped refusal, so the summary's
    // field link and ``aria-invalid`` do not persist.
    setFieldError('')
    setSelected(period)
  }

  function retry(): void {
    setAttempt((value) => value + 1)
  }

  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (!editable || inFlight.current) {
      return
    }
    inFlight.current = true
    const beforeValue = load.settings?.value ?? ''
    submittedValueRef.current = selected
    setSaving(true)
    setStatusMessage('')
    setErrorMessageText('')
    setFieldError('')
    setLiveMessage('保存中…')
    try {
      const envelope = await updateScanPeriodFn(selected, idempotencyKeyFn())
      const data = envelope.data
      if (data === null) {
        throw new Error('设置响应为空。')
      }
      // Success is mount-independent: always retire the key, even if the
      // component unmounted while the PUT was in flight. Otherwise a later save
      // with a different value would reuse it and 409 forever.
      clearIdempotencyKeyFn()
      if (!mounted.current) {
        return
      }
      setLoad({ state: 'ready', settings: data })
      setSelected(data.value)
      setUnknownOutcome(false)
      const message =
        beforeValue === data.value
          ? `已保存：扫描周期 ${data.value}（未变更，未写入新 audit）。生效周期的下一个 UTC 边界 ${data.next_run}。`
          : `已保存：扫描周期 ${beforeValue} → ${data.value}；生效周期的下一个 UTC 边界 ${data.next_run}。audit 已写入；变更在保存时活动排程的下一个边界生效。`
      setStatusMessage(message)
      setLiveMessage(message)
      settleFocusRef.current = 'save'
    } catch (error: unknown) {
      if (isAbortError(error)) {
        return
      }
      if (!mounted.current) {
        return
      }
      const message = errorMessage(error)
      setLiveMessage('')
      if (error instanceof SettingsApiError && error.kind === 'refusal') {
        // The server wrote nothing, so a corrected retry gets a fresh key.
        if (error.code === 'IDEMPOTENCY_CONFLICT') {
          // A 409 is definitive for this payload: the key is bound to a
          // different request or a still-running one. Rotate rather than
          // entering the unknown-outcome/reconcile path, which would dead-end.
          rotateIdempotencyKeyFn()
          setFieldError(
            `该保存与同一 Idempotency-Key 的先前请求冲突；已生成新的 Idempotency-Key，可直接重试。${
              message ? ` ${message}` : ''
            }`,
          )
        } else {
          setFieldError(message)
          clearIdempotencyKeyFn()
        }
        setUnknownOutcome(false)
        settleFocusRef.current = 'summary'
      } else {
        // A timeout/transport failure may have committed. Re-read the effective
        // value and keep the key so a retry replays the original response.
        setUnknownOutcome(true)
        setErrorMessageText(
          `结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`,
        )
        settleFocusRef.current = 'summary'
        try {
          const reconciled = await fetchScanSettingsFn()
          if (mounted.current && reconciled.data) {
            // Refresh only the displayed effective state. Keep `selected` at the
            // attempted value: the copy promises a retry replays the original
            // payload with the same Idempotency-Key, so if the PUT did not
            // commit the form must still hold what the operator tried to save.
            setLoad({ state: 'ready', settings: reconciled.data })
          }
        } catch {
          // Keep the pre-save value; the operator can retry safely.
        }
      }
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
    selected,
    saving,
    statusMessage,
    liveMessage,
    errorMessageText,
    fieldError,
    editable,
    saveError,
    describedBy,
    saveButtonRef,
    errorSummaryRef,
    selectPeriod,
    retry,
    save,
  }
}
