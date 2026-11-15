import { useEffect, useRef } from 'react'

import type { LLMProviderName } from '../api/settings'
import { LLM_PROVIDERS, LLM_ROLE_NAMES } from '../api/settings'
import { SettingsNotice } from './settings-field'
import { SecretField } from './secret-field'
import type { LLMSettingsForm } from '../hooks/use-llm-settings-form'

const PROVIDER_LABELS: Record<LLMProviderName, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
}

const ROLE_LABELS: Record<string, string> = {
  ANALYSIS: 'analysis',
  ENTRY_RECHECK: 'entry re-check',
  MANAGEMENT: 'management',
  REVIEW: 'review',
}

function fieldError(form: LLMSettingsForm, field: string): string | undefined {
  return form.fieldErrors[field]
}

function fieldTarget(field: string): { id: string; label: string } | null {
  if (field.startsWith('base_urls.')) {
    const provider = field.slice('base_urls.'.length) as LLMProviderName
    return {
      id: `llm-base-url-${provider}`,
      label: `${PROVIDER_LABELS[provider] ?? provider} Base URL`,
    }
  }
  if (field.startsWith('roles.')) {
    const role = field.slice('roles.'.length)
    return { id: `llm-role-${role}`, label: `${role} 模型` }
  }
  if (field === 'max_concurrency') {
    return { id: 'llm-max-concurrency', label: '全局并发' }
  }
  if (field === 'max_concurrency_deepseek') {
    return { id: 'llm-max-concurrency-deepseek', label: 'DeepSeek 并发' }
  }
  return null
}

export function LLMLoadStatus({ state }: { state: 'loading' | 'ready' | 'error' }) {
  const message = state === 'loading' ? '加载 LLM 设置…' : state === 'ready' ? 'LLM 设置已加载' : ''
  return (
    <p
      role="status"
      className="status-message"
      id="settings-llm-load-status"
      data-testid="settings-llm-load-status"
    >
      {message}
    </p>
  )
}

export function LLMLoadErrorPanel({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section className="panel" aria-labelledby="llm-load-error">
      <div className="panel-head">
        <h2 id="llm-load-error">LLM 设置不可用</h2>
      </div>
      <SettingsNotice tone="error">读取失败：{message}</SettingsNotice>
      <button type="button" className="control-btn" onClick={onRetry}>
        重试
      </button>
    </section>
  )
}

function CapabilityChip({
  configured,
  status,
  checkedAt,
}: {
  configured: boolean
  status: string
  checkedAt: string | null
}) {
  if (!configured) {
    return <span className="chip chip--muted">未配置</span>
  }
  const tone =
    status === 'STRICT' ? 'chip--ok' : status === 'DEGRADED' ? 'chip--warn' : 'chip--muted'
  return (
    <span className={`chip ${tone}`} title={checkedAt ?? undefined}>
      {status === 'UNCHECKED' ? '未测试' : <span lang="en">{status}</span>}
    </span>
  )
}

interface ProvidersPanelProps {
  form: LLMSettingsForm
  readOnly: boolean
}

export function LLMProvidersPanel({ form, readOnly }: ProvidersPanelProps) {
  const settings = form.load.settings
  const draft = form.draft
  if (settings === null || draft === null) {
    return null
  }
  return (
    <section className="panel" aria-labelledby="llm-providers-heading">
      <div className="panel-head">
        <h2 id="llm-providers-heading">
          <span lang="en">Providers</span> 与密钥
        </h2>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="llm-active-provider">
          当前 <span lang="en">provider</span>
        </label>
        <select
          id="llm-active-provider"
          className="num-input"
          value={draft.provider ?? ''}
          disabled={readOnly}
          aria-disabled={form.saving || undefined}
          aria-describedby="llm-active-provider-hint"
          onChange={(event) => {
            const value = event.target.value
            form.change({
              provider: value === '' ? null : (value as LLMProviderName),
            })
          }}
        >
          <option value="">未配置</option>
          {LLM_PROVIDERS.map((provider) => (
            <option key={provider} value={provider}>
              {PROVIDER_LABELS[provider]}
            </option>
          ))}
        </select>
        <p className="hint" id="llm-active-provider-hint">
          一个生效 <span lang="en">provider</span> 应用于全部四个角色；不存在{' '}
          <span lang="en">per-role provider</span> 路由。
        </p>
      </div>

      {/* DESIGN 1.4.10 permits a horizontally scrollable region; it must be
          keyboard-scrollable, hence the explicit tab stop. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div className="table-wrap" tabIndex={0} role="region" aria-label="Providers">
        <table className="data">
          <caption className="visually-hidden">Provider 状态、掩码密钥、Base URL 与测试</caption>
          <thead>
            <tr>
              <th scope="col">
                <span lang="en">Provider</span>
              </th>
              <th scope="col">状态</th>
              <th scope="col">
                <span lang="en">Key</span>（<span lang="en">write-only</span>）
              </th>
              <th scope="col">
                <span lang="en">Base URL</span>
              </th>
              <th scope="col">最后测试</th>
              <th scope="col">
                <span lang="en">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {settings.providers.map((row) => {
              const provider = row.provider
              const isActive = draft.provider === provider
              const baseUrlError = fieldError(form, `base_urls.${provider}`)
              const probe = form.probe?.provider === provider ? form.probe : null
              const probeDisabled = readOnly || !isActive || !row.configured
              const probeDescription = !isActive
                ? `llm-probe-active-hint-${provider}`
                : !row.configured
                  ? `llm-probe-hint-${provider}`
                  : undefined
              return (
                <tr key={provider}>
                  <th scope="row" className="sym">
                    {PROVIDER_LABELS[provider]}
                  </th>
                  <td>
                    <CapabilityChip
                      configured={row.configured}
                      status={row.capability?.status ?? 'UNCHECKED'}
                      checkedAt={row.capability?.checked_at ?? null}
                    />
                  </td>
                  <td>
                    <span className="mono" data-testid={`llm-provider-mask-${provider}`}>
                      {row.configured
                        ? (row.masked ??
                          (row.key_source === 'env' ? <span lang="en">env</span> : '已配置'))
                        : '—'}
                    </span>
                    {row.key_source !== 'unset' && (
                      <span className="chip chip--muted" data-testid={`llm-key-source-${provider}`}>
                        <span lang="en">{row.key_source}</span>
                      </span>
                    )}
                  </td>
                  <td>
                    <label className="visually-hidden" htmlFor={`llm-base-url-${provider}`}>
                      {PROVIDER_LABELS[provider]} <span lang="en">Base URL</span>
                    </label>
                    <input
                      id={`llm-base-url-${provider}`}
                      className="settings-input mono"
                      type="url"
                      value={draft.base_urls[provider]}
                      disabled={readOnly}
                      readOnly={form.saving}
                      aria-disabled={form.saving || undefined}
                      aria-invalid={baseUrlError ? true : undefined}
                      aria-describedby={
                        baseUrlError
                          ? `llm-base-url-error-${provider} llm-custom-host-hint`
                          : 'llm-custom-host-hint'
                      }
                      onChange={(event) => {
                        form.change({
                          base_urls: { ...draft.base_urls, [provider]: event.target.value },
                        })
                      }}
                    />
                    {baseUrlError && (
                      <p className="field-error" id={`llm-base-url-error-${provider}`}>
                        错误：{baseUrlError}
                      </p>
                    )}
                  </td>
                  <td className="mono">
                    {row.capability ? (
                      <>
                        {row.capability.checked_at} ·{' '}
                        <span lang="en">{row.capability.notes || row.capability.status}</span>
                      </>
                    ) : (
                      '—'
                    )}
                    {probe?.result && (
                      <span data-testid={`llm-probe-${provider}`}>
                        {' '}
                        <span lang="en">{probe.result.status}</span> · {probe.result.latency_ms} ms
                      </span>
                    )}
                    {probe?.error && (
                      <span className="field-error" id={`llm-probe-error-${provider}`}>
                        {' '}
                        错误：{probe.error}
                      </span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="control-btn"
                      disabled={probeDisabled}
                      aria-disabled={form.probeBusy !== null || undefined}
                      aria-label={`${
                        form.probeBusy === provider ? '测试中…' : '测试连接'
                      }（${PROVIDER_LABELS[provider]}，角色 ANALYSIS）`}
                      aria-describedby={probeDescription}
                      onClick={() => {
                        if (form.probeBusy === null && row.configured && isActive) {
                          void form.runProbe(provider)
                        }
                      }}
                    >
                      {form.probeBusy === provider ? '测试中…' : '测试连接'}
                    </button>
                    <p className="hint">
                      测试角色 <span lang="en">ANALYSIS</span>
                    </p>
                    {!isActive && (
                      <p className="hint" id={`llm-probe-active-hint-${provider}`}>
                        只有当前 provider（
                        {draft.provider ? PROVIDER_LABELS[draft.provider] : '未配置'}）可以测试。
                      </p>
                    )}
                    {isActive && !row.configured && (
                      <p className="hint" id={`llm-probe-hint-${provider}`}>
                        未配置：请先保存该 provider 的密钥。
                      </p>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="stack">
        {settings.providers.map((row) => (
          <SecretField
            key={row.provider}
            provider={row.provider}
            row={row}
            readOnly={readOnly}
            busy={form.keyBusy !== null}
            serverError={form.keyErrors[row.provider]}
            onSave={form.saveKey}
            onDelete={form.deleteKey}
          />
        ))}
      </div>

      <div className="field">
        <label className="field-label" htmlFor="llm-allow-custom">
          <input
            id="llm-allow-custom"
            type="checkbox"
            checked={draft.allow_custom_base_url}
            disabled={readOnly}
            aria-disabled={form.saving || undefined}
            aria-describedby="llm-custom-host-hint"
            onChange={(event) => form.change({ allow_custom_base_url: event.target.checked })}
          />
          允许自定义 Base URL
        </label>
        <p className="hint" id="llm-custom-host-hint">
          默认只接受官方 host 或 loopback；显式勾选后才允许其他 host。
        </p>
      </div>
    </section>
  )
}

export function LLMRoleModelsPanel({ form, readOnly }: ProvidersPanelProps) {
  const draft = form.draft
  const settings = form.load.settings
  if (draft === null || settings === null) {
    return null
  }
  return (
    <section className="panel" aria-labelledby="llm-roles-heading">
      <div className="panel-head">
        <h2 id="llm-roles-heading">角色映射</h2>
      </div>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div className="table-wrap" tabIndex={0} role="region" aria-label="角色模型映射">
        <table className="data">
          <caption className="visually-hidden">四个角色使用的模型与生效 provider</caption>
          <thead>
            <tr>
              <th scope="col">
                <span lang="en">Role</span>
              </th>
              <th scope="col">
                <span lang="en">Provider</span>
              </th>
              <th scope="col">
                <span lang="en">Model</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {LLM_ROLE_NAMES.map((role) => {
              const error = fieldError(form, `roles.${role}`)
              return (
                <tr key={role}>
                  <th scope="row">
                    <span lang="en">{ROLE_LABELS[role]}</span>
                  </th>
                  <td className="mono">
                    {draft.provider ? PROVIDER_LABELS[draft.provider] : '未配置'}
                  </td>
                  <td>
                    <label className="visually-hidden" htmlFor={`llm-role-${role}`}>
                      <span lang="en">{ROLE_LABELS[role]} model</span>
                    </label>
                    <input
                      id={`llm-role-${role}`}
                      className="settings-input mono"
                      value={draft.roles[role]}
                      disabled={readOnly}
                      readOnly={form.saving}
                      aria-disabled={form.saving || undefined}
                      aria-invalid={error ? true : undefined}
                      aria-describedby={error ? `llm-role-error-${role}` : 'llm-role-hint'}
                      onChange={(event) =>
                        form.change({ roles: { ...draft.roles, [role]: event.target.value } })
                      }
                    />
                    {error && (
                      <p className="field-error" id={`llm-role-error-${role}`}>
                        错误：{error}
                      </p>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="hint" id="llm-role-hint">
        无默认模型：留空表示该角色未配置，需要它的决策返回{' '}
        <span lang="en">LLM_MODEL_NOT_CONFIGURED</span>。角色表不包含{' '}
        <span lang="en">temperature</span> 或 <span lang="en">timeout</span>。
      </p>
    </section>
  )
}

export function LLMConcurrencyPanel({ form, readOnly }: ProvidersPanelProps) {
  const draft = form.draft
  if (draft === null) {
    return null
  }
  return (
    <section className="panel" aria-labelledby="llm-concurrency-heading">
      <div className="panel-head">
        <h2 id="llm-concurrency-heading">并发</h2>
      </div>
      <div className="risk-grid">
        <div className="field">
          <label className="field-label" htmlFor="llm-max-concurrency">
            全局并发（1–64）
          </label>
          <input
            id="llm-max-concurrency"
            type="number"
            min={1}
            max={64}
            className="num-input"
            value={draft.max_concurrency}
            disabled={readOnly}
            readOnly={form.saving}
            aria-disabled={form.saving || undefined}
            aria-invalid={fieldError(form, 'max_concurrency') ? true : undefined}
            aria-describedby={
              fieldError(form, 'max_concurrency')
                ? 'llm-max-concurrency-error llm-max-concurrency-hint'
                : 'llm-max-concurrency-hint'
            }
            onChange={(event) => form.change({ max_concurrency: event.target.value })}
          />
          {fieldError(form, 'max_concurrency') && (
            <p className="field-error" id="llm-max-concurrency-error">
              错误：{fieldError(form, 'max_concurrency')}
            </p>
          )}
        </div>
        <div className="field">
          <label className="field-label" htmlFor="llm-max-concurrency-deepseek">
            DeepSeek 并发（1–64）
          </label>
          <input
            id="llm-max-concurrency-deepseek"
            type="number"
            min={1}
            max={64}
            className="num-input"
            value={draft.max_concurrency_deepseek}
            disabled={readOnly}
            readOnly={form.saving}
            aria-disabled={form.saving || undefined}
            aria-invalid={fieldError(form, 'max_concurrency_deepseek') ? true : undefined}
            aria-describedby={
              fieldError(form, 'max_concurrency_deepseek')
                ? 'llm-max-concurrency-deepseek-error llm-max-concurrency-hint'
                : 'llm-max-concurrency-hint'
            }
            onChange={(event) => form.change({ max_concurrency_deepseek: event.target.value })}
          />
          {fieldError(form, 'max_concurrency_deepseek') && (
            <p className="field-error" id="llm-max-concurrency-deepseek-error">
              错误：{fieldError(form, 'max_concurrency_deepseek')}
            </p>
          )}
        </div>
      </div>
      <p className="hint" id="llm-max-concurrency-hint">
        保存后下一次调用即使用新并发上限；此值不改变 <span lang="en">provider SDK</span>。
      </p>
    </section>
  )
}

export function LLMUsagePanel({ form }: { form: LLMSettingsForm }) {
  const usage = form.usage
  const periods = usage.periods
  const cells = [
    { key: 'day' as const, label: 'Today' },
    { key: 'week' as const, label: 'This week' },
    { key: 'month' as const, label: 'This month' },
  ]
  return (
    <section className="panel" aria-labelledby="llm-usage-heading">
      <div className="panel-head">
        <h2 id="llm-usage-heading">
          <span lang="en">Usage snapshot</span>
        </h2>
      </div>
      <dl className="stat-grid">
        {cells.map((cell) => {
          const row = periods[cell.key]
          return (
            <div className="stat" key={cell.key}>
              <dt>
                <span lang="en">{cell.label}</span>
              </dt>
              <dd data-testid={`llm-usage-${cell.key}`}>
                {usage.state === 'ready' && row
                  ? `$${row.spend_usd}`
                  : usage.state === 'error'
                    ? 'N/A'
                    : '…'}
                <small className="stat-note">
                  {row ? (
                    <>
                      <span lang="en">{row.call_count} calls</span> ·{' '}
                      <span lang="en">{row.budget_state}</span>
                    </>
                  ) : usage.state === 'error' ? (
                    'usage 不可用'
                  ) : (
                    ''
                  )}
                </small>
              </dd>
            </div>
          )
        })}
        <div className="stat">
          <dt>
            <span lang="en">Unpriced calls</span>
          </dt>
          <dd data-testid="llm-usage-unpriced">
            {usage.unpriced === null ? 'N/A' : usage.unpriced}
            <small className="stat-note">
              <span lang="en">unknown model · surfaced, never zeroed</span>
            </small>
          </dd>
        </div>
      </dl>
      {usage.state === 'error' && (
        <p className="field-error" role="status">
          Usage 暂不可用：{usage.message ?? '未知错误'}；不会把缺失值显示为 0。
        </p>
      )}
    </section>
  )
}

export function LLMSaveStatus({ message }: { message: string }) {
  return (
    <p
      className="status-message"
      id="settings-llm-save-status"
      role="status"
      aria-live="polite"
      data-testid="settings-llm-save-status"
    >
      {message}
    </p>
  )
}

export function LLMErrorSummary({ form }: { form: LLMSettingsForm }) {
  const entries = Object.entries(form.fieldErrors)
  const keyEntries = (
    Object.entries(form.keyErrors) as [LLMProviderName, string | undefined][]
  ).filter((entry): entry is [LLMProviderName, string] => Boolean(entry[1]))
  const probeError = form.probe?.error
    ? { provider: form.probe.provider, message: form.probe.error }
    : null
  const summaryRef = useRef<HTMLDivElement>(null)
  const wasError = useRef(false)
  const hasError =
    Boolean(form.errorText) ||
    entries.length > 0 ||
    keyEntries.length > 0 ||
    probeError !== null

  useEffect(() => {
    // Focus only when an error first appears. Editing an invalid field can
    // shrink ``entries`` while ``errorText`` persists; re-focusing then would
    // yank the caret out of the input the operator is typing in.
    const entered = hasError && !wasError.current
    wasError.current = hasError
    if (!entered) {
      return
    }
    // DESIGN A7: focus moves to the error summary on a failed submit, an API
    // refusal, and a key/probe/delete failure (all set errorText).
    const handle = requestAnimationFrame(() => summaryRef.current?.focus())
    return () => cancelAnimationFrame(handle)
  }, [hasError])

  if (!hasError) {
    return null
  }

  function focusKeyInput(provider: LLMProviderName): void {
    document.getElementById(`llm-key-input-${provider}`)?.focus()
  }

  return (
    <div
      role="alert"
      className="status-message status-message--error"
      data-testid="llm-error-summary"
      ref={summaryRef}
      tabIndex={-1}
    >
      <p>错误：{form.errorText || '部分字段无效，未保存。'}</p>
      {entries.length > 0 && (
        <ul>
          {entries.map(([field, message]) => {
            const target = fieldTarget(field)
            return (
              <li key={field}>
                {target ? (
                  <a
                    href={`#${target.id}`}
                    onClick={(event) => {
                      event.preventDefault()
                      document.getElementById(target.id)?.focus()
                    }}
                  >
                    {target.label}
                  </a>
                ) : (
                  <span>{field}</span>
                )}
                ：{message}
              </li>
            )
          })}
        </ul>
      )}
      {(keyEntries.length > 0 || probeError !== null) && (
        <ul>
          {keyEntries.map(([provider, message]) => (
            <li key={`key-${provider}`}>
              <a
                href={`#llm-key-input-${provider}`}
                onClick={(event) => {
                  event.preventDefault()
                  focusKeyInput(provider)
                }}
              >
                {PROVIDER_LABELS[provider]} 密钥
              </a>
              ：{message}
            </li>
          ))}
          {probeError !== null && (
            <li key="probe-error">
              <a
                href={`#llm-key-input-${probeError.provider}`}
                onClick={(event) => {
                  event.preventDefault()
                  focusKeyInput(probeError.provider)
                }}
              >
                {PROVIDER_LABELS[probeError.provider]} 测试连接
              </a>
              ：{probeError.message}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
