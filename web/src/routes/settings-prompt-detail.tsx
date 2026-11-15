import {
  PREVIEW_NOTICE,
  PromptEditor,
  renderRedactedPreview,
  variablesInPromptText,
} from '../components/prompt-editor'
import { PromptVariablePanel } from '../components/prompt-variable-panel'
import { SettingsSectionNav } from '../components/settings-section-nav'
import { type UsePromptEditorOptions, usePromptEditor } from '../hooks/use-prompt-editor'

export type SettingsPromptDetailPageProps = UsePromptEditorOptions

/** `/settings/prompts/:promptId` (F10b.1): read-only until "编辑". */
export function SettingsPromptDetailPage(props: SettingsPromptDetailPageProps) {
  const editor = usePromptEditor(props)
  const { load, readonly, active, role, draft, editing } = editor

  // The preview always reflects the ACTIVE version, never the unsaved draft or
  // any runtime data: it is the last rendered template, redacted and literal.
  const preview = active === null ? '' : renderRedactedPreview(active.body)
  const present = variablesInPromptText(draft)
  // Validation is editing-only: the hook returns null on the read-only view so
  // a server-accepted active body never shows a false aria-invalid/error.
  const issue = editor.issue

  return (
    <section
      className="system-page settings-page"
      aria-labelledby="settings-prompt-editor-page-heading"
      data-readonly={readonly}
    >
      <div className="page-head">
        <div>
          <p className="kicker">
            <span lang="en">Settings / prompts / {props.promptId}</span>
          </p>
          <h1 id="settings-prompt-editor-page-heading">
            <span lang="en">Prompt</span> 编辑器
          </h1>
          <p className="page-sub">
            只读直到「编辑」；必需变量高亮，<span lang="en">diff</span> 对照{' '}
            <span lang="en">active</span>；预览最后一次渲染（已脱敏）。
            保存创建新版本，永不原地编辑；既有决策继续绑定其原始版本。
          </p>
        </div>
      </div>

      {readonly && (
        <p className="readonly-note" role="note">
          移动端只读：模板编辑在 375px 下禁用；请在桌面端或 CLI 修改。预览与历史仍可查看。
        </p>
      )}

      <div className="settings-cols">
        <SettingsSectionNav active="prompts" />
        <div className="stack">
          <p
            role="status"
            className="visually-hidden"
            data-testid="settings-prompt-detail-load-status"
            aria-live="polite"
          >
            {load.state === 'loading' ? (
              load.detail === null ? (
                <>
                  加载 <span lang="en">Prompt</span> 模板…
                </>
              ) : (
                '刷新中…'
              )
            ) : (
              ''
            )}
          </p>

          {load.state === 'loading' && load.detail === null && (
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
                <button type="button" className="control-btn" onClick={editor.retry}>
                  重试
                </button>
              </div>
            </div>
          )}

          {load.state === 'ready' && active === null && (
            <div className="panel" role="alert">
              <div className="panel-head">
                <h2>
                  <span lang="en">active</span> 版本缺失
                </h2>
              </div>
              <p className="panel-foot">
                当前 <span lang="en">active</span>{' '}
                版本的内容不可用；不会以任何其他版本替代，也不会回放。
              </p>
              <div className="action-row">
                <button type="button" className="control-btn" onClick={editor.retry}>
                  重试
                </button>
              </div>
            </div>
          )}

          {/* Keep the editor mounted across the post-save refetch. Unmounting it
              would remove the re-focused 编辑 control and drop focus to <body>. */}
          {load.detail !== null && active !== null && role !== null && (
            <>
              {/* Always present so sibling indices never shift while a refetch
                  is in flight; the focused 编辑 control must survive the reload. */}
              <p
                className="status-message"
                role="status"
                data-testid="prompt-editor-refresh"
                hidden={load.state !== 'loading'}
              >
                刷新中…
              </p>

              <PromptEditor
                promptId={props.promptId}
                role={role}
                active={active}
                editing={editing}
                draft={draft}
                author={editor.author}
                reason={editor.reason}
                issue={issue}
                saving={editor.saving}
                readOnly={readonly}
                statusMessage={editor.statusMessage}
                liveMessage={editor.liveMessage}
                errorText={editor.errorText}
                errorSummaryRef={editor.errorSummaryRef}
                onChangeBody={editor.changeBody}
                onChangeAuthor={editor.changeAuthor}
                onChangeReason={editor.changeReason}
                onStartEdit={editor.startEdit}
                onDiscard={editor.discard}
                onSave={(activate) => {
                  void editor.save(activate)
                }}
              />

              <PromptVariablePanel
                requiredVariables={active.required_variables}
                presentVariables={present}
                unknownVariables={issue?.unknown ?? []}
                malformed={issue?.malformed ?? false}
                knowledgeOutsideTags={issue?.knowledgeOutsideTags ?? false}
                readOnly={readonly}
              />

              <section className="panel" aria-labelledby="prompt-preview-heading">
                <div className="panel-head">
                  <h2 id="prompt-preview-heading">
                    <span lang="en">Last rendered prompt（redacted）</span>
                  </h2>
                  <span className="chip chip--muted">
                    <span lang="en">active only</span>
                  </span>
                </div>
                <div
                  className="prompt-preview-wrap"
                  role="region"
                  aria-label="Last rendered prompt（已脱敏）"
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable preview
                  tabIndex={0}
                >
                  <pre className="prompt-preview" data-testid="prompt-preview">
                    {preview}
                  </pre>
                </div>
                <p className="hint">{PREVIEW_NOTICE}</p>
              </section>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
