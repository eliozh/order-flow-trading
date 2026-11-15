import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import {
  type RiskCaps,
  type RiskSettingsData,
  type RiskSettingsEnvelope,
  SettingsApiError,
} from '../../api/settings'
import { SettingsRiskPage } from '../settings-risk'
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

const BASE_CAPS: RiskCaps = {
  per_trade_risk_pct: 0.8,
  max_concurrent_positions: 3,
  daily_loss_pct: 3.0,
  max_leverage: 5.0,
  max_notional_pct: 20.0,
  max_slippage_bps: 25.0,
  max_spread_bps: 15.0,
  stale_mark_seconds: 60,
}

const UNITS: Record<keyof RiskCaps, string> = {
  per_trade_risk_pct: '%',
  max_concurrent_positions: 'positions',
  daily_loss_pct: '%',
  max_leverage: 'x',
  max_notional_pct: '%',
  max_slippage_bps: 'bps',
  max_spread_bps: 'bps',
  stale_mark_seconds: 's',
}

function utilization() {
  const metrics = {} as RiskSettingsData['utilization']['metrics']
  for (const field of Object.keys(BASE_CAPS) as (keyof RiskCaps)[]) {
    const unmeasured =
      field === 'max_slippage_bps' || field === 'max_spread_bps' || field === 'stale_mark_seconds'
    metrics[field] = {
      used: unmeasured ? null : 0,
      cap: BASE_CAPS[field],
      unit: UNITS[field],
      source: unmeasured ? 'none' : 'paper_account',
      quality: unmeasured ? 'UNAVAILABLE' : 'COMPLETE',
      note: unmeasured ? 'no live measurement exists for this cap' : null,
    }
  }
  return {
    source: 'paper_account' as const,
    quality: 'COMPLETE' as const,
    stale_inputs: [],
    metrics,
  }
}

function riskData(overrides: Partial<RiskSettingsData> = {}): RiskSettingsData {
  return {
    caps: BASE_CAPS,
    source: 'default',
    defaults: BASE_CAPS,
    utilization: utilization(),
    updated_at: null,
    updated_by: null,
    ...overrides,
  }
}

function riskEnvelope(data: RiskSettingsData): RiskSettingsEnvelope {
  return { ok: true, data, error: null, meta }
}

function confirmationRefusal(): SettingsApiError {
  return new SettingsApiError(
    'loosening per_trade_risk_pct requires confirmation',
    'VALIDATION_FAILED',
    {
      httpStatus: 422,
      details: [
        {
          field: 'per_trade_risk_pct',
          code: 'confirmation_required',
          message: 'loosening per_trade_risk_pct requires confirmation',
        },
      ],
    },
  )
}

interface RenderOptions {
  fetch?: (signal?: AbortSignal) => Promise<RiskSettingsEnvelope>
  update?: (caps: Partial<RiskCaps>, confirm: boolean, key: string) => Promise<RiskSettingsEnvelope>
  isCompact?: boolean
  clear?: () => void
  rotate?: () => string
}

async function renderLoaded(options: RenderOptions = {}) {
  const fetch = options.fetch ?? (async () => riskEnvelope(riskData()))
  const update = options.update ?? (async () => riskEnvelope(riskData()))
  const clear = options.clear ?? vi.fn()
  const rotate = options.rotate ?? vi.fn(() => 'rotated-risk-key')
  render(
    <SettingsRiskPage
      fetchRiskSettingsFn={fetch}
      updateRiskSettingsFn={update}
      idempotencyKeyFn={() => 'risk-key'}
      clearIdempotencyKeyFn={clear}
      rotateIdempotencyKeyFn={rotate}
      isCompact={options.isCompact}
    />,
  )
  await screen.findByTestId('risk-source')
  return { fetch, update, clear, rotate }
}

function changeCap(label: RegExp, value: string): void {
  fireEvent.change(screen.getByRole('spinbutton', { name: label }), {
    target: { value },
  })
}

describe('SettingsRiskPage', () => {
  it('shows the effective caps, source, defaults, and read-only utilization', async () => {
    await renderLoaded()

    expect(screen.getByTestId('risk-source')).toHaveTextContent('source: default')
    expect(screen.getByTestId('risk-defaults')).toHaveTextContent('单笔风险 % 0.8')
    expect(screen.getByTestId('risk-defaults')).toHaveTextContent('Stale 标记秒数 60')
    expect(screen.getByTestId('risk-utilization-source')).toHaveTextContent('source: paper_account')
    expect(screen.getByTestId('risk-utilization-quality')).toHaveTextContent('quality: COMPLETE')

    const measured = screen.getByTestId('risk-meter-per_trade_risk_pct')
    expect(measured).toHaveAttribute('role', 'meter')
    expect(measured).toHaveAttribute('aria-valuemin', '0')
    expect(measured).toHaveAttribute('aria-valuemax', '0.8')
    expect(measured).toHaveAttribute('aria-valuenow', '0')
    expect(measured.getAttribute('aria-valuetext')).toContain('来源 paper_account')
    expect(measured.getAttribute('aria-valuetext')).toContain('质量 COMPLETE')

    const unmeasured = screen.getByTestId('risk-meter-max_spread_bps')
    expect(unmeasured).toHaveAttribute('role', 'group')
    expect(unmeasured).not.toHaveAttribute('aria-valuenow')
    expect(unmeasured).toHaveAttribute('aria-label', expect.stringContaining('不可用'))
    expect(unmeasured).toHaveTextContent('—')
    expect(unmeasured).toHaveTextContent('质量 UNAVAILABLE')
    expect(screen.getAllByText(/no live measurement exists for this cap/).length).toBeGreaterThan(0)
    // Every cap has a labelled meter or read-only unavailable field.
    for (const field of Object.keys(BASE_CAPS)) {
      expect(screen.getByTestId(`risk-meter-${field}`)).toBeInTheDocument()
    }
  })

  it('clamps the meter state when current usage exceeds a tightened cap', async () => {
    const data = riskData()
    data.utilization.metrics.per_trade_risk_pct = {
      ...data.utilization.metrics.per_trade_risk_pct,
      used: 3,
      cap: 0.8,
    }
    await renderLoaded({ fetch: async () => riskEnvelope(data) })

    const meter = screen.getByTestId('risk-meter-per_trade_risk_pct')
    expect(meter).toHaveAttribute('role', 'meter')
    expect(meter).toHaveAttribute('aria-valuemax', '0.8')
    expect(meter).toHaveAttribute('aria-valuenow', '0.8')
    expect(meter.getAttribute('aria-valuetext')).toContain('已超过上限')
  })

  it('shows a persisted source and the last change', async () => {
    await renderLoaded({
      fetch: async () =>
        riskEnvelope(
          riskData({
            source: 'user',
            updated_at: '2026-11-06T10:00:00.000Z',
            updated_by: 'web',
          }),
        ),
    })

    expect(screen.getByTestId('risk-source')).toHaveTextContent('source: user')
    expect(screen.getByText(/上次修改 2026-11-06T10:00:00.000Z · web/)).toBeInTheDocument()
  })

  it('saves a tightened cap directly and announces before -> after and the audit', async () => {
    const update = vi.fn(async (caps: Partial<RiskCaps>) =>
      riskEnvelope(
        riskData({
          caps: { ...BASE_CAPS, ...caps },
          source: 'user',
          updated_at: '2026-11-06T10:00:00.000Z',
          updated_by: 'web',
        }),
      ),
    )
    const clear = vi.fn()
    await renderLoaded({ update, clear })

    changeCap(/单笔风险 %/, '0.5')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('已保存'),
    )
    expect(update).toHaveBeenCalledTimes(1)
    expect(update).toHaveBeenCalledWith({ per_trade_risk_pct: 0.5 }, false, 'risk-key')
    const visible = screen.getByTestId('settings-risk-save-visible')
    expect(visible).toHaveTextContent('0.8')
    expect(visible).toHaveTextContent('0.5')
    expect(visible).toHaveTextContent('audit 已写入')
    expect(clear).toHaveBeenCalled()
    expect(screen.getByTestId('risk-source')).toHaveTextContent('source: user')
    // The form now reflects the committed cap.
    expect(screen.getByRole('spinbutton', { name: /单笔风险 %/ })).toHaveValue(0.5)
  })

  it('opens the diff dialog for a loosening refusal and cancel writes nothing', async () => {
    const update = vi.fn(async () => {
      throw confirmationRefusal()
    })
    const rotate = vi.fn(() => 'rotated-risk-key')
    const clear = vi.fn()
    await renderLoaded({ update, rotate, clear })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })
    const table = within(dialog).getByRole('table', { name: '上限变更前后对比' })
    expect(
      within(table)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(['0.8', '1'])
    // Least destructive control receives the initial focus.
    await waitFor(() => expect(screen.getByRole('button', { name: '取消' })).toHaveFocus())
    // A definitive confirmation refusal retires the pending idempotency key.
    expect(clear).toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(update).toHaveBeenCalledTimes(1)
    expect(update).toHaveBeenCalledWith({ per_trade_risk_pct: 1.0 }, false, 'risk-key')
    expect(rotate).not.toHaveBeenCalled()
    expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('未写入任何变更')
  })

  it('closes the dialog on Escape without calling the API with confirm', async () => {
    const update = vi.fn(async () => {
      throw confirmationRefusal()
    })
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    await screen.findByRole('dialog', { name: '确认放宽风险上限' })

    fireEvent.keyDown(document, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(update).toHaveBeenCalledTimes(1)
  })

  it('confirms a loosening with confirm=true and a rotated key', async () => {
    const update = vi
      .fn<
        (caps: Partial<RiskCaps>, confirm: boolean, key: string) => Promise<RiskSettingsEnvelope>
      >()
      .mockRejectedValueOnce(confirmationRefusal())
      .mockResolvedValueOnce(
        riskEnvelope(
          riskData({
            caps: { ...BASE_CAPS, per_trade_risk_pct: 1.0 },
            source: 'user',
            updated_at: '2026-11-06T10:00:00.000Z',
            updated_by: 'web',
          }),
        ),
      )
    const rotate = vi.fn(() => 'rotated-risk-key')
    await renderLoaded({ update, rotate })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    await screen.findByRole('dialog', { name: '确认放宽风险上限' })

    fireEvent.click(screen.getByRole('button', { name: '确认放宽' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('audit 已写入'),
    )
    expect(update).toHaveBeenCalledTimes(2)
    expect(update).toHaveBeenNthCalledWith(1, { per_trade_risk_pct: 1.0 }, false, 'risk-key')
    expect(update).toHaveBeenNthCalledWith(2, { per_trade_risk_pct: 1.0 }, true, 'rotated-risk-key')
    expect(rotate).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('blocks an invalid daily-loss/per-trade combination without calling the API', async () => {
    const update = vi.fn(async () => riskEnvelope(riskData()))
    await renderLoaded({ update })

    changeCap(/日亏损上限 %/, '0.5')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法保存')
    // The summary receives focus even though the client path never toggles
    // ``saving``, and it links to the invalid field.
    await waitFor(() => expect(alert).toHaveFocus())
    expect(within(alert).getByRole('link', { name: '转到日亏损上限 %' })).toHaveAttribute(
      'href',
      '#risk-daily_loss_pct',
    )
    expect(update).not.toHaveBeenCalled()
    expect(screen.getByRole('spinbutton', { name: /日亏损上限 %/ })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(screen.getByText('日亏损上限必须 ≥ 单笔风险')).toBeInTheDocument()
    // One announcement only: the focused alert speaks, the polite region is
    // cleared rather than repeating the same string.
    expect(screen.getByTestId('settings-risk-save-status').textContent).toBe('')
  })

  it('blocks an out-of-range leverage inline without native validation', async () => {
    const update = vi.fn(async () => riskEnvelope(riskData()))
    await renderLoaded({ update })

    changeCap(/杠杆上限 x/, '200')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法保存')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(screen.getByRole('spinbutton', { name: /杠杆上限 x/ })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(screen.getByText('必须 ≤ 125')).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
  })

  it('accepts and saves a server-valid off-step value such as 0.55', async () => {
    const update = vi.fn(async (caps: Partial<RiskCaps>) =>
      riskEnvelope(riskData({ caps: { ...BASE_CAPS, ...caps }, source: 'user' })),
    )
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '0.55')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ per_trade_risk_pct: 0.55 }, false, 'risk-key'),
    )
    await waitFor(() =>
      expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('已保存'),
    )
  })

  it('ignores Escape while a confirmed save is in flight', async () => {
    let resolveConfirm: ((envelope: RiskSettingsEnvelope) => void) | undefined
    const update = vi
      .fn<
        (caps: Partial<RiskCaps>, confirm: boolean, key: string) => Promise<RiskSettingsEnvelope>
      >()
      .mockRejectedValueOnce(confirmationRefusal())
      .mockImplementationOnce(
        () =>
          new Promise<RiskSettingsEnvelope>((resolve) => {
            resolveConfirm = resolve
          }),
      )
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })

    fireEvent.click(within(dialog).getByRole('button', { name: '确认放宽' }))
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: '保存中…' })).toBeInTheDocument(),
    )

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: '确认放宽风险上限' })).toBeInTheDocument()

    resolveConfirm?.(riskEnvelope(riskData({ caps: { ...BASE_CAPS, per_trade_risk_pct: 1 } })))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps focus inside the dialog while the confirmed save is pending', async () => {
    let resolveConfirm: ((envelope: RiskSettingsEnvelope) => void) | undefined
    const update = vi
      .fn<
        (caps: Partial<RiskCaps>, confirm: boolean, key: string) => Promise<RiskSettingsEnvelope>
      >()
      .mockRejectedValueOnce(confirmationRefusal())
      .mockImplementationOnce(
        () =>
          new Promise<RiskSettingsEnvelope>((resolve) => {
            resolveConfirm = resolve
          }),
      )
    await renderLoaded({ update })

    // The invoker of the modal is the cap input the operator was editing.
    const capInput = screen.getByRole('spinbutton', { name: /单笔风险 %/ })
    capInput.focus()
    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })
    await waitFor(() => expect(screen.getByRole('button', { name: '取消' })).toHaveFocus())

    const confirm = within(dialog).getByRole('button', { name: '确认放宽' })
    confirm.focus()
    fireEvent.click(confirm)
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: '保存中…' })).toBeInTheDocument(),
    )

    // ``busy`` must not re-run the focus effect: focus stays inside the open
    // modal and is never restored to the invoker mid-save.
    const panel = document.querySelector('.dialog-panel')
    expect(panel).not.toBeNull()
    expect(panel?.contains(document.activeElement)).toBe(true)
    expect(capInput).not.toHaveFocus()

    resolveConfirm?.(
      riskEnvelope(riskData({ caps: { ...BASE_CAPS, per_trade_risk_pct: 1 }, source: 'user' })),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // The successful save still moves focus back to the page's save control.
    await waitFor(() => expect(screen.getByRole('button', { name: '保存风控' })).toHaveFocus())
  })

  it('submits on the form Enter path and disables the save control while in flight', async () => {
    let resolveUpdate: ((envelope: RiskSettingsEnvelope) => void) | undefined
    const update = vi.fn(
      () =>
        new Promise<RiskSettingsEnvelope>((resolve) => {
          resolveUpdate = resolve
        }),
    )
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '0.5')
    fireEvent.submit(screen.getByRole('form', { name: '修改风险上限' }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: '保存中…' })).toBeDisabled()
    expect(screen.getByRole('form', { name: '修改风险上限' })).toHaveAttribute('aria-busy', 'true')

    resolveUpdate?.(
      riskEnvelope(
        riskData({
          caps: { ...BASE_CAPS, per_trade_risk_pct: 0.5 },
          source: 'user',
        }),
      ),
    )
    await waitFor(() =>
      expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('已保存'),
    )
    expect(screen.getByRole('button', { name: '保存风控' })).toBeEnabled()
  })

  it('maps server field details to inline errors', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('invalid risk caps', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [
          {
            field: 'max_leverage',
            code: 'invalid_value',
            message: 'max_leverage must be greater than 0 and at most 125',
          },
        ],
      })
    })
    await renderLoaded({ update })

    changeCap(/杠杆上限 x/, '50')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    await waitFor(() =>
      expect(screen.getByText(/max_leverage must be greater than 0/)).toBeInTheDocument(),
    )
    expect(screen.getByRole('spinbutton', { name: /杠杆上限 x/ })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
  })

  it('is read-only at the compact override', async () => {
    await renderLoaded({ isCompact: true })

    expect(screen.getByRole('note')).toHaveTextContent(/桌面端或 CLI/)
    expect(screen.getByRole('button', { name: '保存风控' })).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: /单笔风险 %/ })).toBeDisabled()
    expect(screen.getByRole('spinbutton', { name: /Stale 标记秒数/ })).toBeDisabled()
  })

  it('shows a load error and recovers on retry', async () => {
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<RiskSettingsEnvelope>>()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(riskEnvelope(riskData()))
    render(<SettingsRiskPage fetchRiskSettingsFn={fetch} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('风险上限不可用')
    expect(alert).toHaveTextContent('network down')

    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByTestId('risk-source')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('keeps a busy confirmation focusable instead of native-disabling its controls', async () => {
    let resolveConfirm: ((envelope: RiskSettingsEnvelope) => void) | undefined
    const update = vi
      .fn<
        (caps: Partial<RiskCaps>, confirm: boolean, key: string) => Promise<RiskSettingsEnvelope>
      >()
      .mockRejectedValueOnce(confirmationRefusal())
      .mockImplementationOnce(
        () =>
          new Promise<RiskSettingsEnvelope>((resolve) => {
            resolveConfirm = resolve
          }),
      )
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })

    const confirm = within(dialog).getByRole('button', { name: '确认放宽' })
    confirm.focus()
    fireEvent.click(confirm)

    const busyConfirm = await within(dialog).findByRole('button', { name: '保存中…' })
    const cancel = within(dialog).getByRole('button', { name: '取消' })

    // Native disabling blurs the active control to <body> in real browsers, so
    // the busy state is announced with ``aria-disabled`` only.
    expect(busyConfirm).not.toBeDisabled()
    expect(busyConfirm).not.toHaveAttribute('disabled')
    expect(cancel).not.toBeDisabled()
    expect(cancel).not.toHaveAttribute('disabled')
    expect(busyConfirm).toHaveAttribute('aria-disabled', 'true')
    expect(cancel).toHaveAttribute('aria-disabled', 'true')
    expect(document.activeElement).not.toBe(document.body)
    expect(document.activeElement?.closest('[role="dialog"]')).not.toBeNull()

    resolveConfirm?.(
      riskEnvelope(riskData({ caps: { ...BASE_CAPS, per_trade_risk_pct: 1 }, source: 'user' })),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('marks the diff table as a labelled keyboard-scrollable region', async () => {
    const update = vi.fn(async () => {
      throw confirmationRefusal()
    })
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    await screen.findByRole('dialog', { name: '确认放宽风险上限' })

    const region = screen.getByRole('region', { name: '上限变更前后对比' })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(within(region).getByRole('table', { name: '上限变更前后对比' })).toBeInTheDocument()
  })

  it('keeps the save announcer outside the inert app shell while the dialog is open', async () => {
    const update = vi.fn(async () => {
      throw confirmationRefusal()
    })
    render(
      <div className="app-shell">
        <SettingsRiskPage
          fetchRiskSettingsFn={async () => riskEnvelope(riskData())}
          updateRiskSettingsFn={update}
          idempotencyKeyFn={() => 'risk-key'}
          clearIdempotencyKeyFn={vi.fn()}
          rotateIdempotencyKeyFn={() => 'rotated-risk-key'}
        />
      </div>,
    )
    await screen.findByTestId('risk-source')

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    await screen.findByRole('dialog', { name: '确认放宽风险上限' })

    const shell = document.querySelector('.app-shell')
    expect(shell).not.toBeNull()
    expect(Array.from(shell?.children ?? []).every((child) => child.hasAttribute('inert'))).toBe(
      true,
    )
    const announcer = screen.getByTestId('settings-risk-save-status')
    expect(announcer.closest('[inert]')).toBeNull()
    expect(shell?.contains(announcer)).toBe(false)
  })

  it('builds the confirmation diff from the 422 details and lists only loosened caps', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError(
        'loosening per_trade_risk_pct requires confirmation',
        'VALIDATION_FAILED',
        {
          httpStatus: 422,
          details: [
            {
              field: 'per_trade_risk_pct',
              code: 'confirmation_required',
              message: 'loosening per_trade_risk_pct requires confirmation',
              before: 0.75,
              after: 1.2,
            },
          ],
        },
      )
    })
    await renderLoaded({ update })

    // The patch changes a tightening too, but only the server's flagged
    // loosening may appear in the safety dialog.
    changeCap(/单笔风险 %/, '1.2')
    changeCap(/杠杆上限 x/, '4')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })
    expect(update).toHaveBeenCalledWith(
      { per_trade_risk_pct: 1.2, max_leverage: 4 },
      false,
      'risk-key',
    )
    const table = within(dialog).getByRole('table', { name: '上限变更前后对比' })
    expect(
      within(table)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(['0.75', '1.2'])
    expect(within(dialog).queryByText('杠杆上限 x')).toBeNull()
  })

  it('falls back to the mounted caps and submitted patch when details lack before/after', async () => {
    const update = vi.fn(async () => {
      throw confirmationRefusal()
    })
    await renderLoaded({ update })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })
    const table = within(dialog).getByRole('table', { name: '上限变更前后对比' })
    expect(
      within(table)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(['0.8', '1'])
  })

  it('reports an unknown outcome and the retry note for a transport failure', async () => {
    const update = vi.fn(async () => {
      throw new Error('network down')
    })
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-risk-key')
    await renderLoaded({ update, clear, rotate })

    changeCap(/单笔风险 %/, '0.5')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).toHaveTextContent('同一 Idempotency-Key')
    expect(
      screen.getByText('上一次保存结果未知；重试会使用同一 Idempotency-Key 重放，不会重复写入。'),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('settings-risk-save-visible')).toBeNull()
    // A possibly-committed plain save must keep its key so the retry replays it.
    expect(clear).not.toHaveBeenCalled()
    expect(rotate).not.toHaveBeenCalled()
  })

  it('rotates the idempotency key on a 409 conflict and asks for a retry', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('idempotency conflict', 'IDEMPOTENCY_CONFLICT', {
        httpStatus: 409,
      })
    })
    const rotate = vi.fn(() => 'rotated-risk-key')
    const clear = vi.fn()
    await renderLoaded({ update, rotate, clear })

    changeCap(/单笔风险 %/, '0.5')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('请求冲突')
    expect(alert).toHaveTextContent('已生成新的 Idempotency-Key')
    expect(rotate).toHaveBeenCalledTimes(1)
    expect(clear).not.toHaveBeenCalled()
  })

  it('retires the key but leaks no state or focus after a deferred save settles post-unmount', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      let resolveUpdate: ((envelope: RiskSettingsEnvelope) => void) | undefined
      const update = vi.fn(
        () =>
          new Promise<RiskSettingsEnvelope>((resolve) => {
            resolveUpdate = resolve
          }),
      )
      const clear = vi.fn()
      const { unmount } = render(
        <SettingsRiskPage
          fetchRiskSettingsFn={async () => riskEnvelope(riskData())}
          updateRiskSettingsFn={update}
          idempotencyKeyFn={() => 'risk-key'}
          clearIdempotencyKeyFn={clear}
          rotateIdempotencyKeyFn={() => 'rotated-risk-key'}
        />,
      )
      await screen.findByTestId('risk-source')

      changeCap(/单笔风险 %/, '0.5')
      fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
      await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
      expect(clear).not.toHaveBeenCalled()

      unmount()
      resolveUpdate?.(
        riskEnvelope(riskData({ caps: { ...BASE_CAPS, per_trade_risk_pct: 0.5 }, source: 'user' })),
      )
      await new Promise((resolve) => setTimeout(resolve, 0))

      // The success path retires the key before its mounted guard, but nothing
      // observable may leak into an unmounted page: no status, no alert, and
      // no post-settle focus move.
      expect(clear).toHaveBeenCalledTimes(1)
      expect(consoleError).not.toHaveBeenCalled()
      expect(document.getElementById('settings-risk-heading')).toBeNull()
      expect(screen.queryByTestId('settings-risk-save-status')).toBeNull()
      expect(screen.queryByTestId('settings-risk-save-visible')).toBeNull()
      expect(screen.queryByRole('alert')).toBeNull()
      expect(document.activeElement).toBe(document.body)
    } finally {
      consoleError.mockRestore()
    }
  })

  it('rotates the key and gives accurate guidance when the confirmed save fails', async () => {
    const update = vi
      .fn<
        (caps: Partial<RiskCaps>, confirm: boolean, key: string) => Promise<RiskSettingsEnvelope>
      >()
      .mockRejectedValueOnce(confirmationRefusal())
      .mockRejectedValueOnce(new Error('socket closed'))
    const rotate = vi.fn(() => 'rotated-risk-key')
    const clear = vi.fn()
    await renderLoaded({ update, rotate, clear })

    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    const dialog = await screen.findByRole('dialog', { name: '确认放宽风险上限' })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认放宽' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).toHaveTextContent('已生成新的 Idempotency-Key')
    // The form's plain-save retry would resend this key with ``confirm: false``
    // (guaranteed 409), so the misleading same-key replay copy must not render.
    expect(alert).not.toHaveTextContent('同一 Idempotency-Key')
    expect(
      screen.getByText(
        '上一次确认保存结果未知；已生成新的 Idempotency-Key。请重新保存并在确认对话框中再次确认。',
      ),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('上一次保存结果未知；重试会使用同一 Idempotency-Key 重放，不会重复写入。'),
    ).toBeNull()
    expect(screen.queryByTestId('settings-risk-save-visible')).toBeNull()
    // One rotation supplied the confirmed request key; the ambiguous outcome
    // rotates again so the next save starts on a fresh key.
    expect(rotate).toHaveBeenCalledTimes(2)
    expect(update).toHaveBeenNthCalledWith(
      2,
      { per_trade_risk_pct: 1.0 },
      true,
      'rotated-risk-key',
    )
  })

  it('does not call the API when the form is saved unchanged', async () => {
    const update = vi.fn(async () => riskEnvelope(riskData()))
    await renderLoaded({ update })

    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))

    await waitFor(() =>
      expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('未变更'),
    )
    expect(screen.getByTestId('settings-risk-save-visible')).toHaveTextContent('没有保存任何值')
    expect(update).not.toHaveBeenCalled()
  })

  it('restores focus to the element that opened the dialog when it is cancelled', async () => {
    const update = vi.fn(async () => {
      throw confirmationRefusal()
    })
    await renderLoaded({ update })

    const capInput = screen.getByRole('spinbutton', { name: /单笔风险 %/ })
    capInput.focus()
    changeCap(/单笔风险 %/, '1.0')
    fireEvent.click(screen.getByRole('button', { name: '保存风控' }))
    await screen.findByRole('dialog', { name: '确认放宽风险上限' })
    await waitFor(() => expect(screen.getByRole('button', { name: '取消' })).toHaveFocus())

    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(capInput).toHaveFocus()
  })
})

describe('risk route', () => {
  it('matches /settings/risk only as the risk section', () => {
    expect(resolveRoute('/settings/risk').isSettingsRisk).toBe(true)
    expect(resolveRoute('/settings/risk').isSettings).toBe(false)
    expect(resolveRoute('/settings/risk').isSettingsScan).toBe(false)
    expect(resolveRoute('/settings/risk/extra').isSettingsRisk).toBe(false)
  })
})
