import {
  BudgetErrorSummary,
  BudgetLastAlertPanel,
  BudgetLoadErrorPanel,
  BudgetLoadStatus,
  BudgetSaveStatus,
  BudgetSaveVisible,
  BudgetThresholdsPanel,
  BudgetUnknownOutcomeHint,
  BudgetUsagePanel,
  BudgetUsageStatus,
} from '../components/budget-settings-panels'
import { SettingsSectionNav } from '../components/settings-section-nav'
import {
  type UseBudgetSettingsFormOptions,
  useBudgetSettingsForm,
} from '../hooks/use-budget-settings-form'

export type SettingsBudgetPageProps = UseBudgetSettingsFormOptions

/** `/settings/budget`: F10e thresholds plus read-only usage and last alert. */
export function SettingsBudgetPage(props: SettingsBudgetPageProps) {
  const form = useBudgetSettingsForm(props)
  const { load, readonly } = form
  const data = load.data

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-budget-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">Settings / budget</p>
          <h1 id="settings-budget-heading">LLM 预算阈值</h1>
          <p className="page-sub">
            日/周/月阈值与 warn 百分比；达到阈值时仅告警，绝不停止调用。使用量只读，展示当前花费、状态、
            月末投影与 unpriced 调用；缺失数据显示 N/A，绝不显示 0。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：预算阈值只能在桌面端或 CLI 修改。Kill switch 仍可用。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="budget" />
        <div className="stack">
          <BudgetLoadStatus loading={load.state === 'loading'} />
          {load.state === 'loading' && <p className="status-message">加载预算与用量…</p>}

          {load.state === 'error' && (
            <>
              <BudgetLoadErrorPanel message={load.message} onRetry={form.retry} />
              <BudgetUsagePanel
                data={null}
                reason={load.message ?? '预算读取失败；不会以 0 代替缺失数据。'}
                stale={false}
                refreshing={false}
                notice=""
                hardError
                onRefresh={() => {
                  void form.refreshUsage()
                }}
              />
            </>
          )}

          {load.state === 'ready' && data !== null && (
            <>
              <BudgetUsageStatus message={form.usageLive} />
              <BudgetUsagePanel
                data={data}
                reason={
                  data.usage_quality === 'UNAVAILABLE'
                    ? '用量不可用：成本账本读取失败；阈值仍可编辑，不会以 0 代替缺失数据。'
                    : undefined
                }
                stale={form.usageStale}
                refreshing={form.refreshing}
                saving={form.saving}
                notice={form.usageNotice}
                onRefresh={() => {
                  void form.refreshUsage()
                }}
              />
              <BudgetLastAlertPanel
                alert={data.last_alert}
                degraded={data.usage_quality === 'UNAVAILABLE'}
              />
              <div className="stack">
                <BudgetSaveStatus message={form.liveMessage} />
                <BudgetErrorSummary
                  errorText={form.errorText}
                  invalidFields={form.invalidFields}
                  summaryRef={form.errorSummaryRef}
                />
                <BudgetSaveVisible message={form.statusMessage} />
                <BudgetThresholdsPanel
                  draft={form.draft}
                  errors={form.errors}
                  warnPct={Number(form.draft.warn_pct) || 0}
                  readOnly={!form.editable || form.saving}
                  saving={form.saving}
                  onChange={form.change}
                  onSubmit={(event) => {
                    void form.save(event)
                  }}
                  saveButtonRef={form.saveButtonRef}
                />
              </div>

              {form.unknownOutcome && <BudgetUnknownOutcomeHint />}
            </>
          )}
        </div>
      </div>
    </section>
  )
}
