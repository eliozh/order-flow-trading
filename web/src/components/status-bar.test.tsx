import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { LiveUpdatesProvider } from '../hooks/live-updates-provider'
import { PauseControl, StatusBar } from './status-bar'

describe('StatusBar', () => {
  it('shows environment, daemon, version, and readiness', () => {
    render(<StatusBar env="PAPER" daemon="UP" version="0.1.0" readiness="READY" />)

    expect(screen.getByRole('region', { name: 'System status bar' })).toBeInTheDocument()
    expect(screen.getByText('PAPER')).toBeInTheDocument()
    expect(screen.getByText('daemon')).toBeInTheDocument()
    expect(screen.getByText('UP')).toBeInTheDocument()
    expect(screen.getByText('0.1.0')).toBeInTheDocument()
    expect(screen.getByText('READY')).toBeInTheDocument()
  })

  it('has no dead kill-switch placeholder and no chrome controls', () => {
    render(<StatusBar env="PAPER" daemon="UP" version="0.1.0" readiness="READY" />)

    // Status bar facts only: the kill switch and the pause toggle are rendered
    // after it in the shell chrome so the first three tab stops are the skip
    // links and the kill switch (DESIGN A2.2 / A12 #7).
    expect(screen.queryByText(/not available in this build/i)).not.toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('shows OFFLINE when the daemon probe fails', () => {
    render(<StatusBar daemon="OFFLINE" readiness="UNKNOWN" />)

    expect(screen.getByText('OFFLINE')).toBeInTheDocument()
    expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0)
  })

  it('never claims health with hardcoded defaults', () => {
    render(<StatusBar />)

    expect(screen.getByText('OFFLINE')).toBeInTheDocument()
    expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0)
    expect(screen.queryByText('UP')).not.toBeInTheDocument()
    expect(screen.queryByText('READY')).not.toBeInTheDocument()
  })
})

describe('PauseControl', () => {
  it('toggles the DESIGN A6 global pause with aria-pressed, a visible PAUSED stamp, and an announcement', () => {
    render(
      <LiveUpdatesProvider>
        <PauseControl />
      </LiveUpdatesProvider>,
    )

    const pause = screen.getByRole('button', { name: '暂停实时更新' })
    expect(pause).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByTestId('statusbar-paused')).not.toBeInTheDocument()

    fireEvent.click(pause)

    const resume = screen.getByRole('button', { name: '继续实时更新' })
    expect(resume).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('statusbar-paused')).toHaveTextContent('PAUSED')
    expect(screen.getByRole('status')).toHaveTextContent('实时更新已暂停')

    fireEvent.click(resume)

    expect(screen.getByRole('button', { name: '暂停实时更新' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(screen.queryByTestId('statusbar-paused')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('实时更新已恢复')
  })

  it('renders exactly one control so the kill switch stays the third tab stop', () => {
    render(
      <LiveUpdatesProvider>
        <PauseControl />
      </LiveUpdatesProvider>,
    )

    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('puts the pause control inside a labelled region landmark', () => {
    render(
      <LiveUpdatesProvider>
        <PauseControl />
      </LiveUpdatesProvider>,
    )

    const region = screen.getByRole('region', { name: '实时更新控制' })
    expect(region).toContainElement(screen.getByRole('button', { name: '暂停实时更新' }))
  })
})
