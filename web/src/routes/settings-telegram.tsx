import { useEffect, useRef, useState } from 'react'

import { errorMessage } from '../api/errors'
import { SettingsApiError } from '../api/settings'
import {
  clearTelegramOperationKey,
  deleteTelegramToken,
  fetchTelegramSettings,
  type TelegramReceipt,
  type TelegramReceiptEnvelope,
  type TelegramSettingsData,
  type TelegramSettingsEnvelope,
  type TelegramSettingsPatch,
  type TelegramTokenEnvelope,
  type TelegramToggleName,
  type TelegramToggles,
  rotateTelegramOperationKey,
  sendTelegramTest,
  setTelegramToken,
  telegramOperationKey,
  telegramTestFailureReason,
  updateTelegramSettings,
} from '../api/telegram'
import { AllowlistEditor } from '../components/allowlist-editor'
import { SettingsNotice, SourceChip, useSettingsReadonly } from '../components/settings-field'
import { SettingsSectionNav } from '../components/settings-section-nav'
import { TelegramReceiptTable, TelegramTestReceipt } from '../components/telegram-receipt'

const TOGGLE_COPY: Record<TelegramToggleName, { label: string; detail: string }> = {
  scan_report: {
    label: 'scan report',
    detail: '每次完成的扫描（默认按扫描，不按标的）',
  },
  trigger: {
    label: 'opportunity triggered / invalidated',
    detail: '含 ID，请在桌面端处理',
  },
  entry_recheck: {
    label: 'entry re-check awaiting human',
    detail: '不提供审批或写命令',
  },
  position: {
    label: 'position management executed / blocked',
    detail: '含最终规模',
  },
  risk: {
    label: 'risk alerts',
    detail: '日亏损 / 杠杆 / 数据 stale / daemon down',
  },
  budget: {
    label: 'budget warning 80% / 100%',
    detail: '仅告警；无硬性停止',
  },
}

type LoadState =
  | { state: 'loading' }
  | { state: 'ready'; data: TelegramSettingsData }
  | { state: 'error'; message: string }

interface Draft {
  chatIds: number[]
  allowedUserIds: number[]
  toggles: TelegramToggles
}

export interface SettingsTelegramPageProps {
  isCompact?: boolean
  fetchSettingsFn?: (signal?: AbortSignal) => Promise<TelegramSettingsEnvelope>
  updateSettingsFn?: (
    patch: TelegramSettingsPatch,
    idempotencyKey: string,
  ) => Promise<TelegramSettingsEnvelope>
  setTokenFn?: (token: string, idempotencyKey: string) => Promise<TelegramTokenEnvelope>
  deleteTokenFn?: (idempotencyKey: string) => Promise<TelegramTokenEnvelope>
  testFn?: (idempotencyKey: string, chatId?: number) => Promise<TelegramReceiptEnvelope>
  idempotencyKeyFn?: (label: string) => string
  rotateIdempotencyKeyFn?: (label: string) => string
  clearIdempotencyKeyFn?: (label: string) => void
}

function draftFrom(data: TelegramSettingsData): Draft {
  return {
    chatIds: [...data.chat_ids],
    allowedUserIds: [...data.allowed_user_ids],
    toggles: { ...data.toggles },
  }
}

/** `/settings/telegram` (F10f): token, allowlists, toggles, receipts. */
export function SettingsTelegramPage({
  isCompact,
  fetchSettingsFn = fetchTelegramSettings,
  updateSettingsFn = updateTelegramSettings,
  setTokenFn = setTelegramToken,
  deleteTokenFn = deleteTelegramToken,
  testFn = sendTelegramTest,
  idempotencyKeyFn = telegramOperationKey,
  rotateIdempotencyKeyFn = rotateTelegramOperationKey,
  clearIdempotencyKeyFn = clearTelegramOperationKey,
}: SettingsTelegramPageProps) {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<LoadState>({ state: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [tokenDraft, setTokenDraft] = useState('')
  const [tokenBusy, setTokenBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [notice, setNotice] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [error, setError] = useState('')
  const [tokenSaveFailed, setTokenSaveFailed] = useState(false)
  const [chatError, setChatError] = useState('')
  const [userError, setUserError] = useState('')
  const [testError, setTestError] = useState('')
  const [testReceipt, setTestReceipt] = useState<TelegramReceipt | null>(null)
  const errorSummaryRef = useRef<HTMLSpanElement | null>(null)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad({ state: 'loading' })
    fetchSettingsFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', message: 'Telegram 设置响应为空。' })
          return
        }
        // The editable draft is initialized once per load. Token/receipt
        // updates below must never rebuild it and discard unsaved edits.
        setDraft(draftFrom(envelope.data))
        setLiveMessage(
          envelope.data.token.configured
            ? ''
            : 'Telegram 未配置：关键事件不会推送 Telegram。配置 token 与至少一个 chat ID 后测试发送才可用。',
        )
        setLoad({ state: 'ready', data: envelope.data })
      })
      .catch((failure: unknown) => {
        if (active) {
          setLoad({ state: 'error', message: errorMessage(failure) })
        }
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchSettingsFn, attempt])

  const data = load.state === 'ready' ? load.data : null

  useEffect(() => {
    // A failed save surfaces through an alert; move focus there so keyboard
    // and screen-reader users are told immediately (WCAG 3.3.1).
    if (error) {
      errorSummaryRef.current?.focus()
    }
  }, [error])

  function settle(label: string, failure: unknown): string {
    if (failure instanceof SettingsApiError && failure.code === 'IDEMPOTENCY_CONFLICT') {
      rotateIdempotencyKeyFn(label)
      return `该保存与同一 Idempotency-Key 的先前请求冲突；已生成新的 key，可重试。 ${failure.message}`
    }
    if (failure instanceof SettingsApiError && failure.kind === 'refusal') {
      clearIdempotencyKeyFn(label)
      return `服务器拒绝了本次保存，未写入任何内容。${failure.message}`
    }
    return `结果未知；重试会使用同一 Idempotency-Key 重放。 ${errorMessage(failure)}`
  }

  async function saveToken(): Promise<void> {
    if (readonly || tokenBusy || tokenDraft.trim() === '') {
      return
    }
    const label = 'token'
    setTokenBusy(true)
    setError('')
    setNotice('')
    setTokenSaveFailed(false)
    try {
      const envelope = await setTokenFn(tokenDraft, idempotencyKeyFn(label))
      const status = envelope.data
      if (status) {
        setLoad((current) =>
          current.state === 'ready'
            ? { state: 'ready', data: { ...current.data, token: status } }
            : current,
        )
      }
      clearIdempotencyKeyFn(label)
      setTokenDraft('')
      setNotice('token 已保存；只显示掩码与最后 4 位。')
      setLiveMessage('token 已保存；只显示掩码与最后 4 位。')
    } catch (failure: unknown) {
      setTokenSaveFailed(true)
      setError(settle(label, failure))
    } finally {
      setTokenBusy(false)
    }
  }

  async function removeToken(): Promise<void> {
    if (readonly || tokenBusy || !data) {
      return
    }
    const label = 'token-delete'
    setTokenBusy(true)
    setError('')
    setTokenSaveFailed(false)
    setNotice('')
    try {
      const envelope = await deleteTokenFn(idempotencyKeyFn(label))
      const status = envelope.data
      if (status) {
        setLoad((current) =>
          current.state === 'ready'
            ? { state: 'ready', data: { ...current.data, token: status } }
            : current,
        )
      }
      clearIdempotencyKeyFn(label)
      const message = status?.configured
        ? '存储的 token 已删除；环境变量 token 仍然生效。'
        : 'token 已删除；Telegram 推送已禁用。'
      setNotice(message)
      setLiveMessage(message)
    } catch (failure: unknown) {
      setError(settle(label, failure))
    } finally {
      setTokenBusy(false)
    }
  }

  async function saveSettings(): Promise<void> {
    if (readonly || saving || !draft || !data) {
      return
    }
    const label = 'settings'
    setSaving(true)
    setError('')
    setTokenSaveFailed(false)
    setChatError('')
    setUserError('')
    setNotice('')
    try {
      const envelope = await updateSettingsFn(
        {
          chat_ids: draft.chatIds,
          allowed_user_ids: draft.allowedUserIds,
          toggles: draft.toggles,
        },
        idempotencyKeyFn(label),
      )
      if (envelope.data) {
        setLoad({ state: 'ready', data: envelope.data })
        // Only a successful settings save re-baselines the editable draft.
        setDraft(draftFrom(envelope.data))
      }
      clearIdempotencyKeyFn(label)
      const message = 'Telegram 设置已保存；真实变更写入一条 audit。'
      setNotice(message)
      setLiveMessage(message)
    } catch (failure: unknown) {
      if (failure instanceof SettingsApiError) {
        const chatDetail = failure.details.find(
          (detail) => detail.field === 'chat_ids' || detail.code === 'chat_required_for_pushes',
        )
        const userDetail = failure.details.find((detail) => detail.field === 'allowed_user_ids')
        setChatError(chatDetail?.message ?? (chatDetail ? failure.message : ''))
        setUserError(userDetail?.message ?? '')
      }
      setError(settle(label, failure))
    } finally {
      setSaving(false)
    }
  }

  async function sendTest(): Promise<void> {
    if (readonly || testing || !data || testDisabledReason !== '') {
      return
    }
    const label = 'test'
    setTesting(true)
    setTestError('')
    try {
      const envelope = await testFn(idempotencyKeyFn(label), data.chat_ids[0])
      const created = envelope.data
      if (created) {
        setTestReceipt(created)
        setLoad((current) =>
          current.state === 'ready'
            ? {
                state: 'ready',
                data: {
                  ...current.data,
                  receipts: [created, ...current.data.receipts].slice(0, 6),
                  receipt_total: current.data.receipt_total + 1,
                  latest_test_receipt: created,
                },
              }
            : current,
        )
      }
      clearIdempotencyKeyFn(label)
    } catch (failure: unknown) {
      const apiError = failure instanceof SettingsApiError ? failure : null
      setTestError(telegramTestFailureReason(apiError, errorMessage(failure)))
      if (apiError?.code === 'IDEMPOTENCY_CONFLICT') {
        rotateIdempotencyKeyFn(label)
      } else if (apiError?.kind === 'refusal') {
        clearIdempotencyKeyFn(label)
      }
    } finally {
      setTesting(false)
    }
  }

  const tokenConfigured = data?.token.configured ?? false
  // The token error association only exists while its summary is mounted, so a
  // stale flag can never leave a dangling aria-describedby or a phantom
  // aria-invalid state (WCAG 3.3.1 / 4.1.2).
  const tokenFieldInvalid = tokenSaveFailed && error !== ''
  // The test targets the saved allowlist, so enablement follows it rather than
  // unsaved draft edits (a save is required first).
  const savedChatCount = data?.chat_ids.length ?? 0
  let testDisabledReason = ''
  if (!tokenConfigured) {
    testDisabledReason = '未配置 token；测试发送不可用。'
  } else if (savedChatCount === 0) {
    testDisabledReason = '没有 chat id；测试发送不可用。'
  }
  const unconfigured = load.state === 'ready' && !tokenConfigured

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-telegram-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">Settings / telegram</p>
          <h1 id="settings-telegram-heading">Telegram 设置</h1>
          <p className="page-sub">
            bot token、chat ID、allowlist、推送开关与投递回执。MVP 只读：没有审批或写命令；token
            只写，保存后仅显示掩码与最后 4 位。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：Telegram 设置只能在桌面端或 CLI 修改（`oft telegram`）。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="telegram" />
        <div className="stack">
          <p className="visually-hidden" role="status" data-testid="telegram-live-status">
            {liveMessage}
          </p>
          {load.state === 'loading' && (
            <p className="status-message" role="status">
              加载 Telegram 设置…
            </p>
          )}

          {load.state === 'error' && (
            <>
              <SettingsNotice tone="error">Telegram 设置不可用：{load.message}</SettingsNotice>
              <button
                type="button"
                className="control-btn"
                onClick={() => setAttempt((value) => value + 1)}
              >
                重试
              </button>
            </>
          )}

          {data && draft && (
            <>
              {unconfigured && (
                <p className="status-message" data-testid="telegram-empty">
                  <strong>未配置</strong>：关键事件不会推送 Telegram。配置 token 与至少一个 chat ID
                  后测试发送才可用。
                </p>
              )}

              <div className="action-row">
                <SourceChip source={data.source} testId="telegram-source" />
                <span className="mono-muted" data-testid="telegram-config-status">
                  {tokenConfigured ? (data.token.masked ?? '已配置') : '未配置'}
                </span>
                {data.updated_at && <span className="mono-muted">上次修改 {data.updated_at}</span>}
              </div>

              <section className="panel" aria-labelledby="telegram-connection-heading">
                <div className="panel-head">
                  <h2 id="telegram-connection-heading">连接</h2>
                  <span className="chip chip--calc">只读通道</span>
                </div>
                <div className="stack" style={{ padding: '0 var(--oft-space-4)' }}>
                  <div className="field">
                    <label className="field-label" htmlFor="telegram-token-input">
                      Bot token（只写）
                    </label>
                    <p className="mono" data-testid="telegram-token-mask">
                      {tokenConfigured ? (data.token.masked ?? '已配置') : '未配置'}
                    </p>
                    <div className="action-row">
                      <input
                        id="telegram-token-input"
                        data-testid="telegram-token-input"
                        className="settings-input mono"
                        type="password"
                        autoComplete="new-password"
                        spellCheck={false}
                        value={tokenDraft}
                        disabled={readonly}
                        aria-describedby={
                          tokenFieldInvalid
                            ? 'telegram-token-hint telegram-error-summary'
                            : 'telegram-token-hint'
                        }
                        aria-invalid={tokenFieldInvalid || undefined}
                        placeholder={tokenConfigured ? '输入新 token 以替换' : '粘贴 bot token'}
                        onChange={(event) => {
                          setTokenDraft(event.target.value)
                          setTokenSaveFailed(false)
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            event.preventDefault()
                            void saveToken()
                          }
                        }}
                      />
                      <button
                        type="button"
                        className="control-btn"
                        data-testid="telegram-token-save"
                        disabled={readonly || tokenDraft.trim() === ''}
                        aria-disabled={tokenBusy || undefined}
                        onClick={() => void saveToken()}
                      >
                        {tokenBusy ? '保存中…' : tokenConfigured ? '替换 token' : '保存 token'}
                      </button>
                      {tokenConfigured && (
                        <button
                          type="button"
                          className="control-btn"
                          data-testid="telegram-token-delete"
                          disabled={readonly}
                          aria-disabled={tokenBusy || undefined}
                          onClick={() => void removeToken()}
                        >
                          删除 token
                        </button>
                      )}
                    </div>
                    <p className="hint" id="telegram-token-hint">
                      保存后仅显示掩码与最后 4 位；没有查看原值的控件。未知 chat 收到「未授权」，
                      不返回任何行情或持仓。
                    </p>
                  </div>

                  <AllowlistEditor
                    id="telegram-chat"
                    label="Chat ID"
                    hint="chat 必须是非零整数；群组/超级群为负数。推送至少需要一个 chat。"
                    values={draft.chatIds}
                    onChange={(values) => {
                      setChatError('')
                      setDraft({ ...draft, chatIds: values })
                    }}
                    readOnly={readonly}
                    allowNegative
                    maximum={10}
                    testId="telegram-chat-editor"
                    serverError={chatError}
                  />
                  <AllowlistEditor
                    id="telegram-user"
                    label="Allowlist user ID"
                    hint="用户 id 必须是正整数；未知用户收到固定拒绝。"
                    values={draft.allowedUserIds}
                    onChange={(values) => {
                      setUserError('')
                      setDraft({ ...draft, allowedUserIds: values })
                    }}
                    readOnly={readonly}
                    maximum={20}
                    testId="telegram-user-editor"
                    serverError={userError}
                  />
                </div>
              </section>

              <section className="panel" aria-labelledby="telegram-toggles-heading">
                <div className="panel-head">
                  <h2 id="telegram-toggles-heading">推送事件</h2>
                </div>
                <div className="stack" style={{ padding: '0 var(--oft-space-4)' }}>
                  <div
                    role="group"
                    aria-labelledby="telegram-toggles-heading"
                    aria-describedby="telegram-toggles-hint"
                  >
                    {(Object.keys(TOGGLE_COPY) as TelegramToggleName[]).map((name) => (
                      <label className="check" key={name}>
                        <input
                          type="checkbox"
                          className="edit"
                          data-testid={`telegram-toggle-${name}`}
                          checked={draft.toggles[name]}
                          disabled={readonly}
                          onChange={(event) =>
                            setDraft({
                              ...draft,
                              toggles: { ...draft.toggles, [name]: event.target.checked },
                            })
                          }
                        />{' '}
                        <span>
                          <strong>{TOGGLE_COPY[name].label}</strong>
                          <small>{TOGGLE_COPY[name].detail}</small>
                        </span>
                      </label>
                    ))}
                  </div>
                  <p className="hint" id="telegram-toggles-hint">
                    任一开关开启时至少需要一个 chat id；全部关闭且无 chat 也允许保存。
                  </p>
                </div>
              </section>

              <div className="settings-actions">
                <button
                  type="button"
                  className="control-btn"
                  data-testid="telegram-save"
                  disabled={readonly}
                  aria-disabled={saving || undefined}
                  onClick={() => void saveSettings()}
                >
                  {saving ? '保存中…' : '保存'}
                </button>
                <span className="hint">一次保存写入一条 TELEGRAM_SETTINGS_UPDATED audit。</span>
                {(chatError || userError) && (
                  <p
                    className="status-message status-message--error"
                    id="telegram-settings-error"
                    role="alert"
                  >
                    {[chatError, userError].filter(Boolean).join(' ')}
                  </p>
                )}
              </div>

              <section className="panel" aria-labelledby="telegram-test-heading">
                <div className="panel-head">
                  <h2 id="telegram-test-heading">测试消息</h2>
                </div>
                <div className="stack" style={{ padding: '0 var(--oft-space-4)' }}>
                  <div className="action-row">
                    <button
                      type="button"
                      className="control-btn"
                      data-testid="telegram-test"
                      disabled={readonly}
                      aria-disabled={testing || testDisabledReason !== '' || undefined}
                      aria-describedby={
                        testDisabledReason ? 'telegram-test-disabled-reason' : 'telegram-test-hint'
                      }
                      onClick={() => void sendTest()}
                    >
                      {testing ? '发送中…' : '发送测试'}
                    </button>
                    <span className="hint" id="telegram-test-hint">
                      测试发送是单独动作；一个 chat 不可达不会标记其他 chat 失败。
                    </span>
                  </div>
                  <p
                    className={testDisabledReason ? 'status-message' : 'visually-hidden'}
                    id="telegram-test-disabled-reason"
                    data-testid="telegram-test-disabled-reason"
                    role="status"
                  >
                    {testDisabledReason}
                  </p>
                  <TelegramTestReceipt receipt={testReceipt ?? data.latest_test_receipt} />
                </div>
              </section>

              <section className="panel" aria-labelledby="telegram-receipts-heading">
                <div className="panel-head">
                  <h2 id="telegram-receipts-heading">投递回执 · 最近 6 条</h2>
                  <span className="chip chip--muted">{data.receipt_total} 条</span>
                </div>
                <TelegramReceiptTable receipts={data.receipts.slice(0, 6)} />
              </section>

              {notice && (
                <p className="status-message" data-testid="telegram-save-status">
                  {notice}
                </p>
              )}
              {error && (
                <SettingsNotice tone="error">
                  <span
                    ref={errorSummaryRef}
                    id="telegram-error-summary"
                    data-testid="telegram-error"
                    tabIndex={-1}
                  >
                    {error}
                  </span>
                </SettingsNotice>
              )}
              {testError && (
                <SettingsNotice tone="error">
                  <span data-testid="telegram-test-error">{testError}</span>
                </SettingsNotice>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  )
}
