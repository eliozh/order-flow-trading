import { useCallback, useEffect, useRef, useState } from 'react'

import type {
  TelegramReceipt,
  TelegramReceiptEnvelope,
  TelegramSettingsData,
  TelegramSettingsEnvelope,
  TelegramSettingsPatch,
  TelegramTokenEnvelope,
  TelegramToggleName,
  TelegramToggles,
} from '../api/telegram'
import { telegramTestFailureReason } from '../api/telegram'
import { SettingsApiError } from '../api/settings'
import { AllowlistEditor } from '../components/allowlist-editor'
import { TelegramTestReceipt } from '../components/telegram-receipt'
import { settleOnboardingMutation } from './onboarding-idempotency'
import { useOnboardingLoad } from './use-onboarding-load'

const TOGGLE_LABELS: Record<TelegramToggleName, string> = {
  scan_report: 'scan report',
  trigger: 'trigger / invalidated',
  entry_recheck: 'entry re-check',
  position: 'position management',
  risk: 'risk alerts',
  budget: 'budget 80% / 100%',
}

export interface TelegramStepProps {
  fetchSettingsFn: (signal?: AbortSignal) => Promise<TelegramSettingsEnvelope>
  updateSettingsFn: (
    patch: TelegramSettingsPatch,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<TelegramSettingsEnvelope>
  setTokenFn: (
    token: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<TelegramTokenEnvelope>
  testFn: (idempotencyKey: string, chatId?: number) => Promise<TelegramReceiptEnvelope>
  idempotencyKeyFn: (label: string) => string
  clearIdempotencyKeyFn: (label: string) => void
  rotateIdempotencyKeyFn: (label: string) => string
  announce: (message: string) => void
  onComplete: () => void
  completing: boolean
  narrow: boolean
}

interface Draft {
  chatIds: number[]
  allowedUserIds: number[]
  toggles: TelegramToggles
}

function draftFrom(data: TelegramSettingsData): Draft {
  return {
    chatIds: [...data.chat_ids],
    allowedUserIds: [...data.allowed_user_ids],
    toggles: { ...data.toggles },
  }
}

/**
 * Step 4 (S01.4): optional Telegram read-only channel.
 *
 * It uses the same aggregate service as `/settings/telegram`; skipping writes
 * nothing and leaves every push disabled. Completing requires a saved token and
 * at least one chat id, enforced again by the server prerequisite.
 */
export function TelegramStep({
  fetchSettingsFn,
  updateSettingsFn,
  setTokenFn,
  testFn,
  idempotencyKeyFn,
  clearIdempotencyKeyFn,
  rotateIdempotencyKeyFn,
  announce,
  onComplete,
  completing,
  narrow,
}: TelegramStepProps) {
  const loader = useCallback(
    (signal: AbortSignal) =>
      fetchSettingsFn(signal).then((envelope) => {
        if (!envelope.data) {
          throw new Error('Telegram 设置响应为空。')
        }
        return envelope.data
      }),
    [fetchSettingsFn],
  )
  const load = useOnboardingLoad(loader)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [tokenDraft, setTokenDraft] = useState('')
  const [tokenStatusOverrides, setTokenStatusOverrides] = useState<
    TelegramSettingsData['token'] | null
  >(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [tokenSaveFailed, setTokenSaveFailed] = useState(false)
  const [chatError, setChatError] = useState('')
  const [userError, setUserError] = useState('')
  const [notice, setNotice] = useState('')
  const [testReceipt, setTestReceipt] = useState<TelegramReceipt | null>(null)
  const [testError, setTestError] = useState('')
  const errorSummaryRef = useRef<HTMLParagraphElement | null>(null)

  useEffect(() => {
    setDraft(load.data ? draftFrom(load.data) : null)
    setTokenStatusOverrides(null)
  }, [load.data])

  useEffect(() => {
    if (load.state === 'ready') {
      const configured = load.data?.token.configured ?? false
      announce(configured ? 'Telegram 设置已加载。' : 'Telegram 设置已加载。未配置，推送保持禁用。')
    } else if (load.state === 'error') {
      announce('Telegram 设置不可用。')
    }
  }, [load.state, load.data, announce])

  useEffect(() => {
    if (error) {
      errorSummaryRef.current?.focus()
    }
  }, [error])

  const data = load.data
  const token = tokenStatusOverrides ?? data?.token ?? null
  const tokenConfigured = token?.configured ?? false
  const chatCount = draft?.chatIds.length ?? 0
  // Keep the token error association scoped to its mounted summary: clearing the
  // shared error must also clear aria-invalid and the extra description id.
  const tokenFieldInvalid = tokenSaveFailed && error !== ''

  function settle(label: string, failure: unknown): string {
    return settleOnboardingMutation(failure, label, {
      rotate: rotateIdempotencyKeyFn,
      clear: clearIdempotencyKeyFn,
    }).message
  }

  async function saveToken(): Promise<void> {
    if (narrow || busy || tokenDraft.trim() === '') {
      return
    }
    const label = 'telegram.token'
    setBusy(true)
    setError('')
    setNotice('')
    setTokenSaveFailed(false)
    try {
      const envelope = await setTokenFn(tokenDraft, idempotencyKeyFn(label))
      if (!envelope.data) {
        throw new Error('token 保存响应为空。')
      }
      clearIdempotencyKeyFn(label)
      setTokenStatusOverrides(envelope.data)
      setTokenDraft('')
      setNotice('token 已保存；只显示掩码与最后 4 位。')
      announce('token 已保存；只显示掩码与最后 4 位。')
    } catch (failure: unknown) {
      setTokenSaveFailed(true)
      setError(settle(label, failure))
    } finally {
      setBusy(false)
    }
  }

  async function saveAndComplete(): Promise<void> {
    if (narrow || busy || completing || !draft || testDisabledReason !== '') {
      return
    }
    const label = 'telegram.settings'
    setBusy(true)
    setError('')
    setTokenSaveFailed(false)
    setChatError('')
    setUserError('')
    try {
      const envelope = await updateSettingsFn(
        {
          chat_ids: draft.chatIds,
          allowed_user_ids: draft.allowedUserIds,
          toggles: draft.toggles,
        },
        idempotencyKeyFn(label),
      )
      if (!envelope.data) {
        throw new Error('Telegram 保存响应为空。')
      }
      clearIdempotencyKeyFn(label)
      onComplete()
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
      setBusy(false)
    }
  }

  async function sendTest(): Promise<void> {
    if (narrow || busy || !data || !draft || testDisabledReason !== '') {
      return
    }
    const settingsLabel = 'telegram.test-settings'
    const label = 'telegram.test'
    // Settle the key that was actually in flight: the settings save's key
    // until it succeeds and is cleared, then the test send's key.
    let activeLabel = settingsLabel
    setBusy(true)
    setTestError('')
    try {
      // Persist the current draft first so the test targets an allowlisted chat;
      // the send stays a separate explicit action with its own key.
      const saved = await updateSettingsFn(
        {
          chat_ids: draft.chatIds,
          allowed_user_ids: draft.allowedUserIds,
          toggles: draft.toggles,
        },
        idempotencyKeyFn(settingsLabel),
      )
      if (!saved.data) {
        throw new Error('Telegram 保存响应为空。')
      }
      clearIdempotencyKeyFn(settingsLabel)
      activeLabel = label
      const envelope = await testFn(idempotencyKeyFn(label), draft.chatIds[0])
      if (envelope.data) {
        setTestReceipt(envelope.data)
      }
      clearIdempotencyKeyFn(label)
    } catch (failure: unknown) {
      const settled = settleOnboardingMutation(failure, activeLabel, {
        rotate: rotateIdempotencyKeyFn,
        clear: clearIdempotencyKeyFn,
      })
      const apiError = failure instanceof SettingsApiError ? failure : null
      setTestError(telegramTestFailureReason(apiError, settled.message))
    } finally {
      setBusy(false)
    }
  }

  let testDisabledReason = ''
  if (!tokenConfigured) {
    testDisabledReason = '未配置 token；测试发送不可用。'
  } else if (chatCount === 0) {
    testDisabledReason = '没有 chat id；测试发送不可用。'
  }
  const canFinish = tokenConfigured && chatCount > 0
  const finishReason = !tokenConfigured
    ? 'onboarding-telegram-no-token'
    : chatCount === 0
      ? 'onboarding-telegram-no-chat'
      : 'onboarding-telegram-finish-hint'

  return (
    <div className="stack" data-testid="onboarding-telegram">
      <section className="panel" aria-labelledby="onboarding-telegram-bot-heading">
        <div className="panel-head">
          <h2 id="onboarding-telegram-bot-heading">Bot</h2>
          <span className="chip chip--calc">只读通道 · optional</span>
        </div>
        {load.state === 'loading' && (
          <p className="status-message" role="status">
            加载 Telegram 设置…
          </p>
        )}
        {load.state === 'error' && (
          <div className="stack" style={{ padding: '0 var(--oft-space-4)' }}>
            <p className="status-message status-message--error" role="alert">
              Telegram 设置不可用：{load.message ?? '未知错误'}
            </p>
            <button type="button" className="control-btn" onClick={load.retry}>
              重试
            </button>
          </div>
        )}
        {load.state === 'ready' && data && draft && (
          <div className="stack" style={{ padding: '0 var(--oft-space-4)' }}>
            <div className="field">
              <label className="field-label" htmlFor="onboarding-telegram-token">
                Bot token（只写）
              </label>
              <p className="mono" data-testid="onboarding-telegram-mask">
                {tokenConfigured ? (token?.masked ?? '已配置') : '未配置'}
              </p>
              <div className="action-row">
                <input
                  id="onboarding-telegram-token"
                  data-testid="onboarding-telegram-token"
                  className="settings-input mono"
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  value={tokenDraft}
                  disabled={narrow}
                  aria-describedby={
                    tokenFieldInvalid
                      ? 'onboarding-telegram-token-hint onboarding-telegram-error'
                      : 'onboarding-telegram-token-hint'
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
                  data-testid="onboarding-telegram-token-save"
                  disabled={narrow || tokenDraft.trim() === ''}
                  aria-disabled={busy || undefined}
                  onClick={() => void saveToken()}
                >
                  {tokenConfigured ? '替换 token' : '保存 token'}
                </button>
              </div>
              <p className="hint" id="onboarding-telegram-token-hint">
                保存后仅显示掩码；未知 chat 收到「未授权」而不是数据。没有查看原值的控件。
              </p>
            </div>

            <AllowlistEditor
              id="onboarding-telegram-chat"
              label="Chat ID"
              hint="群组/超级群为负整数；推送至少需要一个 chat。"
              values={draft.chatIds}
              onChange={(values) => {
                setChatError('')
                setDraft({ ...draft, chatIds: values })
              }}
              readOnly={narrow}
              allowNegative
              maximum={10}
              testId="onboarding-telegram-chat-editor"
              serverError={chatError}
            />
            <AllowlistEditor
              id="onboarding-telegram-user"
              label="Allowlist user ID"
              hint="正整数；与 chat 一起决定谁可以查询 bot。"
              values={draft.allowedUserIds}
              onChange={(values) => {
                setUserError('')
                setDraft({ ...draft, allowedUserIds: values })
              }}
              readOnly={narrow}
              maximum={20}
              testId="onboarding-telegram-user-editor"
              serverError={userError}
            />

            <div className="field">
              <span className="field-label" id="onboarding-telegram-toggles-label">
                推送事件
              </span>
              <div
                className="action-row"
                style={{ flexWrap: 'wrap' }}
                role="group"
                aria-labelledby="onboarding-telegram-toggles-label"
                aria-describedby="onboarding-telegram-toggles-hint"
              >
                {(Object.keys(TOGGLE_LABELS) as TelegramToggleName[]).map((name) => (
                  <label className="check" key={name}>
                    <input
                      type="checkbox"
                      className="edit"
                      data-testid={`onboarding-telegram-toggle-${name}`}
                      checked={draft.toggles[name]}
                      disabled={narrow}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          toggles: { ...draft.toggles, [name]: event.target.checked },
                        })
                      }
                    />{' '}
                    <span>
                      <strong>{TOGGLE_LABELS[name]}</strong>
                    </span>
                  </label>
                ))}
              </div>
              <p className="hint" id="onboarding-telegram-toggles-hint">
                任一开关开启时需要至少一个 chat id；全部关闭且无 chat 也允许。
              </p>
            </div>
          </div>
        )}
      </section>

      {load.state === 'ready' && data && draft && (
        <>
          {!tokenConfigured && (
            <p className="status-message" data-testid="onboarding-telegram-empty">
              <strong>未配置</strong>：跳过不会保存任何内容，Telegram 推送保持禁用。
            </p>
          )}
          {!tokenConfigured && (
            <p className="status-message" id="onboarding-telegram-no-token">
              先保存 bot token 才能完成或发送测试。
            </p>
          )}
          {tokenConfigured && chatCount === 0 && (
            <p className="status-message" id="onboarding-telegram-no-chat">
              至少添加一个 chat id 才能完成或发送测试。
            </p>
          )}

          <section className="panel" aria-labelledby="onboarding-telegram-test-heading">
            <div className="panel-head">
              <h2 id="onboarding-telegram-test-heading">测试消息</h2>
            </div>
            <div className="stack" style={{ padding: '0 var(--oft-space-4)' }}>
              <div className="action-row">
                <button
                  type="button"
                  className="control-btn"
                  data-testid="onboarding-telegram-test"
                  disabled={narrow}
                  aria-disabled={busy || testDisabledReason !== '' || undefined}
                  aria-describedby={
                    testDisabledReason
                      ? 'onboarding-telegram-test-reason'
                      : 'onboarding-telegram-finish-hint'
                  }
                  onClick={() => void sendTest()}
                >
                  发送测试
                </button>
                <span
                  className={testDisabledReason ? 'hint' : 'visually-hidden'}
                  id="onboarding-telegram-test-reason"
                  data-testid="onboarding-telegram-test-reason"
                  role="status"
                >
                  {testDisabledReason}
                </span>
              </div>
              <TelegramTestReceipt receipt={testReceipt} />
              {testError && (
                <p className="status-message status-message--error" role="alert">
                  {testError}
                </p>
              )}
            </div>
          </section>

          {notice && <p className="status-message">{notice}</p>}
          {error && (
            <p
              className="status-message status-message--error"
              id="onboarding-telegram-error"
              ref={errorSummaryRef}
              tabIndex={-1}
              role="alert"
            >
              {error}
            </p>
          )}

          <form
            className="settings-actions"
            noValidate
            onSubmit={(event) => {
              event.preventDefault()
              if (canFinish) {
                void saveAndComplete()
              }
            }}
          >
            <button
              type="submit"
              className="control-btn"
              disabled={narrow}
              aria-disabled={!canFinish || completing || busy || undefined}
              aria-describedby={finishReason}
              data-testid="onboarding-telegram-finish"
            >
              {completing ? '保存中…' : '保存并继续'}
            </button>
            <a
              className="btn"
              href="/onboarding/5"
              data-testid="onboarding-telegram-skip"
              onClick={() => announce('已跳过 Telegram：不保存任何内容；推送保持禁用。')}
            >
              暂时跳过
            </a>
            <span className="hint" id="onboarding-telegram-finish-hint">
              步骤 4 为 optional：跳过不会让引导失败，但 Telegram 推送保持禁用。
            </span>
          </form>
        </>
      )}
    </div>
  )
}
