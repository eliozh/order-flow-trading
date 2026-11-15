import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

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
  updateRiskSettings,
} from '../api/settings'
import { ConfirmDialog, type ConfirmDiffRow } from '../components/confirm-dialog'
import {
  draftFromCaps,
  RISK_CAP_LABELS,
  RISK_CAP_META,
  RiskCapForm,
  type RiskDraft,
  type RiskErrors,
  validateRiskDraft,
} from '../components/risk-cap-form'
import { SettingsSectionNav } from '../components/settings-section-nav'
import { SourceChip, useSettingsReadonly } from '../components/settings-field'

interface RiskLoad {
  state: 'loading' | 'ready' | 'error'
  settings: RiskSettingsData | null
  message?: string
}

export interface SettingsRiskPageProps {
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

function formatValue(value: number): string {
  return String(value)
}

function formatMetric(used: number | null, unit: string): string {
  if (used === null) {
    return '不可用'
  }
  return unit === '%' ? `${used.toFixed(2)}%` : `${used}${unit === 'positions' ? '' : ` ${unit}`}`
}

function fieldErrorsFromDetails(
  details: readonly { field?: string | null; message?: string }[],
): RiskErrors {
  const errors: RiskErrors = {}
  for (const detail of details) {
    const field = detail.field
    if (
      field !== null &&
      field !== undefined &&
      (RISK_CAP_FIELDS as readonly string[]).includes(field) &&
      detail.message
    ) {
      errors[field as RiskCapField] = detail.message
    }
  }
  return errors
}

/** `/settings/risk`: effective caps, utilization, and the tighten/confirm save. */
export function SettingsRiskPage({
  fetchRiskSettingsFn = fetchRiskSettings,
  updateRiskSettingsFn = updateRiskSettings,
  idempotencyKeyFn = riskSettingsKey,
  rotateIdempotencyKeyFn = rotateRiskSettingsKey,
  clearIdempotencyKeyFn = clearRiskSettingsKey,
  isCompact,
}: SettingsRiskPageProps) {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<RiskLoad>({ state: 'loading', settings: null })
  const [draft, setDraft] = useState<RiskDraft>(() => draftFromCaps(emptyCaps))
  const [errors, setErrors] = useState<RiskErrors>({})
  const [saving, setSaving] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorText, setErrorText] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pendingPatch, setPendingPatch] = useState<Partial<RiskCaps> | null>(null)
  const [pendingRows, setPendingRows] = useState<ConfirmDiffRow[]>([])
  const [unknownOutcome, setUnknownOutcome] = useState(false)
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
    clearIdempotencyKeyFn()
    setLoad({ state: 'ready', settings: data })
    setDraft(draftFromCaps(data.caps))
    setErrors({})
    setConfirmOpen(false)
    setPendingPatch(null)
    setPendingRows([])
    setUnknownOutcome(false)
    const changed = RISK_CAP_FIELDS.filter((field) => data.caps[field] !== before[field])
    const message =
      changed.length === 0
        ? '已保存：风险上限未变更，未写入新 audit。'
        : `已保存：${changed
            .map(
              (field) =>
                `${RISK_CAP_LABELS[field]} ${formatValue(before[field])} → ${formatValue(
                  data.caps[field],
                )}`,
            )
            .join('；')}。audit 已写入。`
    setStatusMessage(message)
    setLiveMessage(message)
    requestFocus('save')
  }

  function handleSaveError(error: unknown, patch: Partial<RiskCaps>, before: RiskCaps): void {
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
        setPendingRows(
          RISK_CAP_META.filter((meta) => patch[meta.field] !== undefined).map((meta) => ({
            field: meta.field,
            label: meta.label,
            before: formatValue(before[meta.field]),
            after: formatValue(patch[meta.field] as number),
          })),
        )
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
        requestFocus('summary')
        return
      }
    }
    setUnknownOutcome(true)
    setErrorText(`结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`)
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
      applySaved(envelope.data, before)
    } catch (error: unknown) {
      handleSaveError(error, outcome.patch, before)
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
      applySaved(envelope.data, before)
    } catch (error: unknown) {
      setConfirmOpen(false)
      handleSaveError(error, pendingPatch, before)
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setSaving(false)
      }
    }
  }

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-risk-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">Settings / risk</p>
          <h1 id="settings-risk-heading">风险上限</h1>
          <p className="page-sub">
            单笔风险、最大持仓、日亏损、杠杆、最大名义，以及滑点、点差与 stale 标记上限。LLM 提议被
            clamp 到这些上限并取更严者；收紧直接保存，放宽需 before → after
            确认。每次实际变更写入一条 RISK_CAPS_UPDATED audit。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：风险上限只能在桌面端或 CLI 修改。Kill switch 仍可用。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="risk" />
        <div className="stack">
          <p
            role="status"
            className="visually-hidden"
            id="settings-risk-load-status"
            data-testid="settings-risk-load-status"
          >
            {load.state === 'loading' ? '加载风险上限…' : ''}
          </p>

          {load.state === 'loading' && <p className="status-message">加载风险上限…</p>}

          {load.state === 'error' && (
            <div className="panel" role="alert">
              <div className="panel-head">
                <h2>风险上限不可用</h2>
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

          {load.state === 'ready' && settings !== null && (
            <>
              <section className="panel" aria-labelledby="risk-current-heading">
                <div className="panel-head">
                  <h2 id="risk-current-heading">当前生效上限</h2>
                  <SourceChip source={settings.source} testId="risk-source" />
                </div>
                <p className="scan-current-value">
                  来源 <strong className="mono">{settings.source}</strong>
                  {settings.updated_at === null
                    ? ' · 仍在使用默认值，尚未保存过。'
                    : ` · 上次修改 ${settings.updated_at}${
                        settings.updated_by ? ` · ${settings.updated_by}` : ''
                      }`}
                </p>
                <dl className="kv risk-defaults">
                  <dt>默认值（环境配置）</dt>
                  <dd className="mono" data-testid="risk-defaults">
                    {RISK_CAP_META.map((meta, index) => (
                      <span key={meta.field}>
                        {meta.label} {formatValue(settings.defaults[meta.field])}
                        {index < RISK_CAP_META.length - 1 ? ' · ' : ''}
                      </span>
                    ))}
                  </dd>
                </dl>
              </section>

              <section className="panel" aria-labelledby="risk-utilization-heading">
                <div className="panel-head">
                  <h2 id="risk-utilization-heading">利用率（只读）</h2>
                  <span className="chip chip--muted" data-testid="risk-utilization-source">
                    source: {settings.utilization.source}
                  </span>
                  <span
                    className={`chip ${
                      settings.utilization.quality === 'COMPLETE' ? 'chip--ok' : 'chip--warn'
                    }`}
                    data-testid="risk-utilization-quality"
                  >
                    quality: {settings.utilization.quality}
                  </span>
                </div>
                <div className="risk-meters">
                  {RISK_CAP_META.map((meta) => {
                    const metric = settings.utilization.metrics[meta.field]
                    if (metric.used === null) {
                      // Unavailable is not zero: no meter semantics, no numeric
                      // value, and an explicit read-only "—" instead.
                      return (
                        <div
                          className="risk-meter"
                          key={meta.field}
                          role="group"
                          aria-label={`${meta.label}：不可用；来源 ${metric.source}；质量 ${metric.quality}`}
                          data-testid={`risk-meter-${meta.field}`}
                        >
                          <span className="risk-meter-head">
                            <span>{meta.label}</span>
                            <span className="mono">—</span>
                          </span>
                          <span className="hint">
                            不可用 · 来源 {metric.source} · 质量 {metric.quality}
                            {metric.note ? ` · ${metric.note}` : ''}
                          </span>
                        </div>
                      )
                    }
                    const valueText = `${formatMetric(metric.used, metric.unit)} / ${formatMetric(
                      metric.cap,
                      metric.unit,
                    )}`
                    const percentage =
                      metric.cap <= 0
                        ? 0
                        : Math.max(0, Math.min(100, (metric.used / metric.cap) * 100))
                    // Clamp the numeric ARIA state into [0, cap]: tightening a
                    // cap can leave current usage above the new limit.
                    const valueNow = Math.max(0, Math.min(metric.used, metric.cap))
                    const overLimit = metric.used > metric.cap ? '；已超过上限' : ''
                    return (
                      <div
                        className="risk-meter"
                        key={meta.field}
                        role="meter"
                        aria-label={meta.label}
                        aria-valuemin={0}
                        aria-valuemax={metric.cap}
                        aria-valuenow={valueNow}
                        aria-valuetext={`${meta.label}：${valueText}；来源 ${metric.source}；质量 ${metric.quality}${overLimit}`}
                        data-testid={`risk-meter-${meta.field}`}
                      >
                        <span className="risk-meter-head">
                          <span>{meta.label}</span>
                          <span className="mono">{valueText}</span>
                        </span>
                        <span className="risk-meter-track" aria-hidden="true">
                          <span className="risk-meter-fill" style={{ width: `${percentage}%` }} />
                        </span>
                        <span className="hint">
                          来源 {metric.source} · 质量 {metric.quality}
                          {metric.note ? ` · ${metric.note}` : ''}
                        </span>
                      </div>
                    )
                  })}
                </div>
                <p className="hint">
                  利用率只读，来自 paper 账户；滑点、点差与 stale
                  秒数没有实时测量，因此显示为不可用而不是 0。
                </p>
              </section>

              <div className="stack">
                <p
                  role="status"
                  className="visually-hidden"
                  id="settings-risk-save-status"
                  data-testid="settings-risk-save-status"
                >
                  {liveMessage}
                </p>

                {errorText !== '' && (
                  <div
                    className="status-message status-message--error"
                    role="alert"
                    tabIndex={-1}
                    ref={errorSummaryRef}
                    data-testid="settings-risk-error"
                  >
                    <span aria-hidden="true">⚠</span> 错误：{errorText}
                    {invalidFields.map((field) => (
                      <span key={field}>
                        {' '}
                        <a href={`#risk-${field}`}>转到{RISK_CAP_LABELS[field]}</a>
                      </span>
                    ))}
                  </div>
                )}

                {statusMessage !== '' && (
                  // The permanent hidden ``role="status"`` above is the one
                  // polite live region; this visible copy must not announce the
                  // same message twice.
                  <p className="status-message">
                    <span data-testid="settings-risk-save-visible">{statusMessage}</span>
                  </p>
                )}

                <RiskCapForm
                  draft={draft}
                  caps={settings.caps}
                  defaults={settings.defaults}
                  errors={errors}
                  readOnly={!editable}
                  saving={saving}
                  onChange={change}
                  onSubmit={(event) => {
                    void save(event)
                  }}
                  saveButtonRef={saveButtonRef}
                />
              </div>

              {unknownOutcome && (
                <p className="hint">
                  上一次保存结果未知；重试会使用同一 Idempotency-Key 重放，不会重复写入。
                </p>
              )}
            </>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="确认放宽风险上限"
        description="以下上限更宽松。放宽上限会让代码强制的风险边界变松；确认后写入并审计。取消不会写入任何变更。"
        rows={pendingRows}
        busy={saving}
        restoreFocusId="risk-save-button"
        onCancel={cancelLoosen}
        onConfirm={() => {
          void confirmLoosen()
        }}
      />
    </section>
  )
}

/** Zeroed shell used only until the first response initializes the draft. */
const emptyCaps: RiskCaps = {
  per_trade_risk_pct: 0,
  max_concurrent_positions: 0,
  daily_loss_pct: 0,
  max_leverage: 0,
  max_notional_pct: 0,
  max_slippage_bps: 0,
  max_spread_bps: 0,
  stale_mark_seconds: 0,
}
