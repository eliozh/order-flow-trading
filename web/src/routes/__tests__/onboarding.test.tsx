import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { BudgetEnvelope, BudgetUpdateEnvelope, BudgetUpdatePayload } from '../../api/budget'
import type { LLMKeyStatusEnvelope, LLMProviderRow, LLMSettingsEnvelope } from '../../api/settings'
import type {
  OnboardingProgressData,
  OnboardingProgressEnvelope,
  OnboardingStepData,
  OnboardingStepNumber,
} from '../../api/onboarding'
import { completeOnboardingStep, ONBOARDING_STEPS } from '../../api/onboarding'
import { SettingsApiError } from '../../api/settings'
import { RequestTimeoutError } from '../../api/request-timeout'
import type { StatusEnvelope } from '../../api/status'
import { ReadinessError } from '../../api/status'
import type { ReadinessEnvelope, ReadyCheck } from '../../api/status'
import type { ScanSettingsEnvelope } from '../../api/settings'
import type { StarEnvelope, WatchlistEnvelope, WatchlistRow } from '../../api/watchlist'
import { OnboardingPage } from '../onboarding'
import { adjustStarredCount } from '../use-watchlist-draft'

const meta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE' as const,
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

/** Derived from the single client step table, never a divergent copy. */
const STEP_META: OnboardingStepData[] = ONBOARDING_STEPS.map((step) => ({
  ...step,
  completed: false,
}))

function progressData(overrides: Partial<OnboardingProgressData> = {}): OnboardingProgressData {
  const completed = overrides.completed_steps ?? []
  return {
    steps: STEP_META.map((step) => ({ ...step, completed: completed.includes(step.step) })),
    completed_steps: completed,
    resumed_step: 1,
    all_complete: false,
    environment: 'paper',
    updated_at: null,
    updated_by: null,
    ...overrides,
  }
}

function progressEnvelope(data: OnboardingProgressData): OnboardingProgressEnvelope {
  return { ok: true, data, error: null, meta }
}

function readinessEnvelope(checks?: ReadyCheck[]): ReadinessEnvelope {
  const resolved: ReadyCheck[] = checks ?? [
    { name: 'config', status: 'ok', detail: 'settings loaded' },
    { name: 'storage.sqlite', status: 'ok', detail: 'integrity ok' },
    { name: 'storage.parquet', status: 'ok', detail: '/tmp/oft/market' },
    { name: 'clock', status: 'ok', detail: '2026-11-06T10:00:00.000Z' },
    { name: 'market_data', status: 'ok', detail: '8 symbols' },
  ]
  return {
    ok: true,
    data: {
      status: 'ready',
      version: '0.1.0',
      env: 'paper',
      data_dir: '/tmp/oft',
      checked_at: '2026-11-06T10:00:00.000Z',
      checks: resolved,
      latest_scan: null,
    },
    error: null,
    meta,
  }
}

function statusEnvelope(): StatusEnvelope {
  return {
    ok: true,
    data: {
      status: 'ok',
      env: 'paper',
      version: '0.1.0',
      host: '127.0.0.1',
      port: 7611,
      data_dir: '/tmp/oft',
      log_level: 'INFO',
      generated_at: '2026-11-06T10:00:00.000Z',
      universe: { source: 'fixture', as_of: '2026-11-06', count: 1, tradifi_count: 0, symbols: [] },
      schedule: {
        timezone: 'UTC',
        period: '4h',
        pending_period: null,
        next_run_at: '2026-11-06T12:00:00.000Z',
      },
    },
    error: null,
    meta,
  }
}

function providerRow(
  provider: 'openai' | 'anthropic' | 'deepseek',
  overrides: Partial<LLMProviderRow> = {},
): LLMProviderRow {
  return {
    provider,
    configured: false,
    key_source: 'unset',
    masked: null,
    fingerprint: null,
    updated_at: null,
    updated_by: null,
    base_url:
      provider === 'openai'
        ? 'https://api.openai.com'
        : provider === 'anthropic'
          ? 'https://api.anthropic.com'
          : 'https://api.deepseek.com',
    capability: null,
    ...overrides,
  }
}

function llmEnvelope(providers?: LLMProviderRow[]): LLMSettingsEnvelope {
  return {
    ok: true,
    data: {
      provider: null,
      providers: providers ?? [
        providerRow('openai'),
        providerRow('anthropic'),
        providerRow('deepseek'),
      ],
      roles: { ANALYSIS: null, ENTRY_RECHECK: null, MANAGEMENT: null, REVIEW: null },
      base_urls: {
        openai: 'https://api.openai.com',
        anthropic: 'https://api.anthropic.com',
        deepseek: 'https://api.deepseek.com',
      },
      allow_custom_base_url: false,
      concurrency: { global: 4, deepseek: 2 },
      source: 'default',
      updated_at: null,
      updated_by: null,
    },
    error: null,
    meta,
  }
}

function keyStatus(provider: 'openai' | 'anthropic' | 'deepseek'): LLMKeyStatusEnvelope {
  return {
    ok: true,
    data: {
      provider,
      configured: true,
      key_source: 'stored',
      masked: '****4f2a',
      fingerprint: null,
      updated_at: '2026-11-06T10:00:00.000Z',
      updated_by: 'web',
    },
    error: null,
    meta,
  }
}

function watchlistRow(symbol: string, starred: boolean): WatchlistRow {
  return {
    symbol,
    base_asset: symbol.replace('USDT', ''),
    quote_asset: 'USDT',
    contract_type: 'PERPETUAL',
    underlying_type: 'CRYPTO',
    status: 'TRADING',
    tick_size: '0.10',
    step_size: '0.001',
    min_qty: '0.001',
    min_notional: '5',
    price_precision: 2,
    quantity_precision: 3,
    onboard_date: 0,
    tradifi: false,
    listed_date: '2024-01-01',
    starred,
    starred_at: starred ? '2026-11-06T09:00:00.000Z' : null,
  }
}

function watchlistEnvelope(
  rows: WatchlistRow[] = [watchlistRow('BTCUSDT', true), watchlistRow('ETHUSDT', false)],
  overrides: Partial<NonNullable<WatchlistEnvelope['data']>> = {},
): WatchlistEnvelope {
  return {
    ok: true,
    data: {
      groups: [{ name: 'crypto', count: rows.length, symbols: rows.map((row) => row.symbol) }],
      total: rows.length,
      rows,
      next_scan_count: rows.filter((row) => row.starred).length,
      truncated: false,
      star_mutation: true,
      ...overrides,
    },
    error: null,
    meta,
  }
}

function scanEnvelope(
  overrides: Partial<NonNullable<ScanSettingsEnvelope['data']>> = {},
): ScanSettingsEnvelope {
  return {
    ok: true,
    data: {
      key: 'scan.period',
      value: '4h',
      source: 'default',
      supported: ['4h', '8h'],
      next_run: '2026-11-06T12:00:00.000Z',
      timezone: 'UTC',
      updated_at: null,
      updated_by: null,
      ...overrides,
    },
    error: null,
    meta,
  }
}

function budgetEnvelope(): BudgetEnvelope {
  return {
    ok: true,
    data: {
      thresholds: { daily_usd: '10.00', weekly_usd: '50.00', monthly_usd: '150.00', warn_pct: 80 },
      usage_quality: 'COMPLETE',
      budget_state: 'OK',
      periods: {
        day: {
          period_key: '2026-11-06',
          spend_usd: '0.00',
          call_count: 0,
          unpriced_count: 0,
          budget_state: 'OK',
        },
        week: {
          period_key: '2026-W45',
          spend_usd: '0.00',
          call_count: 0,
          unpriced_count: 0,
          budget_state: 'OK',
        },
        month: {
          period_key: '2026-11',
          spend_usd: '0.00',
          call_count: 0,
          unpriced_count: 0,
          budget_state: 'OK',
        },
      },
      projected_month_end_usd: '0.00',
      projection_basis: {
        month_spend_usd: '0.00',
        elapsed_fraction: 0.5,
        as_of: '2026-11-06T10:00:00.000Z',
      },
      last_alert: null,
    },
    error: null,
    meta,
  }
}

function budgetUpdateEnvelope(): BudgetUpdateEnvelope {
  const snapshot = budgetEnvelope().data
  if (snapshot === null) {
    throw new Error('budget fixture must be complete')
  }
  return {
    ok: true,
    data: {
      thresholds: snapshot.thresholds,
      usage_quality: snapshot.usage_quality,
      budget_state: snapshot.budget_state,
      periods: snapshot.periods,
      projected_month_end_usd: snapshot.projected_month_end_usd,
      projection_basis: snapshot.projection_basis,
      last_alert: snapshot.last_alert,
      changed: true,
    },
    error: null,
    meta,
  }
}

interface RenderOptions {
  step?: OnboardingStepNumber | null
  isNarrow?: boolean
  navigate?: (path: string) => void
  progress?: OnboardingProgressData
  fetchProgress?: (signal?: AbortSignal) => Promise<OnboardingProgressEnvelope>
  completeStep?: (step: OnboardingStepNumber, key: string) => Promise<OnboardingProgressEnvelope>
  idempotencyKey?: (label: string) => string
  clearKey?: (label: string) => void
  rotateKey?: (label: string) => string
  readiness?: () => Promise<ReadinessEnvelope>
  status?: () => Promise<StatusEnvelope>
  llm?: () => Promise<LLMSettingsEnvelope>
  setKey?: (
    provider: 'openai' | 'anthropic' | 'deepseek',
    value: string,
    key: string,
  ) => Promise<LLMKeyStatusEnvelope>
  watchlist?: () => Promise<WatchlistEnvelope>
  star?: (symbol: string, starred: boolean) => Promise<StarEnvelope>
  scan?: () => Promise<ScanSettingsEnvelope>
  updateScan?: (period: string, key: string) => Promise<ScanSettingsEnvelope>
  budget?: () => Promise<BudgetEnvelope>
  updateBudget?: (payload: BudgetUpdatePayload, key: string) => Promise<BudgetUpdateEnvelope>
}

function renderOnboarding(options: RenderOptions = {}) {
  const progress = options.progress ?? progressData()
  const completeStep =
    options.completeStep ??
    (async (step: OnboardingStepNumber) => {
      const completed: OnboardingStepNumber[] = step === 1 ? [1] : step === 2 ? [1, 2] : [1, 2, 5]
      return progressEnvelope(
        progressData({
          completed_steps: completed,
          resumed_step: step === 1 ? 2 : 5,
          all_complete: step === 5,
        }),
      )
    })
  const navigate = options.navigate ?? vi.fn()
  const updateScan =
    options.updateScan ??
    (async (period: string) => scanEnvelope({ value: period, source: 'user' }))
  const updateBudget = options.updateBudget ?? (async () => budgetUpdateEnvelope())
  const result = render(
    <OnboardingPage
      step={options.step ?? null}
      isNarrow={options.isNarrow}
      navigate={navigate}
      fetchProgressFn={options.fetchProgress ?? (async () => progressEnvelope(progress))}
      completeStepFn={completeStep}
      idempotencyKeyFn={options.idempotencyKey ?? (() => 'test-key')}
      clearIdempotencyKeyFn={options.clearKey ?? (() => {})}
      rotateIdempotencyKeyFn={options.rotateKey ?? (() => 'rotated-key')}
      fetchReadinessFn={options.readiness ?? (async () => readinessEnvelope())}
      fetchStatusFn={options.status ?? (async () => statusEnvelope())}
      fetchLLMSettingsFn={options.llm ?? (async () => llmEnvelope())}
      setLLMKeyFn={options.setKey ?? (async (provider) => keyStatus(provider))}
      fetchWatchlistFn={options.watchlist ?? (async () => watchlistEnvelope())}
      setStarFn={
        options.star ??
        (async (symbol: string, starred: boolean) => ({
          ok: true,
          data: { symbol, starred, starred_at: starred ? '2026-11-06T10:00:00.000Z' : null },
          error: null,
          meta,
        }))
      }
      fetchScanSettingsFn={options.scan ?? (async () => scanEnvelope())}
      updateScanPeriodFn={updateScan}
      fetchBudgetFn={options.budget ?? (async () => budgetEnvelope())}
      updateBudgetThresholdsFn={updateBudget}
    />,
  )
  return { ...result, navigate, completeStep, updateScan, updateBudget }
}

async function stepHeading(): Promise<HTMLElement> {
  return screen.findByRole('heading', { level: 1 })
}

describe('OnboardingPage step 1 — environment', () => {
  it('renders a fresh install with Paper selected and no config required', async () => {
    renderOnboarding({ step: 1 })

    const heading = await stepHeading()
    expect(heading).toHaveTextContent('环境与本地绑定')
    expect(heading).toHaveFocus()

    const paper = screen.getByRole('radio', { name: /^Paper/ })
    const live = screen.getByRole('radio', { name: /^Live/ })
    expect(paper).toBeChecked()
    expect(paper).toBeDisabled()
    expect(live).not.toBeChecked()
    expect(live).toBeDisabled()
    expect(screen.getByRole('group', { name: '交易环境' })).toHaveTextContent('Live 不在引导中启用')
  })

  it('shows every ready check as its real state with its detail', async () => {
    renderOnboarding({ step: 1 })

    await stepHeading()
    expect(await screen.findByTestId('check-config')).toHaveTextContent('PASS')
    expect(screen.getByTestId('check-storage.sqlite')).toHaveTextContent('PASS')
    expect(screen.getByTestId('check-storage.sqlite')).toHaveTextContent('integrity ok')
    expect(screen.getByTestId('check-clock')).toHaveTextContent('PASS')
    expect(document.getElementById('onboarding-data-dir')).toHaveTextContent('/tmp/oft')
    expect(document.getElementById('onboarding-bind')).toHaveTextContent('127.0.0.1:7611')
    expect(document.getElementById('onboarding-timezone')).toHaveTextContent('UTC')
  })

  it('renders named failures from a real 503 readiness error and still shows status', async () => {
    renderOnboarding({
      step: 1,
      readiness: async () => {
        throw new ReadinessError('One or more readiness checks failed.', 'STORAGE_UNAVAILABLE', [
          { field: 'storage.parquet', code: 'degraded', message: 'market layout not initialized' },
          { field: 'clock', code: 'offline', message: 'clock unavailable' },
        ])
      },
    })

    await stepHeading()
    // The failing checks are named with their real status and detail.
    const parquet = await screen.findByTestId('check-storage.parquet')
    expect(parquet).toHaveTextContent('DEGRADED')
    expect(parquet).toHaveTextContent('market layout not initialized')
    expect(screen.getByTestId('check-clock')).toHaveTextContent('FAIL')
    expect(screen.getByTestId('check-clock')).toHaveTextContent('clock unavailable')
    // The independent status read still populated the environment panel.
    expect(document.getElementById('onboarding-data-dir')).toHaveTextContent('/tmp/oft')
    expect(document.getElementById('onboarding-bind')).toHaveTextContent('127.0.0.1:7611')
    expect(document.getElementById('onboarding-timezone')).toHaveTextContent('UTC')
  })

  it('shows the generic preflight error only when both reads fail', async () => {
    renderOnboarding({
      step: 1,
      readiness: async () => {
        throw new Error('ready down')
      },
      status: async () => {
        throw new Error('status down')
      },
    })

    await stepHeading()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('本机预检不可用')
    expect(screen.queryByTestId('check-config')).toBeNull()
    expect(document.getElementById('onboarding-data-dir')).toHaveTextContent('—')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('persists completion, announces it, and advances to step 2', async () => {
    const completeStep = vi.fn(async () =>
      progressEnvelope(progressData({ completed_steps: [1], resumed_step: 2 })),
    )
    const clearKey = vi.fn()
    const { navigate } = renderOnboarding({ step: 1, completeStep, clearKey })

    await stepHeading()
    fireEvent.click(screen.getByRole('button', { name: '保存并继续' }))

    await waitFor(() => expect(completeStep).toHaveBeenCalledWith(1, 'test-key'))
    expect(clearKey).toHaveBeenCalledWith('step.1')
    expect(navigate).toHaveBeenCalledWith('/onboarding/2')
    expect(screen.getByTestId('onboarding-live')).toHaveTextContent('步骤 1 已完成')
  })

  it('rotates the pending key on an idempotency conflict', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const completeStep = vi.fn(async () => {
      throw new SettingsApiError('same key, different payload', 'IDEMPOTENCY_CONFLICT', {
        httpStatus: 409,
      })
    })
    renderOnboarding({ step: 1, completeStep, rotateKey, clearKey })

    await stepHeading()
    fireEvent.click(screen.getByRole('button', { name: '保存并继续' }))

    const summary = await screen.findByTestId('onboarding-error')
    expect(summary).toHaveTextContent('已生成新的 Idempotency-Key')
    await waitFor(() => expect(rotateKey).toHaveBeenCalledWith('step.1'))
    expect(clearKey).not.toHaveBeenCalled()
  })

  it('keeps the same key and says the outcome is unknown on a timeout', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const completeStep = vi.fn(async () => {
      throw new RequestTimeoutError()
    })
    renderOnboarding({ step: 1, completeStep, rotateKey, clearKey })

    await stepHeading()
    fireEvent.click(screen.getByRole('button', { name: '保存并继续' }))

    const summary = await screen.findByTestId('onboarding-error')
    expect(summary).toHaveTextContent('结果未知')
    expect(summary).toHaveTextContent('同一 Idempotency-Key')
    expect(rotateKey).not.toHaveBeenCalled()
    expect(clearKey).not.toHaveBeenCalled()
  })

  it('clears the key on a definitive refusal and names the field', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const completeStep = vi.fn(async () => {
      throw new SettingsApiError('deferred step', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'step', code: 'step_deferred', message: 'deferred step' }],
      })
    })
    renderOnboarding({ step: 1, completeStep, rotateKey, clearKey })

    await stepHeading()
    fireEvent.click(screen.getByRole('button', { name: '保存并继续' }))

    const summary = await screen.findByTestId('onboarding-error')
    expect(summary).toHaveTextContent('服务器拒绝')
    expect(summary).toHaveTextContent('字段：step')
    expect(clearKey).toHaveBeenCalledWith('step.1')
    expect(rotateKey).not.toHaveBeenCalled()
  })

  it('resumes at the first incomplete step for a bare /onboarding and ?resume', async () => {
    const navigate = vi.fn()
    renderOnboarding({
      step: null,
      progress: progressData({ completed_steps: [1], resumed_step: 2 }),
      navigate,
    })

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/onboarding/2'))
    const heading = screen.getByRole('heading', { level: 1 })
    await waitFor(() => expect(heading).toHaveTextContent('LLM providers'))
  })

  it('offers a retry when the progress read fails', async () => {
    let calls = 0
    const fetchProgress = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        throw new Error('progress down')
      }
      return progressEnvelope(progressData())
    })
    renderOnboarding({ step: 1, fetchProgress })

    expect(await screen.findByTestId('onboarding-progress-error')).toHaveTextContent(
      'progress down',
    )
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(fetchProgress).toHaveBeenCalledTimes(2))
    expect(await stepHeading()).toHaveTextContent('环境与本地绑定')
  })
})

describe('OnboardingPage step nav (DESIGN A2.3)', () => {
  it('renders a static, non-focusable step list with text completion status', async () => {
    renderOnboarding({
      step: 5,
      progress: progressData({ completed_steps: [1, 2], resumed_step: 5 }),
    })

    await stepHeading()
    const nav = screen.getByRole('navigation', { name: '首次配置步骤' })
    // Static list: no focusable step links.
    expect(nav.querySelectorAll('a')).toHaveLength(0)
    expect(nav.querySelectorAll('li')).toHaveLength(5)
    expect(screen.getByTestId('onboarding-nav-5')).toHaveAttribute('aria-current', 'step')
    // Completion is text, not only the glyph.
    expect(screen.getByTestId('onboarding-nav-1')).toHaveTextContent('已完成')
    expect(screen.getByTestId('onboarding-nav-2')).toHaveTextContent('✓')
    expect(nav).toHaveTextContent('Milestone 5')
    expect(nav).toHaveTextContent('Milestone 6')
  })

  it('provides explicit previous/next links for keyboard navigation', async () => {
    const { rerender } = renderOnboarding({ step: 1 })
    await stepHeading()
    expect(screen.queryByTestId('onboarding-prev')).toBeNull()
    expect(screen.getByTestId('onboarding-next')).toHaveAttribute('href', '/onboarding/2')

    rerender(
      <OnboardingPage
        step={5}
        fetchProgressFn={async () => progressEnvelope(progressData({ resumed_step: 5 }))}
        completeStepFn={async () => progressEnvelope(progressData())}
        idempotencyKeyFn={() => 'test-key'}
        clearIdempotencyKeyFn={() => {}}
        rotateIdempotencyKeyFn={() => 'rotated'}
        fetchReadinessFn={async () => readinessEnvelope()}
        fetchStatusFn={async () => statusEnvelope()}
        fetchLLMSettingsFn={async () => llmEnvelope()}
        setLLMKeyFn={async (provider) => keyStatus(provider)}
        fetchWatchlistFn={async () => watchlistEnvelope()}
        setStarFn={async (symbol, starred) => ({
          ok: true,
          data: { symbol, starred, starred_at: null },
          error: null,
          meta,
        })}
        fetchScanSettingsFn={async () => scanEnvelope()}
        updateScanPeriodFn={async (period) => scanEnvelope({ value: period })}
        fetchBudgetFn={async () => budgetEnvelope()}
        updateBudgetThresholdsFn={async () => budgetUpdateEnvelope()}
      />,
    )
    await stepHeading()
    expect(screen.getByTestId('onboarding-prev')).toHaveAttribute('href', '/onboarding/4')
    expect(screen.queryByTestId('onboarding-next')).toBeNull()
  })
})

describe('OnboardingPage step 2 — LLM providers', () => {
  it('keeps provider keys write-only: shows the server mask and retains no value', async () => {
    const sentinel = 'sk-test-SENTINEL-44-4f2a'
    const setKey = vi.fn(async (provider: 'openai' | 'anthropic' | 'deepseek') =>
      keyStatus(provider),
    )
    const completeStep = vi.fn(async () =>
      progressEnvelope(progressData({ completed_steps: [1, 2], resumed_step: 5 })),
    )
    const navigate = vi.fn()
    const clearKey = vi.fn()
    renderOnboarding({ step: 2, setKey, completeStep, navigate, clearKey })

    await stepHeading()
    const input = await screen.findByTestId('llm-key-input-anthropic')
    fireEvent.change(input, { target: { value: sentinel } })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))

    await waitFor(() => expect(setKey).toHaveBeenCalledWith('anthropic', sentinel, 'test-key'))
    expect(clearKey).toHaveBeenCalledWith('llm.anthropic')
    expect(await screen.findByTestId('llm-key-mask-anthropic')).toHaveTextContent('****4f2a')
    expect(input).toHaveValue('')
    expect(document.body.textContent).not.toContain(sentinel)
    expect(screen.queryByRole('button', { name: /显示原值|reveal|查看密钥/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '保存并继续' }))
    await waitFor(() => expect(completeStep).toHaveBeenCalledWith(2, 'test-key'))
    expect(navigate).toHaveBeenCalledWith('/onboarding/5')
  })

  it('keeps the typed value and announces a failed save without echoing it', async () => {
    const sentinel = 'sk-test-SENTINEL-FAIL-9b7c'
    const setKey = vi.fn(async () => {
      throw new SettingsApiError('provider rejected the key', 'VALIDATION_FAILED', {
        httpStatus: 422,
      })
    })
    renderOnboarding({ step: 2, setKey })

    await stepHeading()
    const input = await screen.findByTestId('llm-key-input-anthropic')
    fireEvent.change(input, { target: { value: sentinel } })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('provider rejected the key')
    expect(alert).not.toHaveTextContent(sentinel)
    expect(input).toHaveValue(sentinel)
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('does not claim the key was unsaved when the outcome is unknown', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const setKey = vi.fn(async () => {
      throw new RequestTimeoutError()
    })
    renderOnboarding({ step: 2, setKey, rotateKey, clearKey })

    await stepHeading()
    fireEvent.change(await screen.findByTestId('llm-key-input-openai'), {
      target: { value: 'sk-test' },
    })
    fireEvent.click(screen.getByTestId('llm-key-save-openai'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).not.toHaveTextContent('未保存任何内容')
    expect(rotateKey).not.toHaveBeenCalled()
    expect(clearKey).not.toHaveBeenCalled()
  })

  it('appends the unsaved suffix on a definitive key refusal and clears the key', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const setKey = vi.fn(async () => {
      throw new SettingsApiError('密钥被拒绝', 'VALIDATION_FAILED', { httpStatus: 422 })
    })
    renderOnboarding({ step: 2, setKey, rotateKey, clearKey })

    await stepHeading()
    fireEvent.change(await screen.findByTestId('llm-key-input-anthropic'), {
      target: { value: 'sk-test' },
    })
    fireEvent.click(screen.getByTestId('llm-key-save-anthropic'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('服务器拒绝')
    expect(alert).toHaveTextContent('未保存任何内容')
    await waitFor(() => expect(clearKey).toHaveBeenCalledWith('llm.anthropic'))
    expect(rotateKey).not.toHaveBeenCalled()
  })

  it('keeps the finish description valid while providers fail to load', async () => {
    renderOnboarding({
      step: 2,
      llm: async () => {
        throw new Error('llm down')
      },
    })

    await stepHeading()
    expect(await screen.findByText(/LLM providers 不可用/)).toBeInTheDocument()
    const finish = screen.getByRole('button', { name: '保存并继续' })
    // `#llm-no-provider` only exists in the ready state; the IDREF must not
    // dangle while loading or errored.
    expect(finish).toHaveAttribute('aria-describedby', 'llm-finish-hint')
    expect(document.getElementById('llm-finish-hint')).not.toBeNull()
    expect(document.getElementById('llm-no-provider')).toBeNull()
  })

  it('rotates the provider key on a conflict', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const setKey = vi.fn(async () => {
      throw new SettingsApiError('conflict', 'IDEMPOTENCY_CONFLICT', { httpStatus: 409 })
    })
    renderOnboarding({ step: 2, setKey, rotateKey, clearKey })

    await stepHeading()
    fireEvent.change(await screen.findByTestId('llm-key-input-openai'), {
      target: { value: 'sk-test' },
    })
    fireEvent.click(screen.getByTestId('llm-key-save-openai'))

    await waitFor(() => expect(rotateKey).toHaveBeenCalledWith('llm.openai'))
    expect(clearKey).not.toHaveBeenCalled()
  })

  it('blocks finish without a provider and points at the documented consequence', async () => {
    renderOnboarding({ step: 2 })

    await stepHeading()
    const finish = screen.getByRole('button', { name: '保存并继续' })
    // Kept focusable: aria-disabled rather than native disabled.
    expect(finish).not.toBeDisabled()
    expect(finish).toHaveAttribute('aria-disabled', 'true')
    expect(finish).toHaveAttribute('aria-describedby', 'llm-no-provider')
    const consequence = screen.getByTestId('llm-no-provider')
    expect(consequence).toHaveAttribute('role', 'status')
    expect(consequence).toHaveTextContent('无法生成符合 schema 的决策')
    expect(consequence).toHaveTextContent('入场复核')
    expect(consequence).toHaveTextContent('仓位管理')
  })
})

describe('OnboardingPage deferred steps', () => {
  it('shows Binance as Milestone 5 without inputs or a saved claim', async () => {
    renderOnboarding({ step: 3 })

    const heading = await stepHeading()
    expect(heading).toHaveTextContent('Binance')
    expect(screen.getAllByText(/Milestone 5/).length).toBeGreaterThan(0)
    expect(screen.getByTestId('deferred-step-3')).toHaveTextContent('不会保存任何内容')
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: /保存/ })).toBeNull()
    expect(screen.queryByText(/已保存/)).toBeNull()
    expect(screen.getByRole('link', { name: /继续/ })).toHaveAttribute('href', '/onboarding/5')
  })

  it('shows Telegram as Milestone 6 without inputs or a saved claim', async () => {
    renderOnboarding({ step: 4 })

    const heading = await stepHeading()
    expect(heading).toHaveTextContent('Telegram')
    expect(screen.getAllByText(/Milestone 6/).length).toBeGreaterThan(0)
    expect(screen.getByTestId('deferred-step-4')).toHaveTextContent('不会保存任何内容')
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: /保存/ })).toBeNull()
    expect(screen.queryByText(/已保存/)).toBeNull()
  })
})

describe('OnboardingPage step 5 — watchlist, schedule, budget, review', () => {
  it('uses the real services, surfaces source, and persists only real changes', async () => {
    const updateScan = vi.fn(async (period: string) =>
      scanEnvelope({ value: period, source: 'user' }),
    )
    const updateBudget = vi.fn(async () => {
      const envelope = budgetUpdateEnvelope()
      return {
        ...envelope,
        data: {
          ...envelope.data!,
          thresholds: { ...envelope.data!.thresholds, daily_usd: '20.00' },
        },
      }
    })
    const completeStep = vi.fn(async () =>
      progressEnvelope(
        progressData({ completed_steps: [1, 2, 5], resumed_step: 5, all_complete: true }),
      ),
    )
    const clearKey = vi.fn()
    const { updateScan: scanFn } = renderOnboarding({
      step: 5,
      completeStep,
      updateScan,
      updateBudget,
      clearKey,
      scan: async () => scanEnvelope({ value: '4h', source: 'user' }),
    })

    await stepHeading()
    expect(await screen.findByTestId('scan-source')).toHaveTextContent('source: user')
    expect(screen.getByTestId('scan-effective')).toHaveTextContent('4h')
    expect(screen.getByTestId('scan-next-run')).toHaveTextContent('2026-11-06T12:00:00.000Z')
    expect(screen.getByTestId('watchlist-source')).toHaveTextContent('registry')
    expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('至少 1 个')
    expect(screen.getByRole('spinbutton', { name: '日预算（USD）' })).toHaveValue(10)
    expect(scanFn).not.toHaveBeenCalled()

    // Star an unstarred symbol through the shared watchlist service.
    fireEvent.click(screen.getByRole('button', { name: /星标 ETHUSDT/ }))

    // Change the period and budget, then finish.
    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '日预算（USD）' }), {
      target: { value: '20.00' },
    })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    await waitFor(() => expect(updateScan).toHaveBeenCalledWith('8h', 'test-key'))
    await waitFor(() =>
      expect(updateBudget).toHaveBeenCalledWith({ daily_usd: '20.00' }, 'test-key'),
    )
    await waitFor(() => expect(completeStep).toHaveBeenCalledWith(5, 'test-key'))
    expect(clearKey).toHaveBeenCalledWith('scan.period')
    expect(clearKey).toHaveBeenCalledWith('budget.thresholds')

    const review = await screen.findByTestId('onboarding-review')
    expect(review).toHaveTextContent('Paper')
    expect(review).toHaveTextContent('8h')
    expect(review).toHaveTextContent('20.00')
    expect(screen.getByRole('link', { name: /打开 \/scans/ })).toHaveAttribute('href', '/scans')
    expect(review).toHaveTextContent('oft scan run')
    expect(review).toHaveTextContent('Live 未启用')
    expect(screen.queryByRole('radio', { name: /^Live/ })).toBeNull()

    // The review heading receives focus after the finish transition.
    await waitFor(() => expect(screen.getByRole('heading', { name: /引导完成/ })).toHaveFocus())
  })

  it('renders seeded rows, star count, and budget drafts on the first ready paint', async () => {
    renderOnboarding({ step: 5 })

    await stepHeading()
    // `scan-source` is rendered by the ready branch; every derived value is
    // already present in that same render (no effect-based seeding window).
    await screen.findByTestId('scan-source')
    expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('已星标 1 个')
    expect(screen.getByRole('button', { name: /取消星标 BTCUSDT/ })).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: '日预算（USD）' })).toHaveValue(10)
    expect(screen.getByRole('spinbutton', { name: '周预算（USD）' })).toHaveValue(50)
    expect(screen.getByRole('spinbutton', { name: '月预算（USD）' })).toHaveValue(150)
    expect(screen.queryByTestId('watchlist-truncation')).toBeNull()
  })

  it('does not call the settings mutations when nothing changed', async () => {
    const updateScan = vi.fn(async (period: string) => scanEnvelope({ value: period }))
    const updateBudget = vi.fn(async () => budgetUpdateEnvelope())
    const completeStep = vi.fn(async () =>
      progressEnvelope(progressData({ completed_steps: [1, 2, 5], all_complete: true })),
    )
    renderOnboarding({ step: 5, completeStep, updateScan, updateBudget })

    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    await waitFor(() => expect(completeStep).toHaveBeenCalledWith(5, 'test-key'))
    expect(updateScan).not.toHaveBeenCalled()
    expect(updateBudget).not.toHaveBeenCalled()
    expect(await screen.findByTestId('onboarding-review')).toBeInTheDocument()
  })

  it('renders the review on load when the server says all steps are complete', async () => {
    renderOnboarding({
      step: 5,
      progress: progressData({
        completed_steps: [1, 2, 5],
        resumed_step: 5,
        all_complete: true,
      }),
    })

    expect(await screen.findByTestId('onboarding-review')).toBeInTheDocument()
    // No editable completion action on an already-complete wizard.
    expect(screen.queryByRole('button', { name: '完成引导并查看首次扫描' })).toBeNull()
    expect(screen.getAllByRole('button', { name: /星标/ }).length).toBeGreaterThan(0)
  })

  it('blocks finish without a starred symbol and focuses the error', async () => {
    const completeStep = vi.fn(async () => progressEnvelope(progressData()))
    renderOnboarding({
      step: 5,
      completeStep,
      watchlist: async () =>
        watchlistEnvelope([watchlistRow('BTCUSDT', false), watchlistRow('ETHUSDT', false)]),
    })

    await stepHeading()
    await screen.findByTestId('watchlist-validation')
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    const summary = await screen.findByTestId('onboarding-finish-error')
    expect(summary).toHaveTextContent('至少星标 1 个标的')
    await waitFor(() => expect(summary).toHaveFocus())
    expect(completeStep).not.toHaveBeenCalled()
  })

  it('surfaces a server validation refusal inline, focuses the summary and marks the field', async () => {
    const updateScan = vi.fn(async () => {
      throw new SettingsApiError('scan.period must be one of: 4h, 8h', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'period', code: 'unsupported', message: 'invalid period' }],
      })
    })
    const completeStep = vi.fn(async () => progressEnvelope(progressData()))
    renderOnboarding({ step: 5, updateScan, completeStep })

    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    const summary = await screen.findByTestId('onboarding-finish-error')
    expect(summary).toHaveTextContent('scan.period must be one of: 4h, 8h')
    await waitFor(() => expect(summary).toHaveFocus())
    expect(screen.getByRole('group', { name: '扫描周期' })).toHaveAttribute('aria-invalid', 'true')
    expect(completeStep).not.toHaveBeenCalled()
  })

  it('rotates the scan key on a conflict and keeps it on a timeout', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const conflictScan = vi.fn(async () => {
      throw new SettingsApiError('conflict', 'IDEMPOTENCY_CONFLICT', { httpStatus: 409 })
    })
    const { unmount } = renderOnboarding({
      step: 5,
      updateScan: conflictScan,
      rotateKey,
      clearKey,
    })
    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))
    await waitFor(() => expect(rotateKey).toHaveBeenCalledWith('scan.period'))
    expect(clearKey).not.toHaveBeenCalled()
    unmount()

    const timeoutScan = vi.fn(async () => {
      throw new RequestTimeoutError()
    })
    renderOnboarding({
      step: 5,
      updateScan: timeoutScan,
      rotateKey,
      clearKey,
    })
    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))
    const summary = await screen.findByTestId('onboarding-finish-error')
    expect(summary).toHaveTextContent('保存结果未知')
    expect(summary).toHaveTextContent('结果未知')
    expect(rotateKey).toHaveBeenCalledTimes(1)
    expect(clearKey).not.toHaveBeenCalled()
  })

  it('clears the scan key on a definitive refusal and leads with rejection', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()
    const updateScan = vi.fn(async () => {
      throw new SettingsApiError('scan.period must be one of: 4h, 8h', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'period', code: 'unsupported', message: 'invalid period' }],
      })
    })
    renderOnboarding({ step: 5, updateScan, rotateKey, clearKey })

    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    const summary = await screen.findByTestId('onboarding-finish-error')
    expect(summary).toHaveTextContent('保存被拒绝')
    await waitFor(() => expect(clearKey).toHaveBeenCalledWith('scan.period'))
    expect(rotateKey).not.toHaveBeenCalled()
  })

  it('applies the same idempotency lifecycle to budget.thresholds', async () => {
    const rotateKey = vi.fn(() => 'rotated-key')
    const clearKey = vi.fn()

    const conflictBudget = vi.fn(async () => {
      throw new SettingsApiError('conflict', 'IDEMPOTENCY_CONFLICT', { httpStatus: 409 })
    })
    const conflicted = renderOnboarding({
      step: 5,
      updateBudget: conflictBudget,
      rotateKey,
      clearKey,
    })
    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.change(screen.getByRole('spinbutton', { name: '日预算（USD）' }), {
      target: { value: '20.00' },
    })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))
    await waitFor(() => expect(rotateKey).toHaveBeenCalledWith('budget.thresholds'))
    expect(clearKey).not.toHaveBeenCalled()
    conflicted.unmount()

    const timeoutBudget = vi.fn(async () => {
      throw new RequestTimeoutError()
    })
    const timedOut = renderOnboarding({ step: 5, updateBudget: timeoutBudget, rotateKey, clearKey })
    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.change(screen.getByRole('spinbutton', { name: '日预算（USD）' }), {
      target: { value: '20.00' },
    })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))
    const timeoutSummary = await screen.findByTestId('onboarding-finish-error')
    expect(timeoutSummary).toHaveTextContent('保存结果未知')
    expect(rotateKey).toHaveBeenCalledTimes(1)
    expect(clearKey).not.toHaveBeenCalled()
    timedOut.unmount()

    const refusedBudget = vi.fn(async () => {
      throw new SettingsApiError('budget rejected', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'daily_usd', message: 'budget rejected' }],
      })
    })
    renderOnboarding({ step: 5, updateBudget: refusedBudget, rotateKey, clearKey })
    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.change(screen.getByRole('spinbutton', { name: '日预算（USD）' }), {
      target: { value: '20.00' },
    })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))
    const refusalSummary = await screen.findByTestId('onboarding-finish-error')
    expect(refusalSummary).toHaveTextContent('保存被拒绝')
    await waitFor(() => expect(clearKey).toHaveBeenCalledWith('budget.thresholds'))
    expect(rotateKey).toHaveBeenCalledTimes(1)
  })

  it('renders an all-complete review as a true read-only result', async () => {
    const updateScan = vi.fn(async (period: string) => scanEnvelope({ value: period }))
    const updateBudget = vi.fn(async () => budgetUpdateEnvelope())
    const completeStep = vi.fn(async () => progressEnvelope(progressData()))
    renderOnboarding({
      step: 5,
      progress: progressData({
        completed_steps: [1, 2, 5],
        resumed_step: 5,
        all_complete: true,
      }),
      updateScan,
      updateBudget,
      completeStep,
    })

    expect(await screen.findByTestId('onboarding-review')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: '8h' })).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: '日预算（USD）' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /取消星标 BTCUSDT/ })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '完成引导并查看首次扫描' })).toBeNull()

    // The form submit path is gone: Enter in a budget field cannot re-complete.
    fireEvent.submit(screen.getByRole('form', { name: 'Watchlist、周期与预算' }))
    await waitFor(() => expect(completeStep).not.toHaveBeenCalled())
    expect(updateScan).not.toHaveBeenCalled()
    expect(updateBudget).not.toHaveBeenCalled()
  })

  it('clears a stale star error on recovery and never steals focus', async () => {
    let attempt = 0
    const star = vi.fn(async (symbol: string, starred: boolean) => {
      attempt += 1
      if (attempt === 1) {
        throw new Error('registry busy')
      }
      return {
        ok: true,
        data: { symbol, starred, starred_at: '2026-11-06T10:00:00.000Z' },
        error: null,
        meta,
      }
    })
    renderOnboarding({ step: 5, star })

    await stepHeading()
    const button = await screen.findByRole('button', { name: /星标 ETHUSDT/ })
    button.focus()
    expect(button).toHaveFocus()
    fireEvent.click(button)

    const starError = await screen.findByTestId('onboarding-star-error')
    expect(starError).toHaveTextContent('registry busy')
    // A background failure does not move focus to the summary.
    expect(button).toHaveFocus()
    expect(screen.queryByTestId('onboarding-finish-error')).toBeNull()

    fireEvent.click(button)
    await waitFor(() => expect(screen.queryByTestId('onboarding-star-error')).toBeNull())
    expect(button).toHaveFocus()
    expect(button).toHaveAttribute('aria-pressed', 'true')
  })

  it('marks only the offending budget field invalid and wires its description', async () => {
    renderOnboarding({ step: 5 })

    await stepHeading()
    await screen.findByTestId('scan-source')
    const daily = screen.getByRole('spinbutton', { name: '日预算（USD）' })
    // The seeded server value is present before any submit can run.
    expect(daily).toHaveValue(10)
    fireEvent.change(daily, { target: { value: '1.2.3' } })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    await waitFor(() => expect(daily).toHaveAttribute('aria-invalid', 'true'))
    expect(daily.getAttribute('aria-describedby')).toContain('onboarding-budget-daily-error')
    expect(daily.getAttribute('aria-describedby')).toContain('onboarding-finish-error')
    // The other budget fields are not derived from one stale error string.
    expect(screen.getByRole('spinbutton', { name: '周预算（USD）' })).not.toHaveAttribute(
      'aria-invalid',
    )

    fireEvent.change(daily, { target: { value: '10.00' } })
    expect(daily).not.toHaveAttribute('aria-invalid')
  })

  it('discloses a truncated watchlist and derives the reported quality', async () => {
    const rows = Array.from({ length: 14 }, (_, index) =>
      watchlistRow(`SYM${index}USDT`, index === 0),
    )
    renderOnboarding({
      step: 5,
      watchlist: async () => watchlistEnvelope(rows, { total: 500, truncated: true }),
    })

    await stepHeading()
    await screen.findByTestId('scan-source')
    expect(screen.getByTestId('watchlist-source')).toHaveTextContent('data_quality')
    expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('已星标 1 个')
    const note = await screen.findByTestId('watchlist-truncation')
    expect(note).toHaveTextContent('共 500 个')
    expect(screen.getByRole('link', { name: /完整列表/ })).toHaveAttribute('href', '/watchlist')
    // Only the first named cap is rendered, and the omission is disclosed.
    expect(screen.queryByRole('button', { name: /星标 SYM13USDT/ })).toBeNull()
  })

  it('shows the saved result, not the wizard review, when earlier steps are open', async () => {
    const completeStep = vi.fn(async () =>
      progressEnvelope(
        progressData({ completed_steps: [5], resumed_step: 1, all_complete: false }),
      ),
    )
    renderOnboarding({ step: 5, completeStep })

    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    const saved = await screen.findByTestId('onboarding-step5-saved')
    expect(saved).toHaveTextContent('步骤 5 已保存')
    expect(screen.getByRole('link', { name: /继续步骤 1/ })).toHaveAttribute('href', '/onboarding/1')
    // The server said the wizard is not complete: no review, no completion claim.
    expect(screen.queryByTestId('onboarding-review')).toBeNull()
    expect(screen.queryByText(/引导完成/)).toBeNull()
    expect(screen.getByTestId('onboarding-live')).toHaveTextContent('步骤 5 已保存')
    expect(screen.getByTestId('onboarding-live')).not.toHaveTextContent('引导已完成')
    // Editing stays available so the remaining steps can be completed.
    expect(screen.getByRole('button', { name: '完成引导并查看首次扫描' })).toBeInTheDocument()
  })

  it('uses the server starred total even when the star is outside the visible page', async () => {
    const rows = [watchlistRow('AAAUSDT', false), watchlistRow('BBBUSDT', false)]
    const completeStep = vi.fn(async () =>
      progressEnvelope(
        progressData({ completed_steps: [1, 2, 5], resumed_step: 5, all_complete: true }),
      ),
    )
    renderOnboarding({
      step: 5,
      completeStep,
      watchlist: async () =>
        watchlistEnvelope(rows, { total: 500, truncated: true, next_scan_count: 1 }),
    })

    await stepHeading()
    await screen.findByTestId('scan-source')
    expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('已星标 1 个')

    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    await waitFor(() => expect(completeStep).toHaveBeenCalledWith(5, 'test-key'))
    const review = await screen.findByTestId('onboarding-review')
    expect(review).toHaveTextContent('1 个已星标')
  })

  it('adjusts the server starred total by local star toggles', async () => {
    renderOnboarding({
      step: 5,
      watchlist: async () =>
        watchlistEnvelope([watchlistRow('AAAUSDT', false), watchlistRow('BBBUSDT', false)], {
          next_scan_count: 1,
        }),
    })

    await stepHeading()
    await screen.findByTestId('scan-source')
    // The one starred symbol lives outside this page; the page rows say zero.
    expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('已星标 1 个')

    fireEvent.click(screen.getByRole('button', { name: /星标 AAAUSDT/ }))
    await waitFor(() =>
      expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('已星标 2 个'),
    )

    fireEvent.click(screen.getByRole('button', { name: /取消星标 AAAUSDT/ }))
    await waitFor(() =>
      expect(screen.getByTestId('watchlist-validation')).toHaveTextContent('已星标 1 个'),
    )
  })

  it('does not double-count a starred symbol after a reload', () => {
    const rows = [watchlistRow('AAAUSDT', true)]
    // A fresh server total already includes the star; with no local deltas the
    // count is the server value, never 2.
    expect(adjustStarredCount(1, rows, {})).toBe(1)
    expect(
      adjustStarredCount(1, rows, {
        AAAUSDT: { starred: true, starred_at: '2026-11-06T09:00:00.000Z' },
      }),
    ).toBe(1)
  })

  it('removes the stale summary when the last flagged field is corrected', async () => {
    renderOnboarding({
      step: 5,
      watchlist: async () => watchlistEnvelope([watchlistRow('BTCUSDT', true)]),
    })

    await stepHeading()
    await screen.findByTestId('scan-source')
    const daily = screen.getByRole('spinbutton', { name: '日预算（USD）' })
    fireEvent.change(daily, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    const summary = await screen.findByTestId('onboarding-finish-error')
    expect(summary).toHaveTextContent('日预算')

    fireEvent.change(daily, { target: { value: '10.00' } })
    await waitFor(() => expect(screen.queryByTestId('onboarding-finish-error')).toBeNull())
  })

  it('keeps concurrent validation errors when a pending star later resolves', async () => {
    // An in-flight star must not apply a render-time snapshot of the issues:
    // the submit below flags the watchlist and the daily budget while the star
    // request is still pending, and the star's continuation must only clear
    // its own field, not the still-valid budget error or the summary.
    let resolveStar: (value: StarEnvelope) => void = () => {}
    const star = vi.fn(
      () =>
        new Promise<StarEnvelope>((resolve) => {
          resolveStar = resolve
        }),
    )
    renderOnboarding({
      step: 5,
      star,
      watchlist: async () => watchlistEnvelope([watchlistRow('ETHUSDT', false)]),
    })

    await stepHeading()
    await screen.findByTestId('scan-source')
    fireEvent.click(screen.getByRole('button', { name: /星标 ETHUSDT/ }))

    const daily = screen.getByRole('spinbutton', { name: '日预算（USD）' })
    fireEvent.change(daily, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))

    const summary = await screen.findByTestId('onboarding-finish-error')
    expect(summary).toHaveTextContent('至少星标 1 个标的')
    expect(summary).toHaveTextContent('日预算必须是')
    await waitFor(() => expect(daily).toHaveAttribute('aria-invalid', 'true'))

    await act(async () => {
      resolveStar({
        ok: true,
        data: { symbol: 'ETHUSDT', starred: true, starred_at: '2026-11-06T10:00:00.000Z' },
        error: null,
        meta,
      })
    })

    // The star's success only clears its own issue; the budget error and the
    // assertive summary survive because the budget is still invalid.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /取消星标 ETHUSDT/ })).toBeInTheDocument(),
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByTestId('onboarding-finish-error')).toHaveTextContent('日预算必须是')
    expect(screen.getByTestId('onboarding-finish-error')).not.toHaveTextContent(
      '至少星标 1 个标的',
    )
    expect(daily).toHaveAttribute('aria-invalid', 'true')
  })

  it('disables the star controls while a finish is in flight', async () => {
    let resolveComplete: (value: OnboardingProgressEnvelope) => void = () => {}
    const completeStep = vi.fn(
      () =>
        new Promise<OnboardingProgressEnvelope>((resolve) => {
          resolveComplete = resolve
        }),
    )
    renderOnboarding({ step: 5, completeStep })

    await stepHeading()
    await screen.findByTestId('scan-source')
    const star = screen.getByRole('button', { name: /取消星标 BTCUSDT/ })
    expect(star).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: '完成引导并查看首次扫描' }))
    await waitFor(() => expect(completeStep).toHaveBeenCalled())
    await waitFor(() => expect(star).toBeDisabled())

    resolveComplete(
      progressEnvelope(
        progressData({ completed_steps: [1, 2, 5], resumed_step: 5, all_complete: true }),
      ),
    )
    await screen.findByTestId('onboarding-review')
  })
})

describe('OnboardingPage accessibility and read-only surfaces', () => {
  it('uses exactly one polite live region and never an assertive one', async () => {
    const { container } = renderOnboarding({ step: 1 })
    await stepHeading()

    const regions = Array.from(container.querySelectorAll('[aria-live]'))
    expect(regions).toHaveLength(1)
    expect(regions[0]).toHaveAttribute('aria-live', 'polite')
    expect(container.querySelector('[aria-live="assertive"]')).toBeNull()
  })

  it('is read-only at 375px and points to the desktop/CLI', async () => {
    renderOnboarding({ step: 1, isNarrow: true })

    await stepHeading()
    expect(screen.getByRole('note')).toHaveTextContent(
      '移动端只读：请在桌面端（宽度 ≥ 769px）打开本页完成引导。',
    )
    expect(screen.getByRole('button', { name: '保存并继续' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /Paper/ })).toBeDisabled()
    // The static step list and the previous/next links stay available.
    expect(screen.getByRole('navigation', { name: '首次配置步骤' })).toBeInTheDocument()
    expect(screen.getByTestId('onboarding-next')).toBeInTheDocument()
  })

  it('is read-only at 375px on steps 2 and 5', async () => {
    const { unmount } = renderOnboarding({ step: 2, isNarrow: true })
    await stepHeading()
    const keyInput = await screen.findByTestId('llm-key-input-openai')
    expect(keyInput).toBeDisabled()
    expect(screen.getByTestId('llm-key-save-openai')).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存并继续' })).toBeDisabled()
    unmount()

    renderOnboarding({ step: 5, isNarrow: true })
    await stepHeading()
    await screen.findByTestId('scan-source')
    expect(screen.getByRole('button', { name: /星标 ETHUSDT/ })).toBeDisabled()
    expect(screen.getByRole('radio', { name: '8h' })).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: '日预算（USD）' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '完成引导并查看首次扫描' })).toBeDisabled()
  })

  it('moves focus to the step heading when the step changes', async () => {
    const { rerender, navigate } = renderOnboarding({ step: 1 })
    const first = await stepHeading()
    expect(first).toHaveTextContent('环境与本地绑定')
    expect(first).toHaveFocus()

    rerender(
      <OnboardingPage
        step={2}
        navigate={navigate}
        fetchProgressFn={async () => progressEnvelope(progressData({ resumed_step: 2 }))}
        completeStepFn={async () => progressEnvelope(progressData())}
        idempotencyKeyFn={() => 'test-key'}
        clearIdempotencyKeyFn={() => {}}
        rotateIdempotencyKeyFn={() => 'rotated'}
        fetchReadinessFn={async () => readinessEnvelope()}
        fetchStatusFn={async () => statusEnvelope()}
        fetchLLMSettingsFn={async () => llmEnvelope()}
        setLLMKeyFn={async (provider) => keyStatus(provider)}
        fetchWatchlistFn={async () => watchlistEnvelope()}
        setStarFn={async (symbol, starred) => ({
          ok: true,
          data: { symbol, starred, starred_at: null },
          error: null,
          meta,
        })}
        fetchScanSettingsFn={async () => scanEnvelope()}
        updateScanPeriodFn={async (period) => scanEnvelope({ value: period })}
        fetchBudgetFn={async () => budgetEnvelope()}
        updateBudgetThresholdsFn={async () => budgetUpdateEnvelope()}
      />,
    )

    const second = await stepHeading()
    expect(second).toHaveTextContent('LLM providers')
    await waitFor(() => expect(second).toHaveFocus())
  })

  it('focuses the error summary when a completion fails', async () => {
    const completeStep = vi.fn(async () => {
      throw new SettingsApiError('deferred step', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'step', code: 'step_deferred', message: 'deferred step' }],
      })
    })
    renderOnboarding({ step: 1, completeStep })

    await stepHeading()
    fireEvent.click(screen.getByRole('button', { name: '保存并继续' }))

    const summary = await screen.findByTestId('onboarding-error')
    expect(summary).toHaveTextContent('deferred step')
    await waitFor(() => expect(summary).toHaveFocus())
  })
})

describe('completeOnboardingStep client', () => {
  it('rejects a deferred step PUT with the server validation error', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            ok: false,
            data: null,
            error: {
              code: 'VALIDATION_FAILED',
              message: 'step 3 is deferred',
              category: 'VALIDATION',
              http_status: 422,
              retryable: false,
              reference_id: 'REQ-422',
              details: [{ field: 'step', code: 'step_deferred', message: 'step 3 is deferred' }],
            },
            meta: {},
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      await expect(completeOnboardingStep(3, 'test-key')).rejects.toMatchObject({
        code: 'VALIDATION_FAILED',
        details: [{ field: 'step', code: 'step_deferred' }],
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
