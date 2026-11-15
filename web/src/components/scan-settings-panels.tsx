import type { ScanSettingsData } from '../api/settings'
import type { ScanSettingsForm } from '../hooks/use-scan-settings-form'
import { ReadOnlyField, SourceChip } from './settings-field'

/** Permanent live region for the initial load; its text is written on change. */
export function ScanLoadStatus({ loading }: { loading: boolean }) {
  return (
    <p
      role="status"
      className="visually-hidden"
      id="settings-load-status"
      data-testid="settings-load-status"
    >
      {loading ? '加载扫描设置…' : ''}
    </p>
  )
}

export function ScanLoadErrorPanel({
  message,
  onRetry,
}: {
  message: string | undefined
  onRetry: () => void
}) {
  return (
    <div className="panel" role="alert">
      <div className="panel-head">
        <h2>扫描设置不可用</h2>
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

export function ScanCurrentPanel({ settings }: { settings: ScanSettingsData }) {
  return (
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
  )
}

export function ScanFormPanel({
  form,
  settings,
}: {
  form: ScanSettingsForm
  settings: ScanSettingsData
}) {
  const {
    readonly,
    selected,
    saving,
    editable,
    saveError,
    fieldError,
    liveMessage,
    statusMessage,
    describedBy,
    saveButtonRef,
    errorSummaryRef,
  } = form
  const invalid = fieldError !== '' ? true : undefined

  return (
    <form
      className="panel"
      onSubmit={form.save}
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
              <a href="#scan-period-group">转到扫描周期</a>
            </>
          )}
        </div>
      )}

      <div className="panel-foot">
        <fieldset
          className="settings-radio-group"
          id="scan-period-group"
          tabIndex={-1}
          aria-describedby={describedBy}
          aria-invalid={invalid}
        >
          <legend>扫描周期</legend>
          <div className="seg">
            {settings.supported.map((period) => (
              <label key={period} className="segbtn">
                {/* ``aria-invalid`` is a global ARIA attribute; the lint rule's
                    role table lags the spec for ``radio``. */}
                {/* eslint-disable-next-line jsx-a11y/role-supports-aria-props */}
                <input
                  type="radio"
                  name="scan-period"
                  value={period}
                  checked={selected === period}
                  disabled={!editable}
                  aria-disabled={!editable || saving ? true : undefined}
                  aria-invalid={invalid}
                  aria-describedby={describedBy}
                  onChange={() => form.selectPeriod(period)}
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

      <ScanReadOnlyFields settings={settings} />

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
  )
}

function ScanReadOnlyFields({ settings }: { settings: ScanSettingsData }) {
  return (
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
  )
}
