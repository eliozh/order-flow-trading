import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import {
  type ScanSettingsData,
  type ScanSettingsEnvelope,
  SettingsApiError,
  type SettingsSectionsEnvelope,
} from '../../api/settings'
import { SETTINGS_READONLY_QUERY } from '../../components/settings-field'
import { SettingsPage } from '../settings'
import { SettingsScanPage } from '../settings-scan'
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

function scanData(overrides: Partial<ScanSettingsData> = {}): ScanSettingsData {
  return {
    key: 'scan.period',
    value: '4h',
    source: 'default',
    supported: ['4h', '8h'],
    next_run: '2026-11-06T12:00:00.000Z',
    timezone: 'UTC',
    updated_at: null,
    updated_by: null,
    ...overrides,
  }
}

function scanEnvelope(data: ScanSettingsData): ScanSettingsEnvelope {
  return { ok: true, data, error: null, meta }
}

function sectionsEnvelope(): SettingsSectionsEnvelope {
  return {
    ok: true,
    data: {
      sections: [
        {
          section: 'scan',
          source: 'default',
          last_changed: null,
          keys: ['scan.period'],
        },
      ],
    },
    error: null,
    meta,
  }
}

interface RenderLoadedOptions {
  update?: (period: string, key: string) => Promise<ScanSettingsEnvelope>
  fetch?: (signal?: AbortSignal) => Promise<ScanSettingsEnvelope>
  isCompact?: boolean
  clear?: () => void
  rotate?: () => string
}

async function renderLoaded(options: RenderLoadedOptions = {}) {
  const update =
    options.update ??
    (async (period: string) =>
      scanEnvelope(
        scanData({
          value: period,
          source: 'user',
          updated_at: '2026-11-06T10:00:00.000Z',
          updated_by: 'web',
        }),
      ))
  const fetch = options.fetch ?? (async () => scanEnvelope(scanData()))
  const clear = options.clear ?? vi.fn()
  const rotate = options.rotate ?? vi.fn(() => 'rotated-key')
  const { unmount } = render(
    <SettingsScanPage
      fetchScanSettingsFn={fetch}
      updateScanPeriodFn={update}
      idempotencyKeyFn={() => 'test-key'}
      clearIdempotencyKeyFn={clear}
      rotateIdempotencyKeyFn={rotate}
      isCompact={options.isCompact}
    />,
  )
  await screen.findByTestId('scan-current-value')
  return { update, fetch, clear, rotate, unmount }
}

describe('SettingsScanPage', () => {
  it('shows the effective value, source, supported values, and next run', async () => {
    await renderLoaded()

    expect(screen.getByTestId('scan-current-value')).toHaveTextContent('4h')
    expect(screen.getByTestId('scan-source')).toHaveTextContent('source: default')
    expect(screen.getByText(/可用值 4h, 8h/)).toBeInTheDocument()
    expect(screen.getByTestId('scan-next-run')).toHaveTextContent('2026-11-06T12:00:00.000Z')
    expect(screen.getByTestId('scan-lookback')).toHaveTextContent('7 天')
    expect(screen.getByTestId('scan-timezone')).toHaveTextContent('UTC')
    expect(screen.getAllByText('read-only', { selector: '.chip' })).toHaveLength(3)
    // Each read-only field is a named group, so its label associates with it.
    expect(screen.getByRole('group', { name: /分析回看/ })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: /时区显示/ })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: /下一个 UTC 边界/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: '4h' })).toBeChecked()
    expect(screen.getByRole('radio', { name: '8h' })).not.toBeChecked()
    // The permanent live region exists before any text is written.
    expect(document.getElementById('settings-save-status')).toBeInTheDocument()
  })

  it('saves the selected period, announces old -> new and the next boundary', async () => {
    const update = vi.fn(async (period: string) =>
      scanEnvelope(
        scanData({
          value: period,
          source: 'user',
          updated_at: '2026-11-06T10:00:00.000Z',
          updated_by: 'web',
        }),
      ),
    )
    const clear = vi.fn()
    await renderLoaded({ update, clear })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    const live = document.getElementById('settings-save-status')
    await waitFor(() => expect(live).toHaveTextContent('已保存'))
    expect(live?.textContent).toContain('扫描周期 4h → 8h')
    expect(live?.textContent).toContain('2026-11-06T12:00:00.000Z')
    expect(live?.textContent).toContain('audit')
    expect(screen.getByTestId('settings-save-visible')).toHaveTextContent('已保存')
    expect(update).toHaveBeenCalledWith('8h', 'test-key')
    expect(screen.getByTestId('scan-current-value')).toHaveTextContent('8h')
    expect(screen.getByTestId('scan-source')).toHaveTextContent('source: user')
    expect(clear).toHaveBeenCalled()
    await waitFor(() => expect(screen.getByRole('button', { name: '保存排程' })).toHaveFocus())
  })

  it('keeps the save control focusable and busy while the request is in flight', async () => {
    let resolveUpdate: ((envelope: ScanSettingsEnvelope) => void) | undefined
    const update = vi.fn(
      () =>
        new Promise<ScanSettingsEnvelope>((resolve) => {
          resolveUpdate = resolve
        }),
    )
    await renderLoaded({ update })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    const save = screen.getByRole('button', { name: '保存排程' })
    fireEvent.click(save)

    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    expect(save).not.toBeDisabled()
    expect(screen.getByRole('form')).toHaveAttribute('aria-busy', 'true')
    expect(document.getElementById('settings-save-status')).toHaveTextContent('保存中…')
    expect(screen.getByRole('radio', { name: '8h' })).not.toBeDisabled()

    resolveUpdate?.(
      scanEnvelope(
        scanData({ value: '8h', source: 'user', updated_at: '2026-11-06T10:00:00.000Z' }),
      ),
    )
    await waitFor(() => expect(screen.getByTestId('settings-save-visible')).toBeInTheDocument())
    await waitFor(() => expect(save).toHaveFocus())
  })

  it('announces a refusal in a focused summary and keeps the old value', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('scan.period must be one of: 4h, 8h', 'VALIDATION_FAILED', {
        httpStatus: 422,
        details: [{ field: 'period', code: 'unsupported', message: 'invalid period' }],
      })
    })
    const clear = vi.fn()
    await renderLoaded({ update, clear })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    const summary = await screen.findByRole('alert')
    expect(summary).toHaveTextContent('错误：')
    expect(summary).toHaveTextContent('scan.period must be one of: 4h, 8h')
    expect(summary.querySelector('a')).toHaveAttribute('href', '#scan-period-group')
    await waitFor(() => expect(summary).toHaveFocus())

    const inline = document.getElementById('scan-period-error')
    expect(inline).toHaveTextContent('scan.period must be one of: 4h, 8h')
    const group = screen.getByRole('group', { name: '扫描周期' })
    expect(group).toHaveAttribute('aria-invalid', 'true')
    expect(group.getAttribute('aria-describedby')).toContain('scan-period-error')
    // The summary link targets a focusable target, and each radio mirrors the
    // fieldset's invalid state for assistive technology that does not cascade.
    expect(group).toHaveAttribute('id', 'scan-period-group')
    expect(group).toHaveAttribute('tabindex', '-1')
    for (const period of ['4h', '8h']) {
      const radio = screen.getByRole('radio', { name: period })
      expect(radio).toHaveAttribute('aria-invalid', 'true')
      expect(radio.getAttribute('aria-describedby')).toContain('scan-period-error')
    }
    expect(screen.getByTestId('scan-current-value')).toHaveTextContent('4h')
    expect(clear).toHaveBeenCalled()
  })

  it('reconciles an unknown outcome, keeps the key, and rotates on a new logical save', async () => {
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<ScanSettingsEnvelope>>()
      .mockResolvedValueOnce(scanEnvelope(scanData()))
      .mockResolvedValueOnce(
        scanEnvelope(
          scanData({ value: '8h', source: 'user', updated_at: '2026-11-06T10:00:00.000Z' }),
        ),
      )
    const update = vi.fn(async () => {
      throw new SettingsApiError('Request timed out.', 'TIMEOUT', { kind: 'unknown' })
    })
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-key')
    await renderLoaded({ update, fetch, clear, rotate })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    const summary = await screen.findByRole('alert')
    expect(summary).toHaveTextContent('结果未知')
    expect(summary).toHaveTextContent('Idempotency-Key 重放')
    expect(fetch).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(screen.getByTestId('scan-current-value')).toHaveTextContent('8h'))
    expect(clear).not.toHaveBeenCalled()
    expect(rotate).not.toHaveBeenCalled()

    // Starting a new logical save with a different payload rotates the key.
    fireEvent.click(screen.getByRole('radio', { name: '4h' }))
    expect(rotate).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps the attempted selection when reconcile returns the pre-save value and replays it with the same key', async () => {
    // The PUT never committed, so reconcile still reports the old effective
    // value. The form must keep the attempted selection and a retry must
    // replay the original payload with the same Idempotency-Key.
    const fetch = vi.fn(async () => scanEnvelope(scanData()))
    const update = vi
      .fn<(period: string, key: string) => Promise<ScanSettingsEnvelope>>()
      .mockRejectedValueOnce(
        new SettingsApiError('Request timed out.', 'TIMEOUT', { kind: 'unknown' }),
      )
      .mockResolvedValueOnce(
        scanEnvelope(
          scanData({
            value: '8h',
            source: 'user',
            updated_at: '2026-11-06T10:00:00.000Z',
            updated_by: 'web',
          }),
        ),
      )
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-key')
    await renderLoaded({ update, fetch, clear, rotate })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    const summary = await screen.findByRole('alert')
    expect(summary).toHaveTextContent('结果未知')
    expect(summary).toHaveTextContent('Idempotency-Key 重放')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('form')).toHaveAttribute('aria-busy', 'false'))

    expect(screen.getByTestId('scan-current-value')).toHaveTextContent('4h')
    expect(screen.getByRole('radio', { name: '8h' })).toBeChecked()
    expect(screen.getByRole('radio', { name: '4h' })).not.toBeChecked()
    expect(clear).not.toHaveBeenCalled()
    expect(rotate).not.toHaveBeenCalled()

    // Retrying Save replays the attempted period with the same key.
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(2))
    expect(update).toHaveBeenNthCalledWith(1, '8h', 'test-key')
    expect(update).toHaveBeenNthCalledWith(2, '8h', 'test-key')
    await waitFor(() =>
      expect(screen.getByTestId('settings-save-visible')).toHaveTextContent('已保存'),
    )
  })

  it('treats a 409 conflict as definitive, rotates the key, and never reconciles', async () => {
    const fetch = vi.fn(async () => scanEnvelope(scanData()))
    const update = vi.fn(async () => {
      throw new SettingsApiError('still running', 'IDEMPOTENCY_CONFLICT', {
        httpStatus: 409,
      })
    })
    const clear = vi.fn()
    const rotate = vi.fn(() => 'rotated-key')
    await renderLoaded({ update, fetch, clear, rotate })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    const summary = await screen.findByRole('alert')
    expect(summary).toHaveTextContent('新的 Idempotency-Key')
    expect(summary).toHaveTextContent('可直接重试')
    expect(summary).not.toHaveTextContent('结果未知')
    expect(rotate).toHaveBeenCalledTimes(1)
    expect(clear).not.toHaveBeenCalled()
    // Definitive for this payload: no unknown-outcome reconcile fetch.
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('states that an unchanged save wrote no audit and keeps the old value', async () => {
    const update = vi.fn(async () =>
      scanEnvelope(
        scanData({
          value: '4h',
          source: 'user',
          updated_at: '2026-11-06T10:00:00.000Z',
          updated_by: 'web',
        }),
      ),
    )
    await renderLoaded({ update })

    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))

    const visible = await screen.findByTestId('settings-save-visible')
    expect(visible).toHaveTextContent('未变更')
    expect(visible).toHaveTextContent('未写入新 audit')
    expect(visible).not.toHaveTextContent('audit 已写入')
    expect(screen.getByTestId('scan-current-value')).toHaveTextContent('4h')
  })

  it('clears the idempotency key even when the PUT resolves after unmount', async () => {
    let resolveUpdate: ((envelope: ScanSettingsEnvelope) => void) | undefined
    const update = vi.fn(
      () =>
        new Promise<ScanSettingsEnvelope>((resolve) => {
          resolveUpdate = resolve
        }),
    )
    const clear = vi.fn()
    const { unmount } = await renderLoaded({ update, clear })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))
    await waitFor(() => expect(update).toHaveBeenCalled())

    unmount()
    resolveUpdate?.(
      scanEnvelope(
        scanData({ value: '8h', source: 'user', updated_at: '2026-11-06T10:00:00.000Z' }),
      ),
    )

    await waitFor(() => expect(clear).toHaveBeenCalled())
  })

  it('clears the field error and aria-invalid once a valid radio is selected', async () => {
    const update = vi.fn(async () => {
      throw new SettingsApiError('scan.period must be one of: 4h, 8h', 'VALIDATION_FAILED', {
        httpStatus: 422,
      })
    })
    await renderLoaded({ update })

    fireEvent.click(screen.getByRole('radio', { name: '8h' }))
    fireEvent.click(screen.getByRole('button', { name: '保存排程' }))
    await screen.findByRole('alert')

    const group = screen.getByRole('group', { name: '扫描周期' })
    expect(group).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('radio', { name: '8h' })).toHaveAttribute('aria-invalid', 'true')

    fireEvent.click(screen.getByRole('radio', { name: '4h' }))

    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(group).not.toHaveAttribute('aria-invalid')
    expect(screen.getByRole('radio', { name: '4h' })).not.toHaveAttribute('aria-invalid')
    expect(document.getElementById('scan-period-error')).toBeNull()
  })

  it('shows a load error and recovers when the operator retries', async () => {
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<ScanSettingsEnvelope>>()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(scanEnvelope(scanData()))
    render(<SettingsScanPage fetchScanSettingsFn={fetch} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('扫描设置不可用')
    expect(alert).toHaveTextContent('network down')

    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    await screen.findByTestId('scan-current-value')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('renders a permanent load live region and keeps the visible text non-live', () => {
    const fetch = () => new Promise<ScanSettingsEnvelope>(() => {})
    render(<SettingsScanPage fetchScanSettingsFn={fetch} />)

    const region = document.getElementById('settings-load-status')
    expect(region).toHaveAttribute('role', 'status')
    expect(region).toHaveTextContent('加载扫描设置…')
    expect(screen.getByText('加载扫描设置…', { selector: 'p.status-message' })).not.toHaveAttribute(
      'role',
    )
  })

  it('is read-only at the compact override with the desktop/CLI note', async () => {
    await renderLoaded({ isCompact: true })

    expect(screen.getByRole('note')).toHaveTextContent(/桌面端或 CLI/)
    expect(screen.getByRole('button', { name: '保存排程' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: '8h' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: '4h' })).toBeDisabled()
  })

  it('gates read-only on the phone-width coarse-pointer query', () => {
    expect(SETTINGS_READONLY_QUERY).toBe('(max-width: 767.98px) and (pointer: coarse)')
  })
})

describe('SettingsPage', () => {
  it('links the scan section and exposes the section nav', async () => {
    render(<SettingsPage fetchSectionsFn={async () => sectionsEnvelope()} />)

    const link = await screen.findByRole('link', { name: /扫描与排程/ })
    expect(link).toHaveAttribute('href', '/settings/scan')
    const nav = screen.getByRole('navigation', { name: '设置分区导航' })
    expect(nav).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /概览/ })).toHaveAttribute('aria-current', 'page')
  })
})

describe('settings routes', () => {
  it('matches only the shell and the scan section in this slice', () => {
    expect(resolveRoute('/settings').isSettings).toBe(true)
    expect(resolveRoute('/settings/').isSettings).toBe(true)
    expect(resolveRoute('/settings/scan').isSettingsScan).toBe(true)
    expect(resolveRoute('/settings/scan').isSettings).toBe(false)

    const deferred = resolveRoute('/settings/risk')
    expect(deferred.isSettings).toBe(false)
    expect(deferred.isSettingsScan).toBe(false)
  })
})
