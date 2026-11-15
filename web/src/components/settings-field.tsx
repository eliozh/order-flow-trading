import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

import type { SettingSource } from '../api/settings'

/**
 * Settings-local read-only signal. Only a phone-width *coarse-pointer* surface
 * is read-only, so a true 768px tablet stays editable and 200%/400% desktop
 * zoom (720/360 CSS px with a fine pointer) keeps the form operable.
 */
export const SETTINGS_READONLY_QUERY = '(max-width: 767.98px) and (pointer: coarse)'

export function detectSettingsReadonly(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false
  }
  return window.matchMedia(SETTINGS_READONLY_QUERY).matches
}

/** Reactive settings-only read-only signal; ``override`` wins for tests/SSR. */
export function useSettingsReadonly(override?: boolean): boolean {
  const [matches, setMatches] = useState<boolean>(detectSettingsReadonly)

  useEffect(() => {
    if (override !== undefined) {
      return
    }
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return
    }
    const media = window.matchMedia(SETTINGS_READONLY_QUERY)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [override])

  return override ?? matches
}

interface SourceChipProps {
  source: SettingSource
  testId?: string
}

/** The reported provenance chip; the source is never inferred or guessed. */
export function SourceChip({ source, testId }: SourceChipProps) {
  return (
    <span className={`chip ${source === 'user' ? 'chip--calc' : 'chip--ok'}`} data-testid={testId}>
      source: {source}
    </span>
  )
}

interface ReadOnlyFieldProps {
  id: string
  label: string
  value: string
  note?: string
  testId?: string
}

/**
 * A value shown for context that is never persisted by this slice. The visible
 * "read-only" chip is part of the label so screen readers announce it too.
 */
export function ReadOnlyField({ id, label, value, note, testId }: ReadOnlyFieldProps) {
  return (
    <div className="field" role="group" aria-labelledby={`${id}-label`}>
      <span className="field-label" id={`${id}-label`}>
        {label} <span className="chip chip--muted">read-only</span>
      </span>
      <p id={id} className="readonly-value mono" data-testid={testId}>
        {value}
      </p>
      {note ? <p className="hint">{note}</p> : null}
    </div>
  )
}

interface SettingsNoticeProps {
  id?: string
  tone: 'status' | 'error'
  children: ReactNode
}

/** A save/error announcement with the required live-region role. */
export function SettingsNotice({ id, tone, children }: SettingsNoticeProps) {
  return (
    <p
      id={id}
      className={tone === 'error' ? 'status-message status-message--error' : 'status-message'}
      role={tone === 'error' ? 'alert' : 'status'}
    >
      {children}
    </p>
  )
}
