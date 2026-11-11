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
  render(
    <div className="app-shell">
      <KillSwitchBar fetchStatusFn={status} activateFn={activate} deactivateFn={deactivate} />
    </div>,
  )
  return { status, activate, deactivate }
}

describe('KillSwitchBar', () => {
  it('is available immediately and activates with one press when inactive', async () => {
    const { status, activate } = setup(false)

    const button = await screen.findByRole('button', { name: /启用 Kill switch（立即停止新订单）/ })
    fireEvent.click(button)

    await waitFor(() => expect(activate).toHaveBeenCalledTimes(1))
    expect(status).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/KILL SWITCH ON/)).toBeInTheDocument()
    expect(activate.mock.calls.length).toBe(1)
    expect(screen.getByRole('status')).toHaveTextContent(/已启用/)
  })

  it('announces an UNKNOWN state chip while the probe is still in flight', () => {
    render(
      <div className="app-shell">
        <KillSwitchBar
          fetchStatusFn={() => new Promise<KillSwitchEnvelope>(() => undefined)}
          activateFn={async () => envelope(true)}
          deactivateFn={async () => envelope(false)}
        />
      </div>,
    )

    expect(screen.getByText('状态未知')).toBeInTheDocument()
    // Unknown is never presented as trading-enabled.
    expect(screen.queryByText(/KILL SWITCH OFF/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /启用 Kill switch/ })).toBeEnabled()
  })

  it('remains available when the status probe fails and never asserts OFF', async () => {
    render(
      <div className="app-shell">
        <KillSwitchBar
          fetchStatusFn={async () => {
            throw new Error('status unavailable')
          }}
          activateFn={async () => envelope(true)}
          deactivateFn={async () => envelope(false)}
        />
      </div>,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(/status unavailable/)
    expect(screen.getByText('状态未知')).toBeInTheDocument()
    expect(screen.queryByText(/KILL SWITCH OFF/)).not.toBeInTheDocument()
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
    expect(await screen.findByText(/KILL SWITCH OFF/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /启用 Kill switch/ })).toBeInTheDocument()
  })

  it('surfaces a server token rejection inside the dialog and changes nothing', async () => {
    const deactivate = vi.fn(async () => {
      throw new Error('the RESUME token is required')
    })
    render(
      <div className="app-shell">
        <KillSwitchBar
          fetchStatusFn={async () => envelope(true)}
          activateFn={async () => envelope(true)}
          deactivateFn={deactivate}
        />
      </div>,
    )
    fireEvent.click(await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ }))
    const dialog = screen.getByRole('dialog', { name: /关闭 Kill switch/ })
    fireEvent.change(within(dialog).getByLabelText(/输入 RESUME/), {
      target: { value: 'RESUME' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /输入 RESUME 并关闭/ }))

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent(/RESUME token is required/)
    expect(screen.getByRole('dialog', { name: /关闭 Kill switch/ })).toBeInTheDocument()
    expect(screen.getByText(/KILL SWITCH ON/)).toBeInTheDocument()
  })

  it('traps focus, locks scroll, and restores focus on Escape', async () => {
    setup(true)
    const invoke = await screen.findByRole('button', {
      name: /Kill switch 开启，可停止新订单/,
    })
    invoke.focus()
    fireEvent.click(invoke)

    const dialog = screen.getByRole('dialog', { name: /关闭 Kill switch/ })
    expect(dialog).toHaveAttribute('aria-describedby', 'kill-switch-off-consequences')
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('hidden')

    const tokenInput = within(dialog).getByLabelText(/输入 RESUME/)
    expect(tokenInput).toHaveFocus()

    const cancel = within(dialog).getByRole('button', { name: /取消/ })
    const confirm = within(dialog).getByRole('button', { name: /输入 RESUME 并关闭/ })
    expect(cancel).toBeInTheDocument()
    fireEvent.change(tokenInput, { target: { value: 'RESUME' } })
    confirm.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(tokenInput).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(invoke).toHaveFocus()
    expect(document.querySelector('.app-shell')).not.toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('')
  })

  it('submits the resume form with Enter', async () => {
    const { deactivate } = setup(true)
    fireEvent.click(await screen.findByRole('button', { name: /Kill switch 开启，可停止新订单/ }))
    const dialog = screen.getByRole('dialog', { name: /关闭 Kill switch/ })
    const form = dialog.querySelector('form')
    expect(form).not.toBeNull()
    fireEvent.change(within(dialog).getByLabelText(/输入 RESUME/), {
      target: { value: 'RESUME' },
    })
    fireEvent.submit(form as HTMLFormElement)

    await waitFor(() => expect(deactivate).toHaveBeenCalledWith('RESUME'))
  })

  it('revalidates the status when the window regains focus', async () => {
    const { status } = setup(false)
    await screen.findByRole('button', { name: /启用 Kill switch/ })
    expect(status).toHaveBeenCalledTimes(1)

    fireEvent(window, new Event('focus'))

    await waitFor(() => expect(status).toHaveBeenCalledTimes(2))
  })
})
