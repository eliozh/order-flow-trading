/**
 * F10b.1 required-variable checklist.
 *
 * The server is the authority on which variables are required; this panel only
 * reflects the draft against that list and names a missing variable verbatim.
 * It is informational, so the checklist is a keyboard-scrollable region rather
 * than a set of fake toggles.
 */

export interface PromptVariablePanelProps {
  requiredVariables: readonly string[]
  presentVariables: readonly string[]
  unknownVariables: readonly string[]
  malformed: boolean
  knowledgeOutsideTags: boolean
  readOnly: boolean
}

export function PromptVariablePanel({
  requiredVariables,
  presentVariables,
  unknownVariables,
  malformed,
  knowledgeOutsideTags,
  readOnly,
}: PromptVariablePanelProps) {
  return (
    <section className="panel" aria-labelledby="prompt-variables-heading">
      <div className="panel-head">
        <h2 id="prompt-variables-heading">必需变量</h2>
        <span className="hint">缺少任一变量会阻止保存</span>
      </div>
      <div
        className="table-wrap prompt-variable-wrap"
        role="region"
        aria-label="必需变量检查"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard-scrollable checklist
        tabIndex={0}
      >
        <ul className="prompt-variable-list">
          {requiredVariables.map((name) => {
            const present = presentVariables.includes(name)
            return (
              <li
                key={name}
                className={`prompt-variable-item${present ? '' : ' prompt-variable-missing'}`}
                data-testid={`prompt-variable-${name}`}
              >
                <span
                  className={`prompt-variable-status${present ? '' : ' prompt-variable-status--missing'}`}
                  aria-hidden="true"
                >
                  {present ? '✓' : '✕'}
                </span>
                <span className="mono">{`{{${name}}}`}</span>
                <small>{present ? '已存在 · 由代码注入；模板不得自行构造' : '缺少必需变量'}</small>
              </li>
            )
          })}
        </ul>
      </div>
      {unknownVariables.length > 0 && (
        <p className="field-error" data-testid="prompt-variable-unknown">
          未知变量：{unknownVariables.map((name) => `{{${name}}}`).join('、')}
        </p>
      )}
      {malformed && (
        <p className="field-error" data-testid="prompt-variable-malformed">
          占位符语法错误：请成对使用 {'{{'} 与 {'}}'}
        </p>
      )}
      {knowledgeOutsideTags && (
        <p className="field-error" data-testid="prompt-variable-knowledge">
          {'{{knowledge}}'} 必须位于 <span className="mono">&lt;knowledge&gt;</span> 分隔符内
        </p>
      )}
      <p className="hint">
        变量在渲染时由代码填充；预览已脱敏，不显示原始 <span lang="en">prompt secrets</span>。
        {readOnly ? ' 375px 下模板只读，请使用桌面端或 CLI。' : ''}
      </p>
    </section>
  )
}
