/**
 * F10b.2 unified diff renderer.
 *
 * Diff lines and version bodies are untrusted text. They are rendered as plain
 * text nodes inside `<pre>`; no HTML, markdown, or link interpretation happens
 * here, so a body containing `<script>` or `javascript:` stays literal.
 */

import type { ReactNode } from 'react'

export type DiffLineTone = 'added' | 'removed' | 'hunk' | 'context'

/** Classify one unified-diff line by its leading marker. */
export function diffLineTone(line: string): DiffLineTone {
  if (line.startsWith('@@')) {
    return 'hunk'
  }
  if (line.startsWith('+') && !line.startsWith('+++')) {
    return 'added'
  }
  if (line.startsWith('-') && !line.startsWith('---')) {
    return 'removed'
  }
  return 'context'
}

export interface PromptDiffProps {
  diff: string
  /** Rendered labels; the English `active` marker is wrapped with lang="en". */
  fromLabel: ReactNode
  toLabel: ReactNode
  /** Plain-text labels for the scroll region's accessible name. */
  fromText: string
  toText: string
}

export function PromptDiff({ diff, fromLabel, toLabel, fromText, toText }: PromptDiffProps) {
  const lines = diff.split('\n')
  return (
    <section className="panel prompt-diff" aria-labelledby="prompt-diff-heading">
      <div className="panel-head">
        <h2 id="prompt-diff-heading">
          <span lang="en">Diff</span>
        </h2>
        <span className="mono hint" data-testid="prompt-diff-label">
          {fromLabel} → {toLabel}
        </span>
      </div>
      <div
        className="table-wrap"
        role="region"
        aria-label={`版本差异 ${fromText} 到 ${toText}`}
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable diff
        tabIndex={0}
      >
        <pre className="prompt-diff-body" data-testid="prompt-diff-body">
          {lines.map((line, index) => (
            <span
              // Diff lines have no stable identity; the list is immutable text.
              key={`${index}-${line.slice(0, 12)}`}
              className={`prompt-diff-line prompt-diff-${diffLineTone(line)}`}
            >
              {line}
            </span>
          ))}
        </pre>
      </div>
    </section>
  )
}
