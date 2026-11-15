import type { ReactElement } from 'react'

import { ConfirmDialog, type ConfirmDiffRow } from '../components/confirm-dialog'
import { PromptDiff } from '../components/prompt-diff'
import { SettingsSectionNav } from '../components/settings-section-nav'
import { type UsePromptHistoryOptions, usePromptHistory } from '../hooks/use-prompt-history'

export type SettingsPromptHistoryPageProps = UsePromptHistoryOptions

function stateChips(version: {
  is_active: boolean
  is_default: boolean
  status: string
}): ReactElement {
  return (
    <span className="action-row">
      {version.is_active && (
        <span className="chip chip--calc">
          <span lang="en">active</span>
        </span>
      )}
      {version.is_default && (
        <span className="chip chip--muted">
          <span lang="en">built-in default</span>
        </span>
      )}
      <span className="chip chip--muted">
        <span lang="en">{version.status}</span>
      </span>
    </span>
  )
}

/** `/settings/prompts/:promptId/history` (F10b.2): history, diff, activation. */
export function SettingsPromptHistoryPage(props: SettingsPromptHistoryPageProps) {
  const history = usePromptHistory(props)
  const { load, readonly, activeVersion, dialog, diff } = history
  const detail = load.detail
  const versions = detail === null ? [] : [...detail.versions].reverse()
  const activePresent =
    detail !== null && detail.versions.some((version) => version.version === detail.active_version)

  const dialogRows: ConfirmDiffRow[] =
    dialog === null
      ? []
      : dialog.kind === 'activate'
        ? [
            {
              field: 'version',
              label: '激活版本',
              before: (
                <>
                  v{activeVersion ?? 0} <span lang="en">active</span>
                </>
              ),
              after: `v${dialog.version}`,
            },
          ]
        : [
            {
              field: 'version',
              label: (
                <>
                  当前 <span lang="en">active</span> 版本（保持不变）
                </>
              ),
              before: `v${dialog.activeVersion}`,
              after: (
                <>
                  新的 <span lang="en">built-in default</span> 版本（新建，
                  <span lang="en">DRAFT</span>）
                </>
              ),
            },
          ]

  const diffStatus =
    diff.state === 'loading' && history.selectedVersion !== null && activeVersion !== null
      ? `正在加载 v${history.selectedVersion} 与 active v${activeVersion} 的差异…`
      : diff.state === 'ready' && diff.data !== null
        ? `差异已就绪：v${diff.data.from_version} → v${diff.data.to_version}`
        : ''

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-prompt-history-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <span lang="en">Settings / prompts / {props.promptId} / history</span>
          </p>
          <h1 id="settings-prompt-history-heading">
            <span lang="en">Prompt</span> 版本历史
          </h1>
          <p className="page-sub">
            作者、时间、原因与激活状态。可显式激活旧版本；缺失版本禁用回放。恢复默认创建新的{' '}
            <span lang="en">DRAFT</span> 版本并显示当前 <span lang="en">active</span> 版本；激活前{' '}
            <span lang="en">active</span> 保持不变，历史保持完整。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：模板激活与恢复只能在桌面端或 CLI 完成。历史与 <span lang="en">diff</span>{' '}
          仍可查看。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="prompts" />
        <div className="stack">
          <p
            role="status"
            className="visually-hidden"
            data-testid="settings-prompt-history-load-status"
            aria-live="polite"
          >
            {load.state === 'loading' ? (detail === null ? '加载版本历史…' : '刷新中…') : ''}
          </p>

          {load.state === 'loading' && detail === null && (
            <p className="status-message">加载版本历史…</p>
          )}

          {load.state === 'error' && detail === null && (
            <div className="panel" role="alert">
              <div className="panel-head">
                <h2>版本历史不可用</h2>
              </div>
              <p className="panel-foot">{load.message ?? '未知错误'}；不会回放或激活任何版本。</p>
              <div className="action-row">
                <button type="button" className="control-btn" onClick={history.retry}>
                  重试
                </button>
              </div>
            </div>
          )}

          {/* Keep the ready content (and its focusable heading) mounted across a
              post-mutation refetch: unmounting the focused heading would drop
              focus to <body>. The inline status announces the refresh instead. */}
          {detail !== null && (
            <>
              {/* Always present so sibling indices never shift while a refetch
                  is in flight; the focused heading must survive the reload. */}
              <p
                className="status-message"
                role="status"
                data-testid="prompt-history-refresh"
                hidden={load.state !== 'loading'}
              >
                刷新中…
              </p>

              <p
                role="status"
                className="visually-hidden"
                data-testid="prompt-history-status"
                aria-live="polite"
              >
                {history.liveMessage}
              </p>

              {/* A failed refetch after a confirmed mutation keeps the previous
                  detail mounted. Non-destructive: the success message and the
                  focused heading stay reachable. */}
              {load.state === 'error' && (
                <div
                  className="status-message status-message--error"
                  role="alert"
                  data-testid="prompt-history-refresh-error"
                >
                  <span aria-hidden="true">⚠</span> 刷新失败；仍显示上次加载的内容：
                  {load.message ?? '未知错误'}
                  <button type="button" className="control-btn" onClick={history.retry}>
                    重试
                  </button>
                </div>
              )}

              {history.errorText !== '' && (
                <div
                  className="status-message status-message--error"
                  role="alert"
                  tabIndex={-1}
                  ref={history.errorSummaryRef}
                  data-testid="prompt-history-error"
                >
                  <span aria-hidden="true">⚠</span> 错误：{history.errorText}
                </div>
              )}

              <section className="panel" aria-labelledby="prompt-history-list-heading">
                <div className="panel-head">
                  <h2 id="prompt-history-list-heading" ref={history.headingRef} tabIndex={-1}>
                    <span className="mono">{detail.prompt_id}</span> 版本历史
                  </h2>
                  <span className="action-row">
                    <span className="chip chip--calc">
                      v{detail.active_version} <span lang="en">active</span>
                    </span>
                    <button
                      type="button"
                      className="control-btn"
                      onClick={history.requestRestore}
                      disabled={readonly || history.saving || detail.versions.length === 0}
                    >
                      恢复内置默认
                    </button>
                  </span>
                </div>
                <div
                  className="table-wrap"
                  role="region"
                  aria-label="Prompt 版本历史"
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable table
                  tabIndex={0}
                >
                  <table className="data prompt-history-table">
                    <caption className="hint">
                      {detail.prompt_id} 版本历史，{detail.versions.length} 个版本。
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">
                          <span lang="en">Version</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Author</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Created</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Reason</span>
                        </th>
                        <th scope="col">
                          <span lang="en">State</span>
                        </th>
                        <th scope="col">
                          <span lang="en">Action</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {!activePresent && (
                        <tr data-testid="prompt-version-missing">
                          <th scope="row" className="mono">
                            v{detail.active_version}
                          </th>
                          <td className="mono-muted">—</td>
                          <td className="mono-muted">—</td>
                          <td className="mono-muted">内容缺失</td>
                          <td>
                            <span className="chip chip--danger">
                              <span lang="en">missing</span>
                            </span>
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn sm"
                              aria-disabled="true"
                              aria-describedby="prompt-version-missing-note"
                              disabled
                            >
                              不可回放
                            </button>
                            <span id="prompt-version-missing-note" className="hint">
                              该版本内容已缺失，回放会伪造输入，因此禁用。
                            </span>
                          </td>
                        </tr>
                      )}
                      {versions.map((version) => (
                        <tr key={version.version} data-testid={`prompt-version-${version.version}`}>
                          <th scope="row" className="mono">
                            v{version.version}
                          </th>
                          <td className="mono">{version.author}</td>
                          <td className="mono">{version.created_at}</td>
                          <td>{version.reason === '' ? '—' : version.reason}</td>
                          <td>{stateChips(version)}</td>
                          <td>
                            <span className="action-row">
                              {version.is_active ? (
                                <span className="mono-muted">当前</span>
                              ) : (
                                <button
                                  type="button"
                                  className="btn sm"
                                  onClick={() => history.requestActivate(version.version)}
                                  disabled={readonly || history.saving}
                                >
                                  激活 v{version.version}
                                </button>
                              )}
                              {!version.is_active && version.version !== activeVersion && (
                                <button
                                  type="button"
                                  className="btn sm"
                                  onClick={() => history.selectVersion(version.version)}
                                  aria-pressed={history.selectedVersion === version.version}
                                  aria-controls="prompt-diff-panel"
                                  aria-label={`查看 diff：v${version.version} 与 active v${
                                    activeVersion ?? detail.active_version
                                  } 的差异`}
                                >
                                  查看 <span lang="en">diff</span>
                                </button>
                              )}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="hint">
                  激活旧版本不会删除新版本；每次决策记录其使用的版本。恢复默认需确认；激活前{' '}
                  <span lang="en">active</span> 保持不变。
                </p>
              </section>

              <p
                role="status"
                aria-live="polite"
                className="visually-hidden"
                data-testid="prompt-diff-status"
              >
                {diffStatus}
              </p>

              <div id="prompt-diff-panel">
                {detail.versions.length <= 1 ? (
                  <p className="status-message">
                    只有一个内置版本；创建自定义版本后可在此比较差异。
                  </p>
                ) : diff.state === 'loading' ? (
                  <p className="status-message" data-testid="prompt-diff-loading">
                    加载 <span lang="en">diff</span>…
                  </p>
                ) : diff.state === 'error' ? (
                  <div className="status-message status-message--error" role="alert">
                    <span lang="en">Diff</span> 不可用：{diff.message ?? '未知错误'}
                  </div>
                ) : diff.state === 'ready' && diff.data !== null ? (
                  <PromptDiff
                    diff={diff.data.diff}
                    fromLabel={
                      <>
                        v{diff.data.from_version}
                        {diff.data.from_version === detail.active_version && (
                          <>
                            {' '}
                            <span lang="en">active</span>
                          </>
                        )}
                      </>
                    }
                    toLabel={
                      <>
                        v{diff.data.to_version}
                        {diff.data.to_version === detail.active_version && (
                          <>
                            {' '}
                            <span lang="en">active</span>
                          </>
                        )}
                      </>
                    }
                    fromText={`v${diff.data.from_version}${
                      diff.data.from_version === detail.active_version ? ' active' : ''
                    }`}
                    toText={`v${diff.data.to_version}${
                      diff.data.to_version === detail.active_version ? ' active' : ''
                    }`}
                  />
                ) : (
                  <p className="status-message">
                    选择一个版本查看与 <span lang="en">active</span> 的差异。
                  </p>
                )}
              </div>

              <p className="status-message" data-testid="prompt-history-visible">
                {history.statusMessage}
              </p>
            </>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={dialog !== null}
        title={
          dialog?.kind === 'restore' ? (
            <>
              恢复 <span lang="en">Prompt</span> 默认版本
            </>
          ) : (
            <>
              激活 <span lang="en">Prompt</span> 版本
            </>
          )
        }
        description={
          dialog?.kind === 'restore' ? (
            <>
              恢复默认会创建一个新的内置默认 <span lang="en">DRAFT</span> 版本并显示当前{' '}
              <span lang="en">active</span> 版本；历史版本全部保留。激活前{' '}
              <span lang="en">active</span> 保持不变。
            </>
          ) : (
            <>
              激活会切换下一次分析使用的版本；新版本不会删除，历史保持完整。确认后写入{' '}
              <span lang="en">audit</span>。
            </>
          )
        }
        rows={dialogRows}
        confirmLabel={dialog?.kind === 'restore' ? '恢复默认' : '激活'}
        idPrefix={dialog?.kind === 'restore' ? 'prompt-restore-confirm' : 'prompt-activate-confirm'}
        tableLabel="版本变更前后对比"
        fieldHeader="版本"
        beforeHeader="变更前"
        afterHeader="变更后"
        busy={history.saving}
        busyLabel={dialog?.kind === 'restore' ? '恢复默认中…' : '激活中…'}
        hint={
          dialog?.kind === 'restore' ? (
            <>
              恢复默认创建新的 <span lang="en">DRAFT</span>{' '}
              版本；需要再次显式激活才会生效。取消不会写入任何变更。
            </>
          ) : (
            '取消不会写入任何变更。'
          )
        }
        onCancel={history.cancelDialog}
        onConfirm={() => {
          void history.confirmDialog()
        }}
      />
    </section>
  )
}
