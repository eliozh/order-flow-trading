import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import type { KillSwitchEnvelope, KillSwitchStatus } from '../../api/paper'
import { KillSwitchBar } from '../kill-switch'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T12:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function envelope(active: boolean): KillSwitchEnvelope {
  const data: KillSwitchStatus = {
    active,
    sentinel_path: 'kill-switch',
    checked_at: '2026-11-06T12:00:00.000Z',
  }
  return { ok: true, data, error: null, meta }
}

function setup(active = false) {
  const status = vi.fn(async () => envelope(active))
  const activate = vi.fn(async () => envelope(true))
  const deactivate = vi.fn(async (token: string) => {
    if (token !== 'RESUME') {
      throw new Error('the RESUME token is required')
    }
    return envelope(false)
  })
  render(<KillSwitchBar fetchStatusFn={status} activateFn={activate} deactivateFn={deactivate} />)
  return { status, activate, deactivate }
}

describe('KillSwitchBar', () => {
  it('is available immediately and activates with one press when inactive', async () => {
    const { status, activate } = setup(false)

    const button = await screen.findByRole('button', { name: /Kill switch 关闭，立即停止新订单/ })
    fireEvent.click(button)

    await waitFor(() => expect(activate).toHaveBeenCalledTimes(1))
    expect(status).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/KILL SWITCH ON/)).toBeInTheDocument()
    expect(activate.mock.calls.length).toBe(1)
  })

  it('remains available when the status probe fails', async () => {
    render(
      <KillSwitchBar
        fetchStatusFn={async () => {
          throw new Error('status unavailable')
        }}
        activateFn={async () => envelope(true)}
        deactivateFn={async () => envelope(false)}
      />,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(/status unavailable/)
    expect(screen.getByRole('button', { name: /启用 Kill switch/ })).toBeEnabled()
  })

  it('requires the typed RESUME token before turning the switch off', async () => {
    const { deactivate } = setup(true)
    fireEvent.click(await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ }))

    const dialog = screen.getByRole('dialog', { name: /关闭 Kill switch/ })
    const confirm = within(dialog).getByRole('button', { name: /输入 RESUME 并关闭/ })
    expect(confirm).toBeDisabled()

    fireEvent.change(within(dialog).getByLabelText(/输入 RESUME/), {
      target: { value: 'resume' },
    })
    expect(confirm).toBeDisabled()

    fireEvent.change(within(dialog).getByLabelText(/输入 RESUME/), {
      target: { value: 'RESUME' },
    })
    expect(confirm).toBeEnabled()
    fireEvent.click(confirm)

    await waitFor(() => expect(deactivate).toHaveBeenCalledWith('RESUME'))
    expect(await screen.findByText(/Kill switch 关闭，立即停止新订单/)).toBeInTheDocument()
  })

  it('surfaces a server token rejection and changes nothing', async () => {
    const deactivate = vi.fn(async () => {
      throw new Error('the RESUME token is required')
    })
    render(
      <KillSwitchBar
        fetchStatusFn={async () => envelope(true)}
        activateFn={async () => envelope(true)}
        deactivateFn={deactivate}
      />,
    )
    fireEvent.click(await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ }))
    const dialog = screen.getByRole('dialog', { name: /关闭 Kill switch/ })
    fireEvent.change(within(dialog).getByLabelText(/输入 RESUME/), {
      target: { value: 'RESUME' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /输入 RESUME 并关闭/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/RESUME token is required/)
    expect(screen.getByText(/KILL SWITCH ON/)).toBeInTheDocument()
  })
})
