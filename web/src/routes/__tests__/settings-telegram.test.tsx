import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import { SettingsApiError } from '../../api/settings'
import type {
  TelegramReceipt,
  TelegramReceiptEnvelope,
  TelegramSettingsData,
  TelegramSettingsEnvelope,
  TelegramSettingsPatch,
  TelegramTokenEnvelope,
} from '../../api/telegram'
import { resolveRoute } from '../route-match'
import { SettingsTelegramPage } from '../settings-telegram'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-18T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function settings(overrides: Partial<TelegramSettingsData> = {}): TelegramSettingsData {
  return {
    token: {
      configured: false,
      masked: null,
      last4: null,
      source: 'unset',
      updated_at: null,
    },
    chat_ids: [],
    allowed_user_ids: [],
    toggles: {
      scan_report: true,
      trigger: true,
      entry_recheck: true,
      position: true,
      risk: true,
      budget: true,
    },
    source: 'default',
    updated_at: null,
    updated_by: null,
    receipts: [],
    receipt_total: 0,
    latest_test_receipt: null,
    ...overrides,
  }
}

function configured(overrides: Partial<TelegramSettingsData> = {}): TelegramSettingsEnvelope {
  return envelope(
    settings({
      token: {
        configured: true,
        masked: '****9f2a',
        last4: '9f2a',
        source: 'stored',
        updated_at: '2026-11-18T10:00:00.000Z',
      },
      chat_ids: [1001],
      allowed_user_ids: [2002],
      source: 'user',
      updated_at: '2026-11-18T10:00:00.000Z',
      updated_by: 'web',
      ...overrides,
    }),
  )
}

function envelope(data: TelegramSettingsData): TelegramSettingsEnvelope {
  return { ok: true, data, error: null, meta }
}

function receipt(state: TelegramReceipt['state'] = 'queued'): TelegramReceipt {
  return {
    event_key: 'test:1001:nonce',
    kind: 'test',
    chat_id: 1001,
    reply_to_message_id: null,
    state,
    attempts: 0,
    message_id: state === 'sent' ? 4822 : null,
    last_error_code: state === 'failed' ? 'TELEGRAM_TOKEN_INVALID' : null,
    next_attempt_at: '2026-11-18T10:00:00.000Z',
    created_at: '2026-11-18T10:00:00.000Z',
    sent_at: state === 'sent' ? '2026-11-18T10:00:05.000Z' : null,
  }
}

function receiptEnvelope(state: TelegramReceipt['state'] = 'queued'): TelegramReceiptEnvelope {
  return { ok: true, data: receipt(state), error: null, meta }
}

function tokenEnvelope(): TelegramTokenEnvelope {
  return {
    ok: true,
    data: {
      configured: true,
      masked: '****stuv',
      last4: 'stuv',
      source: 'stored',
      updated_at: '2026-11-18T10:00:00.000Z',
    },
    error: null,
    meta,
  }
}

function renderPage(overrides: {
  isCompact?: boolean
  fetchSettings?: () => Promise<TelegramSettingsEnvelope>
  updateSettings?: (patch: TelegramSettingsPatch, key: string) => Promise<TelegramSettingsEnvelope>
  setToken?: (token: string, key: string) => Promise<TelegramTokenEnvelope>
  deleteToken?: (key: string) => Promise<TelegramTokenEnvelope>
  test?: (key: string, chatId?: number) => Promise<TelegramReceiptEnvelope>
  clearKey?: (label: string) => void
  rotateKey?: (label: string) => string
}) {
  const updateSettings = overrides.updateSettings ?? (async () => configured())
  return render(
    <SettingsTelegramPage
      isCompact={overrides.isCompact}
      fetchSettingsFn={overrides.fetchSettings ?? (async () => envelope(settings()))}
      updateSettingsFn={updateSettings}
      setTokenFn={overrides.setToken ?? (async () => tokenEnvelope())}
      deleteTokenFn={overrides.deleteToken ?? (async () => tokenEnvelope())}
      testFn={overrides.test ?? (async () => receiptEnvelope())}
      idempotencyKeyFn={() => 'test-key'}
      rotateIdempotencyKeyFn={overrides.rotateKey ?? (() => 'rotated-key')}
      clearIdempotencyKeyFn={overrides.clearKey ?? (() => {})}
    />,
  )
}

describe('SettingsTelegramPage — empty and token lifecycle', () => {
  it('shows 未配置 with the push consequence and disables test with a reason', async () => {
    renderPage({})

    const empty = await screen.findByTestId('telegram-empty')
    expect(empty).toHaveTextContent('未配置')
    expect(empty).toHaveTextContent('不会推送 Telegram')
    expect(screen.getByTestId('telegram-source')).toHaveTextContent('source: default')
    expect(screen.getByTestId('telegram-config-status')).toHaveTextContent('未配置')
    // The consequence is announced through a live region that existed first.
    expect(screen.getByTestId('telegram-live-status')).toHaveAttribute('role', 'status')
    expect(screen.getByTestId('telegram-live-status')).toHaveTextContent('未配置')
    const test = screen.getByTestId('telegram-test')
    // Kept focusable (aria-disabled), so the reason is reachable on focus.
    expect(test).not.toBeDisabled()
    expect(test).toHaveAttribute('aria-disabled', 'true')
    expect(test).toHaveAttribute('aria-describedby', 'telegram-test-disabled-reason')
    expect(screen.getByTestId('telegram-test-disabled-reason')).toHaveTextContent('未配置 token')
    // The six toggles are one named group with the chat requirement attached.
    expect(screen.getByRole('group', { name: '推送事件' })).toHaveAccessibleDescription(
      /至少需要一个 chat id/,
    )
    test.focus()
    expect(test).toHaveFocus()
  })

  it('saves the token write-only, clears the field, and never renders the value', async () => {
    const sentinel = '123456789:AASettingsSentinelToken_abcdefghijklmnopqrstuv'
    const setToken = vi.fn(async () => tokenEnvelope())
    const clearKey = vi.fn()
    renderPage({ setToken, clearKey })

    const input = await screen.findByTestId('telegram-token-input')
    expect(input).toHaveAttribute('type', 'password')
    fireEvent.change(input, { target: { value: sentinel } })
    fireEvent.click(screen.getByTestId('telegram-token-save'))

    await waitFor(() => expect(setToken).toHaveBeenCalledWith(sentinel, 'test-key'))
    expect(await screen.findByTestId('telegram-token-mask')).toHaveTextContent('****stuv')
    expect(input).toHaveValue('')
    expect(document.body.textContent).not.toContain(sentinel)
    expect(clearKey).toHaveBeenCalledWith('token')
    expect(screen.queryByRole('button', { name: /显示原值|查看密钥|reveal/i })).toBeNull()
  })

  it('deletes the token and reports that pushes are disabled', async () => {
    const deleteToken = vi.fn(async () => ({
      ok: true as const,
      data: {
        configured: false,
        masked: null,
        last4: null,
        source: 'unset' as const,
        updated_at: null,
      },
      error: null,
      meta,
    }))
    renderPage({
      fetchSettings: async () => configured(),
      deleteToken,
    })

    await screen.findByTestId('telegram-token-delete')
    fireEvent.click(screen.getByTestId('telegram-token-delete'))

    await waitFor(() => expect(deleteToken).toHaveBeenCalledWith('test-key'))
    expect(await screen.findByTestId('telegram-save-status')).toHaveTextContent('推送已禁用')
    expect(screen.getByTestId('telegram-config-status')).toHaveTextContent('未配置')
  })
})

describe('SettingsTelegramPage — allowlists, toggles, and test send', () => {
  it('saves chat/user ids and toggles with the shared idempotency key', async () => {
    const updateSettings = vi.fn(async () => configured({ chat_ids: [1001, -1002] }))
    renderPage({
      fetchSettings: async () => configured({ chat_ids: [1001] }),
      updateSettings,
    })

    await screen.findByTestId('telegram-chat-input')
    fireEvent.change(screen.getByTestId('telegram-chat-input'), {
      target: { value: '-1002' },
    })
    fireEvent.click(screen.getByTestId('telegram-chat-add'))
    fireEvent.click(screen.getByTestId('telegram-toggle-scan_report'))
    fireEvent.click(screen.getByTestId('telegram-save'))

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          chat_ids: [-1002, 1001],
          allowed_user_ids: [2002],
          toggles: expect.objectContaining({ scan_report: false }),
        }),
        'test-key',
      ),
    )
    expect(await screen.findByTestId('telegram-save-status')).toHaveTextContent('已保存')
  })

  it('surfaces a server refusal without claiming a write', async () => {
    const updateSettings = vi.fn(async () => {
      throw new SettingsApiError('at least one chat id is required', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'chat_ids', code: 'chat_required_for_pushes' }],
      })
    })
    renderPage({ fetchSettings: async () => configured(), updateSettings })

    await screen.findByTestId('telegram-save')
    fireEvent.click(screen.getByTestId('telegram-save'))

    const error = await screen.findByTestId('telegram-error')
    expect(error).toHaveTextContent('未写入任何内容')
    expect(error).toHaveTextContent('at least one chat id is required')
  })

  it('keeps unsaved edits when the token is saved and persists them next', async () => {
    const updateSettings = vi.fn(async () =>
      configured({ chat_ids: [-1002], toggles: { ...settings().toggles, scan_report: false } }),
    )
    renderPage({
      fetchSettings: async () => envelope(settings()),
      updateSettings,
    })

    await screen.findByTestId('telegram-chat-input')
    fireEvent.change(screen.getByTestId('telegram-chat-input'), {
      target: { value: '-1002' },
    })
    fireEvent.click(screen.getByTestId('telegram-chat-add'))
    fireEvent.click(screen.getByTestId('telegram-toggle-scan_report'))
    expect(screen.getByTestId('telegram-toggle-scan_report')).not.toBeChecked()

    fireEvent.change(screen.getByTestId('telegram-token-input'), {
      target: { value: '123456789:AASettingsSentinelToken_abcdefghijklmnopqrstuv' },
    })
    fireEvent.click(screen.getByTestId('telegram-token-save'))
    await waitFor(() =>
      expect(screen.getByTestId('telegram-token-mask')).toHaveTextContent('****stuv'),
    )

    // The token response must not rebuild the draft from server data.
    expect(screen.getByTestId('telegram-toggle-scan_report')).not.toBeChecked()
    expect(screen.getByRole('button', { name: '移除 Chat ID -1002' })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('telegram-save'))
    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          chat_ids: [-1002],
          toggles: expect.objectContaining({ scan_report: false }),
        }),
        'test-key',
      ),
    )
  })

  it('associates a failed token save with the error and moves focus there', async () => {
    const setToken = vi.fn(async () => {
      throw new SettingsApiError('token rejected', 'VALIDATION_FAILED', { httpStatus: 422 })
    })
    renderPage({ setToken })

    const input = await screen.findByTestId('telegram-token-input')
    fireEvent.change(input, { target: { value: 'bad-token' } })
    fireEvent.click(screen.getByTestId('telegram-token-save'))

    const summary = await screen.findByTestId('telegram-error')
    expect(summary).toHaveFocus()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', 'telegram-token-hint telegram-error-summary')
  })

  it('clears the token invalid state once a settings save clears the error', async () => {
    const setToken = vi.fn(async () => {
      throw new SettingsApiError('token rejected', 'VALIDATION_FAILED', { httpStatus: 422 })
    })
    renderPage({ fetchSettings: async () => configured(), setToken })

    const input = await screen.findByTestId('telegram-token-input')
    fireEvent.change(input, { target: { value: 'bad-token' } })
    fireEvent.click(screen.getByTestId('telegram-token-save'))
    await screen.findByTestId('telegram-error')
    expect(input).toHaveAttribute('aria-invalid', 'true')

    fireEvent.click(screen.getByTestId('telegram-save'))
    await waitFor(() => expect(screen.queryByTestId('telegram-error')).toBeNull())

    // The shared error is gone, so the token field must not claim to be invalid
    // or point at an unmounted summary.
    expect(input).not.toHaveAttribute('aria-invalid')
    const ids = (input.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) {
      expect(document.getElementById(id)).not.toBeNull()
    }
  })

  it('clears the token invalid state when the token is removed after a failed save', async () => {
    const setToken = vi.fn(async () => {
      throw new SettingsApiError('token rejected', 'VALIDATION_FAILED', { httpStatus: 422 })
    })
    renderPage({ fetchSettings: async () => configured(), setToken })

    const input = await screen.findByTestId('telegram-token-input')
    fireEvent.change(input, { target: { value: 'bad-token' } })
    fireEvent.click(screen.getByTestId('telegram-token-save'))
    await screen.findByTestId('telegram-error')
    expect(input).toHaveAttribute('aria-invalid', 'true')

    fireEvent.click(screen.getByTestId('telegram-token-delete'))
    await waitFor(() => expect(screen.queryByTestId('telegram-error')).toBeNull())

    expect(input).not.toHaveAttribute('aria-invalid')
    const ids = (input.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) {
      expect(document.getElementById(id)).not.toBeNull()
    }
  })

  it('sends a test message and shows its receipt', async () => {
    const test = vi.fn(async () => receiptEnvelope('queued'))
    renderPage({ fetchSettings: async () => configured(), test })

    await screen.findByTestId('telegram-test')
    fireEvent.click(screen.getByTestId('telegram-test'))

    await waitFor(() => expect(test).toHaveBeenCalledWith('test-key', 1001))
    expect(await screen.findByTestId('telegram-test-receipt')).toHaveTextContent('排队中')
    // The always-mounted polite region announces the result.
    expect(screen.getByTestId('telegram-test-status')).toHaveAttribute('role', 'status')
    expect(screen.getByTestId('telegram-test-status')).toHaveTextContent('排队中')
  })

  it('shows a stable failure reason and does not mark other chats failed', async () => {
    const test = vi.fn(async () => {
      throw new SettingsApiError('unauthorized', 'TELEGRAM_TOKEN_INVALID', {
        httpStatus: 422,
      })
    })
    renderPage({ fetchSettings: async () => configured(), test })

    await screen.findByTestId('telegram-test')
    fireEvent.click(screen.getByTestId('telegram-test'))

    const error = await screen.findByTestId('telegram-test-error')
    expect(error).toHaveTextContent('401 Unauthorized')
    // The aggregate's own per-chat receipts are untouched.
    expect(screen.queryByTestId('telegram-receipts')).toBeNull()
  })

  it('keeps the idempotency key on an unknown outcome and rotates on a conflict', async () => {
    const clearKey = vi.fn()
    const rotateKey = vi.fn(() => 'rotated')
    const updateSettings = vi.fn(async () => {
      throw new SettingsApiError('conflict', 'IDEMPOTENCY_CONFLICT', { httpStatus: 409 })
    })
    renderPage({
      fetchSettings: async () => configured(),
      updateSettings,
      clearKey,
      rotateKey,
    })

    await screen.findByTestId('telegram-save')
    fireEvent.click(screen.getByTestId('telegram-save'))

    await waitFor(() => expect(rotateKey).toHaveBeenCalledWith('settings'))
    expect(clearKey).not.toHaveBeenCalled()
  })
})

describe('SettingsTelegramPage — read-only at 375px', () => {
  it('resolves the dedicated route without matching the overview', () => {
    expect(resolveRoute('/settings/telegram').isSettingsTelegram).toBe(true)
    expect(resolveRoute('/settings/telegram').isSettings).toBe(false)
    expect(resolveRoute('/settings/telegram/extra').isSettingsTelegram).toBe(false)
  })

  it('disables every control and points at desktop/CLI', async () => {
    renderPage({ isCompact: true, fetchSettings: async () => configured() })

    await screen.findByTestId('telegram-token-input')
    expect(screen.getByRole('note')).toHaveTextContent('移动端只读')
    expect(screen.getByTestId('telegram-token-input')).toBeDisabled()
    expect(screen.getByTestId('telegram-chat-input')).toBeDisabled()
    expect(screen.getByTestId('telegram-toggle-risk')).toBeDisabled()
    expect(screen.getByTestId('telegram-save')).toBeDisabled()
    expect(screen.getByTestId('telegram-test')).toBeDisabled()
  })
})
