import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchBudget } from '../../api/budget'
import type {
  BudgetAlert,
  BudgetData,
  BudgetEnvelope,
  BudgetThresholds,
  BudgetUpdateData,
  BudgetUpdateEnvelope,
  BudgetUpdatePayload,
} from '../../api/budget'
import { validateBudgetDraft } from '../../hooks/use-budget-settings-form'
import { SettingsApiError } from '../../api/settings'
import type { ApiMeta } from '../../api/status'
import { SettingsBudgetPage } from '../settings-budget'
import { resolveRoute } from '../route-match'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const THRESHOLDS = {
  daily_usd: '10.00',
  weekly_usd: '50.00',
  monthly_usd: '150.00',
  warn_pct: 80,
}

const ALERT: BudgetAlert = {
  period: 'day',
  state: 'WARN',
  spend_usd: '8.25000000',
  threshold_usd: '10.00',
  occurred_at: '2026-11-09T10:00:00.000Z',
  provider: 'openai',
  model: 'gpt-fixture',
}

function budgetData(overrides: Partial<BudgetData> = {}): BudgetData {
  return {
    thresholds: THRESHOLDS,
    usage_quality: 'COMPLETE',
    budget_state: 'OK',
    periods: {
      day: {
        period_key: '2026-11-09',
        spend_usd: '2.18000000',
        call_count: 3,
        unpriced_count: 0,
        budget_state: 'OK',
      },
      week: {
        period_key: '2026-W46',
        spend_usd: '7.42000000',
        call_count: 9,
        unpriced_count: 1,
        budget_state: 'OK',
      },
      month: {
        period_key: '2026-11',
        spend_usd: '21.60000000',
        call_count: 20,
        unpriced_count: 2,
        budget_state: 'OK',
      },
    },
    projected_month_end_usd: '48.70000000',
    projection_basis: {
      month_spend_usd: '21.60000000',
      elapsed_fraction: 0.4435,
      as_of: '2026-11-09T10:00:00.000Z',
    },
    last_alert: null,
    ...overrides,
  }
}

function budgetEnvelope(data: BudgetData): BudgetEnvelope {
  return { ok: true, data, error: null, meta }
}

function updateEnvelope(
  overrides: Partial<BudgetData> & { changed?: boolean } = {},
): BudgetUpdateEnvelope {
  const { changed = true, ...rest } = overrides
  const data: BudgetUpdateData = { ...budgetData(), ...rest, changed }
  return { ok: true, data, error: null, meta }
}

interface RenderOptions {
  fetch?: (signal?: AbortSignal) => Promise<BudgetEnvelope>
  update?: (payload: BudgetUpdatePayload, key: string) => Promise<BudgetUpdateEnvelope>
  isCompact?: boolean
  clear?: () => void
  rotate?: () => string
}

async function renderLoaded(options: RenderOptions = {}) {
  const fetch = options.fetch ?? (async () => budgetEnvelope(budgetData({ last_alert: ALERT })))
  const update = options.update ?? (async () => updateEnvelope())
  const clear = options.clear ?? vi.fn()
  const rotate = options.rotate ?? vi.fn(() => 'rotated-budget-key')
  render(
    <SettingsBudgetPage
      fetchBudgetFn={fetch}
      updateBudgetFn={update}
      idempotencyKeyFn={() => 'budget-key'}
      clearIdempotencyKeyFn={clear}
      rotateIdempotencyKeyFn={rotate}
      isCompact={options.isCompact}
    />,
  )
  await screen.findByTestId('budget-usage')
  return { fetch, update, clear, rotate }
}

function change(label: RegExp, value: string): void {
  fireEvent.change(screen.getByRole('textbox', { name: label }), {
    target: { value },
  })
}

describe('SettingsBudgetPage', () => {
  it('shows thresholds, warn %, spend, state, projection, unpriced, and the last alert', async () => {
    await renderLoaded()

    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('10.00')
    expect(screen.getByRole('textbox', { name: 'Weekly USD' })).toHaveValue('50.00')
    expect(screen.getByRole('textbox', { name: 'Monthly USD' })).toHaveValue('150.00')
    expect(screen.getByRole('textbox', { name: '警告阈值 %' })).toHaveValue('80')

    expect(screen.getByTestId('budget-today-spend')).toHaveTextContent('$2.18000000')
    expect(screen.getByTestId('budget-weekly-spend')).toHaveTextContent('$7.42000000')
    expect(screen.getByTestId('budget-monthly-spend')).toHaveTextContent('$21.60000000')
    expect(screen.getByTestId('budget-projection')).toHaveTextContent('$48.70000000')
    expect(screen.getByTestId('budget-unpriced-count')).toHaveTextContent('2')

    const dayMeter = screen.getByTestId('budget-meter-day')
    expect(dayMeter).toHaveAttribute('role', 'meter')
    expect(dayMeter).toHaveAttribute('aria-valuemin', '0')
    expect(dayMeter).toHaveAttribute('aria-valuemax', '10')
    expect(dayMeter).toHaveAttribute('aria-valuenow', '2.18')
    expect(dayMeter.getAttribute('aria-valuetext')).toContain('状态 OK')
    expect(dayMeter.getAttribute('aria-valuetext')).toContain('调用 3 次')
    const monthMeter = screen.getByTestId('budget-meter-month')
    expect(monthMeter.getAttribute('aria-valuetext')).toContain('状态 OK')
    expect(monthMeter.getAttribute('aria-valuetext')).toContain('调用 20 次')
    expect(monthMeter).toHaveTextContent('unpriced 2 次')

    const banner = screen.getByTestId('budget-unpriced-banner')
    expect(banner).toHaveTextContent('2 次 unpriced')
    expect(banner).toHaveTextContent('绝不静默归零')

    const alert = screen.getByTestId('budget-last-alert')
    expect(alert).toHaveTextContent('2026-11-09T10:00:00.000Z')
    expect(alert).toHaveTextContent('WARN')
    expect(alert).toHaveTextContent('openai')
    expect(alert).toHaveTextContent('gpt-fixture')

    expect(screen.getAllByText(/绝不停止/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/不会通过\s*Telegram 投递/).length).toBeGreaterThan(0)
  })

  it('renders an explicit no-alert message when no threshold was crossed', async () => {
    await renderLoaded({ fetch: async () => budgetEnvelope(budgetData()) })

    expect(screen.getByTestId('budget-last-alert')).toHaveTextContent('尚无阈值告警')
  })

  it('renders N/A for every usage value when the GET fails, never zero', async () => {
    const fetch = vi.fn(async () => {
      throw new Error('budget ledger unavailable')
    })
    render(<SettingsBudgetPage fetchBudgetFn={fetch} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('预算设置不可用')
    expect(alert).toHaveTextContent('budget ledger unavailable')

    for (const testId of [
      'budget-usage-spend',
      'budget-usage-state',
      'budget-usage-projection',
      'budget-usage-unpriced',
    ]) {
      expect(screen.getByTestId(testId)).toHaveTextContent('N/A')
    }
    expect(screen.queryByText('$0.00000000')).not.toBeInTheDocument()
    expect(screen.queryByRole('form', { name: '修改预算阈值' })).not.toBeInTheDocument()
    expect(screen.getByTestId('budget-usage')).toHaveTextContent('阈值不可编辑，直到重新加载成功')
    expect(screen.queryByText(/阈值仍可编辑/)).not.toBeInTheDocument()
    // The hard-error path keeps only the page-level reload retry.
    expect(screen.queryByRole('button', { name: '重试用量' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })

  it('keeps the last good values and marks them STALE while refreshing', async () => {
    let resolveRefresh: ((envelope: BudgetEnvelope) => void) | undefined
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((resolve) => {
            resolveRefresh = resolve
          }),
      )
    await renderLoaded({ fetch })

    fireEvent.click(screen.getByRole('button', { name: '刷新用量' }))

    await waitFor(() =>
      expect(screen.getByTestId('budget-usage')).toHaveAttribute('aria-busy', 'true'),
    )
    expect(screen.getByTestId('budget-today-spend')).toHaveTextContent('$2.18000000')
    expect(screen.getAllByText('STALE').length).toBeGreaterThan(0)

    resolveRefresh?.(budgetEnvelope(budgetData({ projected_month_end_usd: '50.00000000' })))
    await waitFor(() =>
      expect(screen.getByTestId('budget-projection')).toHaveTextContent('$50.00000000'),
    )
    expect(screen.getByTestId('budget-usage')).toHaveAttribute('aria-busy', 'false')
  })

  it('keeps the last good values marked STALE when a refresh fails', async () => {
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockRejectedValueOnce(new Error('socket closed'))
    await renderLoaded({ fetch })

    fireEvent.click(screen.getByRole('button', { name: '刷新用量' }))

    const notice = await screen.findByTestId('budget-refresh-notice')
    expect(notice).toHaveTextContent('刷新失败')
    expect(notice).toHaveTextContent('socket closed')
    expect(notice).toHaveTextContent('保留上次成功值并标记 STALE。')
    expect(screen.getByTestId('budget-today-spend')).toHaveTextContent('$2.18000000')
    expect(screen.getAllByText('STALE').length).toBeGreaterThan(0)
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage-status')).toHaveTextContent(
        '刷新失败：socket closed。保留上次成功值并标记 STALE。',
      ),
    )
  })

  it('shows and announces degraded refresh copy without claiming STALE', async () => {
    let rejectRefresh: ((error: unknown) => void) | undefined
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(
        budgetEnvelope(
          budgetData({
            usage_quality: 'UNAVAILABLE',
            budget_state: null,
            periods: null,
            projected_month_end_usd: null,
            projection_basis: null,
            last_alert: null,
          }),
        ),
      )
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((_resolve, reject) => {
            rejectRefresh = reject
          }),
      )
    await renderLoaded({ fetch })

    fireEvent.click(screen.getByRole('button', { name: '重试用量' }))

    // The degraded panel shows N/A with no STALE badge, so neither the
    // in-flight nor the failure copy may reference a last-good value.
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage-status')).toHaveTextContent(
        '正在刷新用量；用量当前不可用（N/A）。',
      ),
    )
    expect(screen.getByTestId('budget-usage-status')).not.toHaveTextContent('STALE')

    await act(async () => {
      rejectRefresh?.(new Error('socket closed'))
    })

    const notice = await screen.findByTestId('budget-refresh-notice')
    expect(notice).toHaveTextContent('刷新失败：socket closed；用量仍不可用（N/A），阈值可编辑。')
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage-status')).toHaveTextContent(
        '刷新失败：socket closed；用量仍不可用（N/A），阈值可编辑。',
      ),
    )
    expect(screen.getByTestId('budget-usage-status')).not.toHaveTextContent('STALE')
    expect(screen.getByTestId('budget-usage-status')).not.toHaveTextContent('上次成功值')
    expect(screen.getByTestId('budget-usage-degraded')).toBeInTheDocument()
  })

  it('blocks an ordering violation inline without calling update', async () => {
    const update = vi.fn(async () => updateEnvelope())
    await renderLoaded({ update })

    change(/Weekly USD/, '5.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法保存')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(within(alert).getByRole('link', { name: '转到Weekly USD' })).toHaveAttribute(
      'href',
      '#budget-weekly',
    )
    expect(screen.getByText('Weekly 必须 ≥ Daily')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Weekly USD' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(update).not.toHaveBeenCalled()
  })

  it('accepts an unrelated save over a CLI-created crossed snapshot', async () => {
    const crossed: BudgetThresholds = { ...THRESHOLDS, daily_usd: '50.00', weekly_usd: '10.00' }
    const fetch = vi.fn(async () => budgetEnvelope(budgetData({ thresholds: crossed })))
    const update = vi.fn(async () => updateEnvelope({ thresholds: { ...crossed, warn_pct: 75 } }))
    await renderLoaded({ fetch, update })

    // The stored set is already crossed (daily 50 > weekly 10), as a CLI write
    // can leave it. Editing an unrelated field must not be blocked: the PUT
    // accepts it because the draft introduces no new crossing.
    change(/警告阈值 %/, '75')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(update).toHaveBeenCalledWith(
      {
        daily_usd: '50.00',
        weekly_usd: '10.00',
        monthly_usd: '150.00',
        warn_pct: 75,
      },
      'budget-key',
    )
    expect(screen.queryByText('Weekly 必须 ≥ Daily')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the Chinese warn label out of lang="en" while English terms stay tagged', async () => {
    await renderLoaded()

    expect(screen.getByText('警告阈值 %').closest('[lang="en"]')).toBeNull()
    expect(screen.getAllByText('Daily USD')[0].closest('[lang="en"]')).not.toBeNull()

    change(/警告阈值 %/, '0')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    const link = await screen.findByRole('link', { name: '转到警告阈值 %' })
    expect(link.querySelector('[lang="en"]')).toBeNull()
  })

  it('blocks an out-of-range warn percentage inline without native validation', async () => {
    const update = vi.fn(async () => updateEnvelope())
    await renderLoaded({ update })

    change(/警告阈值 %/, '0')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法保存')
    expect(screen.getByText('警告阈值必须大于 0 且不超过 100')).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
  })

  it('accepts a sub-one warn percentage and sends it unchanged', async () => {
    const update = vi.fn(async () =>
      updateEnvelope({ thresholds: { ...THRESHOLDS, warn_pct: 0.5 } }),
    )
    await renderLoaded({ update })

    change(/警告阈值 %/, '0.5')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    expect(update).toHaveBeenCalledWith(
      {
        daily_usd: '10.00',
        weekly_usd: '50.00',
        monthly_usd: '150.00',
        warn_pct: 0.5,
      },
      'budget-key',
    )
    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('已保存'),
    )
  })

  it('clears a stale ordering error when a coupled field is edited', async () => {
    const update = vi.fn(async () => updateEnvelope())
    await renderLoaded({ update })

    change(/Weekly USD/, '5.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await screen.findByText('Weekly 必须 ≥ Daily')
    expect(screen.getByRole('textbox', { name: 'Weekly USD' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )

    // Editing the paired lower bound makes weekly 5 ≥ daily 2 valid, so the
    // weekly error must clear without resubmitting.
    change(/Daily USD/, '2.00')
    expect(screen.queryByText('Weekly 必须 ≥ Daily')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Weekly USD' })).not.toHaveAttribute('aria-invalid')

    // Editing the field itself keeps the error clear.
    change(/Weekly USD/, '60.00')
    expect(screen.queryByText('Weekly 必须 ≥ Daily')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Weekly USD' })).not.toHaveAttribute('aria-invalid')
    expect(update).not.toHaveBeenCalled()
  })

  it('maps server field details to inline errors and the summary links', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('invalid budget thresholds', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [
          {
            field: 'monthly_usd',
            code: 'ordering',
            message: 'monthly_usd must be greater than or equal to weekly_usd',
          },
        ],
      })
    })
    await renderLoaded({ update })

    change(/Monthly USD/, '200.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    await waitFor(() =>
      expect(
        screen.getByText('monthly_usd must be greater than or equal to weekly_usd'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByRole('textbox', { name: 'Monthly USD' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    const alert = screen.getByRole('alert')
    expect(within(alert).getByRole('link', { name: '转到Monthly USD' })).toHaveAttribute(
      'href',
      '#budget-monthly',
    )
  })

  it('rotates the idempotency key on a 409 conflict and keeps the pending key unused', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('conflict', 'IDEMPOTENCY_CONFLICT', { httpStatus: 409 })
    })
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-budget-key')
    await renderLoaded({ update, clear, rotate })

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('请求冲突')
    expect(alert).toHaveTextContent('新的 Idempotency-Key')
    expect(rotate).toHaveBeenCalledTimes(1)
    expect(clear).not.toHaveBeenCalled()
  })

  it('clears the pending key after a definitive validation refusal', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('bad value', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [
          {
            field: 'monthly_usd',
            code: 'ordering',
            message: 'monthly_usd must be greater than or equal to weekly_usd',
          },
        ],
      })
    })
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-budget-key')
    await renderLoaded({ update, clear, rotate })

    change(/Monthly USD/, '200.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-error')).toHaveTextContent('bad value'),
    )
    expect(clear).toHaveBeenCalledTimes(1)
    expect(rotate).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Monthly USD' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(
      screen.getByText('monthly_usd must be greater than or equal to weekly_usd'),
    ).toBeInTheDocument()
  })

  it('saves a valid change with the pending key, announces the audit, and focuses save', async () => {
    const savedThresholds = { ...THRESHOLDS, daily_usd: '20.00' }
    const update = vi.fn(async () => updateEnvelope({ thresholds: savedThresholds }))
    const clear = vi.fn()
    await renderLoaded({ update, clear })

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('已保存'),
    )
    expect(update).toHaveBeenCalledTimes(1)
    expect(update).toHaveBeenCalledWith(
      {
        daily_usd: '20.00',
        weekly_usd: '50.00',
        monthly_usd: '150.00',
        warn_pct: 80,
      },
      'budget-key',
    )
    const visible = screen.getByTestId('settings-budget-save-visible')
    expect(visible).toHaveTextContent('10.00')
    expect(visible).toHaveTextContent('20.00')
    expect(visible).toHaveTextContent('audit 已写入')
    expect(visible).toHaveTextContent('绝不停止')
    expect(visible).toHaveTextContent('Telegram')
    expect(clear).toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('20.00')
    await waitFor(() => expect(screen.getByRole('button', { name: '保存阈值' })).toHaveFocus())
  })

  it('reports an unchanged save without claiming a new audit', async () => {
    const update = vi.fn(async () => updateEnvelope({ changed: false }))
    await renderLoaded({ update })

    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('未变更'),
    )
    expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('未写入新 audit')
    expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('绝不停止')
  })

  it('reports an unknown outcome and keeps the key after a transport failure', async () => {
    const update = vi.fn(async () => {
      throw new Error('network down')
    })
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-budget-key')
    await renderLoaded({ update, clear, rotate })

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).toHaveTextContent('同一 Idempotency-Key')
    expect(
      screen.getByText('上一次保存结果未知；重试会使用同一 Idempotency-Key 重放，不会重复写入。'),
    ).toBeInTheDocument()
    expect(clear).not.toHaveBeenCalled()
    expect(rotate).not.toHaveBeenCalled()
  })

  it('is read-only at the compact override while keeping values visible', async () => {
    await renderLoaded({ isCompact: true })

    expect(screen.getByText(/移动端只读/)).toHaveTextContent(/桌面端或 CLI/)
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: '警告阈值 %' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存阈值' })).toBeDisabled()
    expect(screen.getByTestId('budget-today-spend')).toHaveTextContent('$2.18000000')
  })

  it('keeps thresholds editable with N/A usage and a degraded marker', async () => {
    const fetch = vi.fn(async () =>
      budgetEnvelope(
        budgetData({
          usage_quality: 'UNAVAILABLE',
          budget_state: null,
          periods: null,
          projected_month_end_usd: null,
          projection_basis: null,
          last_alert: null,
        }),
      ),
    )
    await renderLoaded({ fetch })

    for (const testId of [
      'budget-usage-spend',
      'budget-usage-state',
      'budget-usage-projection',
      'budget-usage-unpriced',
    ]) {
      expect(screen.getByTestId(testId)).toHaveTextContent('N/A')
    }
    expect(screen.getByTestId('budget-usage-degraded')).toHaveTextContent('degraded')
    expect(screen.getByTestId('budget-usage-missing')).toHaveTextContent('阈值仍可编辑')
    expect(screen.queryByText('$0.00000000')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('10.00')
    expect(screen.getByRole('button', { name: '保存阈值' })).toBeEnabled()
    expect(screen.getByTestId('budget-last-alert')).toHaveTextContent('degraded')

    // The degraded panel offers its own keyboard-operable retry.
    fireEvent.click(screen.getByRole('button', { name: '重试用量' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage-status')).toHaveTextContent(
        '用量刷新后仍不可用（N/A）；阈值可编辑。',
      ),
    )
  })

  it('announces refresh start and success outside the busy subtree', async () => {
    let resolveRefresh: ((envelope: BudgetEnvelope) => void) | undefined
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((resolve) => {
            resolveRefresh = resolve
          }),
      )
    await renderLoaded({ fetch })

    fireEvent.click(screen.getByRole('button', { name: '刷新用量' }))

    await waitFor(() =>
      expect(screen.getByTestId('budget-usage-status')).toHaveTextContent('正在刷新用量'),
    )
    resolveRefresh?.(budgetEnvelope(budgetData()))
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage-status')).toHaveTextContent('用量已刷新。'),
    )
  })

  it('ignores a refresh that lands after a save and keeps the server snapshot', async () => {
    let resolveRefresh: ((envelope: BudgetEnvelope) => void) | undefined
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((resolve) => {
            resolveRefresh = resolve
          }),
      )
    const savedThresholds = { ...THRESHOLDS, daily_usd: '20.00' }
    const update = vi.fn(async () => updateEnvelope({ thresholds: savedThresholds }))
    await renderLoaded({ fetch, update })

    fireEvent.click(screen.getByRole('button', { name: '刷新用量' }))
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage')).toHaveAttribute('aria-busy', 'true'),
    )

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('已保存'),
    )
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('20.00')

    // The refresh snapshot was read before the save; it must not restore the
    // older thresholds into the displayed data.
    resolveRefresh?.(budgetEnvelope(budgetData()))
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage')).toHaveAttribute('aria-busy', 'false'),
    )
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('20.00')
    expect(screen.getByTestId('budget-meter-day')).toHaveAttribute('aria-valuemax', '20')
  })
  it('drops a pre-save refresh after an unknown save outcome', async () => {
    let resolveRefresh: ((envelope: BudgetEnvelope) => void) | undefined
    const reconciled = budgetEnvelope(
      budgetData({ thresholds: { ...THRESHOLDS, daily_usd: '25.00' } }),
    )
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((resolve) => {
            resolveRefresh = resolve
          }),
      )
      .mockResolvedValueOnce(reconciled)
    const update = vi.fn(async () => {
      throw new Error('network down')
    })
    await renderLoaded({ fetch, update })

    fireEvent.click(screen.getByRole('button', { name: '刷新用量' }))
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage')).toHaveAttribute('aria-busy', 'true'),
    )

    change(/Daily USD/, '25.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('结果未知'))
    await waitFor(() =>
      expect(screen.getByTestId('budget-meter-day')).toHaveAttribute('aria-valuemax', '25'),
    )

    // Applying the reconciled snapshot clears the superseded refresh's STALE
    // badge and "正在刷新用量" announcement.
    expect(screen.queryAllByText('STALE')).toHaveLength(0)
    expect(screen.getByTestId('budget-usage-status')).toBeEmptyDOMElement()
    expect(screen.queryByTestId('budget-refresh-notice')).not.toBeInTheDocument()

    // The pre-save refresh carries the older snapshot; the reconciliation
    // generation bump must keep the newer server thresholds on screen.
    resolveRefresh?.(budgetEnvelope(budgetData()))
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新用量' })).toBeEnabled())
    expect(screen.getByTestId('budget-meter-day')).toHaveAttribute('aria-valuemax', '25')

    // The superseded refresh returned without finalizing its STALE/refresh
    // notice; it must not bring them back.
    expect(screen.queryAllByText('STALE')).toHaveLength(0)
    expect(screen.getByTestId('budget-usage-status')).toBeEmptyDOMElement()
    expect(screen.queryByTestId('budget-refresh-notice')).not.toBeInTheDocument()
  })

  it('drops a pre-save refresh rejection after the reconcile snapshot', async () => {
    let rejectRefresh: ((error: unknown) => void) | undefined
    const reconciled = budgetEnvelope(
      budgetData({ thresholds: { ...THRESHOLDS, daily_usd: '25.00' } }),
    )
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((_resolve, reject) => {
            rejectRefresh = reject
          }),
      )
      .mockResolvedValueOnce(reconciled)
    const update = vi.fn(async () => {
      throw new Error('network down')
    })
    await renderLoaded({ fetch, update })

    fireEvent.click(screen.getByRole('button', { name: '刷新用量' }))
    await waitFor(() =>
      expect(screen.getByTestId('budget-usage')).toHaveAttribute('aria-busy', 'true'),
    )

    change(/Daily USD/, '25.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('结果未知'))
    await waitFor(() =>
      expect(screen.getByTestId('budget-meter-day')).toHaveAttribute('aria-valuemax', '25'),
    )

    // The refresh was started before the save; its rejection lands after the
    // unknown-outcome reconcile applied the newer snapshot and must not stamp
    // STALE or a failure notice over it.
    await act(async () => {
      rejectRefresh?.(new Error('socket closed'))
    })
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新用量' })).toBeEnabled())
    expect(screen.queryAllByText('STALE')).toHaveLength(0)
    expect(screen.getByTestId('budget-usage-status')).toBeEmptyDOMElement()
    expect(screen.queryByTestId('budget-refresh-notice')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('25.00')
    expect(screen.getByTestId('budget-meter-day')).toHaveAttribute('aria-valuemax', '25')
  })

  it('drops a stale reconcile response when a retry save already committed', async () => {
    let resolveReconcile: ((envelope: BudgetEnvelope) => void) | undefined
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<BudgetEnvelope>>()
      .mockResolvedValueOnce(budgetEnvelope(budgetData()))
      .mockImplementationOnce(
        () =>
          new Promise<BudgetEnvelope>((resolve) => {
            resolveReconcile = resolve
          }),
      )
    const update = vi
      .fn<(payload: BudgetUpdatePayload, idempotencyKey: string) => Promise<BudgetUpdateEnvelope>>()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(updateEnvelope({ thresholds: { ...THRESHOLDS, daily_usd: '30.00' } }))
    await renderLoaded({ fetch, update })

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('结果未知'))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))

    // The retry succeeds and installs the authoritative daily 30 snapshot.
    change(/Daily USD/, '30.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('已保存'),
    )
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('30.00')

    // The reconcile read was started before the retry committed; its older
    // snapshot must not overwrite the newer authoritative one.
    await act(async () => {
      resolveReconcile?.(
        budgetEnvelope(budgetData({ thresholds: { ...THRESHOLDS, daily_usd: '20.00' } })),
      )
    })
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).toHaveValue('30.00')
    expect(screen.getByTestId('budget-meter-day')).toHaveAttribute('aria-valuemax', '30')
  })

  it('marks the controls busy with aria-disabled during a save without dropping focus', async () => {
    let resolveUpdate: ((envelope: BudgetUpdateEnvelope) => void) | undefined
    const fetch = vi.fn(async () => budgetEnvelope(budgetData()))
    const update = vi.fn(
      () =>
        new Promise<BudgetUpdateEnvelope>((resolve) => {
          resolveUpdate = resolve
        }),
    )
    await renderLoaded({ fetch, update })

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '保存中…' })).toHaveAttribute(
        'aria-disabled',
        'true',
      ),
    )

    // The busy save button and refresh stay focusable (native ``disabled`` on
    // a focused control would drop focus to <body>), and the refresh click is
    // ignored by the hook's in-flight guard.
    expect(screen.getByRole('button', { name: '保存中…' })).not.toBeDisabled()
    const refresh = screen.getByRole('button', { name: '刷新用量' })
    expect(refresh).toHaveAttribute('aria-disabled', 'true')
    expect(refresh).not.toBeDisabled()
    fireEvent.click(refresh)
    expect(fetch).toHaveBeenCalledTimes(1)

    // Threshold inputs stay enabled but busy for the duration of the save.
    const daily = screen.getByRole('textbox', { name: 'Daily USD' })
    expect(daily).toBeEnabled()
    expect(daily).toHaveAttribute('aria-disabled', 'true')

    resolveUpdate?.(updateEnvelope({ thresholds: { ...THRESHOLDS, daily_usd: '20.00' } }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '刷新用量' })).not.toHaveAttribute('aria-disabled'),
    )
    expect(screen.getByRole('textbox', { name: 'Daily USD' })).not.toHaveAttribute('aria-disabled')
  })

  it('renders the server snapshot state after a save instead of recomputing it', async () => {
    const data = budgetData()
    const periods = {
      ...(data.periods as NonNullable<BudgetData['periods']>),
      day: { ...data.periods!.day, budget_state: 'WARN' as const },
    }
    const update = vi.fn(async () =>
      updateEnvelope({
        thresholds: { ...THRESHOLDS, daily_usd: '20.00' },
        periods,
        budget_state: 'WARN',
      }),
    )
    await renderLoaded({ update })

    change(/Daily USD/, '20.00')
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }))
    await waitFor(() =>
      expect(screen.getByTestId('settings-budget-save-visible')).toHaveTextContent('已保存'),
    )

    // 2.18 of 20.00 is well below warn 80%, so local float math would say OK;
    // the server snapshot is the authority and says WARN.
    expect(screen.getByTestId('budget-meter-day').getAttribute('aria-valuetext')).toContain(
      '状态 WARN',
    )
  })
})

describe('validateBudgetDraft ordering parity', () => {
  const crossedCurrent: BudgetThresholds = {
    daily_usd: '50.00',
    weekly_usd: '10.00',
    monthly_usd: '150.00',
    warn_pct: 80,
  }
  const crossedDraft = {
    daily_usd: '50.00',
    weekly_usd: '10.00',
    monthly_usd: '150.00',
    warn_pct: '75',
  }

  it('rejects a crossing the draft introduces over an uncrossed current set', () => {
    const outcome = validateBudgetDraft(
      { ...crossedDraft, daily_usd: '10.00', weekly_usd: '5.00' },
      THRESHOLDS,
    )
    expect(outcome.errors.weekly_usd).toBe('Weekly 必须 ≥ Daily')
    expect(outcome.valid).toBe(false)
  })

  it('accepts an already-crossed current pair while the pure call stays strict', () => {
    // With no ``current`` the prior set is assumed uncrossed, so the pure
    // validator still reports the crossing.
    expect(validateBudgetDraft(crossedDraft).errors.weekly_usd).toBe('Weekly 必须 ≥ Daily')
    // Passing an uncrossed current set reports the newly introduced crossing.
    expect(validateBudgetDraft(crossedDraft, THRESHOLDS).errors.weekly_usd).toBe(
      'Weekly 必须 ≥ Daily',
    )
    // The same crossing already exists on the server, so it is not new.
    expect(validateBudgetDraft(crossedDraft, crossedCurrent).errors).toEqual({})
  })
})

describe('fetchBudget envelope validation', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function stubResponse(data: unknown): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ ...budgetEnvelope(data as BudgetData) }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    )
  }

  it('rejects a malformed money string instead of rendering a zero', async () => {
    const data = budgetData()
    const malformed = {
      ...data,
      periods: {
        ...(data.periods as NonNullable<BudgetData['periods']>),
        day: { ...data.periods!.day, spend_usd: '' },
      },
    }
    stubResponse(malformed)

    await expect(fetchBudget()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('rejects an out-of-range elapsed fraction', async () => {
    stubResponse(
      budgetData({
        projection_basis: {
          month_spend_usd: '21.60000000',
          elapsed_fraction: 0,
          as_of: '2026-11-09T10:00:00.000Z',
        },
      }),
    )

    await expect(fetchBudget()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('accepts a degraded snapshot with nullable usage', async () => {
    stubResponse(
      budgetData({
        usage_quality: 'UNAVAILABLE',
        budget_state: null,
        periods: null,
        projected_month_end_usd: null,
        projection_basis: null,
        last_alert: null,
      }),
    )

    await expect(fetchBudget()).resolves.toMatchObject({
      data: { usage_quality: 'UNAVAILABLE', periods: null },
    })
  })
})

describe('budget route', () => {
  it('matches /settings/budget only as the budget section', () => {
    expect(resolveRoute('/settings/budget').isSettingsBudget).toBe(true)
    expect(resolveRoute('/settings/budget').isSettings).toBe(false)
    expect(resolveRoute('/settings/budget').isSettingsRisk).toBe(false)
    expect(resolveRoute('/settings/budget/extra').isSettingsBudget).toBe(false)
  })
})
