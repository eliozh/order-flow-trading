import { useEffect, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  fetchPromptSettings,
  PROMPT_ROLE_LABELS,
  type PromptSettingsData,
  type PromptSettingsEnvelope,
  type PromptSettingsRow,
} from '../api/prompts'
import { SettingsSectionNav } from '../components/settings-section-nav'
import { useSettingsReadonly } from '../components/settings-field'

export interface SettingsPromptsPageProps {
  fetchPromptSettingsFn?: (signal?: AbortSignal) => Promise<PromptSettingsEnvelope>
  isCompact?: boolean
}

interface PromptListLoad {
  state: 'loading' | 'ready' | 'error'
  data: PromptSettingsData | null
  message?: string
}

function editorHref(promptId: string): string {
  return `/settings/prompts/${encodeURIComponent(promptId)}`
}

function historyHref(promptId: string): string {
  return `${editorHref(promptId)}/history`
}

function PromptRow({ row }: { row: PromptSettingsRow }) {
  return (
    <tr data-testid={`prompt-row-${row.prompt_id}`}>
      <th scope="row">
        <span>{PROMPT_ROLE_LABELS[row.role]}</span>
        <br />
        <span className="mono hint">{row.role}</span>
      </th>
      <td>
        <a className="rowlink mono" href={editorHref(row.prompt_id)}>
          {row.prompt_id}
        </a>
      </td>
      <td>
        <span className="chip chip--calc" data-testid={`prompt-active-${row.prompt_id}`}>
          v{row.active_version} <span lang="en">active</span>
        </span>
        {row.is_builtin_default ? (
          <span className="chip chip--ok" data-testid={`prompt-builtin-${row.prompt_id}`}>
            <span lang="en">built-in default</span> v1
          </span>
        ) : (
          <span className="chip chip--muted" data-testid={`prompt-custom-${row.prompt_id}`}>
            <span lang="en">custom</span> · {row.version_count} <span lang="en">versions</span>
          </span>
        )}
      </td>
      <td className="mono" data-testid={`prompt-author-${row.prompt_id}`}>
        {row.author}
      </td>
      <td className="mono" data-testid={`prompt-updated-${row.prompt_id}`}>
        {row.updated_at}
      </td>
      <td className="num mono">
        {row.version_count}（latest v{row.latest_version}）
      </td>
      <td>
        <span className="action-row">
          <a className="btn sm" href={editorHref(row.prompt_id)}>
            编辑
          </a>
          <a className="btn sm" href={historyHref(row.prompt_id)}>
            历史
          </a>
        </span>
      </td>
    </tr>
  )
}

/** `/settings/prompts` (F10b): the four USER roles and their active version. */
export function SettingsPromptsPage({
  fetchPromptSettingsFn = fetchPromptSettings,
  isCompact,
}: SettingsPromptsPageProps) {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<PromptListLoad>({ state: 'loading', data: null })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    fetchPromptSettingsFn(controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setLoad({ state: 'error', data: null, message: 'Prompt 响应为空。' })
          return
        }
        setLoad({ state: 'ready', data: envelope.data })
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setLoad({ state: 'error', data: null, message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchPromptSettingsFn, attempt])

  const items = load.data?.items ?? []
  const allBuiltin = items.length > 0 && items.every((item) => item.is_builtin_default)

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-prompts-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <span lang="en">Settings / prompts</span>
          </p>
          <h1 id="settings-prompts-heading">
            <span lang="en">Prompt</span> 模板
          </h1>
          <p className="page-sub">
            按角色管理模板；保存创建新版本，永不原地编辑。恢复默认与激活历史版本可审计。每次决策记录其使用的模板版本，历史版本永久保留。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：模板编辑器在 375px 下只读；请在桌面端或 CLI 修改。历史与预览仍可查看。
        </p>
      )}

      <p
        role="status"
        className="visually-hidden"
        data-testid="settings-prompts-load-status"
        aria-live="polite"
      >
        {load.state === 'loading' ? (
          <>
            加载 <span lang="en">Prompt</span> 模板…
          </>
        ) : (
          ''
        )}
      </p>

      <div className="settings-cols">
        <SettingsSectionNav active="prompts" />
        <div className="stack">
          {load.state === 'loading' && (
            <p className="status-message">
              加载 <span lang="en">Prompt</span> 模板…
            </p>
          )}

          {load.state === 'error' && (
            <div className="panel" role="alert">
              <div className="panel-head">
                <h2>
                  <span lang="en">Prompt</span> 模板不可用
                </h2>
              </div>
              <p className="panel-foot">{load.message ?? '未知错误'}</p>
              <div className="action-row">
                <button
                  type="button"
                  className="control-btn"
                  onClick={() => setAttempt((value) => value + 1)}
                >
                  重试
                </button>
              </div>
            </div>
          )}

          {load.state === 'ready' && items.length === 0 && (
            <section className="panel" aria-labelledby="prompt-empty-heading">
              <div className="panel-head">
                <h2 id="prompt-empty-heading">尚未创建自定义模板</h2>
              </div>
              <p className="panel-foot">
                当前使用内置默认 v1。可以打开编辑器创建新版本；恢复默认随时可用。
              </p>
            </section>
          )}

          {load.state === 'ready' && items.length > 0 && (
            <>
              {allBuiltin && (
                <p className="status-message" role="status" data-testid="prompts-builtin-banner">
                  使用内置默认 v1：自定义模板尚未创建；恢复默认不是破坏性操作。
                </p>
              )}
              <section className="panel" aria-labelledby="prompt-list-heading">
                <div className="panel-head">
                  <h2 id="prompt-list-heading">模板列表</h2>
                  <span className="hint">
                    4 <span lang="en">roles</span> · 历史版本永久保留
                  </span>
                </div>
                <div
                  className="table-wrap"
                  role="region"
                  aria-label="Prompt 模板列表"
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table
                  tabIndex={0}
                >
                  <table className="data prompt-table">
                    <caption className="hint">
                      每次决策记录模板版本；保存创建新版本，永不原地编辑。
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">
                          <span lang="en">Role</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Template</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Active</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Author</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Updated</span>
                        </th>
                        <th scope="col" className="num">
                          <span lang="en">Versions</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((row) => (
                        <PromptRow key={row.prompt_id} row={row} />
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="hint">
                  “<span lang="en">built-in default v1</span>”
                  表示该角色尚未创建任何自定义版本。激活旧版本不会删除新版本；每次决策记录其使用的版本。
                </p>
              </section>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
