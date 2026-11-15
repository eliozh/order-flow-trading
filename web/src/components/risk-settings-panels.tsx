import { createPortal } from 'react-dom'
import type { RefObject } from 'react'

import type { RiskCapField, RiskSettingsData } from '../api/settings'
import { RISK_CAP_LABELS, RISK_CAP_META } from './risk-cap-form'
import { formatCapValue } from '../hooks/use-risk-settings-form'
import { SourceChip } from './settings-field'

function formatMetric(used: number | null, unit: string): string {
  if (used === null) {
    return '不可用'
  }
  return unit === '%' ? `${used.toFixed(2)}%` : `${used}${unit === 'positions' ? '' : ` ${unit}`}`
}

/** Permanent live region for the initial load; its text is written on change. */
export function RiskLoadStatus({ loading }: { loading: boolean }) {
  return (
    <p
      role="status"
      className="visually-hidden"
      id="settings-risk-load-status"
      data-testid="settings-risk-load-status"
    >
      {loading ? '加载风险上限…' : ''}
    </p>
  )
}

export function RiskLoadErrorPanel({
  message,
  onRetry,
}: {
  message: string | undefined
  onRetry: () => void
}) {
  return (
    <div className="panel" role="alert">
      <div className="panel-head">
        <h2>风险上限不可用</h2>
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

export function RiskCurrentPanel({ settings }: { settings: RiskSettingsData }) {
  return (
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
              {meta.label} {formatCapValue(settings.defaults[meta.field])}
              {index < RISK_CAP_META.length - 1 ? ' · ' : ''}
            </span>
          ))}
        </dd>
      </dl>
    </section>
  )
}

function RiskMeter({ settings, field }: { settings: RiskSettingsData; field: RiskCapField }) {
  const meta = RISK_CAP_META.find((candidate) => candidate.field === field)
  const metric = settings.utilization.metrics[field]
  if (meta === undefined || metric === undefined) {
    return null
  }
  if (metric.used === null) {
    // Unavailable is not zero: no meter semantics, no numeric value, and an
    // explicit read-only "—" instead.
    return (
      <div
        className="risk-meter"
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
    metric.cap <= 0 ? 0 : Math.max(0, Math.min(100, (metric.used / metric.cap) * 100))
  // Clamp the numeric ARIA state into [0, cap]: tightening a cap can leave
  // current usage above the new limit.
  const valueNow = Math.max(0, Math.min(metric.used, metric.cap))
  const overLimit = metric.used > metric.cap ? '；已超过上限' : ''
  return (
    <div
      className="risk-meter"
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
}

export function RiskUtilizationPanel({ settings }: { settings: RiskSettingsData }) {
  return (
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
        {RISK_CAP_META.map((meta) => (
          <RiskMeter key={meta.field} settings={settings} field={meta.field} />
        ))}
      </div>
      <p className="hint">
        利用率只读，来自 paper 账户；滑点、点差与 stale 秒数没有实时测量，因此显示为不可用而不是 0。
      </p>
    </section>
  )
}

/**
 * The single polite save announcer, portalled to `<body>` so it stays outside
 * the inert `.app-shell` while the confirmation modal is open.
 */
export function RiskSaveStatus({ message }: { message: string }) {
  if (typeof document === 'undefined') {
    return null
  }
  return createPortal(
    <p
      role="status"
      className="visually-hidden"
      id="settings-risk-save-status"
      data-testid="settings-risk-save-status"
    >
      {message}
    </p>,
    document.body,
  )
}

export function RiskErrorSummary({
  errorText,
  invalidFields,
  summaryRef,
}: {
  errorText: string
  invalidFields: readonly RiskCapField[]
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
  )
}

export function RiskSaveVisible({ message }: { message: string }) {
  if (message === '') {
    return null
  }
  // The permanent hidden ``role="status"`` above is the one polite live region;
  // this visible copy must not announce the same message twice.
  return (
    <p className="status-message">
      <span data-testid="settings-risk-save-visible">{message}</span>
    </p>
  )
}

export function RiskUnknownOutcomeHint({ confirmed = false }: { confirmed?: boolean }) {
  return (
    <p className="hint">
      {confirmed
        ? '上一次确认保存结果未知；已生成新的 Idempotency-Key。请重新保存并在确认对话框中再次确认。'
        : '上一次保存结果未知；重试会使用同一 Idempotency-Key 重放，不会重复写入。'}
    </p>
  )
}
