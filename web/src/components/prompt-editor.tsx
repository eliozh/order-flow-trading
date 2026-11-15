/**
 * Pure client-side prompt text helpers plus the editor form.
 *
 * Template bodies are untrusted text and are rendered as text nodes only. The
 * preview substitutes every placeholder with a clearly redacted summary; it
 * never interpolates configured secrets or runtime market data and never
 * executes template text.
 */

import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

import { PROMPT_ROLE_LABELS, type PromptRole, type PromptVersionData } from '../api/prompts'

/** Optional variables accepted by the server for each role. */
export const OPTIONAL_PROMPT_VARIABLES: Record<PromptRole, readonly string[]> = {
  ANALYSIS: ['as_of', 'snapshot'],
  ENTRY_RECHECK: ['side', 'thesis'],
  MANAGEMENT: [],
  REVIEW: [],
}

export const PREVIEW_NOTICE = (
  <>
    <span lang="en">secrets are never included; market text is untrusted data</span>
    {' · 预览已脱敏，密钥永不包含'}
  </>
)

export interface PromptTextIssue {
  empty: boolean
  malformed: boolean
  missing: string[]
  unknown: string[]
  knowledgeOutsideTags: boolean
  valid: boolean
}

const PLACEHOLDER_SOURCE = String.raw`\{\{\s*([a-z][a-z0-9_]*)\s*\}\}`
const KNOWLEDGE_TAG_SOURCE = String.raw`<\s*(/?)knowledge\s*>`

function placeholders(body: string): RegExpMatchArray[] {
  return Array.from(body.matchAll(new RegExp(PLACEHOLDER_SOURCE, 'g')))
}

/** Placeholder names in order of first appearance, de-duplicated. */
export function variablesInPromptText(body: string): string[] {
  const variables: string[] = []
  for (const match of placeholders(body)) {
    if (!variables.includes(match[1])) {
      variables.push(match[1])
    }
  }
  return variables
}

function knowledgeTagIntervals(body: string): Array<[number, number]> {
  const intervals: Array<[number, number]> = []
  let opening: number | null = null
  for (const tag of body.matchAll(new RegExp(KNOWLEDGE_TAG_SOURCE, 'g'))) {
    const index = tag.index ?? 0
    if (tag[1] !== '/') {
      opening = index + tag[0].length
    } else if (opening !== null) {
      intervals.push([opening, index])
      opening = null
    }
  }
  return intervals
}

/**
 * Validate a draft against the active version's required variables.
 *
 * Mirrors the server's rules so an invalid template is blocked inline with no
 * write: empty body, malformed braces, unknown placeholders, a missing
 * required variable, or `{{knowledge}}` outside `<knowledge>` delimiters.
 */
export function validatePromptText(
  body: string,
  requiredVariables: readonly string[],
  role: PromptRole,
): PromptTextIssue {
  const matches = placeholders(body)
  const variables = variablesInPromptText(body)
  const malformed =
    body.split('{{').length - 1 !== matches.length || body.split('}}').length - 1 !== matches.length
  const allowed = new Set<string>([...requiredVariables, ...OPTIONAL_PROMPT_VARIABLES[role]])
  const unknown = variables.filter((name) => !allowed.has(name))
  const missing = requiredVariables.filter((name) => !variables.includes(name))
  let knowledgeOutsideTags = false
  if (variables.includes('knowledge')) {
    const intervals = knowledgeTagIntervals(body)
    knowledgeOutsideTags = matches
      .filter((match) => match[1] === 'knowledge')
      .some((match) => {
        const start = match.index ?? 0
        const end = start + match[0].length
        return !intervals.some(([from, to]) => start >= from && end <= to)
      })
  }
  const empty = body.trim() === ''
  return {
    empty,
    malformed,
    missing,
    unknown,
    knowledgeOutsideTags,
    valid:
      !empty && !malformed && missing.length === 0 && unknown.length === 0 && !knowledgeOutsideTags,
  }
}

/**
 * The literal, redacted preview of one template body.
 *
 * Each `{{variable}}` becomes `[redacted: name]`. No configured secret and no
 * runtime market value is ever substituted, and hostile text (`<script>`, a
 * `javascript:` link) stays literal because it is rendered as a text node.
 */
export function renderRedactedPreview(body: string): string {
  return body.replace(
    new RegExp(PLACEHOLDER_SOURCE, 'g'),
    (_match, name: string) => `[redacted: ${name}]`,
  )
}

export interface PromptEditorProps {
  promptId: string
  role: PromptRole
  active: PromptVersionData
  editing: boolean
  draft: string
  author: string
  reason: string
  issue: PromptTextIssue | null
  saving: boolean
  readOnly: boolean
  statusMessage: string
  liveMessage: string
  errorText: string
  errorSummaryRef: RefObject<HTMLDivElement | null>
  onChangeBody: (value: string) => void
  onChangeAuthor: (value: string) => void
  onChangeReason: (value: string) => void
  onStartEdit: () => void
  onDiscard: () => void
  onSave: (activate: boolean) => void
}

function issueMessages(issue: PromptTextIssue): string[] {
  const messages: string[] = []
  if (issue.empty) {
    messages.push('模板正文不能为空')
  }
  if (issue.malformed) {
    messages.push('占位符语法错误：请成对使用 {{ 与 }}')
  }
  for (const name of issue.missing) {
    messages.push(`缺少必需变量 {{${name}}}`)
  }
  for (const name of issue.unknown) {
    messages.push(`未知变量 {{${name}}}`)
  }
  if (issue.knowledgeOutsideTags) {
    messages.push('{{knowledge}} 必须位于 <knowledge>...</knowledge> 内')
  }
  return messages
}

/** F10b.1: read-only until "编辑"; saving always creates a new version. */
export function PromptEditor({
  promptId,
  role,
  active,
  editing,
  draft,
  author,
  reason,
  issue,
  saving,
  readOnly,
  statusMessage,
  liveMessage,
  errorText,
  errorSummaryRef,
  onChangeBody,
  onChangeAuthor,
  onChangeReason,
  onStartEdit,
  onDiscard,
  onSave,
}: PromptEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const editButtonRef = useRef<HTMLButtonElement>(null)
  const wasEditing = useRef(false)
  const messages = issue === null ? [] : issueMessages(issue)
  const invalid = issue !== null && !issue.valid
  const saveLocked = readOnly || !editing

  // The "编辑" control unmounts when editing begins; move focus into the body
  // so the keyboard flow (editor -> variables -> save) never drops to <body>.
  useEffect(() => {
    if (editing) {
      textareaRef.current?.focus()
    }
  }, [editing])

  // After save or discard the editor leaves edit mode. Move focus to the
  // re-rendered "编辑" control instead of letting native disabled/unmounted
  // buttons drop it to <body>.
  useEffect(() => {
    if (editing) {
      wasEditing.current = true
      return
    }
    if (!wasEditing.current || saving) {
      return
    }
    wasEditing.current = false
    editButtonRef.current?.focus()
  }, [editing, saving])

  return (
    <section className="panel" aria-labelledby="prompt-editor-heading">
      <div className="panel-head">
        <h2 id="prompt-editor-heading">
          <span className="mono">{promptId}</span> · v{active.version}{' '}
          <span className="chip chip--calc">
            <span lang="en">active</span>
          </span>
        </h2>
        <span className="action-row">
          <a className="btn" href={`/settings/prompts/${encodeURIComponent(promptId)}/history`}>
            历史
          </a>
          {!editing && (
            <button
              ref={editButtonRef}
              type="button"
              className="control-btn"
              onClick={onStartEdit}
              disabled={readOnly || saving}
            >
              编辑
            </button>
          )}
        </span>
      </div>

      <p
        role="status"
        className="visually-hidden"
        data-testid="prompt-editor-status"
        aria-live="polite"
      >
        {liveMessage}
      </p>

      {errorText !== '' && (
        <div
          className="status-message status-message--error"
          role="alert"
          tabIndex={-1}
          ref={errorSummaryRef}
          data-testid="prompt-editor-error"
        >
          <span aria-hidden="true">⚠</span> 错误：{errorText}{' '}
          <a href="#prompt-body">转到模板正文</a>
        </div>
      )}

      <form
        className="stack prompt-form"
        aria-label="编辑 Prompt 模板"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          onSave(false)
        }}
      >
        <div className="field">
          <label className="field-label" htmlFor="prompt-body">
            模板正文{editing ? '' : '（只读）'}{' '}
            <span className="chip chip--muted">{PROMPT_ROLE_LABELS[role]}</span>
          </label>
          <textarea
            ref={textareaRef}
            id="prompt-body"
            className="prompt-textarea"
            spellCheck={false}
            readOnly={!editing}
            disabled={readOnly}
            value={draft}
            aria-invalid={invalid || undefined}
            aria-describedby="prompt-body-hint prompt-body-issues"
            onChange={(event) => onChangeBody(event.target.value)}
          />
          <p className="hint" id="prompt-body-hint">
            保存创建新版本，永不原地编辑。语法与必需变量在保存前校验。既有决策继续绑定其原始版本。
          </p>
          <div id="prompt-body-issues" data-testid="prompt-editor-issues">
            {messages.map((message) => (
              <p className="field-error" key={message}>
                {message}
              </p>
            ))}
          </div>
        </div>

        <div className="prompt-meta">
          <div className="field">
            <label className="field-label" htmlFor="prompt-author">
              作者
            </label>
            <input
              id="prompt-author"
              className="num-input"
              value={author}
              maxLength={64}
              disabled={readOnly || !editing || saving}
              onChange={(event) => onChangeAuthor(event.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="prompt-reason">
              变更原因
            </label>
            <input
              id="prompt-reason"
              className="num-input"
              value={reason}
              maxLength={500}
              disabled={readOnly || !editing || saving}
              onChange={(event) => onChangeReason(event.target.value)}
            />
          </div>
        </div>

        <div className="action-row">
          {editing && (
            <button
              type="button"
              className="control-btn"
              aria-disabled={saving || undefined}
              onClick={() => {
                if (!saving) {
                  onDiscard()
                }
              }}
            >
              丢弃
            </button>
          )}
          <button
            type="submit"
            className="control-btn"
            disabled={saveLocked}
            aria-disabled={saving || undefined}
            onClick={(event) => {
              if (saveLocked || saving) {
                event.preventDefault()
              }
            }}
          >
            保存为新版本
          </button>
          <button
            type="button"
            className="control-btn"
            disabled={saveLocked}
            aria-disabled={saving || undefined}
            onClick={() => {
              if (!saveLocked && !saving) {
                onSave(true)
              }
            }}
          >
            保存并激活
          </button>
          <span className="hint">
            新版本默认 <span lang="en">DRAFT</span>
            ；选择保存并激活会在同一次写入后显式激活并审计。
          </span>
        </div>
      </form>

      <p className="status-message" data-testid="prompt-editor-visible">
        {statusMessage}
      </p>
    </section>
  )
}
