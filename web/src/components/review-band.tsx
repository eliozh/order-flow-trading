import { numberOrNull, type JournalReviewData } from '../api/journal'
import { confidenceBand } from '../api/reports'
import { ProvenanceStamp } from './provenance'

const READONLY_NOTE_ID = 'journal-review-readonly-note'

interface ReviewBandProps {
  review: JournalReviewData | null
  loading?: boolean
  readError?: string | null
  generating?: boolean
  /** A polite generation announcement ("复盘已生成"). */
  announcement?: string | null
  actionError?: string | null
  actionErrorKind?: 'definitive' | 'unknown' | null
  readonly?: boolean
  onGenerate: () => void
  onRetryRead?: () => void
}

const BAND_LABELS = { high: '高', medium: '中', low: '低' } as const
const BAND_CLASSES = {
  high: 'chip chip--ok',
  medium: 'chip chip--warn',
  low: 'chip chip--danger',
} as const

function asString(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null
}

/**
 * The allowlisted provenance rows. Unknown keys are never rendered, so a
 * tampered envelope cannot smuggle prompt text or other untrusted strings into
 * the band; the values are always React text nodes.
 */
function provenanceRows(
  provenance: Record<string, unknown> | null | undefined,
): Array<[string, string]> {
  const rows: Array<[string, string]> = []
  if (!provenance) {
    return rows
  }
  const provider = asString(provenance.provider)
  if (provider) {
    rows.push(['Provider', provider])
  }
  const model = asString(provenance.model)
  if (model) {
    rows.push(['Model', model])
  }
  const promptId = asString(provenance.prompt_id)
  const promptVersion =
    typeof provenance.prompt_version === 'number' ? `v${provenance.prompt_version}` : null
  if (promptId || promptVersion) {
    rows.push(['Prompt', [promptId, promptVersion].filter(Boolean).join(' ') || '—'])
  }
  const cost = asRecord(provenance.cost)
  if (cost) {
    const amount = asString(cost.amount_usd)
    const currency = asString(cost.currency) ?? 'USD'
    if (amount) {
      rows.push(['Cost', `${amount} ${currency}`])
    } else if (cost.unpriced === true) {
      rows.push(['Cost', 'unpriced'])
    }
  }
  const usage = asRecord(provenance.usage)
  if (usage && typeof usage.total_tokens === 'number') {
    rows.push(['Tokens', String(usage.total_tokens)])
  }
  const knowledge = asString(provenance.knowledge_artifact_hash)
  if (knowledge) {
    rows.push(['Knowledge', knowledge])
  }
  const knowledgeStatus = asString(provenance.knowledge_status)
  if (knowledgeStatus) {
    rows.push(['Knowledge status', knowledgeStatus])
  }
  return rows
}

/** One shared, allowlisted provenance block for the review and entry decision. */
export function ReviewProvenance({
  provenance,
}: {
  provenance: Record<string, unknown> | null | undefined
}) {
  const rows = provenanceRows(provenance)
  if (rows.length === 0) {
    return (
      <p className="band-note" lang="en">
        Provenance N/A
      </p>
    )
  }
  return (
    <dl className="band-meta">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt lang="en">{label}</dt>
          <dd lang="en">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/**
 * The advisory LLM review band.
 *
 * The review is model output: it is rendered as text nodes only, it always
 * carries the "仅建议，不自动修改规则" heading, and no control can turn it into a
 * rule, prompt, risk cap, or order. A failed generation never blanks the
 * surrounding trade metrics, and a retry reuses the caller's idempotency key.
 */
export function ReviewBand({
  review,
  loading = false,
  readError = null,
  generating = false,
  announcement = null,
  actionError = null,
  actionErrorKind = null,
  readonly = false,
  onGenerate,
  onRetryRead,
}: ReviewBandProps) {
  const band = confidenceBand(review?.confidence ?? null)
  const confidenceValue = numberOrNull(review?.confidence ?? null)
  const disabled = readonly || generating
  const buttonLabel =
    actionError !== null ? '重试生成' : review?.status === 'FOUND' ? '重新生成' : '生成复盘'
  const showAction =
    !loading &&
    review?.status !== 'PARTIAL' &&
    // Action copy tells the user to retry after a POST failure; keep the control
    // visible even when the background read also failed.
    (actionError !== null || readError === null || review?.status === 'FOUND')

  const generate = (): void => {
    if (disabled) {
      return
    }
    // The caller owns the request intent: an explicit regenerate vs. a replay
    // of the failed attempt must not be inferred from the review status here.
    onGenerate()
  }

  return (
    <section
      className="band band--llm"
      aria-labelledby="journal-review-heading"
      data-testid="review-band"
    >
      <div className="band-head">
        <h2 id="journal-review-heading">LLM 复盘（仅建议，不自动修改规则）</h2>
        <ProvenanceStamp kind="LLM" />
      </div>
      <p className="band-note">仅建议：本复盘不会修改任何规则、prompt、风险上限或订单。</p>

      {readonly && (
        <p id={READONLY_NOTE_ID} className="readonly-note" role="note">
          移动端只读：复盘生成请在桌面端或 CLI 完成。Kill switch 仍可用。
        </p>
      )}

      {/*
        The live region is a sibling of the aria-busy subtree on purpose: a
        polite status nested inside an aria-busy element is not announced, so
        progress and completion would otherwise be silent for screen readers.
      */}
      <p role="status" aria-live="polite" data-testid="journal-review-status">
        {generating ? '正在生成复盘…' : (announcement ?? '')}
      </p>

      <div data-testid="review-band-body" aria-busy={generating}>
        {generating && (
          <p className="status-message" data-testid="journal-review-generating">
            正在生成复盘…
          </p>
        )}

        {loading && (
          <p className="status-message" role="status">
            加载复盘…
          </p>
        )}

        {readError !== null && (
          <div className="journal-review-actions">
            <p className="llm-reason" role="alert">
              复盘读取失败：{readError}
            </p>
            {onRetryRead && (
              <button type="button" className="btn" onClick={onRetryRead}>
                重试读取复盘
              </button>
            )}
          </div>
        )}

        {!loading && review?.status === 'FOUND' && (
          <>
            <dl className="kv">
              <dt lang="en">Decision</dt>
              <dd className="mono">{review.decision_id ?? '—'}</dd>
              <dt lang="en">Confidence</dt>
              <dd className="mono">
                {confidenceValue === null ? '—' : confidenceValue.toFixed(2)}{' '}
                {band && <span className={BAND_CLASSES[band]}>{BAND_LABELS[band]}</span>}
              </dd>
              <dt lang="en">Validation</dt>
              <dd className="mono">{review.validation_status ?? '—'}</dd>
            </dl>
            {review.rationale !== null && <p className="prose">{review.rationale}</p>}
            {review.caveats.length > 0 && (
              <ul className="journal-caveats">
                {review.caveats.map((caveat, index) => (
                  <li key={`${index}-${caveat.slice(0, 16)}`}>{caveat}</li>
                ))}
              </ul>
            )}
            <ReviewProvenance provenance={review.provenance} />
          </>
        )}

        {!loading && readError === null && review?.status === 'MISSING' && (
          <>
            <p className="llm-reason">尚未生成复盘。</p>
            {review.reason !== null && <p className="llm-reason">原因：{review.reason}</p>}
          </>
        )}

        {!loading && readError === null && review?.status === 'PARTIAL' && (
          <p className="llm-reason">无法展示建议内容：{review.reason ?? '复盘数据不完整'}</p>
        )}

        {!loading && readError === null && review === null && (
          <p className="llm-reason">尚未生成复盘。</p>
        )}

        {showAction && (
          <div className="journal-review-actions">
            <button
              type="button"
              className="btn"
              aria-disabled={disabled}
              aria-busy={generating}
              aria-describedby={readonly ? READONLY_NOTE_ID : undefined}
              onClick={generate}
            >
              {buttonLabel}
            </button>
          </div>
        )}

        {actionError !== null && (
          <div className="journal-review-actions" role="alert" data-testid="journal-review-error">
            <p className="llm-reason">复盘生成失败：{actionError}</p>
            {actionErrorKind === 'unknown' ? (
              <p className="llm-reason">
                结果未知：服务器可能已保存复盘。重试会复用同一
                Idempotency-Key，服务器将重放已存结果。
              </p>
            ) : (
              <p className="llm-reason">未生成复盘；可重试。</p>
            )}
          </div>
        )}

        {review?.idempotent_replay === true && (
          <p className="band-note" data-testid="journal-review-replay">
            幂等重放：复用已存结果
          </p>
        )}
      </div>
    </section>
  )
}
