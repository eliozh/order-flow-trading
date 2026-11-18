import { markNavFocus } from '../components/nav'
import type { OnboardingProgressData, OnboardingStepNumber } from '../api/onboarding'

export interface StepNavProps {
  progress: OnboardingProgressData
  activeStep: OnboardingStepNumber
}

/**
 * The 01..05 step list shared by every step (DESIGN A2.3).
 *
 * The list is intentionally static and non-focusable: the step name, deferred
 * milestone, and completed status are text (a visually hidden "已完成" carries
 * the status, never the ✓ glyph alone). Keyboard users move between steps with
 * the explicit 上一步/下一步 buttons, and direct `/onboarding/N` URLs keep
 * working because those buttons are ordinary links.
 */
export function StepNav({ progress, activeStep }: StepNavProps) {
  return (
    <nav className="settings-nav" aria-label="首次配置步骤">
      {/* Safari/VoiceOver drops list semantics for `list-style: none`, so the
          explicit role is intentional (WCAG 1.3.1). */}
      {/* eslint-disable-next-line jsx-a11y/no-redundant-roles */}
      <ol className="onboarding-step-list" role="list">
        {progress.steps.map((step) => {
          const active = step.step === activeStep
          return (
            <li
              key={step.step}
              data-testid={`onboarding-nav-${step.step}`}
              aria-current={active ? 'step' : undefined}
            >
              <span className="mono dim">{String(step.step).padStart(2, '0')}</span>{' '}
              <span>{step.title}</span>
              {step.deferred && (
                <span className="chip chip--muted">Milestone {step.deferred_milestone}</span>
              )}
              {step.optional && !step.deferred && (
                <span className="chip chip--muted">optional</span>
              )}
              {step.completed && (
                <span className="onboarding-complete">
                  <span className="ok" aria-hidden="true">
                    ✓
                  </span>
                  <span className="visually-hidden">已完成</span>
                </span>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

export interface StepNavButtonsProps {
  step: OnboardingStepNumber
}

/**
 * Explicit previous/next links (DESIGN A2.3). Full-page anchors, so the direct
 * route URLs stay the same; `markNavFocus` makes the destination heading the
 * focus target after the load.
 */
export function StepNavButtons({ step }: StepNavButtonsProps) {
  const previous = step > 1 ? ((step - 1) as OnboardingStepNumber) : null
  const next = step < 5 ? ((step + 1) as OnboardingStepNumber) : null
  if (previous === null && next === null) {
    return null
  }
  return (
    <div className="settings-actions onboarding-step-buttons">
      {previous !== null && (
        <a
          className="btn"
          href={`/onboarding/${previous}`}
          onClick={markNavFocus}
          data-testid="onboarding-prev"
        >
          上一步
        </a>
      )}
      {next !== null && (
        <a
          className="btn"
          href={`/onboarding/${next}`}
          onClick={markNavFocus}
          data-testid="onboarding-next"
        >
          下一步
        </a>
      )}
    </div>
  )
}
