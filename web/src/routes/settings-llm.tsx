import {
  LLMConcurrencyPanel,
  LLMErrorSummary,
  LLMLoadErrorPanel,
  LLMLoadStatus,
  LLMProvidersPanel,
  LLMRoleModelsPanel,
  LLMSaveStatus,
  LLMUsagePanel,
} from '../components/llm-settings-panels'
import { SettingsSectionNav } from '../components/settings-section-nav'
import { type UseLLMSettingsFormOptions, useLLMSettingsForm } from '../hooks/use-llm-settings-form'

export type SettingsLLMPageProps = UseLLMSettingsFormOptions

/**
 * `/settings/llm` (F10a): one active provider, four role models, official base
 * URLs, concurrency, write-only keys, the capability probe, and usage.
 *
 * There is deliberately no reveal control and no temperature/timeout field.
 */
export function SettingsLLMPage(props: SettingsLLMPageProps) {
  const form = useLLMSettingsForm(props)
  const { load, readonly, draft } = form
  const hasKeyEdit = load.state === 'ready' && draft !== null

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-llm-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <span lang="en">Settings / llm</span>
          </p>
          <h1 id="settings-llm-heading">
            <span lang="en">LLM providers</span> 与模型
          </h1>
          <p className="page-sub">
            OpenAI / Anthropic / DeepSeek 密钥只写；按角色选择模型；测试连接显示严格 schema 或降级
            状态。密钥永不回显，只显示固定掩码与最后 4 位。保存后下一次调用立即使用新值。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：LLM provider 与密钥只能在桌面端或 CLI 修改。Kill switch 仍可用。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="llm" />
        <div className="stack">
          <LLMLoadStatus state={load.state} />

          {load.state === 'error' && (
            <LLMLoadErrorPanel message={load.message ?? '未知错误'} onRetry={form.retry} />
          )}

          {hasKeyEdit && (
            <>
              <form
                className="stack"
                aria-label="LLM 设置"
                noValidate
                onSubmit={(event) => void form.save(event)}
              >
                <LLMSaveStatus message={form.liveMessage} />
                <LLMErrorSummary form={form} />
                <LLMProvidersPanel form={form} readOnly={!form.editable} />
                <LLMRoleModelsPanel form={form} readOnly={!form.editable} />
                <LLMConcurrencyPanel form={form} readOnly={!form.editable} />
                <div className="action-row">
                  <button
                    type="submit"
                    className="control-btn"
                    disabled={!form.editable}
                    aria-disabled={form.saving || undefined}
                  >
                    {form.saving ? '保存中…' : '保存 LLM 设置'}
                  </button>
                  <span className="hint">
                    一次保存写入一条 <span lang="en">CONFIG_UPDATED</span>{' '}
                    <span lang="en">audit</span>；未变更的保存不写入。
                  </span>
                </div>
              </form>

              <LLMUsagePanel form={form} />
            </>
          )}
        </div>
      </div>
    </section>
  )
}
