import { useEffect, useRef, useState } from 'react'

import { fetchBudget, updateBudgetThresholds } from '../api/budget'
import type { BudgetEnvelope, BudgetUpdateEnvelope, BudgetUpdatePayload } from '../api/budget'
import { errorMessage } from '../api/errors'
import {
  clearOnboardingOperationKey,
  completeOnboardingStep,
  fetchOnboardingProgress,
  ONBOARDING_STEPS,
  onboardingOperationKey,
  rotateOnboardingOperationKey,
  type OnboardingProgressData,
  type OnboardingProgressEnvelope,
  type OnboardingStepNumber,
} from '../api/onboarding'
import { fetchLLMSettings, fetchScanSettings, setLLMKey, updateScanPeriod } from '../api/settings'
import type {
  LLMKeyStatusEnvelope,
  LLMProviderName,
  LLMSettingsEnvelope,
  ScanSettingsEnvelope,
} from '../api/settings'
import { fetchReadiness, fetchStatus } from '../api/status'
import type { ReadinessEnvelope, StatusEnvelope } from '../api/status'
import { fetchWatchlist, setStar } from '../api/watchlist'
import type { StarEnvelope, WatchlistEnvelope } from '../api/watchlist'
import { useSettingsReadonly } from '../components/settings-field'
import { settleOnboardingMutation } from './onboarding-idempotency'
import {
  DeferredStep,
  EnvironmentStep,
  LLMStep,
  StepNav,
  StepNavButtons,
  WatchlistStep,
} from './onboarding-step'

const STEP_SUBTITLES: Record<OnboardingStepNumber, string> = {
  1: '数据目录、时区、Paper/Live 与本地绑定。Paper 已选中并锁定；Live 不在引导中启用。',
  2: '添加 OpenAI / Anthropic / DeepSeek 密钥。密钥写入后只显示掩码；至少一个 provider 才能继续。',
  3: 'Binance 密钥与权限读取属于 Milestone 5；本步骤在引导中不保存任何内容。',
  4: 'Telegram 只读通道属于 Milestone 6；本步骤在引导中不保存任何内容。',
  5: '选择首批标的、扫描周期与预算阈值；完成后的复盘显示生效值与下一步。',
}

export interface OnboardingPageProps {
  step?: OnboardingStepNumber | null
  isNarrow?: boolean
  navigate?: (path: string) => void
  fetchProgressFn?: (signal?: AbortSignal) => Promise<OnboardingProgressEnvelope>
  completeStepFn?: (
    step: OnboardingStepNumber,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<OnboardingProgressEnvelope>
  idempotencyKeyFn?: (label: string) => string
  clearIdempotencyKeyFn?: (label: string) => void
  rotateIdempotencyKeyFn?: (label: string) => string
  fetchReadinessFn?: (signal?: AbortSignal) => Promise<ReadinessEnvelope>
  fetchStatusFn?: (signal?: AbortSignal) => Promise<StatusEnvelope>
  fetchLLMSettingsFn?: (signal?: AbortSignal) => Promise<LLMSettingsEnvelope>
  setLLMKeyFn?: (
    provider: LLMProviderName,
    apiKey: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<LLMKeyStatusEnvelope>
  fetchWatchlistFn?: (signal?: AbortSignal) => Promise<WatchlistEnvelope>
  setStarFn?: (symbol: string, starred: boolean, signal?: AbortSignal) => Promise<StarEnvelope>
  fetchScanSettingsFn?: (signal?: AbortSignal) => Promise<ScanSettingsEnvelope>
  updateScanPeriodFn?: (
    period: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<ScanSettingsEnvelope>
  fetchBudgetFn?: (signal?: AbortSignal) => Promise<BudgetEnvelope>
  updateBudgetThresholdsFn?: (
    payload: BudgetUpdatePayload,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<BudgetUpdateEnvelope>
}

interface StepError {
  message: string
}

function defaultNavigate(path: string): void {
  if (typeof window !== 'undefined') {
    window.location.assign(path)
  }
}

function fallbackProgress(activeStep: OnboardingStepNumber): OnboardingProgressData {
  return {
    steps: ONBOARDING_STEPS.map((step) => ({ ...step, completed: false })),
    completed_steps: [],
    resumed_step: activeStep,
    all_complete: false,
    environment: 'paper',
    updated_at: null,
    updated_by: null,
  }
}

interface ProgressLoad {
  state: 'loading' | 'ready' | 'error'
  progress?: OnboardingProgressData
  message?: string
}

/**
 * `/onboarding` and `/onboarding/1..5`: the resumable Paper-only wizard.
 *
 * The shell owns progress, resume, step completion, and focus; each step reads
 * and writes its own settings through the existing services. Step 3 (Binance)
 * and step 4 (Telegram) are deferred and can never be completed here.
 */
export function OnboardingPage({
  step = null,
  isNarrow,
  navigate = defaultNavigate,
  fetchProgressFn = fetchOnboardingProgress,
  completeStepFn = completeOnboardingStep,
  idempotencyKeyFn = onboardingOperationKey,
  clearIdempotencyKeyFn = clearOnboardingOperationKey,
  rotateIdempotencyKeyFn = rotateOnboardingOperationKey,
  fetchReadinessFn = fetchReadiness,
  fetchStatusFn = fetchStatus,
  fetchLLMSettingsFn = fetchLLMSettings,
  setLLMKeyFn = setLLMKey,
  fetchWatchlistFn = fetchWatchlist,
  setStarFn = setStar,
  fetchScanSettingsFn = fetchScanSettings,
  updateScanPeriodFn = updateScanPeriod,
  fetchBudgetFn = fetchBudget,
  updateBudgetThresholdsFn = updateBudgetThresholds,
}: OnboardingPageProps) {
  // Only a phone-width coarse-pointer surface is read-only, so a zoomed desktop
  // at 320-360 CSS px keeps the wizard operable (SC 1.4.4/1.4.10).
  const narrow = useSettingsReadonly(isNarrow)
  const [load, setLoad] = useState<ProgressLoad>({ state: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const [announcement, setAnnouncement] = useState('')
  const [stepError, setStepError] = useState<StepError | null>(null)
  const [completing, setCompleting] = useState<OnboardingStepNumber | null>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const errorRef = useRef<HTMLDivElement>(null)
  const redirectedRef = useRef(false)
  const inFlight = useRef(false)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad({ state: 'loading' })
    fetchProgressFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', message: '引导进度响应为空。' })
          return
        }
        setLoad({ state: 'ready', progress: envelope.data })
      })
      .catch((error: unknown) => {
        if (!active) {
          return
        }
        setLoad({ state: 'error', message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchProgressFn, attempt])

  const activeStep: OnboardingStepNumber =
    step ?? (load.state === 'ready' && load.progress ? load.progress.resumed_step : 1)

  // Bare `/onboarding` and `?resume` land on the server's resumed step exactly
  // once, even if the progress response changes afterwards.
  useEffect(() => {
    if (step !== null || load.state !== 'ready' || !load.progress || redirectedRef.current) {
      return
    }
    redirectedRef.current = true
    navigate(`/onboarding/${load.progress.resumed_step}`)
  }, [step, load, navigate])

  // The step heading receives focus on load and on every step change.
  useEffect(() => {
    headingRef.current?.focus()
  }, [activeStep])

  useEffect(() => {
    if (stepError !== null) {
      errorRef.current?.focus()
    }
  }, [stepError])

  useEffect(() => {
    if (load.state === 'ready') {
      setAnnouncement('引导进度已加载。')
    } else if (load.state === 'error') {
      setAnnouncement('引导进度不可用。')
    }
  }, [load.state])

  async function complete(stepNumber: OnboardingStepNumber): Promise<boolean> {
    if (inFlight.current) {
      return false
    }
    inFlight.current = true
    setStepError(null)
    setCompleting(stepNumber)
    const label = `step.${stepNumber}`
    try {
      const envelope = await completeStepFn(stepNumber, idempotencyKeyFn(label))
      if (!envelope.data) {
        throw new Error('引导进度响应为空。')
      }
      clearIdempotencyKeyFn(label)
      setLoad({ state: 'ready', progress: envelope.data })
      setAnnouncement(`步骤 ${stepNumber} 已完成。`)
      if (stepNumber !== 5) {
        navigate(`/onboarding/${envelope.data.resumed_step}`)
      }
      return true
    } catch (error: unknown) {
      const failure = settleOnboardingMutation(error, label, {
        rotate: rotateIdempotencyKeyFn,
        clear: clearIdempotencyKeyFn,
      })
      // A fresh object (not just the string) keeps a repeated identical failure
      // observable, so focus re-runs on the second attempt.
      setStepError({ message: failure.message })
      return false
    } finally {
      inFlight.current = false
      setCompleting(null)
    }
  }

  const progress =
    load.state === 'ready' && load.progress ? load.progress : fallbackProgress(activeStep)
  const stepMeta = progress.steps.find((item) => item.step === activeStep)
  // Server titles are canonical; the client table is a pre-load fallback only.
  const title =
    stepMeta?.title ??
    ONBOARDING_STEPS.find((item) => item.step === activeStep)?.title ??
    '首次配置'
  const subtitle = STEP_SUBTITLES[activeStep]

  function renderStep(current: OnboardingStepNumber) {
    switch (current) {
      case 1:
        return (
          <EnvironmentStep
            fetchReadinessFn={fetchReadinessFn}
            fetchStatusFn={fetchStatusFn}
            announce={setAnnouncement}
            onComplete={() => void complete(1)}
            completing={completing === 1}
            narrow={narrow}
          />
        )
      case 2:
        return (
          <LLMStep
            fetchLLMSettingsFn={fetchLLMSettingsFn}
            setLLMKeyFn={setLLMKeyFn}
            idempotencyKeyFn={idempotencyKeyFn}
            clearIdempotencyKeyFn={clearIdempotencyKeyFn}
            rotateIdempotencyKeyFn={rotateIdempotencyKeyFn}
            announce={setAnnouncement}
            onComplete={() => void complete(2)}
            completing={completing === 2}
            narrow={narrow}
          />
        )
      case 3:
        return (
          <DeferredStep
            step={3}
            title={title}
            milestone={stepMeta?.deferred_milestone ?? 5}
            narrow={narrow}
          />
        )
      case 4:
        return (
          <DeferredStep
            step={4}
            title={title}
            milestone={stepMeta?.deferred_milestone ?? 6}
            narrow={narrow}
          />
        )
      case 5:
        return (
          <WatchlistStep
            fetchWatchlistFn={fetchWatchlistFn}
            setStarFn={setStarFn}
            fetchScanSettingsFn={fetchScanSettingsFn}
            updateScanPeriodFn={updateScanPeriodFn}
            fetchBudgetFn={fetchBudgetFn}
            updateBudgetThresholdsFn={updateBudgetThresholdsFn}
            idempotencyKeyFn={idempotencyKeyFn}
            clearIdempotencyKeyFn={clearIdempotencyKeyFn}
            rotateIdempotencyKeyFn={rotateIdempotencyKeyFn}
            announce={setAnnouncement}
            onFinish={() => complete(5)}
            finishing={completing === 5}
            narrow={narrow}
            allComplete={progress.all_complete}
          />
        )
      default: {
        const exhaustive: never = current
        return exhaustive
      }
    }
  }

  return (
    <section
      className="system-page settings-page onboarding-page"
      aria-labelledby="onboarding-heading"
      data-readonly={narrow}
    >
      <div className="page-head">
        <div>
          <p className="kicker">首次配置 / 步骤 {activeStep} / 5</p>
          <h1 id="onboarding-heading" ref={headingRef} tabIndex={-1}>
            {title} <span className="mono dim">步骤 {activeStep} / 5</span>
          </h1>
          <p className="page-sub">{subtitle}</p>
        </div>
      </div>

      <p className="visually-hidden" role="status" aria-live="polite" data-testid="onboarding-live">
        {announcement}
      </p>

      {narrow && (
        <p className="readonly-note" role="note" id="onboarding-readonly-note">
          移动端只读：请在桌面端或使用 CLI（oft init --resume）完成引导。
        </p>
      )}

      {load.state === 'loading' && (
        <p className="status-message" role="status">
          正在加载引导进度…
        </p>
      )}

      {load.state === 'error' && (
        <div className="stack" data-testid="onboarding-progress-error">
          <p className="status-message status-message--error" role="alert">
            引导进度不可用：{load.message ?? '未知错误'}
          </p>
          <button type="button" className="control-btn" onClick={() => setAttempt((n) => n + 1)}>
            重试
          </button>
        </div>
      )}

      {stepError !== null && (
        <div
          ref={errorRef}
          role="alert"
          tabIndex={-1}
          data-testid="onboarding-error"
          className="status-message status-message--error"
        >
          {stepError.message}
        </div>
      )}

      <div className="settings-cols">
        <StepNav progress={progress} activeStep={activeStep} />
        <div className="stack">
          {renderStep(activeStep)}
          <StepNavButtons step={activeStep} />
        </div>
      </div>
    </section>
  )
}

export default OnboardingPage
