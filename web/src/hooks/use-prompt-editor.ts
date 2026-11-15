/**
 * F10b.1 editor state: load the template, validate a draft inline, and save a
 * new immutable version.
 *
 * There is no update path: one save is one POST that creates a new version.
 * A definitive refusal retires the pending idempotency key; an ambiguous
 * transport failure keeps it so a retry replays rather than writing twice.
 */

import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  clearPromptVersionKey,
  createPromptVersion,
  fetchPromptDetail,
  PromptApiError,
  type PromptDetailData,
  type PromptDetailEnvelope,
  type PromptRole,
  type PromptVersionData,
  type PromptVersionEnvelope,
  promptVersionKey,
  rotatePromptVersionKey,
} from '../api/prompts'
import { validatePromptText, type PromptTextIssue } from '../components/prompt-editor'
import { useSettingsReadonly } from '../components/settings-field'

export interface PromptEditorLoad {
  state: 'loading' | 'ready' | 'error'
  detail: PromptDetailData | null
  message?: string
}

export interface UsePromptEditorOptions {
  promptId: string
  fetchPromptDetailFn?: (promptId: string, signal?: AbortSignal) => Promise<PromptDetailEnvelope>
  createPromptVersionFn?: (
    promptId: string,
    input: { body: string; author?: string; reason?: string; activate?: boolean },
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<PromptVersionEnvelope>
  promptVersionKeyFn?: (promptId: string) => string
  rotatePromptVersionKeyFn?: (promptId: string) => string
  clearPromptVersionKeyFn?: (promptId: string) => void
  isCompact?: boolean
}

export interface PromptEditorState {
  readonly: boolean
  load: PromptEditorLoad
  active: PromptVersionData | null
  role: PromptRole | null
  editing: boolean
  draft: string
  author: string
  reason: string
  issue: PromptTextIssue | null
  saving: boolean
  statusMessage: string
  liveMessage: string
  errorText: string
  errorField: 'author' | 'reason' | 'body' | null
  errorSummaryRef: RefObject<HTMLDivElement | null>
  retry: () => void
  startEdit: () => void
  discard: () => void
  changeBody: (value: string) => void
  changeAuthor: (value: string) => void
  changeReason: (value: string) => void
  save: (activate: boolean) => Promise<void>
}

function activeVersionOf(detail: PromptDetailData | null): PromptVersionData | null {
  if (detail === null) {
    return null
  }
  return detail.versions.find((version) => version.version === detail.active_version) ?? null
}

function issueSummary(issue: PromptTextIssue): string {
  const parts: string[] = []
  if (issue.empty) {
    parts.push('模板正文不能为空')
  }
  if (issue.malformed) {
    parts.push(
      issue.malformedLine !== null && issue.malformedColumn !== null
        ? `占位符语法错误（第 ${issue.malformedLine} 行第 ${issue.malformedColumn} 列）`
        : '占位符语法错误',
    )
  }
  if (issue.tooLarge) {
    parts.push('模板正文超过 64 KiB（UTF-8）限制')
  }
  for (const name of issue.missing) {
    parts.push(`缺少必需变量 {{${name}}}`)
  }
  for (const name of issue.unknown) {
    parts.push(`未知变量 {{${name}}}`)
  }
  if (issue.knowledgeOutsideTags) {
    parts.push('{{knowledge}} 必须位于 <knowledge> 标签内')
  }
  return `无法保存：${parts.join('；')}。未写入任何内容。`
}

/** The first server-named field, scoped to the editor's deep-link targets. */
function refusalField(details: readonly { field?: string | null }[]): 'author' | 'reason' | 'body' {
  const field = details.find((detail) => typeof detail.field === 'string')?.field
  if (field === 'author' || field === 'reason') {
    return field
  }
  return 'body'
}

/** Combine the envelope message with any distinct ``details[].message`` values. */
function refusalText(message: string, details: readonly { message?: string }[]): string {
  const parts = [message, ...details.map((detail) => detail.message ?? '')].filter(
    (part) => part.trim() !== '',
  )
  return Array.from(new Set(parts)).join('；')
}

export function usePromptEditor({
  promptId,
  fetchPromptDetailFn = fetchPromptDetail,
  createPromptVersionFn = createPromptVersion,
  promptVersionKeyFn = promptVersionKey,
  rotatePromptVersionKeyFn = rotatePromptVersionKey,
  clearPromptVersionKeyFn = clearPromptVersionKey,
  isCompact,
}: UsePromptEditorOptions): PromptEditorState {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<PromptEditorLoad>({ state: 'loading', detail: null })
  const [attempt, setAttempt] = useState(0)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [author, setAuthor] = useState('web')
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorText, setErrorText] = useState('')
  const [errorField, setErrorField] = useState<'author' | 'reason' | 'body' | null>(null)
  const [focusTick, setFocusTick] = useState(0)
  const mounted = useRef(true)
  const inFlight = useRef(false)
  const errorSummaryRef = useRef<HTMLDivElement>(null)
  const settleFocusRef = useRef<'summary' | null>(null)

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
    fetchPromptDetailFn(promptId, controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad((previous) => ({
            state: 'error',
            detail: previous.detail,
            message: 'Prompt 响应为空。',
          }))
          return
        }
        setLoad({ state: 'ready', detail: data })
        const activeVersion =
          data.versions.find((version) => version.version === data.active_version) ?? null
        setDraft(activeVersion?.body ?? '')
        setEditing(false)
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        // A refetch after a confirmed write may fail; keep the previous detail
        // so the editor and its success message are not torn down.
        setLoad((previous) => ({
          state: 'error',
          detail: previous.detail,
          message: errorMessage(error),
        }))
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchPromptDetailFn, promptId, attempt])

  useEffect(() => {
    if (saving) {
      return
    }
    if (settleFocusRef.current === null) {
      return
    }
    settleFocusRef.current = null
    errorSummaryRef.current?.focus()
  }, [saving, focusTick])

  const active = activeVersionOf(load.detail)
  const role = load.detail?.role ?? null
  const issue =
    editing && active !== null && role !== null
      ? validatePromptText(draft, active.required_variables, role)
      : null

  function requestFocus(target: 'summary'): void {
    settleFocusRef.current = target
    setFocusTick((value) => value + 1)
  }

  function retry(): void {
    setAttempt((value) => value + 1)
  }

  function startEdit(): void {
    if (active === null || readonly || inFlight.current) {
      return
    }
    setDraft(active.body)
    setEditing(true)
    setStatusMessage('')
    setLiveMessage('编辑中：保存将创建新版本，永不原地编辑。')
    setErrorText('')
    setErrorField(null)
  }

  function discard(): void {
    if (inFlight.current) {
      return
    }
    setDraft(active?.body ?? '')
    setEditing(false)
    setErrorText('')
    setErrorField(null)
    const message = '已丢弃未保存的更改；未写入任何内容。'
    setStatusMessage(message)
    setLiveMessage(message)
  }

  function changeBody(value: string): void {
    if (inFlight.current) {
      return
    }
    setDraft(value)
    setErrorText('')
    setErrorField(null)
  }

  function changeAuthor(value: string): void {
    if (inFlight.current) {
      return
    }
    setAuthor(value)
    setErrorText('')
    setErrorField(null)
  }

  function changeReason(value: string): void {
    if (inFlight.current) {
      return
    }
    setReason(value)
    setErrorText('')
    setErrorField(null)
  }

  function handleSaveError(error: unknown): void {
    if (!mounted.current || isAbortError(error)) {
      return
    }
    const message = errorMessage(error)
    setLiveMessage('')
    if (error instanceof PromptApiError && error.kind === 'refusal') {
      const text = refusalText(message, error.details)
      const field = refusalField(error.details)
      if (error.code === 'IDEMPOTENCY_CONFLICT') {
        rotatePromptVersionKeyFn(promptId)
        setErrorText(`请求冲突；已生成新的 Idempotency-Key，可直接重试。${text ? ` ${text}` : ''}`)
      } else {
        clearPromptVersionKeyFn(promptId)
        setErrorText(text)
      }
      setErrorField(field)
      requestFocus('summary')
      return
    }
    // The POST may have committed; keep the key so a retry replays it.
    setErrorField('body')
    setErrorText(`结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`)
    requestFocus('summary')
  }

  async function save(activate: boolean): Promise<void> {
    if (!editing || saving || readonly || inFlight.current || active === null || role === null) {
      return
    }
    const currentIssue = validatePromptText(draft, active.required_variables, role)
    if (!currentIssue.valid) {
      setErrorText(issueSummary(currentIssue))
      setErrorField('body')
      setLiveMessage('')
      setStatusMessage('')
      requestFocus('summary')
      return
    }
    inFlight.current = true
    setSaving(true)
    setStatusMessage('')
    setErrorText('')
    setErrorField(null)
    setLiveMessage('保存中…')
    try {
      const envelope = await createPromptVersionFn(
        promptId,
        { body: draft, author, reason, activate },
        promptVersionKeyFn(promptId),
      )
      // The write may have committed even if the page unmounted; retire the
      // key before consulting the mounted guard.
      clearPromptVersionKeyFn(promptId)
      if (!mounted.current) {
        return
      }
      const created = envelope.data
      if (!created) {
        throw new Error('Prompt 响应为空。')
      }
      const stateLabel = created.status === 'ACTIVE' ? 'active' : created.status
      const message =
        created.status === 'ACTIVE'
          ? `已创建并激活 v${created.version}（${stateLabel}）；下一次分析使用 v${created.version}；audit 已写入；既有决策仍绑定其原始版本。`
          : `已创建 v${created.version}（${stateLabel}）；激活前下一次分析继续使用 active v${active.version}；既有决策仍绑定其原始版本。`
      setStatusMessage(message)
      setLiveMessage(message)
      // Leave edit mode showing the still-active body. Until a successful
      // refetch replaces the detail snapshot, the read-only body must match
      // the heading/preview (the saved draft is described by the message).
      setDraft(active.body)
      setEditing(false)
      setAttempt((value) => value + 1)
    } catch (error: unknown) {
      handleSaveError(error)
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setSaving(false)
      }
    }
  }

  return {
    readonly,
    load,
    active,
    role,
    editing,
    draft,
    author,
    reason,
    issue,
    saving,
    statusMessage,
    liveMessage,
    errorText,
    errorField,
    errorSummaryRef,
    retry,
    startEdit,
    discard,
    changeBody,
    changeAuthor,
    changeReason,
    save,
  }
}
