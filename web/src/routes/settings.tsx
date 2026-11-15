import { useEffect, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  fetchSettingsSections,
  type SettingsSectionData,
  type SettingsSectionsEnvelope,
} from '../api/settings'
import { SettingsNotice, SourceChip, useSettingsReadonly } from '../components/settings-field'
import { SettingsSectionNav } from '../components/settings-section-nav'

interface SettingsPageProps {
  fetchSectionsFn?: (signal?: AbortSignal) => Promise<SettingsSectionsEnvelope>
  isCompact?: boolean
}

interface SettingsLoad {
  state: 'loading' | 'ready' | 'error'
  sections: SettingsSectionData[]
  message?: string
}

const SECTION_COPY: Record<string, { title: string; description: string }> = {
  scan: {
    title: '扫描与排程',
    description: '扫描周期（4h/8h）、来源、下一个 UTC 边界。',
  },
}

/** `/settings`: the section index. Values are not shown here, only provenance. */
export function SettingsPage({
  fetchSectionsFn = fetchSettingsSections,
  isCompact,
}: SettingsPageProps) {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<SettingsLoad>({ state: 'loading', sections: [] })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchSectionsFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', sections: [], message: '设置响应为空。' })
          return
        }
        setLoad({ state: 'ready', sections: data.sections })
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setLoad({ state: 'error', sections: [], message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchSectionsFn, attempt])

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">Settings / overview</p>
          <h1 id="settings-heading">设置 / Settings</h1>
          <p className="page-sub">
            高后果变更需显式保存、审计并确认。本切片只包含扫描周期；其他设置分区随后续切片提供。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：设置修改只能在桌面端或 CLI 完成。Kill switch 仍可用。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="overview" />
        <div className="stack">
          {/* Permanent live region: text is written on a load-state change. */}
          <p
            role="status"
            className="visually-hidden"
            id="settings-sections-status"
            data-testid="settings-sections-status"
          >
            {load.state === 'loading' ? '加载设置分区…' : ''}
          </p>

          {load.state === 'loading' && <p className="status-message">加载设置分区…</p>}

          {load.state === 'error' && (
            <>
              <SettingsNotice tone="error">
                设置分区不可用：{load.message ?? '未知错误'}
              </SettingsNotice>
              <button
                type="button"
                className="control-btn"
                onClick={() => setAttempt((value) => value + 1)}
              >
                重试
              </button>
            </>
          )}

          {load.state === 'ready' && (
            <section className="panel" aria-labelledby="settings-sections-heading">
              <div className="panel-head">
                <h2 id="settings-sections-heading">设置分区</h2>
              </div>
              <div className="settings-card-grid">
                {load.sections.map((section) => {
                  const copy = SECTION_COPY[section.section] ?? {
                    title: section.section,
                    description: section.keys.join(', '),
                  }
                  return (
                    <a
                      key={section.section}
                      className="settings-card"
                      href={`/settings/${section.section}`}
                    >
                      <span className="kicker">{section.section.toUpperCase()}</span>
                      <strong>{copy.title}</strong>
                      <span className="settings-card-sub">{copy.description}</span>
                      <span className="settings-card-meta">
                        <SourceChip source={section.source} />
                        <span className="mono-muted">
                          {section.last_changed ?? 'never (default)'}
                        </span>
                      </span>
                      <span className="settings-card-go">打开 →</span>
                    </a>
                  )
                })}
              </div>
            </section>
          )}
        </div>
      </div>
    </section>
  )
}
