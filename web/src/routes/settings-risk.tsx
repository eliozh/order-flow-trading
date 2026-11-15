import { ConfirmDialog } from '../components/confirm-dialog'
import { RiskCapForm } from '../components/risk-cap-form'
import {
  RiskCurrentPanel,
  RiskErrorSummary,
  RiskLoadErrorPanel,
  RiskLoadStatus,
  RiskSaveStatus,
  RiskSaveVisible,
  RiskUnknownOutcomeHint,
  RiskUtilizationPanel,
} from '../components/risk-settings-panels'
import { SettingsSectionNav } from '../components/settings-section-nav'
import {
  type UseRiskSettingsFormOptions,
  useRiskSettingsForm,
} from '../hooks/use-risk-settings-form'

export type SettingsRiskPageProps = UseRiskSettingsFormOptions

/** `/settings/risk`: effective caps, utilization, and the tighten/confirm save. */
export function SettingsRiskPage(props: SettingsRiskPageProps) {
  const form = useRiskSettingsForm(props)
  const { load, readonly } = form
  const settings = load.settings

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
          <RiskLoadStatus loading={load.state === 'loading'} />
          {load.state === 'loading' && <p className="status-message">加载风险上限…</p>}

          {load.state === 'error' && (
            <RiskLoadErrorPanel message={load.message} onRetry={form.retry} />
          )}

          {load.state === 'ready' && settings !== null && (
            <>
              <RiskCurrentPanel settings={settings} />
              <RiskUtilizationPanel settings={settings} />

              <div className="stack">
                <RiskSaveStatus message={form.liveMessage} />
                <RiskErrorSummary
                  errorText={form.errorText}
                  invalidFields={form.invalidFields}
                  summaryRef={form.errorSummaryRef}
                />
                <RiskSaveVisible message={form.statusMessage} />
                <RiskCapForm
                  draft={form.draft}
                  caps={settings.caps}
                  defaults={settings.defaults}
                  errors={form.errors}
                  readOnly={!form.editable}
                  saving={form.saving}
                  onChange={form.change}
                  onSubmit={(event) => {
                    void form.save(event)
                  }}
                  saveButtonRef={form.saveButtonRef}
                />
              </div>

              {form.unknownOutcome && (
                <RiskUnknownOutcomeHint confirmed={form.unknownOutcomeConfirmed} />
              )}
            </>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={form.confirmOpen}
        title="确认放宽风险上限"
        description="以下上限更宽松。放宽上限会让代码强制的风险边界变松；确认后写入并审计。取消不会写入任何变更。"
        rows={form.pendingRows}
        busy={form.saving}
        restoreFocusId="risk-save-button"
        onCancel={form.cancelLoosen}
        onConfirm={() => {
          void form.confirmLoosen()
        }}
      />
    </section>
  )
}
