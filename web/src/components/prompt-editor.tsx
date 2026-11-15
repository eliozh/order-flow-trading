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

import {
  MAX_PROMPT_BODY_BYTES,
  PROMPT_ROLE_LABELS,
  type PromptRole,
  type PromptVersionData,
} from '../api/prompts'

/** Optional variables accepted by the server for each role. */
export const OPTIONAL_PROMPT_VARIABLES: Record<PromptRole, readonly string[]> = {
  ANALYSIS: ['as_of', 'snapshot'],
  ENTRY_RECHECK: ['side', 'thesis'],
  MANAGEMENT: [],
  REVIEW: [],
}

export const PREVIEW_NOTICE = (
  <>
    <span lang="en">
      placeholders become [redacted: name]; no runtime value or configured secret is injected;
      template text renders literally
    </span>
    {' · 占位符替换为 [redacted: name]；不注入运行时值或已配置密钥；模板文本按字面显示'}
  </>
)

export interface PromptTextIssue {
  empty: boolean
  malformed: boolean
  /** 1-based line of the first unmatched ``{{``/``}}``; null when balanced. */
  malformedLine: number | null
  /** 1-based column of the first unmatched ``{{``/``}}``; null when balanced. */
  malformedColumn: number | null
  missing: string[]
  unknown: string[]
  knowledgeOutsideTags: boolean
  tooLarge: boolean
  byteLength: number
  valid: boolean
}

export interface LineColumn {
  line: number
  column: number
}

/** First-occurrence line number for every placeholder in a body. */
export function variableLineNumbers(body: string): Record<string, number> {
  const lines: Record<string, number> = {}
  for (const match of placeholders(body)) {
    const name = match[1]
    if (!(name in lines)) {
      lines[name] = lineColumnAt(body, match.index ?? 0).line
    }
  }
  return lines
}

/**
 * Pure 1-based line/column math for a character offset.
 *
 * A newline advances the line and resets the column to 1, so an offset that
 * points at the first character after ``\n`` reads as the next line's column 1.
 */
export function lineColumnAt(body: string, offset: number): LineColumn {
  const end = Math.max(0, Math.min(offset, body.length))
  let line = 1
  let column = 1
  for (let index = 0; index < end; index += 1) {
    if (body[index] === '\n') {
      line += 1
      column = 1
    } else {
      column += 1
    }
  }
  return { line, column }
}

/**
 * Offset of the first unmatched ``{{`` or ``}}`` (string index), or null.
 *
 * Non-overlapping token pairs are pushed/popped; an unmatched close is
 * returned immediately and a leftover open reports the earliest one.
 */
export function firstUnmatchedOffset(body: string): number | null {
  const open: number[] = []
  let index = 0
  while (index < body.length - 1) {
    const pair = body.slice(index, index + 2)
    if (pair === '{{') {
      open.push(index)
      index += 2
      continue
    }
    if (pair === '}}') {
      if (open.length === 0) {
        return index
      }
      open.pop()
      index += 2
      continue
    }
    index += 1
  }
  return open.length > 0 ? open[0] : null
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
 * write: empty body, malformed braces, a body over the 64 KiB UTF-8 budget,
 * unknown placeholders, a missing required variable, or `{{knowledge}}`
 * outside `<knowledge>` delimiters.
 */
export function validatePromptText(
  body: string,
  requiredVariables: readonly string[],
  role: PromptRole,
): PromptTextIssue {
  const matches = placeholders(body)
  const variables = variablesInPromptText(body)
  // Keep parity with the server: every ``{{`` and ``}}`` must belong to one
  // valid placeholder, so a balanced-but-empty ``{{ }}`` is still malformed.
  const malformed =
    body.split('{{').length - 1 !== matches.length || body.split('}}').length - 1 !== matches.length
  const malformedOffset = firstUnmatchedOffset(body)
  const position =
    malformed && malformedOffset !== null ? lineColumnAt(body, malformedOffset) : null
  const byteLength = new TextEncoder().encode(body).length
  const tooLarge = byteLength > MAX_PROMPT_BODY_BYTES
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
    malformedLine: position?.line ?? null,
    malformedColumn: position?.column ?? null,
    missing,
    unknown,
    knowledgeOutsideTags,
    tooLarge,
    byteLength,
    valid:
      !empty &&
      !malformed &&
      !tooLarge &&
      missing.length === 0 &&
      unknown.length === 0 &&
      !knowledgeOutsideTags,
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
  /** Field named by a server refusal's details; scopes the summary deep link. */
  errorField: 'author' | 'reason' | 'body' | null
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
    messages.push(
      issue.malformedLine !== null && issue.malformedColumn !== null
        ? `占位符语法错误：第 ${issue.malformedLine} 行第 ${issue.malformedColumn} 列；请成对使用 {{ 与 }}`
        : '占位符语法错误：请成对使用 {{ 与 }}',
    )
  }
  if (issue.tooLarge) {
    messages.push('模板正文超过 64 KiB（UTF-8）限制')
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

function errorLinkTarget(errorField: PromptEditorProps['errorField']): {
  href: string
  label: string
} {
  if (errorField === 'author') {
    return { href: '#prompt-author', label: '转到作者' }
  }
  if (errorField === 'reason') {
    return { href: '#prompt-reason', label: '转到变更原因' }
  }
  return { href: '#prompt-body', label: '转到模板正文' }
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
  errorField,
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
  const errorLink = errorLinkTarget(errorField)

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
          <a href={errorLink.href}>{errorLink.label}</a>
        </div>
      )}

      <form
        className="stack prompt-form"
        aria-label="编辑 Prompt 模板"
        noValidate
        onSubmit={(event) => {
          // The save controls are explicit buttons; Enter never writes.
          event.preventDefault()
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
            // Remaining focusable/scrollable/copyable at 375 is required: a
            // read-only body must be reachable by keyboard, switch, and voice.
            readOnly={readOnly || !editing}
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
              readOnly={readOnly || !editing || saving}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  // Enter must never trigger an implicit form submit here.
                  event.preventDefault()
                }
              }}
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
              readOnly={readOnly || !editing || saving}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  // Enter must never trigger an implicit form submit here.
                  event.preventDefault()
                }
              }}
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
            type="button"
            className="control-btn"
            disabled={saveLocked}
            aria-disabled={saving || undefined}
            onClick={() => {
              if (!saveLocked && !saving) {
                onSave(false)
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
