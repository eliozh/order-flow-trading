import { useEffect, useState } from 'react'

interface AllowlistEditorProps {
  /** Stable id prefix for labels, hints, and test hooks. */
  id: string
  label: string
  hint: string
  values: number[]
  onChange: (values: number[]) => void
  readOnly: boolean
  /** Chat ids may be nonzero negative (groups); user ids are positive. */
  allowNegative?: boolean
  maximum: number
  testId?: string
  /** A server refusal for this field; announced and tied to the add input. */
  serverError?: string
}

/**
 * A bounded id editor rendered as removable chips plus one add field.
 *
 * The field is labelled, the current list is announced as a group, and every
 * remove action has a text accessible name (never a bare glyph). Invalid input
 * is announced through a field-scoped alert and does not mutate the list.
 */
export function AllowlistEditor({
  id,
  label,
  hint,
  values,
  onChange,
  readOnly,
  allowNegative = false,
  maximum,
  testId,
  serverError,
}: AllowlistEditorProps) {
  const inputId = `${id}-input`
  const labelId = `${id}-label`
  const hintId = `${id}-hint`
  const errorId = `${id}-error`
  const listId = `${id}-list`
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  // ``undefined`` means "no pending focus move"; ``null`` targets the add input.
  const [focusTarget, setFocusTarget] = useState<number | null | undefined>(undefined)
  const kind = allowNegative ? '非零整数' : '正整数'

  useEffect(() => {
    if (focusTarget === undefined) {
      return
    }
    const target =
      focusTarget === null
        ? document.getElementById(inputId)
        : document.getElementById(`${id}-remove-${focusTarget}`)
    target?.focus()
    setFocusTarget(undefined)
  }, [focusTarget, id, inputId])

  function add(): void {
    if (readOnly) {
      return
    }
    setError('')
    const tokens = draft
      .split(/[\s,]+/)
      .map((token) => token.trim())
      .filter((token) => token.length > 0)
    if (tokens.length === 0) {
      setError('请输入至少一个 id。')
      return
    }
    const proposed = new Set(values)
    for (const token of tokens) {
      if (!/^-?\d+$/.test(token)) {
        setError(`${label} 必须是${kind}；不会保存。`)
        return
      }
      const value = Number(token)
      if (!Number.isSafeInteger(value) || value === 0 || (!allowNegative && value < 0)) {
        setError(`${label} 必须是${kind}；不会保存。`)
        return
      }
      proposed.add(value)
    }
    if (proposed.size > maximum) {
      setError(`${label} 最多 ${maximum} 个 id。`)
      return
    }
    const added = [...proposed].filter((value) => !values.includes(value))
    onChange([...proposed].sort((left, right) => left - right))
    setDraft('')
    setLiveMessage(`已添加 ${label} ${added.join(', ')}。`)
  }

  function remove(value: number): void {
    if (readOnly) {
      return
    }
    setError('')
    const index = values.indexOf(value)
    const remaining = values.filter((item) => item !== value)
    onChange(remaining)
    setLiveMessage(`已移除 ${label} ${value}。`)
    // The focused remove button unmounts; keep the keyboard user in place by
    // moving to the next chip's button (or the add input when none remain).
    setFocusTarget(remaining[index] ?? remaining[index - 1] ?? null)
  }

  const shownError = error || serverError || ''
  const describedBy = `${hintId}${shownError ? ` ${errorId}` : ''}`

  return (
    <div className="field" role="group" aria-labelledby={labelId} data-testid={testId}>
      <span className="field-label" id={labelId}>
        {label}{' '}
        <span className="chip chip--muted">
          {values.length}/{maximum}
        </span>
      </span>
      <ul
        id={listId}
        className="action-row"
        style={{ flexWrap: 'wrap', listStyle: 'none', padding: 0, margin: 0 }}
        aria-label={`${label} 当前值`}
      >
        {values.length === 0 ? (
          <li className="hint">未配置</li>
        ) : (
          values.map((value) => (
            <li key={value}>
              <span className="chip">
                <span className="mono">{value}</span>{' '}
                <button
                  type="button"
                  id={`${id}-remove-${value}`}
                  className="control-btn allowlist-remove"
                  disabled={readOnly}
                  aria-label={`移除 ${label} ${value}`}
                  onClick={() => remove(value)}
                >
                  移除
                </button>
              </span>
            </li>
          ))
        )}
      </ul>
      <p className="visually-hidden" role="status" data-testid={`${id}-announce`}>
        {liveMessage}
      </p>
      <div className="action-row">
        <label className="visually-hidden" htmlFor={inputId}>
          添加 {label}
        </label>
        <input
          id={inputId}
          data-testid={`${id}-input`}
          className="settings-input mono"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          disabled={readOnly}
          aria-describedby={describedBy}
          aria-invalid={shownError ? true : undefined}
          placeholder={allowNegative ? '例如 1001 或 -100200300' : '例如 2002'}
          onChange={(event) => {
            setDraft(event.target.value)
            setError('')
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              add()
            }
          }}
        />
        <button
          type="button"
          className="control-btn"
          data-testid={`${id}-add`}
          disabled={readOnly}
          aria-label={`添加 ${label}`}
          onClick={add}
        >
          添加
        </button>
      </div>
      <p className="hint" id={hintId}>
        {hint}
      </p>
      {shownError ? (
        <p className="status-message status-message--error" id={errorId} role="alert">
          错误：{shownError}
        </p>
      ) : null}
    </div>
  )
}
