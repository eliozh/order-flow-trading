/* eslint-disable jsx-a11y/no-autofocus -- focus starts on the least destructive control (cancel or token) */
/* eslint-disable jsx-a11y/no-noninteractive-element-interactions -- role=dialog closes on Escape */
import { useEffect, useState } from 'react'

import {
  activateKillSwitch,
  deactivateKillSwitch,
  fetchKillSwitch,
  type KillSwitchEnvelope,
  type KillSwitchStatus,
} from '../api/paper'

interface KillSwitchBarProps {
  fetchStatusFn?: () => Promise<KillSwitchEnvelope>
  activateFn?: () => Promise<KillSwitchEnvelope>
  deactivateFn?: (token: string) => Promise<KillSwitchEnvelope>
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

/**
 * The global kill switch. Stopping is always allowed and immediate; resuming
 * requires the typed RESUME token. It is never hidden at 375px: stopping must
 * never be blocked by a read-only or narrow surface.
 */
export function KillSwitchBar({
  fetchStatusFn = fetchKillSwitch,
  activateFn = activateKillSwitch,
  deactivateFn = deactivateKillSwitch,
}: KillSwitchBarProps) {
  const [status, setStatus] = useState<KillSwitchStatus | null>(null)
  const [probeError, setProbeError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [token, setToken] = useState('')
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchStatusFn()
      .then((envelope) => {
        if (!active) {
          return
        }
        if (envelope.data) {
          setStatus(envelope.data)
          setProbeError(null)
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setProbeError(message(error))
        }
      })
    return () => {
      active = false
    }
  }, [fetchStatusFn])

  async function activate(): Promise<void> {
    setActing(true)
    setActionError(null)
    try {
      const envelope = await activateFn()
      if (envelope.data) {
        setStatus(envelope.data)
        setProbeError(null)
      }
    } catch (error: unknown) {
      setActionError(message(error))
    } finally {
      setActing(false)
    }
  }

  async function resume(): Promise<void> {
    setActing(true)
    setActionError(null)
    try {
      const envelope = await deactivateFn(token)
      if (envelope.data) {
        setStatus(envelope.data)
      }
      setOpen(false)
      setToken('')
    } catch (error: unknown) {
      setActionError(message(error))
    } finally {
      setActing(false)
    }
  }

  const active = status?.active === true

  return (
    <section
      aria-label="Kill switch"
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 12,
        padding: '8px 16px',
        borderBottom: '1px solid var(--oft-border-default)',
        background: active ? '#2a0e12' : 'var(--oft-bg-panel)',
        color: 'var(--oft-text-primary)',
        fontFamily: 'var(--oft-font-mono)',
        fontSize: 11,
      }}
    >
      <span className={active ? 'chip chip--danger' : 'chip chip--ok'}>
        {active ? 'KILL SWITCH ON' : 'KILL SWITCH OFF'}
      </span>

      {probeError !== null && (
        <span role="alert" className="value-down">
          Kill switch 状态不可用：{probeError}
        </span>
      )}
      {actionError !== null && (
        <span role="alert" className="value-down">
          {actionError}
        </span>
      )}

      {status === null ? (
        <button
          type="button"
          className="btn"
          disabled={acting}
          onClick={() => {
            void activate()
          }}
        >
          启用 Kill switch
        </button>
      ) : active ? (
        <button
          type="button"
          className="btn"
          aria-label="Kill switch 开启，可停止新订单"
          disabled={acting}
          onClick={() => {
            setActionError(null)
            setToken('')
            setOpen(true)
          }}
        >
          Kill switch 开启，可停止新订单
        </button>
      ) : (
        <button
          type="button"
          className="btn"
          aria-label="Kill switch 关闭，立即停止新订单"
          disabled={acting}
          onClick={() => {
            void activate()
          }}
        >
          Kill switch 关闭，立即停止新订单
        </button>
      )}

      <span className="mono-muted">
        {active ? 'no new orders will be sent · 停止无需确认' : '关闭需输入 RESUME；移动端仍可用'}
      </span>

      {open && active && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="kill-switch-off-title"
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 20,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 16,
            background: '#07080ab8',
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setOpen(false)
            }
          }}
        >
          <div
            style={{
              width: 'min(520px, 100%)',
              border: '1px solid var(--oft-border-strong)',
              background: 'var(--oft-bg-raised)',
              padding: 16,
            }}
          >
            <h2 id="kill-switch-off-title" style={{ marginTop: 0 }}>
              关闭 Kill switch
            </h2>
            <p className="status-message">
              恢复订单发送会让新的 Paper 订单进入模拟引擎。输入 RESUME 继续。
            </p>
            <label
              htmlFor="kill-switch-token"
              style={{ display: 'block', margin: '12px 0 6px', fontSize: 12 }}
            >
              输入 RESUME 以恢复订单发送
            </label>
            <input
              id="kill-switch-token"
              className="mono"
              value={token}
              autoFocus
              autoCapitalize="off"
              spellCheck={false}
              onChange={(event) => setToken(event.target.value)}
              style={{
                width: '100%',
                minHeight: 32,
                padding: '4px 10px',
                border: '1px solid var(--oft-border-control)',
                background: 'var(--oft-bg-inset, #07080a)',
                color: 'var(--oft-text-primary)',
              }}
            />
            <div className="action-row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setOpen(false)
                  setToken('')
                  setActionError(null)
                }}
              >
                取消
              </button>
              <button
                type="button"
                className="btn"
                disabled={token !== 'RESUME' || acting}
                onClick={() => {
                  void resume()
                }}
              >
                输入 RESUME 并关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
