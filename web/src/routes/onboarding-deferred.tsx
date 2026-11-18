import type { OnboardingStepNumber } from '../api/onboarding'

export interface DeferredStepProps {
  /** Only step 3 (Binance) is deferred in this slice. */
  step: 3
  title: string
  milestone: number
  narrow: boolean
}

/**
 * A visibly deferred step. There are no inputs, no save control, and no claim
 * that anything was configured: the panel states the milestone that will own it.
 */
export function DeferredStep({ step, title, milestone, narrow }: DeferredStepProps) {
  const next: OnboardingStepNumber = 5
  return (
    <section className="panel" aria-labelledby={`deferred-${step}-heading`}>
      <div className="panel-head">
        <h2 id={`deferred-${step}-heading`}>延后步骤</h2>
        <span className="chip chip--muted">Milestone {milestone}</span>
      </div>
      <p className="status-message" role="note" data-testid={`deferred-step-${step}`}>
        {title} 的配置将在 Milestone {milestone} 提供。本步骤在引导中不会保存任何内容，也没有输入或
        完成状态；当前仍为 Paper。
      </p>
      <div className="settings-actions">
        <a className="btn" href={`/onboarding/${next}`}>
          继续到步骤 {next}（无需配置）
        </a>
        {narrow && <span className="hint">移动端只读；请在桌面端或使用 CLI 完成引导。</span>}
      </div>
    </section>
  )
}
