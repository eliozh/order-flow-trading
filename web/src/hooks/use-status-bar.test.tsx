import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ReadinessEnvelope } from '../api/status'
import { OFFLINE_STATUS, useStatusBar } from './use-status-bar'

const meta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T00:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

function readiness(status: 'ready' | 'not_ready'): ReadinessEnvelope {
  return {
    ok: status === 'ready',
    data: {
      status,
      version: '0.9.3',
      data_dir: '/tmp/data',
      checked_at: '2026-11-06T00:00:00.000Z',
      checks: [],
      latest_scan: null,
    },
    error: null,
    meta,
  }
}

describe('useStatusBar', () => {
  it('reports UP/READY and the served version when ready', async () => {
    const { result } = renderHook(() => useStatusBar(async () => readiness('ready')))

    await waitFor(() => expect(result.current.readiness).toBe('READY'))
    expect(result.current.daemon).toBe('UP')
    expect(result.current.version).toBe('0.9.3')
  })

  it('reports UP/NOT_READY when the daemon answers but is not ready', async () => {
    const { result } = renderHook(() => useStatusBar(async () => readiness('not_ready')))

    await waitFor(() => expect(result.current.readiness).toBe('NOT_READY'))
    expect(result.current.daemon).toBe('UP')
  })

  it('reports OFFLINE/UNKNOWN/UNKNOWN when the probe fails', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('daemon down'))
    const { result } = renderHook(() => useStatusBar(fetcher))

    await waitFor(() => expect(fetcher).toHaveBeenCalled())
    expect(result.current).toEqual(OFFLINE_STATUS)
    expect(result.current.daemon).toBe('OFFLINE')
    expect(result.current.readiness).toBe('UNKNOWN')
    expect(result.current.version).toBe('UNKNOWN')
  })

  it('does not refetch when the fetcher identity changes', async () => {
    const fetcher = vi.fn(async () => readiness('ready'))
    const { result, rerender } = renderHook(
      ({ probe }: { probe: () => Promise<ReadinessEnvelope> }) => useStatusBar(probe),
      { initialProps: { probe: fetcher as () => Promise<ReadinessEnvelope> } },
    )

    await waitFor(() => expect(result.current.readiness).toBe('READY'))
    rerender({ probe: async () => readiness('not_ready') })

    expect(result.current.readiness).toBe('READY')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
