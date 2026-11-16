import { useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import type { LLMProviderName, LLMProviderRow } from '../api/settings'
import { ConfirmDialog } from './confirm-dialog'

const PROVIDER_LABELS: Record<LLMProviderName, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
}

interface SecretFieldProps {
  provider: LLMProviderName
  row: LLMProviderRow
  readOnly: boolean
  busy: boolean
  /** Server-side key error for this provider (never a key value). */
  serverError?: string
  /**
   * Render the server error inline as an assertive alert and focus the input.
   * Enable only when no external error summary owns the announcement; on
   * `/settings/llm` the shared summary already announces and focuses it.
   */
  announceError?: boolean
  /**
   * The server definitively refused the write (nothing was stored). The
   * "未保存任何内容。" suffix is appended only then; an unknown outcome must
   * never claim the write did not happen.
   */
  serverErrorRefusal?: boolean
  /** Status text under the field (defaults to the mask / configured state). */
  statusText?: string
  onSave: (provider: LLMProviderName, value: string) => Promise<boolean>
  /** Omit to render a write-only field with no delete action (onboarding). */
  onDelete?: (provider: LLMProviderName) => Promise<void>
}

/**
 * A write-only provider key field (DESIGN A7).
 *
 * The value lives only in this input until submit, then is cleared. It is never
 * prefilled, echoed, logged, or shown again: after a save the row renders only
 * the server's fixed mask. There is deliberately no reveal control and no
 * endpoint behind one.
 *
 * The delete confirmation is a real modal (role=dialog, aria-modal, initial
 * focus on 取消, Escape, focus containment, focus restoration) so a keyboard
 * user is never dropped to <body>.
 */
export function SecretField({
  provider,
  row,
  readOnly,
  busy,
  serverError,
  announceError = false,
  serverErrorRefusal = false,
  statusText,
  onSave,
  onDelete,
}: SecretFieldProps) {
  const generatedId = useId()
  const inputId = `llm-key-input-${provider}`
  const labelId = `${generatedId}-label`
  const hintId = `${generatedId}-hint`
  const localErrorId = `${generatedId}-error`
  const serverErrorId = `${generatedId}-server-error`
  const maskId = `${generatedId}-mask`
  const [value, setValue] = useState('')
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [localError, setLocalError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const providerLabel = PROVIDER_LABELS[provider]

  const label = row.configured ? `替换 ${provider} 密钥（只写）` : `添加 ${provider} 密钥（只写）`
  const status =
    statusText ??
    (row.configured
      ? (row.masked ?? (row.key_source === 'env' ? '环境变量已配置' : '已配置'))
      : row.key_source === 'unavailable'
        ? '不可用'
        : '未配置')
  // The mask/configured state is part of the field's description so a
  // write-only status is announced when the input receives focus, not only in
  // browse mode.
  const describedBy = [
    maskId,
    hintId,
    localError ? localErrorId : null,
    announceError && serverError ? serverErrorId : null,
  ]
    .filter((item): item is string => item !== null)
    .join(' ')

  // A failed save keeps the typed value and announces the error; move focus to
  // the field so the operator can correct it immediately. When an external
  // summary owns the announcement (settings), it also owns the focus move.
  useEffect(() => {
    if (announceError && serverError) {
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [announceError, serverError])

  async function submit(): Promise<void> {
    if (readOnly || busy) {
      return
    }
    setLocalError('')
    if (!value.trim()) {
      setLocalError('请输入密钥；留空不会保存。')
      // H6: the empty-key error is announced (role=alert) and the key field
      // receives focus so a screen-reader user can correct it immediately.
      requestAnimationFrame(() => inputRef.current?.focus())
      return
    }
    const saved = await onSave(provider, value)
    if (saved) {
      // The browser no longer holds the plaintext after a successful save.
      setValue('')
    }
  }

  function submitOnEnter(event: KeyboardEvent<HTMLInputElement>): void {
    if (event.key === 'Enter') {
      event.preventDefault()
      void submit()
    }
  }

  function cancelDelete(): void {
    setConfirmingDelete(false)
  }

  function confirmDelete(): void {
    if (!onDelete) {
      return
    }
    setConfirmingDelete(false)
    void (async () => {
      await onDelete(provider)
      // The trigger unmounts on success; the key input is the row's stable
      // focus target then. On failure the modal hook restores the invoker.
      requestAnimationFrame(() => document.getElementById(inputId)?.focus())
    })()
  }

  return (
    <div className="field" role="group" aria-label={provider}>
      <label className="field-label" id={labelId} htmlFor={inputId}>
        {label}
      </label>
      <p className="mono" id={maskId} data-testid={`llm-key-mask-${provider}`}>
        {status}
      </p>
      <div className="action-row">
        <input
          id={inputId}
          data-testid={inputId}
          ref={inputRef}
          type="password"
          className="settings-input mono"
          autoComplete="new-password"
          spellCheck={false}
          value={value}
          disabled={readOnly}
          readOnly={busy}
          aria-describedby={describedBy}
          aria-invalid={localError || serverError ? true : undefined}
          placeholder={row.configured ? '输入新密钥以替换' : '粘贴密钥'}
          onChange={(event) => {
            setValue(event.target.value)
            setLocalError('')
          }}
          onKeyDown={submitOnEnter}
        />
        <button
          type="button"
          className="control-btn"
          data-testid={`llm-key-save-${provider}`}
          disabled={readOnly}
          aria-disabled={busy || undefined}
          aria-label={`${row.configured ? '替换密钥' : '保存密钥'}（${providerLabel}）`}
          onClick={() => void submit()}
        >
          {row.configured ? '替换密钥' : '保存密钥'}
        </button>
        {row.configured && onDelete && (
          <button
            type="button"
            id={`llm-key-delete-${provider}`}
            className="control-btn"
            disabled={readOnly}
            aria-disabled={busy || undefined}
            aria-label={`删除密钥（${providerLabel}）`}
            onClick={() => {
              if (!busy) {
                setConfirmingDelete(true)
              }
            }}
          >
            删除密钥
          </button>
        )}
      </div>
      <p className="hint" id={hintId}>
        密钥只写：保存后仅显示固定掩码与最后 4 位；没有查看原值的控件。粘贴允许，绝不会被阻止。
      </p>
      {localError && (
        <p className="status-message status-message--error" id={localErrorId} role="alert">
          错误：{localError}
        </p>
      )}
      {announceError && serverError && (
        <p className="status-message status-message--error" id={serverErrorId} role="alert">
          错误：{serverError}
          {serverErrorRefusal ? ' 未保存任何内容。' : ''}
        </p>
      )}
      {onDelete && (
        <ConfirmDialog
          open={confirmingDelete}
          idPrefix={`llm-delete-${provider}`}
          title={`删除 ${provider} 密钥`}
          description={`删除只移除 ${provider} 存储的密钥；同一 provider 的环境变量密钥，如果已设置，仍然生效；不会回退到其他 provider。两者都没有时，需要该 provider 的决策返回 LLM_PROVIDER_NOT_CONFIGURED。`}
          rows={[]}
          confirmLabel="确认删除"
          busy={busy}
          restoreFocusId={inputId}
          hint="密钥值从不显示；删除后仅保留最终配置状态。"
          onCancel={cancelDelete}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  )
}
