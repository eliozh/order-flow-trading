import { useCallback, useEffect, useState } from 'react'

import type {
  LLMKeyStatusEnvelope,
  LLMProviderName,
  LLMRoleName,
  LLMSettingsData,
  LLMSettingsEnvelope,
} from '../api/settings'
import { SecretField } from '../components/secret-field'
import { settleOnboardingMutation } from './onboarding-idempotency'
import { useOnboardingLoad } from './use-onboarding-load'

const PROVIDER_LABELS: Record<LLMProviderName, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
}

export interface LLMStepProps {
  fetchLLMSettingsFn: (signal?: AbortSignal) => Promise<LLMSettingsEnvelope>
  setLLMKeyFn: (
    provider: LLMProviderName,
    apiKey: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<LLMKeyStatusEnvelope>
  idempotencyKeyFn: (label: string) => string
  clearIdempotencyKeyFn: (label: string) => void
  rotateIdempotencyKeyFn: (label: string) => string
  announce: (message: string) => void
  onComplete: () => void
  completing: boolean
  narrow: boolean
}

/**
 * Step 2: write-only provider keys and the read-only role map.
 *
 * A save never exposes the typed value: the shared `SecretField` clears it on
 * success and renders only the server mask. The finish control is
 * `aria-disabled` (not natively disabled) when no provider is configured, and
 * points at the documented consequence, so a keyboard user hears why it cannot
 * proceed.
 */
export function LLMStep({
  fetchLLMSettingsFn,
  setLLMKeyFn,
  idempotencyKeyFn,
  clearIdempotencyKeyFn,
  rotateIdempotencyKeyFn,
  announce,
  onComplete,
  completing,
  narrow,
}: LLMStepProps) {
  const [saving, setSaving] = useState<LLMProviderName | null>(null)
  const [keyErrors, setKeyErrors] = useState<
    Partial<Record<LLMProviderName, { message: string; refusal: boolean }>>
  >({})
  const [savedOverrides, setSavedOverrides] = useState<
    Partial<Record<LLMProviderName, LLMKeyStatusEnvelope['data']>>
  >({})

  const loader = useCallback(
    async (signal: AbortSignal): Promise<LLMSettingsData> => {
      const envelope = await fetchLLMSettingsFn(signal)
      if (!envelope.data) {
        throw new Error('LLM 设置响应为空。')
      }
      return envelope.data
    },
    [fetchLLMSettingsFn],
  )
  const load = useOnboardingLoad(loader)
  const { state } = load

  // A reload is authoritative: drop the post-save overrides.
  useEffect(() => {
    setSavedOverrides({})
  }, [load.data])

  useEffect(() => {
    if (state === 'ready') {
      announce('LLM providers 已加载。')
    } else if (state === 'error') {
      announce('LLM providers 不可用。')
    }
  }, [state, announce])

  async function saveKey(provider: LLMProviderName, value: string): Promise<boolean> {
    if (narrow || state !== 'ready') {
      return false
    }
    const label = `llm.${provider}`
    setSaving(provider)
    setKeyErrors({})
    try {
      const envelope = await setLLMKeyFn(provider, value, idempotencyKeyFn(label))
      const status = envelope.data
      if (!status) {
        throw new Error('密钥保存响应为空。')
      }
      // A response for a different provider is not a confirmation of this save.
      if (status.provider !== provider) {
        throw new Error('密钥保存响应与请求的 provider 不一致；结果未知。')
      }
      clearIdempotencyKeyFn(label)
      setSavedOverrides((previous) => ({ ...previous, [provider]: status }))
      announce(`${PROVIDER_LABELS[provider]} 密钥已保存；仅显示掩码。`)
      return true
    } catch (error: unknown) {
      const failure = settleOnboardingMutation(error, label, {
        rotate: rotateIdempotencyKeyFn,
        clear: clearIdempotencyKeyFn,
      })
      // Keep only this provider's error so at most one alert is live.
      setKeyErrors({
        [provider]: {
          message: failure.message,
          refusal: !failure.unknown && !failure.conflict,
        },
      })
      return false
    } finally {
      setSaving(null)
    }
  }

  const data = load.data
  const providers =
    data?.providers.map((row) => {
      const saved = savedOverrides[row.provider]
      return saved
        ? {
            ...row,
            configured: saved.configured,
            key_source: saved.key_source,
            masked: saved.masked,
            fingerprint: saved.fingerprint,
            updated_at: saved.updated_at,
            updated_by: saved.updated_by,
          }
        : row
    }) ?? []
  const anyConfigured = providers.some((row) => row.configured)
  const canFinish = state === 'ready' && anyConfigured && !narrow && !completing
  // `#llm-no-provider` exists only in the ready state; keep the IDREF valid
  // (and pointing at the always-present hint) while loading or errored.
  const finishDescription =
    anyConfigured || state !== 'ready' ? 'llm-finish-hint' : 'llm-no-provider'

  return (
    <div className="stack">
      <section className="panel" aria-labelledby="onboarding-providers-heading">
        <div className="panel-head">
          <h2 id="onboarding-providers-heading">Providers</h2>
        </div>
        {state === 'loading' && (
          <p className="status-message" role="status">
            加载 LLM providers…
          </p>
        )}
        {state === 'error' && (
          <>
            <p className="status-message status-message--error" role="alert">
              LLM providers 不可用：{load.message ?? '未知错误'}
            </p>
            <div className="settings-actions">
              <button type="button" className="control-btn" onClick={load.retry}>
                重试
              </button>
            </div>
          </>
        )}
        {state === 'ready' && data && (
          <>
            <p className="status-message" data-testid="llm-provider-summary">
              {providers.filter((row) => row.configured).length} 个 provider 已配置 ·{' '}
              {providers.filter((row) => !row.configured).length} 个未配置
            </p>
            <div className="stack onboarding-pad">
              {providers.map((row) => (
                <SecretField
                  key={row.provider}
                  provider={row.provider}
                  row={row}
                  readOnly={narrow}
                  busy={saving === row.provider}
                  serverError={keyErrors[row.provider]?.message}
                  serverErrorRefusal={keyErrors[row.provider]?.refusal}
                  announceError
                  onSave={saveKey}
                />
              ))}
            </div>
          </>
        )}
      </section>

      {!anyConfigured && state === 'ready' && (
        <p
          className="status-message"
          role="status"
          id="llm-no-provider"
          data-testid="llm-no-provider"
        >
          还没有配置任何 provider。没有 provider 就无法生成符合 schema 的决策（分析、入场复核、
          仓位管理）；扫描报告仍可查看，但不会产生新的 LLM 输出。配置至少一个密钥后才能继续。
        </p>
      )}

      {state === 'ready' && data && (
        <section className="panel" aria-labelledby="onboarding-roles-heading">
          <div className="panel-head">
            <h2 id="onboarding-roles-heading">角色映射（只读）</h2>
          </div>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table */}
          <div className="table-wrap" role="region" aria-label="角色到模型映射" tabIndex={0}>
            <table className="data-table">
              <caption>角色到模型映射</caption>
              <thead>
                <tr>
                  <th scope="col">Role</th>
                  <th scope="col">Model</th>
                  <th scope="col">Provider base URL</th>
                </tr>
              </thead>
              <tbody>
                {(Object.keys(data.roles) as (keyof typeof data.roles)[]).map(
                  (role: LLMRoleName) => (
                    <tr key={role}>
                      <td className="mono">{role}</td>
                      <td className="mono">{data.roles[role] ?? '—'}</td>
                      <td className="mono">
                        {data.provider
                          ? data.base_urls[data.provider]
                          : Object.values(data.base_urls)[0]}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <form
        className="settings-actions"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          if (canFinish) {
            onComplete()
          }
        }}
      >
        <button
          type="submit"
          className="control-btn"
          disabled={narrow}
          aria-disabled={!canFinish || undefined}
          aria-describedby={finishDescription}
        >
          {completing ? '保存中…' : '保存并继续'}
        </button>
        <span className="hint" id="llm-finish-hint">
          至少需要 1 个 provider；否则无法生成符合 schema 的决策。
        </span>
      </form>
    </div>
  )
}
