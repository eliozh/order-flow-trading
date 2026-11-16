import { useCallback, useEffect } from 'react'

import type { ReadinessData, ReadinessEnvelope, ReadyCheck, StatusEnvelope } from '../api/status'
import { ReadOnlyField } from '../components/settings-field'
import { useOnboardingLoad } from './use-onboarding-load'

const CHECK_LABELS: Record<ReadyCheck['status'], string> = {
  ok: 'PASS',
  degraded: 'DEGRADED',
  offline: 'FAIL',
  unknown: 'UNKNOWN',
}

/** UNKNOWN is not a failure: it gets a neutral chip, not danger. */
const CHECK_CHIP: Record<ReadyCheck['status'], string> = {
  ok: 'chip chip--ok',
  degraded: 'chip chip--warn',
  offline: 'chip chip--danger',
  unknown: 'chip chip--muted',
}

export interface EnvironmentStepProps {
  fetchReadinessFn: (signal?: AbortSignal) => Promise<ReadinessEnvelope>
  fetchStatusFn: (signal?: AbortSignal) => Promise<StatusEnvelope>
  onComplete: () => void
  completing: boolean
  narrow: boolean
  announce: (message: string) => void
}

interface EnvironmentData {
  readiness: ReadinessData
  status: NonNullable<StatusEnvelope['data']>
}

export function EnvironmentStep({
  fetchReadinessFn,
  fetchStatusFn,
  onComplete,
  completing,
  narrow,
  announce,
}: EnvironmentStepProps) {
  const loader = useCallback(
    async (signal: AbortSignal): Promise<EnvironmentData> => {
      const [readiness, status] = await Promise.all([
        fetchReadinessFn(signal),
        fetchStatusFn(signal),
      ])
      if (!readiness.data || !status.data) {
        throw new Error('预检响应为空；未执行任何保存。')
      }
      return { readiness: readiness.data, status: status.data }
    },
    [fetchReadinessFn, fetchStatusFn],
  )

  const load = useOnboardingLoad(loader)
  const { state } = load

  useEffect(() => {
    if (state === 'ready') {
      announce('本机预检已加载。')
    } else if (state === 'error') {
      announce('本机预检不可用。')
    }
  }, [state, announce])

  const data = load.data
  const timezone = data?.status.schedule?.timezone ?? 'UTC'

  return (
    <div className="stack">
      <section className="panel" aria-labelledby="onboarding-preflight-heading">
        <div className="panel-head">
          <h2 id="onboarding-preflight-heading">本机预检</h2>
        </div>
        {state === 'loading' && (
          <p className="status-message" role="status">
            正在执行本机预检…
          </p>
        )}
        {state === 'error' && (
          <>
            <p className="status-message status-message--error" role="alert">
              本机预检不可用：{load.message ?? '未知错误'}。未执行任何保存。
            </p>
            <div className="settings-actions">
              <button type="button" className="control-btn" onClick={load.retry}>
                重试
              </button>
            </div>
          </>
        )}
        {state === 'ready' && data && (
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
          <div className="table-wrap" role="region" aria-label="本机预检结果" tabIndex={0}>
            <table className="data-table">
              <caption>命名的预检项及其真实状态</caption>
              <thead>
                <tr>
                  <th scope="col">检查</th>
                  <th scope="col">状态</th>
                  <th scope="col">详情</th>
                </tr>
              </thead>
              <tbody>
                {data.readiness.checks.map((check) => (
                  <tr key={check.name} data-testid={`check-${check.name}`}>
                    <td className="mono">{check.name}</td>
                    <td>
                      <span className={CHECK_CHIP[check.status]}>{CHECK_LABELS[check.status]}</span>
                    </td>
                    <td>{check.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="onboarding-environment-heading">
        <div className="panel-head">
          <h2 id="onboarding-environment-heading">环境</h2>
        </div>
        <div className="settings-readonly-grid">
          <ReadOnlyField
            id="onboarding-data-dir"
            label="数据目录"
            value={data?.status.data_dir ?? '—'}
            note="配置、SQLite、回补数据与审计日志均在此目录。"
          />
          <ReadOnlyField
            id="onboarding-bind"
            label="本地绑定"
            value={data ? `${data.status.host}:${data.status.port}` : '—'}
            note="默认仅绑定 127.0.0.1；无账户体系。"
          />
          <ReadOnlyField
            id="onboarding-timezone"
            label="时区（仅显示）"
            value={timezone}
            note="存储始终为 ISO-8601 UTC；此处仅影响本地显示。"
          />
        </div>
        <fieldset className="settings-radio-group onboarding-pad">
          <legend>交易环境</legend>
          <label className="check">
            <input type="radio" name="onboarding-env" defaultChecked disabled />{' '}
            <span>
              <strong>Paper（默认）</strong>
              <small>模拟成交与完整记账；用于验证闭环与 4 周 Live gate。</small>
            </span>
          </label>
          <label className="check">
            <input type="radio" name="onboarding-env" disabled />{' '}
            <span>
              <strong>Live</strong>
              <small>Live 不在引导中启用；需 Milestone 5–6 的 paper 证据后在设置中单独开启</small>
            </span>
          </label>
        </fieldset>
        <div className="settings-actions">
          <button
            type="button"
            className="control-btn"
            disabled={narrow}
            aria-disabled={completing || undefined}
            onClick={() => {
              if (!narrow && !completing) {
                onComplete()
              }
            }}
          >
            {completing ? '保存中…' : '保存并继续'}
          </button>
          <span className="hint">
            退出可用 <code>oft init --resume</code> 继续。
          </span>
        </div>
      </section>
    </div>
  )
}
