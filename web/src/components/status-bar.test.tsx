import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { StatusBar } from './status-bar'

describe('StatusBar', () => {
  it('shows environment, daemon, version, and readiness', () => {
    render(<StatusBar env="PAPER" daemon="UP" version="0.1.0" readiness="READY" />)

    expect(screen.getByRole('region', { name: 'System status bar' })).toBeInTheDocument()
    expect(screen.getByText('PAPER')).toBeInTheDocument()
    expect(screen.getByText('daemon')).toBeInTheDocument()
    expect(screen.getByText('UP')).toBeInTheDocument()
    expect(screen.getByText('0.1.0')).toBeInTheDocument()
    expect(screen.getByText('READY')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Kill switch/i })).toBeInTheDocument()
  })

  it('shows OFFLINE when the daemon probe fails', () => {
    render(<StatusBar daemon="OFFLINE" readiness="UNKNOWN" />)

    expect(screen.getByText('OFFLINE')).toBeInTheDocument()
    expect(screen.getByText('UNKNOWN')).toBeInTheDocument()
  })
})
