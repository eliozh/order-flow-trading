import type { FormEvent, RefObject } from 'react'

import { RISK_CAP_BOUNDS, type RiskCapField, type RiskCaps } from '../api/settings'

export interface RiskCapMeta {
  field: RiskCapField
  label: string
  /** The server's ceiling, or ``null`` when it enforces none. */
  max: number | null
  integer: boolean
  /** The visible allowed range, e.g. "> 0 且 ≤ 100%". */
  rangeLabel: string
}

export const RISK_CAP_META: readonly RiskCapMeta[] = [
  {
    field: 'per_trade_risk_pct',
    label: '单笔风险 %',
    max: RISK_CAP_BOUNDS.per_trade_risk_pct,
    integer: false,
    rangeLabel: '> 0 且 ≤ 100%',
  },
  {
    field: 'max_concurrent_positions',
    label: '最大同时持仓',
    max: RISK_CAP_BOUNDS.max_concurrent_positions,
    integer: true,
    rangeLabel: '整数且 ≥ 1',
  },
  {
    field: 'daily_loss_pct',
    label: '日亏损上限 %',
    max: RISK_CAP_BOUNDS.daily_loss_pct,
    integer: false,
    rangeLabel: '> 0 且 ≤ 100%，且 ≥ 单笔风险',
  },
  {
    field: 'max_leverage',
    label: '杠杆上限 x',
    max: RISK_CAP_BOUNDS.max_leverage,
    integer: false,
    rangeLabel: '> 0 且 ≤ 125x',
  },
  {
    field: 'max_notional_pct',
    label: '最大名义敞口 %',
    max: RISK_CAP_BOUNDS.max_notional_pct,
    integer: false,
    rangeLabel: '> 0 且 ≤ 100%',
  },
  {
    field: 'max_slippage_bps',
    label: '最大滑点 bps',
    max: RISK_CAP_BOUNDS.max_slippage_bps,
    integer: false,
    rangeLabel: '> 0 且 ≤ 10000 bps',
  },
  {
    field: 'max_spread_bps',
    label: '最大点差 bps',
    max: RISK_CAP_BOUNDS.max_spread_bps,
    integer: false,
    rangeLabel: '> 0 且 ≤ 10000 bps',
  },
  {
    field: 'stale_mark_seconds',
    label: 'Stale 标记秒数',
    max: RISK_CAP_BOUNDS.stale_mark_seconds,
    integer: true,
    rangeLabel: '整数且 ≥ 1',
  },
]

export const RISK_CAP_LABELS: Record<RiskCapField, string> = Object.fromEntries(
  RISK_CAP_META.map((meta) => [meta.field, meta.label]),
) as Record<RiskCapField, string>

export type RiskDraft = Record<RiskCapField, string>
export type RiskErrors = Partial<Record<RiskCapField, string>>

export interface RiskValidation {
  errors: RiskErrors
  patch: Partial<RiskCaps>
  valid: boolean
}

/** The form's string draft initialized from the effective caps. */
export function draftFromCaps(caps: RiskCaps): RiskDraft {
  return {
    per_trade_risk_pct: String(caps.per_trade_risk_pct),
    max_concurrent_positions: String(caps.max_concurrent_positions),
    daily_loss_pct: String(caps.daily_loss_pct),
    max_leverage: String(caps.max_leverage),
    max_notional_pct: String(caps.max_notional_pct),
    max_slippage_bps: String(caps.max_slippage_bps),
    max_spread_bps: String(caps.max_spread_bps),
    stale_mark_seconds: String(caps.stale_mark_seconds),
  }
}

function parseCap(meta: RiskCapMeta, raw: string): { value?: number; error?: string } {
  const trimmed = raw.trim()
  if (trimmed === '') {
    return { error: '必填' }
  }
  const value = Number(trimmed)
  if (!Number.isFinite(value)) {
    return { error: '必须是有限数字' }
  }
  if (value <= 0) {
    return { error: '必须大于 0' }
  }
  if (meta.integer && !Number.isInteger(value)) {
    return { error: '必须是整数' }
  }
  if (meta.max !== null && value > meta.max) {
    return { error: `必须 ≤ ${meta.max}` }
  }
  return { value }
}

/**
 * Client-side validation mirrors the server's ranges and the cross-field rule.
 * A violation blocks the save and calls no API; the server still re-validates.
 * ``patch`` names only the caps that actually changed.
 */
export function validateRiskDraft(draft: RiskDraft, caps: RiskCaps): RiskValidation {
  const errors: RiskErrors = {}
  const parsed = {} as Record<RiskCapField, number>
  for (const meta of RISK_CAP_META) {
    const result = parseCap(meta, draft[meta.field])
    if (result.error !== undefined) {
      errors[meta.field] = result.error
    } else if (result.value !== undefined) {
      parsed[meta.field] = result.value
    }
  }
  if (
    errors.per_trade_risk_pct === undefined &&
    errors.daily_loss_pct === undefined &&
    parsed.daily_loss_pct < parsed.per_trade_risk_pct
  ) {
    errors.daily_loss_pct = '日亏损上限必须 ≥ 单笔风险'
  }
  const patch: Partial<RiskCaps> = {}
  for (const meta of RISK_CAP_META) {
    const value = parsed[meta.field]
    if (value !== undefined && value !== caps[meta.field]) {
      patch[meta.field] = value
    }
  }
  return { errors, patch, valid: Object.keys(errors).length === 0 }
}

interface RiskCapFormProps {
  draft: RiskDraft
  caps: RiskCaps
  defaults: RiskCaps
  errors: RiskErrors
  readOnly: boolean
  saving: boolean
  onChange: (field: RiskCapField, value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  saveButtonRef: RefObject<HTMLButtonElement | null>
}

/** The eight numeric cap inputs with visible ranges and inline errors. */
export function RiskCapForm({
  draft,
  caps,
  defaults,
  errors,
  readOnly,
  saving,
  onChange,
  onSubmit,
  saveButtonRef,
}: RiskCapFormProps) {
  return (
    /*
     * ``noValidate`` keeps native constraint validation out of the way: the
     * browser otherwise blocks submit before React runs, so the designed
     * Chinese inline errors, ``aria-invalid``, and the error summary never
     * render, and server-valid off-step values such as 0.55 are rejected.
     * ``min``/``max`` remain advisory input hints only.
     */
    <form
      className="panel"
      aria-label="修改风险上限"
      aria-busy={saving}
      noValidate
      onSubmit={onSubmit}
    >
      <div className="panel-head">
        <h2>修改风险上限</h2>
      </div>
      <div className="risk-grid">
        {RISK_CAP_META.map((meta) => {
          const error = errors[meta.field]
          const hintId = `risk-${meta.field}-hint`
          const errorId = `risk-${meta.field}-error`
          const describedBy = error ? `${hintId} ${errorId}` : hintId
          return (
            <div className="field" key={meta.field}>
              <label className="field-label" htmlFor={`risk-${meta.field}`}>
                {meta.label}
                <span className="hint">{meta.rangeLabel}</span>
              </label>
              <input
                id={`risk-${meta.field}`}
                className="mono num-input"
                type="number"
                inputMode="decimal"
                min={meta.integer ? 1 : 0}
                max={meta.max ?? undefined}
                value={draft[meta.field]}
                disabled={readOnly}
                aria-disabled={readOnly || saving ? true : undefined}
                aria-invalid={error ? true : undefined}
                aria-describedby={describedBy}
                onChange={(event) => onChange(meta.field, event.target.value)}
              />
              <p className="hint" id={hintId}>
                默认 {defaults[meta.field]}；当前生效 {caps[meta.field]}。
              </p>
              {error && (
                <p className="field-error" id={errorId}>
                  {error}
                </p>
              )}
            </div>
          )
        })}
      </div>
      <div className="action-row settings-actions">
        <button
          type="submit"
          id="risk-save-button"
          className="btn"
          ref={saveButtonRef}
          disabled={readOnly || saving}
          aria-disabled={readOnly || saving ? true : undefined}
        >
          {saving ? '保存中…' : '保存风控'}
        </button>
        {readOnly && <span className="hint">请在桌面端或 CLI 修改。</span>}
      </div>
    </form>
  )
}
