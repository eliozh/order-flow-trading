import { errorMessage } from '../api/errors'
import { SettingsApiError } from '../api/settings'

/** How one failed onboarding mutation is classified for the operator. */
export interface OnboardingMutationFailure {
  message: string
  /** True when the outcome is unknown (timeout/transport): the key is kept. */
  unknown: boolean
  conflict: boolean
  /** Server-reported field names, when the refusal named any. */
  fields: string[]
}

export interface OnboardingKeyLifecycle {
  rotate: (label: string) => unknown
  clear: (label: string) => void
}

function fieldsFrom(error: SettingsApiError): string[] {
  const fields = error.details
    .map((detail) => detail.field)
    .filter((field): field is string => typeof field === 'string' && field.length > 0)
  return [...new Set(fields)]
}

/**
 * Apply the repository's idempotency-key lifecycle and produce UI copy.
 *
 * - `IDEMPOTENCY_CONFLICT`: the presented key is dead for this payload; rotate
 *   so an edited retry is a deliberate new request.
 * - a definitive refusal: the server wrote nothing; clear the key.
 * - anything else (timeout/transport): the write may have committed; keep the
 *   key so a same-payload retry replays the original response.
 */
export function settleOnboardingMutation(
  error: unknown,
  label: string,
  { rotate, clear }: OnboardingKeyLifecycle,
): OnboardingMutationFailure {
  if (error instanceof SettingsApiError && error.code === 'IDEMPOTENCY_CONFLICT') {
    rotate(label)
    return {
      message:
        `该保存与同一 Idempotency-Key 的先前请求冲突；已生成新的 Idempotency-Key，可直接重试。 ${error.message}`.trim(),
      unknown: false,
      conflict: true,
      fields: fieldsFrom(error),
    }
  }
  if (error instanceof SettingsApiError && error.kind === 'refusal') {
    clear(label)
    const fields = fieldsFrom(error)
    const scope = fields.length > 0 ? `字段：${fields.join('、')}。` : ''
    return {
      message: `服务器拒绝了本次保存，未写入任何内容。${scope}${error.message}`.trim(),
      unknown: false,
      conflict: false,
      fields,
    }
  }
  return {
    message: `结果未知，重试会使用同一 Idempotency-Key 重放。 ${errorMessage(error)}`.trim(),
    unknown: true,
    conflict: false,
    fields: [],
  }
}
