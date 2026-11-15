import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import {
  type LLMKeyStatus,
  type LLMKeyStatusEnvelope,
  type LLMSettingsData,
  type LLMSettingsEnvelope,
  type LLMTestEnvelope,
  type LLMUsageData,
  type LLMUsageEnvelope,
  type LMUsagePeriod,
  SettingsApiError,
} from '../../api/settings'
import { SettingsLLMPage } from '../settings-llm'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const SECRET = 'sk-web-SENTINEL-DO-NOT-RENDER-4f2a'

function settingsData(overrides: Partial<LLMSettingsData> = {}): LLMSettingsData {
  return {
    provider: 'openai',
    providers: [
      {
        provider: 'openai',
        configured: true,
        key_source: 'encrypted_file',
        masked: '****4f2a',
        fingerprint: 'f'.repeat(64),
        updated_at: '2026-11-06T09:00:00.000Z',
        updated_by: 'web',
        base_url: 'https://api.openai.com/v1',
        capability: null,
      },
      {
        provider: 'anthropic',
        configured: false,
        key_source: 'unset',
        masked: null,
        fingerprint: null,
        updated_at: null,
        updated_by: null,
        base_url: 'https://api.anthropic.com',
        capability: null,
      },
      {
        provider: 'deepseek',
        configured: false,
        key_source: 'unset',
        masked: null,
        fingerprint: null,
        updated_at: null,
        updated_by: null,
        base_url: 'https://api.deepseek.com',
        capability: null,
      },
    ],
    roles: {
      ANALYSIS: 'gpt-a',
      ENTRY_RECHECK: null,
      MANAGEMENT: null,
      REVIEW: null,
    },
    base_urls: {
      openai: 'https://api.openai.com/v1',
      anthropic: 'https://api.anthropic.com',
      deepseek: 'https://api.deepseek.com',
    },
    allow_custom_base_url: false,
    concurrency: { global: 2, deepseek: 1 },
    source: 'user',
    updated_at: '2026-11-06T09:00:00.000Z',
    updated_by: 'web',
    ...overrides,
  }
}

function settingsEnvelope(data: LLMSettingsData): LLMSettingsEnvelope {
  return { ok: true, data, error: null, meta }
}

function usageData(period: LMUsagePeriod): LLMUsageData {
  const spend = period === 'day' ? '1.00' : period === 'week' ? '2.00' : '3.00'
  return {
    period,
    period_key: `2026-11-06/${period}`,
    spend_usd: spend,
    call_count: 2,
    unpriced_count: period === 'month' ? 3 : 1,
    budget_state: 'OK',
    thresholds: {
      daily_usd: '10.00',
      weekly_usd: '50.00',
      monthly_usd: '150.00',
      warn_pct: 80,
    },
    by_provider: [],
    by_model: [],
  }
}

function usageEnvelope(period: LMUsagePeriod): LLMUsageEnvelope {
  return { ok: true, data: usageData(period), error: null, meta }
}

function keyStatus(configured: boolean, masked: string | null): LLMKeyStatus {
  return {
    provider: 'openai',
    configured,
    key_source: configured ? 'encrypted_file' : 'unset',
    masked,
    fingerprint: configured ? 'f'.repeat(64) : null,
    updated_at: configured ? '2026-11-06T10:00:00.000Z' : null,
    updated_by: configured ? 'web' : null,
  }
}

interface RenderOptions {
  data?: LLMSettingsData
  update?: (patch: unknown, key: string) => Promise<LLMSettingsEnvelope>
  setKey?: (provider: string, value: string, key: string) => Promise<LLMKeyStatusEnvelope>
  deleteKey?: (provider: string, key: string) => Promise<LLMKeyStatusEnvelope>
  probe?: (provider: string, role: string) => Promise<LLMTestEnvelope>
  isCompact?: boolean
  rotateSettingsKey?: () => string
  rotateProviderKey?: (provider: string) => string
}

async function renderPage(options: RenderOptions = {}) {
  const data = options.data ?? settingsData()
  const update =
    options.update ??
    (async () => settingsEnvelope({ ...data, updated_at: '2026-11-06T10:00:00.000Z' }))
  const setKey =
    options.setKey ??
    (async (_provider: string, value: string) => ({
      ok: true as const,
      data: { ...keyStatus(true, `****${value.slice(-4)}`), provider: 'openai' as const },
      error: null,
      meta,
    }))
  const removeKey =
    options.deleteKey ??
    (async () => ({
      ok: true as const,
      data: keyStatus(false, null),
      error: null,
      meta,
    }))
  const probe =
    options.probe ??
    (async (provider: string) => ({
      ok: true as const,
      data: {
        provider: provider as 'openai',
        model: 'gpt-a',
        status: 'STRICT' as const,
        strict_schema: true,
        degraded: false,
        max_output_tokens: 4096,
        error_code: null,
        notes: 'strict schema accepted',
        latency_ms: 12,
        checked_at: '2026-11-06T10:00:00.000Z',
      },
      error: null,
      meta,
    }))
  const fetchUsage = async (period: LMUsagePeriod) => usageEnvelope(period)
  const rotateSettingsKey = options.rotateSettingsKey ?? vi.fn(() => 'rotated')
  const rotateProviderKey = options.rotateProviderKey ?? vi.fn(() => 'rotated-provider')
  const { unmount } = render(
    <SettingsLLMPage
      fetchSettingsFn={async () => settingsEnvelope(data)}
      updateSettingsFn={update}
      setKeyFn={setKey}
      deleteKeyFn={removeKey}
      probeFn={probe}
      fetchUsageFn={fetchUsage}
      settingsKeyFn={() => 'settings-key'}
      clearSettingsKeyFn={vi.fn()}
      rotateSettingsKeyFn={rotateSettingsKey}
      providerKeyFn={() => 'provider-key'}
      rotateProviderKeyFn={rotateProviderKey}
      clearProviderKeyFn={vi.fn()}
      isCompact={options.isCompact}
    />,
  )
  await screen.findByTestId('llm-provider-mask-openai')
  return {
    update,
    setKey,
    removeKey,
    probe,
    unmount,
    rotateSettingsKey,
    rotateProviderKey,
  }
}

describe('SettingsLLMPage', () => {
  it('shows provider rows, only the mask, four roles, and usage', async () => {
    await renderPage()

    expect(screen.getByTestId('llm-provider-mask-openai')).toHaveTextContent('****4f2a')
    expect(screen.getByTestId('llm-provider-mask-anthropic')).toHaveTextContent('—')
    expect(screen.getByTestId('llm-key-source-openai')).toHaveTextContent('encrypted_file')
    expect(screen.getByLabelText('analysis model')).toHaveValue('gpt-a')
    expect(screen.getByLabelText('review model')).toHaveValue('')
    // No temperature or timeout column is invented.
    expect(screen.queryByRole('columnheader', { name: /temperature/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: /timeout/i })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('llm-usage-day')).toHaveTextContent('$1.00'))
    expect(screen.getByTestId('llm-usage-unpriced')).toHaveTextContent('3')
  })

  it('saves the role model, base URL, and concurrency as one patch', async () => {
    const update = vi.fn(async () =>
      settingsEnvelope({
        ...settingsData(),
        roles: { ANALYSIS: 'gpt-b', ENTRY_RECHECK: null, MANAGEMENT: null, REVIEW: null },
      }),
    )
    await renderPage({ update })

    fireEvent.change(screen.getByLabelText('analysis model'), { target: { value: 'gpt-b' } })
    fireEvent.change(screen.getByLabelText('全局并发（1–64）'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    const [patch, key] = update.mock.calls[0] as unknown as [Record<string, unknown>, string]
    expect(key).toBe('settings-key')
    expect(patch.roles).toMatchObject({ ANALYSIS: 'gpt-b', REVIEW: null })
    expect(patch.max_concurrency).toBe(5)
    expect(patch.base_urls).toMatchObject({ openai: 'https://api.openai.com/v1' })
    await waitFor(() =>
      expect(screen.getByTestId('settings-llm-save-status')).toHaveTextContent('已保存'),
    )
  })

  it('blocks a bad concurrency inline without calling the API', async () => {
    const update = vi.fn()
    await renderPage({ update })

    fireEvent.change(screen.getByLabelText('全局并发（1–64）'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))

    await waitFor(() => expect(screen.getByTestId('llm-error-summary')).toHaveTextContent('错误'))
    expect(update).not.toHaveBeenCalled()
  })

  it('stores a key write-only and never leaves the plaintext in the DOM', async () => {
    const setKey = vi.fn(async () => ({
      ok: true as const,
      data: { ...keyStatus(true, '****4f2a'), provider: 'anthropic' as const },
      error: null,
      meta,
    }))
    await renderPage({ setKey })

    const input = screen.getByTestId('llm-key-input-anthropic')
    fireEvent.change(input, { target: { value: SECRET } })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))

    await waitFor(() => expect(setKey).toHaveBeenCalledWith('anthropic', SECRET, 'provider-key'))
    await waitFor(() => expect(input).toHaveValue(''))
    expect(document.body.textContent).not.toContain(SECRET)
    expect(screen.getByTestId('settings-llm-save-status')).toHaveTextContent('****4f2a')
  })

  it('requires confirmation before deleting a key', async () => {
    const removeKey = vi.fn(async () => ({
      ok: true as const,
      data: keyStatus(false, null),
      error: null,
      meta,
    }))
    await renderPage({ deleteKey: removeKey })

    fireEvent.click(screen.getByRole('button', { name: '删除密钥（OpenAI）' }))
    expect(removeKey).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() => expect(removeKey).toHaveBeenCalledWith('openai', 'provider-key'))
    await waitFor(() =>
      expect(screen.getByTestId('llm-provider-mask-openai')).toHaveTextContent('—'),
    )
  })

  it('runs the capability probe and renders the recorded status', async () => {
    const probe = vi.fn(async () => ({
      ok: true as const,
      data: {
        provider: 'openai' as const,
        model: 'gpt-a',
        status: 'DEGRADED' as const,
        strict_schema: false,
        degraded: true,
        max_output_tokens: 4096,
        error_code: null,
        notes: 'degraded: JSON mode fallback',
        latency_ms: 42,
        checked_at: '2026-11-06T10:00:00.000Z',
      },
      error: null,
      meta,
    }))
    await renderPage({ probe })

    fireEvent.click(screen.getByRole('button', { name: '测试连接（OpenAI，角色 ANALYSIS）' }))

    await waitFor(() =>
      expect(screen.getByTestId('llm-probe-openai')).toHaveTextContent('DEGRADED'),
    )
    expect(screen.getByTestId('llm-probe-openai')).toHaveTextContent('42 ms')
  })

  it('is read-only on a compact surface with a desktop/CLI note', async () => {
    await renderPage({ isCompact: true })

    expect(screen.getByRole('note')).toHaveTextContent('移动端只读')
    expect(screen.getByRole('button', { name: '保存 LLM 设置' })).toBeDisabled()
    expect(screen.getByLabelText('analysis model')).toBeDisabled()
  })
})

describe('SettingsLLMPage regressions', () => {
  it('opens the delete confirmation as a modal and restores focus on Escape', async () => {
    await renderPage()
    const deleteButton = screen.getByRole('button', { name: '删除密钥（OpenAI）' })
    deleteButton.focus()
    fireEvent.click(deleteButton)

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const cancel = screen.getByRole('button', { name: '取消' })
    await waitFor(() => expect(cancel).toHaveFocus())

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(deleteButton).toHaveFocus())
  })

  it('clears a field error by its field prefix after the input is edited', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('不接受的 host', 'VALIDATION_FAILED', {
        kind: 'refusal',
        details: [
          {
            field: 'base_urls.openai',
            code: 'unofficial_host',
            message: '不接受的 host',
          },
        ],
      })
    })
    await renderPage({ update })
    const input = screen.getByLabelText('OpenAI Base URL')
    fireEvent.change(input, { target: { value: 'https://evil.example' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))

    await waitFor(() => expect(input).toHaveAttribute('aria-invalid', 'true'))
    expect(screen.getByTestId('llm-error-summary')).toHaveTextContent('OpenAI Base URL')

    fireEvent.change(input, { target: { value: 'https://api.openai.com/v1' } })
    await waitFor(() => expect(input).not.toHaveAttribute('aria-invalid'))
    // The stale summary is cleared once the field is edited, not only on save.
    expect(screen.queryByTestId('llm-error-summary')).not.toBeInTheDocument()
  })

  it('focuses the error summary after a failed save and links to the field', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('并发无效', 'VALIDATION_FAILED', {
        kind: 'refusal',
        details: [{ field: 'concurrency', code: 'out_of_range', message: '并发无效' }],
      })
    })
    await renderPage({ update })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))

    const summary = await screen.findByTestId('llm-error-summary')
    await waitFor(() => expect(summary).toHaveFocus())
    // One server field translates to both concurrency errors.
    expect(screen.getByLabelText('全局并发（1–64）')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('DeepSeek 并发（1–64）')).toHaveAttribute('aria-invalid', 'true')
    fireEvent.click(screen.getByRole('link', { name: '全局并发' }))
    expect(screen.getByLabelText('全局并发（1–64）')).toHaveFocus()
  })

  it('associates a rejected key write with the key field', async () => {
    const setKey = vi.fn(async () => {
      throw new SettingsApiError('密钥被拒绝', 'VALIDATION_FAILED', { kind: 'refusal' })
    })
    await renderPage({ setKey })
    const input = screen.getByTestId('llm-key-input-anthropic')
    fireEvent.change(input, { target: { value: SECRET } })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))

    await waitFor(() => expect(input).toHaveAttribute('aria-invalid', 'true'))
    const describedBy = input.getAttribute('aria-describedby') ?? ''
    expect(describedBy).not.toBe('')
    expect(document.getElementById(describedBy.split(' ').pop() as string)).toHaveTextContent(
      '未保存任何内容',
    )
    expect(screen.getByTestId('llm-error-summary')).toHaveTextContent('密钥被拒绝')
    // M10: the summary names the provider and links to its key input.
    const keyLink = screen.getByRole('link', { name: 'Anthropic 密钥' })
    fireEvent.click(keyLink)
    expect(input).toHaveFocus()
  })

  it('keeps focus in the edited field while other errors keep the summary visible', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('并发无效', 'VALIDATION_FAILED', {
        kind: 'refusal',
        details: [{ field: 'concurrency', code: 'out_of_range', message: '并发无效' }],
      })
    })
    await renderPage({ update })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))

    const summary = await screen.findByTestId('llm-error-summary')
    await waitFor(() => expect(summary).toHaveFocus())

    fireEvent.click(screen.getByRole('link', { name: '全局并发' }))
    const input = screen.getByLabelText('全局并发（1–64）')
    expect(input).toHaveFocus()

    fireEvent.change(input, { target: { value: '5' } })
    // The DeepSeek bound error remains, so the summary is still mounted; the
    // edited field's error and the stale headline are gone.
    await waitFor(() =>
      expect(screen.getByTestId('llm-error-summary')).not.toHaveTextContent('全局并发'),
    )
    // Editing must not pull focus out of the input.
    expect(input).toHaveFocus()
  })

  it('keeps the submitted input focused while a save is in flight', async () => {
    let resolveUpdate!: (envelope: LLMSettingsEnvelope) => void
    const update = vi.fn(
      () =>
        new Promise<LLMSettingsEnvelope>((resolve) => {
          resolveUpdate = resolve
        }),
    )
    await renderPage({ update })
    const input = screen.getByLabelText('analysis model')
    input.focus()
    fireEvent.change(input, { target: { value: 'gpt-b' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    await waitFor(() => expect(screen.getByRole('button', { name: '保存中…' })).toBeInTheDocument())
    const saveButton = screen.getByRole('button', { name: '保存中…' })
    expect(saveButton).toHaveAttribute('aria-disabled', 'true')
    // The text inputs stay focusable (read-only, not natively disabled) so the
    // Enter-submit does not drop focus to <body>.
    expect(input).not.toBeDisabled()
    expect(input).toHaveAttribute('aria-disabled', 'true')
    expect(document.activeElement).not.toBe(document.body)
    expect(input).toHaveFocus()

    resolveUpdate(settingsEnvelope(settingsData()))
    await waitFor(() =>
      expect(screen.getByTestId('settings-llm-save-status')).toHaveTextContent('已保存'),
    )
  })

  it('marks residual English tokens with lang="en"', async () => {
    const base = settingsData()
    const data = settingsData({
      providers: base.providers.map((row) =>
        row.provider === 'openai'
          ? {
              ...row,
              configured: true,
              key_source: 'env' as const,
              masked: null,
              fingerprint: null,
              capability: {
                status: 'STRICT' as const,
                strict_schema: true,
                degraded: false,
                checked_at: '2026-11-06T10:00:00.000Z',
                notes: 'strict schema accepted',
              },
            }
          : row,
      ),
    })
    await renderPage({ data })

    expect(screen.getByText('strict schema accepted')).toHaveAttribute('lang', 'en')
    expect(screen.getByText('unknown model · surfaced, never zeroed')).toHaveAttribute('lang', 'en')
    expect(screen.getByText('analysis model')).toHaveAttribute('lang', 'en')
    for (const token of screen.getAllByText('env')) {
      expect(token).toHaveAttribute('lang', 'en')
    }
    for (const token of screen.getAllByText('2 calls')) {
      expect(token).toHaveAttribute('lang', 'en')
    }
    for (const token of screen.getAllByText('OK')) {
      expect(token).toHaveAttribute('lang', 'en')
    }
    for (const label of screen.getAllByText('Base URL')) {
      expect(label).toHaveAttribute('lang', 'en')
    }
  })

  it('announces the truthful delete outcome when an env key remains', async () => {
    const removeKey = vi.fn(async () => ({
      ok: true as const,
      data: { ...keyStatus(true, null), provider: 'openai' as const, key_source: 'env' as const },
      error: null,
      meta,
    }))
    await renderPage({ deleteKey: removeKey })
    fireEvent.click(screen.getByRole('button', { name: '删除密钥（OpenAI）' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-llm-save-status')).toHaveTextContent(
        '环境变量密钥仍然生效',
      ),
    )
    expect(screen.getByTestId('llm-provider-mask-openai')).toHaveTextContent('env')
  })

  it('announces that a deleted key neither falls back nor leaves a config error hidden', async () => {
    const removeKey = vi.fn(async () => ({
      ok: true as const,
      data: keyStatus(false, null),
      error: null,
      meta,
    }))
    await renderPage({ deleteKey: removeKey })
    fireEvent.click(screen.getByRole('button', { name: '删除密钥（OpenAI）' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-llm-save-status')).toHaveTextContent(
        'LLM_PROVIDER_NOT_CONFIGURED',
      ),
    )
  })
})

describe('SettingsLLMPage fix round 1', () => {
  it('merges a successful probe into the row capability chip (M3)', async () => {
    const probe = vi.fn(async () => ({
      ok: true as const,
      data: {
        provider: 'openai' as const,
        model: 'gpt-a',
        status: 'DEGRADED' as const,
        strict_schema: false,
        degraded: true,
        max_output_tokens: 4096,
        error_code: null,
        notes: 'degraded: JSON mode fallback',
        latency_ms: 42,
        checked_at: '2026-11-06T10:00:00.000Z',
      },
      error: null,
      meta,
    }))
    await renderPage({ probe })

    fireEvent.click(screen.getByRole('button', { name: '测试连接（OpenAI，角色 ANALYSIS）' }))

    const mask = await screen.findByTestId('llm-provider-mask-openai')
    const row = mask.closest('tr') as HTMLElement
    const chip = within(row).getByTitle('2026-11-06T10:00:00.000Z')
    await waitFor(() => expect(chip).toHaveTextContent('DEGRADED'))
  })

  it('disables probing a non-active provider and names the probed role (M4)', async () => {
    const base = settingsData()
    const data = settingsData({
      providers: base.providers.map((row) =>
        row.provider === 'anthropic'
          ? {
              ...row,
              configured: true,
              key_source: 'encrypted_file' as const,
              masked: '****9c1d',
              fingerprint: 'a'.repeat(64),
            }
          : row,
      ),
    })
    const probe = vi.fn()
    await renderPage({ data, probe })

    const inactive = screen.getByRole('button', { name: '测试连接（Anthropic，角色 ANALYSIS）' })
    expect(inactive).toBeDisabled()
    const describedBy = inactive.getAttribute('aria-describedby') ?? ''
    expect(document.getElementById(describedBy)).toHaveTextContent('只有当前 provider')
    fireEvent.click(inactive)
    expect(probe).not.toHaveBeenCalled()
    // The visible text states the probed role.
    expect(screen.getAllByText('ANALYSIS').length).toBeGreaterThan(0)
  })

  it('announces an empty key error and focuses the key input (H6)', async () => {
    await renderPage()
    const input = screen.getByTestId('llm-key-input-anthropic')
    fireEvent.click(screen.getByRole('button', { name: '保存密钥（Anthropic）' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('请输入密钥')
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('links a probe failure to the provider key input (M10)', async () => {
    const probe = vi.fn(async () => {
      throw new SettingsApiError('测试失败', 'LLM_PROVIDER_NOT_CONFIGURED', { kind: 'refusal' })
    })
    await renderPage({ probe })
    fireEvent.click(screen.getByRole('button', { name: '测试连接（OpenAI，角色 ANALYSIS）' }))

    const link = await screen.findByRole('link', { name: 'OpenAI 测试连接' })
    fireEvent.click(link)
    expect(screen.getByTestId('llm-key-input-openai')).toHaveFocus()
  })

  it('connects the custom-host hint to the checkbox and base URLs (M10)', async () => {
    await renderPage()
    const checkbox = screen.getByRole('checkbox', { name: '允许自定义 Base URL' })
    expect(checkbox).toHaveAttribute('aria-describedby', 'llm-custom-host-hint')
    expect(document.getElementById('llm-custom-host-hint')).toHaveTextContent('官方 host')
    expect(screen.getByLabelText('OpenAI Base URL').getAttribute('aria-describedby')).toContain(
      'llm-custom-host-hint',
    )
  })

  it('marks English role row headers with lang="en" (M10)', async () => {
    await renderPage()
    const header = screen
      .getAllByRole('rowheader')
      .find((element) => element.textContent === 'analysis')
    expect(header).toBeDefined()
    expect(header?.querySelector('[lang="en"]')).not.toBeNull()
  })

  it('rotates the settings idempotency key on a conflict, not on an unknown outcome (M6)', async () => {
    const conflict = vi.fn(async () => {
      throw new SettingsApiError('冲突', 'IDEMPOTENCY_CONFLICT', { kind: 'refusal' })
    })
    const conflicted = await renderPage({ update: conflict })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))
    await waitFor(() => expect(conflicted.rotateSettingsKey).toHaveBeenCalledTimes(1))
    conflicted.unmount()

    const unknown = vi.fn(async () => {
      throw new Error('network down')
    })
    const unknownPage = await renderPage({ update: unknown })
    fireEvent.click(screen.getByRole('button', { name: '保存 LLM 设置' }))
    await waitFor(() =>
      expect(screen.getByTestId('llm-error-summary')).toHaveTextContent('结果未知'),
    )
    expect(unknownPage.rotateSettingsKey).not.toHaveBeenCalled()
  })

  it('clears a stale probe error when a key is saved (M-A)', async () => {
    const probe = vi.fn(async () => {
      throw new SettingsApiError('测试失败', 'LLM_PROVIDER_NOT_CONFIGURED', { kind: 'refusal' })
    })
    const setKey = vi.fn(async () => ({
      ok: true as const,
      data: { ...keyStatus(true, '****9c1d'), provider: 'anthropic' as const },
      error: null,
      meta,
    }))
    await renderPage({ probe, setKey })
    fireEvent.click(screen.getByRole('button', { name: '测试连接（OpenAI，角色 ANALYSIS）' }))
    await waitFor(() =>
      expect(screen.getByTestId('llm-error-summary')).toHaveTextContent('测试失败'),
    )

    fireEvent.change(screen.getByTestId('llm-key-input-anthropic'), {
      target: { value: SECRET },
    })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))

    await waitFor(() =>
      expect(screen.getByTestId('settings-llm-save-status')).toHaveTextContent('密钥已保存'),
    )
    expect(screen.queryByTestId('llm-error-summary')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain('测试失败')
  })

  it('rotates the provider idempotency key on a key conflict, not on an unknown outcome (M6)', async () => {
    const conflict = vi.fn(async () => {
      throw new SettingsApiError('冲突', 'IDEMPOTENCY_CONFLICT', { kind: 'refusal' })
    })
    const conflicted = await renderPage({ setKey: conflict })
    const input = screen.getByTestId('llm-key-input-anthropic')
    fireEvent.change(input, { target: { value: SECRET } })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))
    await waitFor(() => expect(conflicted.rotateProviderKey).toHaveBeenCalledWith('anthropic'))
    conflicted.unmount()

    const unknown = vi.fn(async () => {
      throw new Error('network down')
    })
    const unknownPage = await renderPage({ setKey: unknown })
    const secondInput = screen.getByTestId('llm-key-input-anthropic')
    fireEvent.change(secondInput, { target: { value: SECRET } })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))
    await waitFor(() =>
      expect(screen.getByTestId('llm-error-summary')).toHaveTextContent('network down'),
    )
    expect(unknownPage.rotateProviderKey).not.toHaveBeenCalled()
  })
})
