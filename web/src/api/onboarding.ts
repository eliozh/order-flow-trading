/**
 * Typed client for paper-only onboarding progress (slice 44, F01).
 *
 * Only step completion is persisted here; each step's actual configuration is
 * read and written through the existing settings/watchlist/LLM/budget clients.
 * The response is validated against the cross-field invariants the UI relies on
 * before it reaches the UI, and the payload contains no secret: the wizard never
 * echoes a key or a token.
 */

import type { ApiEnvelope } from './contract'
import { createPendingKeyStore, mutationHeaders } from './idempotency'
import { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS, requestSettings } from './settings'

export { MUTATION_TIMEOUT_MS, READ_TIMEOUT_MS }

export const ONBOARDING_PATH = '/api/v1/settings/onboarding'
export const ONBOARDING_KEY_PREFIX = 'oft.settings.onboarding.'

export type OnboardingStepId = 'environment' | 'llm' | 'binance' | 'telegram' | 'watchlist'
export type OnboardingStepNumber = 1 | 2 | 3 | 4 | 5

export interface OnboardingStepMeta {
  step: OnboardingStepNumber
  id: OnboardingStepId
  /** Fallback copy only; a ready progress response's title is canonical. */
  title: string
  deferred: boolean
  deferred_milestone: number | null
  /** Optional steps are real but never block ``all_complete``. */
  optional: boolean
}

export interface OnboardingStepData extends OnboardingStepMeta {
  completed: boolean
}

export interface OnboardingProgressData {
  steps: OnboardingStepData[]
  completed_steps: OnboardingStepNumber[]
  resumed_step: OnboardingStepNumber
  all_complete: boolean
  environment: 'paper'
  updated_at: string | null
  updated_by: string | null
}

export type OnboardingProgressEnvelope = ApiEnvelope<OnboardingProgressData>

/**
 * The single client step table: ids, deferred milestones, and the server's
 * canonical titles (used only as a pre-load fallback). The rendered nav and
 * headings come from the server's `progress.steps[].title`.
 *
 * Step 3 (Binance) is deferred; step 4 (Telegram) is a real optional step.
 */
export const ONBOARDING_STEPS = [
  {
    step: 1,
    id: 'environment',
    title: '环境与本地绑定',
    deferred: false,
    deferred_milestone: null,
    optional: false,
  },
  {
    step: 2,
    id: 'llm',
    title: 'LLM providers',
    deferred: false,
    deferred_milestone: null,
    optional: false,
  },
  {
    step: 3,
    id: 'binance',
    title: 'Binance 密钥与权限',
    deferred: true,
    deferred_milestone: 5,
    optional: false,
  },
  {
    step: 4,
    id: 'telegram',
    title: 'Telegram 只读通道',
    deferred: false,
    deferred_milestone: null,
    optional: true,
  },
  {
    step: 5,
    id: 'watchlist',
    title: 'Watchlist、周期与预算',
    deferred: false,
    deferred_milestone: null,
    optional: false,
  },
] as const satisfies readonly OnboardingStepMeta[]

export const ONBOARDING_STEP_NUMBERS: readonly OnboardingStepNumber[] = [1, 2, 3, 4, 5]
export const ACTIVE_ONBOARDING_STEPS: ReadonlySet<OnboardingStepNumber> = new Set(
  ONBOARDING_STEPS.filter((step) => !step.deferred).map((step) => step.step),
)
export const DEFERRED_ONBOARDING_STEPS: ReadonlySet<OnboardingStepNumber> = new Set(
  ONBOARDING_STEPS.filter((step) => step.deferred).map((step) => step.step),
)

/** The canonical identity for each step number; used to reject drift. */
const STEP_BY_NUMBER: Record<OnboardingStepNumber, OnboardingStepMeta> = {
  1: ONBOARDING_STEPS[0],
  2: ONBOARDING_STEPS[1],
  3: ONBOARDING_STEPS[2],
  4: ONBOARDING_STEPS[3],
  5: ONBOARDING_STEPS[4],
}

const onboardingKeys = createPendingKeyStore('web-settings-onboarding', ONBOARDING_KEY_PREFIX)

/** One stable pending key per logical onboarding mutation (step/scan/budget/key). */
export function onboardingOperationKey(label: string): string {
  return onboardingKeys.keyFor(label)
}

/** Create a fresh key for a deliberate new save (e.g. after a 409 conflict). */
export function rotateOnboardingOperationKey(label: string): string {
  return onboardingKeys.rotateKey(label)
}

/** Forget a pending key after success or a definitive refusal. */
export function clearOnboardingOperationKey(label: string): void {
  onboardingKeys.clearKey(label)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isStepNumber(value: unknown): value is OnboardingStepNumber {
  return value === 1 || value === 2 || value === 3 || value === 4 || value === 5
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

/** Reject a step whose id/deferred/milestone pair disagrees with the table. */
function isStep(value: unknown): value is OnboardingStepData {
  if (!isRecord(value) || !isStepNumber(value.step)) {
    return false
  }
  const canonical = STEP_BY_NUMBER[value.step]
  const milestone = value.deferred_milestone
  if (
    value.id !== canonical.id ||
    typeof value.title !== 'string' ||
    value.title.length === 0 ||
    value.deferred !== DEFERRED_ONBOARDING_STEPS.has(value.step) ||
    value.optional !== canonical.optional ||
    typeof value.completed !== 'boolean'
  ) {
    return false
  }
  if (canonical.deferred) {
    return (
      typeof milestone === 'number' &&
      Number.isInteger(milestone) &&
      milestone > 0 &&
      milestone === canonical.deferred_milestone
    )
  }
  return milestone === null
}

function isProgress(value: unknown): value is OnboardingProgressData {
  if (!isRecord(value) || !Array.isArray(value.steps)) {
    return false
  }
  const steps = value.steps
  if (steps.length !== ONBOARDING_STEP_NUMBERS.length || !steps.every(isStep)) {
    return false
  }
  const seen = new Set<number>()
  for (const step of steps) {
    if (seen.has(step.step)) {
      return false
    }
    seen.add(step.step)
  }
  if (ONBOARDING_STEP_NUMBERS.some((step) => !seen.has(step))) {
    return false
  }
  const completed = value.completed_steps
  if (!Array.isArray(completed)) {
    return false
  }
  const completedSet = new Set<number>()
  for (const step of completed) {
    if (!isStepNumber(step) || !ACTIVE_ONBOARDING_STEPS.has(step) || completedSet.has(step)) {
      return false
    }
    completedSet.add(step)
  }
  return (
    isStepNumber(value.resumed_step) &&
    typeof value.all_complete === 'boolean' &&
    value.environment === 'paper' &&
    isNullableString(value.updated_at) &&
    isNullableString(value.updated_by)
  )
}

/** Read completed steps and the server-derived resume point. */
export async function fetchOnboardingProgress(
  signal?: AbortSignal,
): Promise<OnboardingProgressEnvelope> {
  return requestSettings<OnboardingProgressData>(
    ONBOARDING_PATH,
    { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    isProgress,
    signal,
    READ_TIMEOUT_MS,
  )
}

/** Mark one active step complete; the caller supplies the stable key. */
export async function completeOnboardingStep(
  step: OnboardingStepNumber,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<OnboardingProgressEnvelope> {
  return requestSettings<OnboardingProgressData>(
    ONBOARDING_PATH,
    {
      method: 'PUT',
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({ step }),
      credentials: 'same-origin',
    },
    isProgress,
    signal,
    MUTATION_TIMEOUT_MS,
  )
}
