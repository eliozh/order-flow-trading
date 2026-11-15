import { SettingsSectionNav } from '../components/settings-section-nav'
import {
  ScanCurrentPanel,
  ScanFormPanel,
  ScanLoadErrorPanel,
  ScanLoadStatus,
} from '../components/scan-settings-panels'
import {
  type UseScanSettingsFormOptions,
  useScanSettingsForm,
} from '../hooks/use-scan-settings-form'

export type SettingsScanPageProps = UseScanSettingsFormOptions

/** `/settings/scan`: the F10d scan-period form (4h/8h only). */
export function SettingsScanPage(props: SettingsScanPageProps) {
  const form = useScanSettingsForm(props)
  const { load, readonly } = form

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
            读取生效的扫描周期、来源与下一个 UTC 边界；修改仅支持 4h 与 8h。仅在值真正改变时写入一条
            audit 记录；值未变更的保存不会写入新 audit。daemon 在下一个边界生效。
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
          <ScanLoadStatus loading={load.state === 'loading'} />
          {load.state === 'loading' && <p className="status-message">加载扫描设置…</p>}

          {load.state === 'error' && (
            <ScanLoadErrorPanel message={load.message} onRetry={form.retry} />
          )}

          {load.state === 'ready' && load.settings && (
            <>
              <ScanCurrentPanel settings={load.settings} />
              <ScanFormPanel form={form} settings={load.settings} />
            </>
          )}
        </div>
      </div>
    </section>
  )
}
