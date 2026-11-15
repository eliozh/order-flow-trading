import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

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
import { ReadOnlyField, SourceChip, useSettingsReadonly } from '../components/settings-field'
import { SettingsSectionNav } from '../components/settings-section-nav'

interface SettingsScanPageProps {
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

interface ScanLoad {
  state: 'loading' | 'ready' | 'error'
  settings: ScanSettingsData | null
  message?: string
}

type SettleFocus = 'save' | 'summary' | null

/** `/settings/scan`: the F10d scan-period form (4h/8h only). */
export function SettingsScanPage({
  fetchScanSettingsFn = fetchScanSettings,
  updateScanPeriodFn = updateScanPeriod,
  isCompact,
  idempotencyKeyFn = scanPeriodKey,
  clearIdempotencyKeyFn = clearScanPeriodKey,
  rotateIdempotencyKeyFn = rotateScanPeriodKey,
}: SettingsScanPageProps) {
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
      setFieldError('')
    }
    setSelected(period)
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
      if (!mounted.current) {
        return
      }
      setLoad({ state: 'ready', settings: data })
      setSelected(data.value)
      setUnknownOutcome(false)
      const change =
        beforeValue === data.value
          ? `扫描周期 ${data.value}（未变更）`
          : `扫描周期 ${beforeValue} → ${data.value}`
      const message = `已保存：${change}；生效周期的下一个 UTC 边界 ${data.next_run}。audit 已写入；变更在保存时活动排程的下一个边界生效。`
      setStatusMessage(message)
      setLiveMessage(message)
      clearIdempotencyKeyFn()
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
        setFieldError(message)
        setUnknownOutcome(false)
        clearIdempotencyKeyFn()
        settleFocusRef.current = 'summary'
      } else {
        // A timeout/transport/409 may have committed. Re-read the effective
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

  const describedBy = fieldError ? 'scan-period-hint scan-period-error' : 'scan-period-hint'

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-scan-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">Settings / scan</p>
          <h1 id="settings-scan-heading">扫描周期与排程</h1>
          <p className="page-sub">
            读取生效的扫描周期、来源与下一个 UTC 边界；修改仅支持 4h 与 8h，保存写入一条 audit
            记录，daemon 在下一个边界生效。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：扫描周期只能在桌面端或 CLI 修改。Kill switch 仍可用。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="scan" />
        <div className="stack">
          {load.state === 'loading' && (
            <p className="status-message" role="status">
              加载扫描设置…
            </p>
          )}

          {load.state === 'error' && (
            <div className="panel" role="alert">
              <div className="panel-head">
                <h2>扫描设置不可用</h2>
              </div>
              <p className="panel-foot">{load.message ?? '未知错误'}</p>
              <div className="action-row">
                <button
                  type="button"
                  className="control-btn"
                  onClick={() => setAttempt((value) => value + 1)}
                >
                  重试
                </button>
              </div>
            </div>
          )}

          {load.state === 'ready' && settings && (
            <>
              <section className="panel" aria-labelledby="scan-current-heading">
                <div className="panel-head">
                  <h2 id="scan-current-heading">当前生效值</h2>
                  <SourceChip source={settings.source} testId="scan-source" />
                </div>
                <p className="scan-current-value" data-testid="scan-current-value">
                  扫描周期 <strong className="mono">{settings.value}</strong>
                </p>
                <p className="hint" id="scan-period-hint">
                  可用值 {settings.supported.join(', ')}；变更在下一个 UTC 边界生效。
                </p>
                {settings.updated_at === null ? (
                  <p className="hint">仍在使用默认值，尚未保存过。</p>
                ) : (
                  <p className="hint">
                    上次修改 {settings.updated_at}
                    {settings.updated_by ? ` · ${settings.updated_by}` : ''}
                  </p>
                )}
              </section>

              <form
                className="panel"
                onSubmit={save}
                aria-labelledby="scan-form-heading"
                aria-busy={saving}
              >
                <div className="panel-head">
                  <h2 id="scan-form-heading">修改扫描周期</h2>
                </div>

                {/* Permanent live region: text is written into an existing node. */}
                <p
                  role="status"
                  className="visually-hidden"
                  id="settings-save-status"
                  data-testid="settings-save-status"
                >
                  {liveMessage}
                </p>

                {saveError !== '' && (
                  <div
                    className="status-message status-message--error"
                    id="settings-error-summary"
                    role="alert"
                    tabIndex={-1}
                    ref={errorSummaryRef}
                    data-testid="settings-error-summary"
                  >
                    <span aria-hidden="true">⚠</span> 错误：{saveError}
                    {fieldError !== '' && (
                      <>
                        {' '}
                        <a href="#scan-period-error">转到扫描周期</a>
                      </>
                    )}
                  </div>
                )}

                <div className="panel-foot">
                  <fieldset
                    className="settings-radio-group"
                    aria-describedby={describedBy}
                    aria-invalid={fieldError !== '' ? true : undefined}
                  >
                    <legend>扫描周期</legend>
                    <div className="seg">
                      {settings.supported.map((period) => (
                        <label key={period} className="segbtn">
                          <input
                            type="radio"
                            name="scan-period"
                            value={period}
                            checked={selected === period}
                            disabled={!editable}
                            aria-disabled={!editable || saving ? true : undefined}
                            onChange={() => selectPeriod(period)}
                          />
                          <span className="mono">{period}</span>
                        </label>
                      ))}
                    </div>
                    {fieldError !== '' && (
                      <p className="field-error" id="scan-period-error">
                        {fieldError}
                      </p>
                    )}
                  </fieldset>
                </div>

                <div className="settings-readonly-grid">
                  <ReadOnlyField
                    id="scan-lookback"
                    label="分析回看"
                    value="7 天"
                    note="read-only；没有 pipeline 消费该值，本切片不持久化。"
                    testId="scan-lookback"
                  />
                  <ReadOnlyField
                    id="scan-timezone"
                    label="时区显示"
                    value={settings.timezone}
                    note="read-only；所有边界按 UTC 对齐。"
                    testId="scan-timezone"
                  />
                  <ReadOnlyField
                    id="scan-next-run"
                    label="下一个 UTC 边界（生效周期）"
                    value={settings.next_run}
                    note="read-only；生效周期的下一个 UTC 边界；已保存的变更在保存时活动排程的下一个边界生效。"
                    testId="scan-next-run"
                  />
                </div>

                <div className="action-row settings-actions">
                  <button
                    type="submit"
                    className="btn"
                    ref={saveButtonRef}
                    disabled={!editable}
                    aria-disabled={!editable || saving ? true : undefined}
                  >
                    {saving ? '保存中…' : '保存排程'}
                  </button>
                  {readonly && <span className="hint">请在桌面端或 CLI 修改。</span>}
                </div>

                {statusMessage !== '' && (
                  <p className="status-message" data-testid="settings-save-visible">
                    {statusMessage}
                  </p>
                )}
              </form>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
