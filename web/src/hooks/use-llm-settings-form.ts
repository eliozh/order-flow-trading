import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  clearLLMProviderKey,
  clearLLMSettingsKey,
  deleteLLMKey,
  type LLMKeyStatus,
  type LLMKeyStatusEnvelope,
  type LLMProviderName,
  type LLMRoleName,
  type LLMSettingsData,
  type LLMSettingsEnvelope,
  type LLMSettingsPatch,
  type LLMTestEnvelope,
  type LLMTestResult,
  type LLMUsageData,
  type LLMUsageEnvelope,
  type LMUsagePeriod,
  llmProviderKey,
  llmSettingsKey,
  probeLLM,
  fetchLLMSettings,
  fetchLLMUsage,
  rotateLLMProviderKey,
  rotateLLMSettingsKey,
  setLLMKey,
  SettingsApiError,
  updateLLMSettings,
} from '../api/settings'
import { useSettingsReadonly } from '../components/settings-field'

export interface LLMDraft {
  provider: LLMProviderName | null
  roles: Record<LLMRoleName, string>
  base_urls: Record<LLMProviderName, string>
  allow_custom_base_url: boolean
  max_concurrency: string
  max_concurrency_deepseek: string
}

export interface LLMLoad {
  state: 'loading' | 'ready' | 'error'
  settings: LLMSettingsData | null
  message?: string
}

export interface LLMUsageLoad {
  state: 'loading' | 'ready' | 'error'
  periods: Partial<Record<LMUsagePeriod, LLMUsageData>>
  unpriced: number | null
  message?: string
}

export interface LLMProbeState {
  provider: LLMProviderName
  result: LLMTestResult | null
  error: string
}

export interface UseLLMSettingsFormOptions {
  fetchSettingsFn?: (signal?: AbortSignal) => Promise<LLMSettingsEnvelope>
  updateSettingsFn?: (
    patch: LLMSettingsPatch,
    key: string,
    signal?: AbortSignal,
  ) => Promise<LLMSettingsEnvelope>
  setKeyFn?: (
    provider: LLMProviderName,
    apiKey: string,
    key: string,
    signal?: AbortSignal,
  ) => Promise<LLMKeyStatusEnvelope>
  deleteKeyFn?: (
    provider: LLMProviderName,
    key: string,
    signal?: AbortSignal,
  ) => Promise<LLMKeyStatusEnvelope>
  probeFn?: (
    provider: LLMProviderName,
    role: LLMRoleName,
    signal?: AbortSignal,
  ) => Promise<LLMTestEnvelope>
  fetchUsageFn?: (period: LMUsagePeriod, signal?: AbortSignal) => Promise<LLMUsageEnvelope>
  isCompact?: boolean
  settingsKeyFn?: () => string
  clearSettingsKeyFn?: () => void
  rotateSettingsKeyFn?: () => string
  providerKeyFn?: (provider: LLMProviderName) => string
  rotateProviderKeyFn?: (provider: LLMProviderName) => string
  clearProviderKeyFn?: (provider: LLMProviderName) => void
}

export interface LLMSettingsForm {
  readonly: boolean
  load: LLMLoad
  usage: LLMUsageLoad
  draft: LLMDraft | null
  editable: boolean
  saving: boolean
  keyBusy: LLMProviderName | null
  probeBusy: LLMProviderName | null
  probe: LLMProbeState | null
  statusMessage: string
  liveMessage: string
  errorText: string
  fieldErrors: Record<string, string>
  keyErrors: Partial<Record<LLMProviderName, string>>
  change: (patch: Partial<LLMDraft>) => void
  save: (event: FormEvent<HTMLFormElement>) => Promise<void>
  saveKey: (provider: LLMProviderName, value: string) => Promise<boolean>
  deleteKey: (provider: LLMProviderName) => Promise<void>
  runProbe: (provider: LLMProviderName) => Promise<void>
  retry: () => void
}

function draftFrom(settings: LLMSettingsData): LLMDraft {
  return {
    provider: settings.provider,
    roles: {
      ANALYSIS: settings.roles.ANALYSIS ?? '',
      ENTRY_RECHECK: settings.roles.ENTRY_RECHECK ?? '',
      MANAGEMENT: settings.roles.MANAGEMENT ?? '',
      REVIEW: settings.roles.REVIEW ?? '',
    },
    base_urls: { ...settings.base_urls },
    allow_custom_base_url: settings.allow_custom_base_url,
    max_concurrency: String(settings.concurrency.global),
    max_concurrency_deepseek: String(settings.concurrency.deepseek),
  }
}

function parseLimit(value: string, field: string): number {
  const parsed = Number(value.trim())
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 64) {
    throw new FieldError(field, '并发必须是 1–64 的整数。')
  }
  return parsed
}

class FieldError extends Error {
  readonly field: string
  constructor(field: string, message: string) {
    super(message)
    this.field = field
  }
}

/** Load/save state for `/settings/llm`; keys never enter this hook. */
export function useLLMSettingsForm({
  fetchSettingsFn = fetchLLMSettings,
  updateSettingsFn = updateLLMSettings,
  setKeyFn = setLLMKey,
  deleteKeyFn = deleteLLMKey,
  probeFn = probeLLM,
  fetchUsageFn = fetchLLMUsage,
  isCompact,
  settingsKeyFn = llmSettingsKey,
  clearSettingsKeyFn = clearLLMSettingsKey,
  rotateSettingsKeyFn = rotateLLMSettingsKey,
  providerKeyFn = llmProviderKey,
  rotateProviderKeyFn = rotateLLMProviderKey,
  clearProviderKeyFn = clearLLMProviderKey,
}: UseLLMSettingsFormOptions = {}): LLMSettingsForm {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<LLMLoad>({ state: 'loading', settings: null })
  const [usage, setUsage] = useState<LLMUsageLoad>({
    state: 'loading',
    periods: {},
    unpriced: null,
  })
  const [draft, setDraft] = useState<LLMDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const [keyBusy, setKeyBusy] = useState<LLMProviderName | null>(null)
  const [probeBusy, setProbeBusy] = useState<LLMProviderName | null>(null)
  const [probe, setProbe] = useState<LLMProbeState | null>(null)
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorText, setErrorText] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [keyErrors, setKeyErrors] = useState<Partial<Record<LLMProviderName, string>>>({})
  const [attempt, setAttempt] = useState(0)
  const mounted = useRef(true)
  const inFlight = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchSettingsFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', settings: null, message: 'LLM 设置响应为空。' })
          return
        }
        setLoad({ state: 'ready', settings: data })
        setDraft(draftFrom(data))
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
  }, [fetchSettingsFn, attempt])

  const loadUsage = useRef<(signal?: AbortSignal) => Promise<void>>(async () => {})
  loadUsage.current = async (signal?: AbortSignal) => {
    const periods: LMUsagePeriod[] = ['day', 'week', 'month']
    try {
      const results = await Promise.all(periods.map((period) => fetchUsageFn(period, signal)))
      if (!mounted.current) {
        return
      }
      const byPeriod: Partial<Record<LMUsagePeriod, LLMUsageData>> = {}
      for (const result of results) {
        if (result.data) {
          byPeriod[result.data.period] = result.data
        }
      }
      setUsage({
        state: 'ready',
        periods: byPeriod,
        unpriced: byPeriod.month?.unpriced_count ?? byPeriod.week?.unpriced_count ?? 0,
      })
    } catch (error: unknown) {
      if (isAbortError(error)) {
        return
      }
      setUsage((previous) => ({
        ...previous,
        state: 'error',
        message: errorMessage(error),
      }))
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    void loadUsage.current(controller.signal)
    return () => controller.abort()
  }, [])

  function change(patch: Partial<LLMDraft>): void {
    if (inFlight.current) {
      return
    }
    setDraft((previous) => (previous === null ? previous : { ...previous, ...patch }))
    const patched = Object.keys(patch)
    const clearsFieldError = Object.keys(fieldErrors).some((field) =>
      patched.some((key) => field === key || field.startsWith(`${key}.`)),
    )
    setFieldErrors((previous) => {
      const next = { ...previous }
      for (const key of patched) {
        for (const field of Object.keys(next)) {
          // API details are keyed ``base_urls.<provider>`` / ``roles.<ROLE>``,
          // while a patch is keyed by the top-level document field.
          if (field === key || field.startsWith(`${key}.`)) {
            delete next[field]
          }
        }
      }
      return next
    })
    if (clearsFieldError) {
      // The headline describes the refusal that produced the field error; once
      // the operator edits the field it is stale and must not stay on screen.
      setErrorText('')
    }
  }

  function applySettings(data: LLMSettingsData): void {
    setLoad({ state: 'ready', settings: data })
    setDraft(draftFrom(data))
  }

  function replaceProvider(status: LLMKeyStatus): void {
    setLoad((previous) => {
      if (previous.settings === null) {
        return previous
      }
      const providers = previous.settings.providers.map((row) =>
        row.provider === status.provider
          ? {
              ...row,
              configured: status.configured,
              key_source: status.key_source,
              masked: status.masked,
              fingerprint: status.fingerprint,
              updated_at: status.updated_at,
              updated_by: status.updated_by,
            }
          : row,
      )
      return { ...previous, settings: { ...previous.settings, providers } }
    })
  }

  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (!editable() || inFlight.current || saving || draft === null) {
      return
    }
    if (keyBusy !== null || probeBusy !== null) {
      // A settings PUT response replaces the whole document, including the
      // provider rows a concurrent key/probe mutation is updating.
      setErrorText('另一个密钥或测试操作仍在进行中，请稍后再保存。')
      return
    }
    setErrorText('')
    setFieldErrors({})
    setStatusMessage('')
    let patch: LLMSettingsPatch | null = null
    try {
      patch = {
        provider: draft.provider,
        roles: {
          ANALYSIS: draft.roles.ANALYSIS.trim() || null,
          ENTRY_RECHECK: draft.roles.ENTRY_RECHECK.trim() || null,
          MANAGEMENT: draft.roles.MANAGEMENT.trim() || null,
          REVIEW: draft.roles.REVIEW.trim() || null,
        },
        base_urls: { ...draft.base_urls },
        allow_custom_base_url: draft.allow_custom_base_url,
        max_concurrency: parseLimit(draft.max_concurrency, 'max_concurrency'),
        max_concurrency_deepseek: parseLimit(
          draft.max_concurrency_deepseek,
          'max_concurrency_deepseek',
        ),
      }
    } catch (error: unknown) {
      if (error instanceof FieldError) {
        setFieldErrors({ [error.field]: error.message })
        setErrorText(error.message)
        return
      }
      throw error
    }
    if (patch === null) {
      return
    }
    inFlight.current = true
    setSaving(true)
    setLiveMessage('保存中…')
    try {
      const envelope = await updateSettingsFn(patch, settingsKeyFn())
      clearSettingsKeyFn()
      if (!mounted.current || !envelope.data) {
        return
      }
      applySettings(envelope.data)
      const message = `已保存 LLM 设置；下一次分析/复核/管理调用立即使用新值，无需重启。`
      setStatusMessage(message)
      setLiveMessage(message)
      await loadUsage.current()
    } catch (error: unknown) {
      if (isAbortError(error) || !mounted.current) {
        return
      }
      const message = errorMessage(error)
      setLiveMessage('')
      if (error instanceof SettingsApiError && error.kind === 'refusal') {
        if (error.code === 'IDEMPOTENCY_CONFLICT') {
          rotateSettingsKeyFn()
        } else {
          clearSettingsKeyFn()
        }
        const details = Object.fromEntries(
          error.details
            .filter((detail) => typeof detail.field === 'string')
            .flatMap((detail) => {
              const field = detail.field as string
              const detailMessage = detail.message ?? message
              if (field === 'concurrency') {
                // The server validates both bounds under one field name.
                return [
                  ['max_concurrency', detailMessage],
                  ['max_concurrency_deepseek', detailMessage],
                ] as [string, string][]
              }
              return [[field, detailMessage]] as [string, string][]
            }),
        )
        setFieldErrors(details)
        setErrorText(message)
      } else {
        setErrorText(
          `结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`,
        )
      }
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setSaving(false)
      }
    }
  }

  function editable(): boolean {
    return load.state === 'ready' && draft !== null && !readonly
  }

  async function saveKey(provider: LLMProviderName, value: string): Promise<boolean> {
    if (readonly || keyBusy !== null || inFlight.current || saving) {
      return false
    }
    setKeyBusy(provider)
    setErrorText('')
    setKeyErrors((previous) => ({ ...previous, [provider]: '' }))
    setLiveMessage('保存密钥中…')
    try {
      const envelope = await setKeyFn(provider, value, providerKeyFn(provider))
      clearProviderKeyFn(provider)
      if (!mounted.current || !envelope.data) {
        return false
      }
      replaceProvider(envelope.data)
      const message = `${provider} 密钥已保存 · ${envelope.data.masked ?? '已配置'}（仅显示掩码）`
      setStatusMessage(message)
      setLiveMessage(message)
      return true
    } catch (error: unknown) {
      if (isAbortError(error) || !mounted.current) {
        return false
      }
      const message = errorMessage(error)
      setLiveMessage('')
      if (error instanceof SettingsApiError && error.code === 'IDEMPOTENCY_CONFLICT') {
        rotateProviderKeyFn(provider)
      } else if (error instanceof SettingsApiError && error.kind === 'refusal') {
        clearProviderKeyFn(provider)
      }
      setKeyErrors((previous) => ({ ...previous, [provider]: message }))
      setErrorText(message)
      return false
    } finally {
      if (mounted.current) {
        setKeyBusy(null)
      }
    }
  }

  async function deleteKey(provider: LLMProviderName): Promise<void> {
    if (readonly || keyBusy !== null || inFlight.current || saving) {
      return
    }
    setKeyBusy(provider)
    setErrorText('')
    setKeyErrors((previous) => ({ ...previous, [provider]: '' }))
    try {
      const envelope = await deleteKeyFn(provider, providerKeyFn(provider))
      clearProviderKeyFn(provider)
      if (!mounted.current || !envelope.data) {
        return
      }
      replaceProvider(envelope.data)
      const message = envelope.data.configured
        ? `${provider} 存储的密钥已删除；环境变量密钥仍然生效。`
        : `${provider} 密钥已删除；该 provider 未配置，不会回退到其他密钥。`
      setStatusMessage(message)
      setLiveMessage(message)
      setProbe((previous) => (previous?.provider === provider ? null : previous))
    } catch (error: unknown) {
      if (isAbortError(error) || !mounted.current) {
        return
      }
      if (error instanceof SettingsApiError && error.code === 'IDEMPOTENCY_CONFLICT') {
        rotateProviderKeyFn(provider)
      }
      const message = errorMessage(error)
      setKeyErrors((previous) => ({ ...previous, [provider]: message }))
      setErrorText(message)
    } finally {
      if (mounted.current) {
        setKeyBusy(null)
      }
    }
  }

  async function runProbe(provider: LLMProviderName): Promise<void> {
    if (readonly || probeBusy !== null || keyBusy !== null || inFlight.current || saving) {
      return
    }
    setProbeBusy(provider)
    setErrorText('')
    try {
      const envelope = await probeFn(provider, 'ANALYSIS')
      if (!mounted.current || !envelope.data) {
        return
      }
      setProbe({ provider, result: envelope.data, error: '' })
      setLiveMessage(`${provider} 测试：${envelope.data.status} · ${envelope.data.latency_ms} ms`)
      await loadUsage.current()
    } catch (error: unknown) {
      if (isAbortError(error) || !mounted.current) {
        return
      }
      const message = errorMessage(error)
      setProbe({ provider, result: null, error: message })
      setErrorText(message)
    } finally {
      if (mounted.current) {
        setProbeBusy(null)
      }
    }
  }

  return {
    readonly,
    load,
    usage,
    draft,
    editable: editable(),
    saving,
    keyBusy,
    probeBusy,
    probe,
    statusMessage,
    liveMessage,
    errorText,
    fieldErrors,
    keyErrors,
    change,
    save,
    saveKey,
    deleteKey,
    runProbe,
    retry: () => setAttempt((value) => value + 1),
  }
}
