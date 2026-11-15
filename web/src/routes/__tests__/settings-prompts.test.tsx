import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiMeta } from '../../api/status'
import {
  type PromptDetailData,
  type PromptDetailEnvelope,
  type PromptDiffData,
  type PromptDiffEnvelope,
  type PromptRole,
  type PromptSettingsData,
  type PromptSettingsEnvelope,
  type PromptVersionData,
  type PromptVersionEnvelope,
  PromptApiError,
} from '../../api/prompts'
import { diffLineTone } from '../../components/prompt-diff'
import { renderRedactedPreview, validatePromptText } from '../../components/prompt-editor'
import { SettingsPromptDetailPage } from '../settings-prompt-detail'
import { SettingsPromptHistoryPage } from '../settings-prompt-history'
import { SettingsPromptsPage } from '../settings-prompts'
import { resolveRoute } from '../route-match'

const meta: ApiMeta = {
  request_id: 'REQ-TEST',
  generated_at: '2026-11-06T10:00:00.000Z',
  duration_ms: 1,
  provenance: 'CALC',
  data_quality: 'COMPLETE',
  warnings: [],
  pagination: null,
  idempotent_replay: false,
}

const REQUIRED: Record<PromptRole, string[]> = {
  ANALYSIS: ['symbol', 'timeframe', 'metrics', 'schema', 'knowledge'],
  ENTRY_RECHECK: ['symbol', 'timeframe', 'trigger', 'schema', 'knowledge'],
  MANAGEMENT: ['symbol', 'timeframe', 'position', 'schema', 'knowledge'],
  REVIEW: ['symbol', 'timeframe', 'position', 'schema', 'knowledge'],
}

const VALID_ANALYSIS_BODY = `Symbol: {{symbol}}
Timeframe: {{timeframe}}
<market_data>{{metrics}}</market_data>
<knowledge>{{knowledge}}</knowledge>
CUSTOM MARKER
{{schema}}
`

const HOSTILE_ANALYSIS_BODY = `${VALID_ANALYSIS_BODY}<script>alert(1)</script>
<a href="javascript:alert(1)">click</a>
`

const ROLE_ORDER: PromptRole[] = ['ANALYSIS', 'ENTRY_RECHECK', 'MANAGEMENT', 'REVIEW']
const PROMPT_IDS: Record<PromptRole, string> = {
  ANALYSIS: 'analysis',
  ENTRY_RECHECK: 'entry_recheck',
  MANAGEMENT: 'management',
  REVIEW: 'review',
}
const ROLE_LABELS: Record<PromptRole, string> = {
  ANALYSIS: '市场分析 / Analysis',
  ENTRY_RECHECK: '入场复核 / Entry re-check',
  MANAGEMENT: '仓位管理 / Management',
  REVIEW: '复盘总结 / Review',
}

function settingsRow(
  role: PromptRole,
  overrides: Partial<PromptSettingsData['items'][number]> = {},
): PromptSettingsData['items'][number] {
  return {
    prompt_id: PROMPT_IDS[role],
    role,
    active_version: 1,
    author: 'system',
    updated_at: '2026-11-08T12:00:00.000Z',
    version_count: 1,
    latest_version: 1,
    required_variables: REQUIRED[role],
    is_builtin_default: true,
    ...overrides,
  }
}

function settingsData(overrides: Partial<PromptSettingsData> = {}): PromptSettingsData {
  return {
    items: ROLE_ORDER.map((role) => settingsRow(role)),
    total: 4,
    ...overrides,
  }
}

function settingsEnvelope(data: PromptSettingsData): PromptSettingsEnvelope {
  return { ok: true, data, error: null, meta }
}

function promptVersion(overrides: Partial<PromptVersionData> = {}): PromptVersionData {
  return {
    prompt_id: 'analysis',
    version: 1,
    status: 'ACTIVE',
    is_active: true,
    body: VALID_ANALYSIS_BODY,
    body_hash: 'a'.repeat(64),
    required_variables: REQUIRED.ANALYSIS,
    author: 'system',
    reason: 'built-in default',
    is_default: true,
    created_at: '2026-11-08T12:00:00.000Z',
    system_prompt_id: 'analysis:system',
    system_prompt_version: 1,
    system_prompt_hash: 'b'.repeat(64),
    knowledge_artifact_hash: null,
    ...overrides,
  }
}

function detailData(overrides: Partial<PromptDetailData> = {}): PromptDetailData {
  return {
    prompt_id: 'analysis',
    role: 'ANALYSIS',
    kind: 'USER',
    active_version: 1,
    created_at: '2026-11-08T12:00:00.000Z',
    updated_at: '2026-11-08T12:00:00.000Z',
    versions: [promptVersion()],
    ...overrides,
  }
}

function detailEnvelope(data: PromptDetailData): PromptDetailEnvelope {
  return { ok: true, data, error: null, meta }
}

function diffData(fromVersion: number, toVersion: number, diff: string): PromptDiffData {
  return { prompt_id: 'analysis', from_version: fromVersion, to_version: toVersion, diff }
}

function diffEnvelope(data: PromptDiffData): PromptDiffEnvelope {
  return { ok: true, data, error: null, meta }
}

function versionEnvelope(data: PromptVersionData): PromptVersionEnvelope {
  return { ok: true, data, error: null, meta }
}

/** A promise the test resolves explicitly, so a loading state must commit. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

// -- pure helpers -----------------------------------------------------------

describe('prompt editor helpers', () => {
  it('accepts the built-in analysis body', () => {
    const issue = validatePromptText(VALID_ANALYSIS_BODY, REQUIRED.ANALYSIS, 'ANALYSIS')
    expect(issue.valid).toBe(true)
    expect(issue.missing).toEqual([])
    expect(issue.unknown).toEqual([])
  })

  it('names a missing required variable verbatim', () => {
    const issue = validatePromptText(
      'Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n<knowledge>{{knowledge}}</knowledge>\n{{schema}}',
      REQUIRED.ANALYSIS,
      'ANALYSIS',
    )
    expect(issue.valid).toBe(false)
    expect(issue.missing).toEqual(['metrics'])
  })

  it('flags unknown, malformed, and unwrapped knowledge placeholders', () => {
    const unknown = validatePromptText(
      `${VALID_ANALYSIS_BODY}{{hidden}}`,
      REQUIRED.ANALYSIS,
      'ANALYSIS',
    )
    expect(unknown.unknown).toEqual(['hidden'])

    const malformed = validatePromptText(
      `${VALID_ANALYSIS_BODY}{{oops`,
      REQUIRED.ANALYSIS,
      'ANALYSIS',
    )
    expect(malformed.malformed).toBe(true)
    expect(malformed.valid).toBe(false)

    const unwrapped = validatePromptText(
      'Symbol: {{symbol}}\nTimeframe: {{timeframe}}\n<market_data>{{metrics}}</market_data>\n{{knowledge}}\n{{schema}}',
      REQUIRED.ANALYSIS,
      'ANALYSIS',
    )
    expect(unwrapped.knowledgeOutsideTags).toBe(true)
    expect(unwrapped.valid).toBe(false)
  })

  it('redacts placeholders and keeps hostile text literal', () => {
    const preview = renderRedactedPreview(HOSTILE_ANALYSIS_BODY)
    expect(preview).toContain('[redacted: symbol]')
    expect(preview).toContain('[redacted: metrics]')
    expect(preview).toContain('<script>alert(1)</script>')
    expect(preview).toContain('<a href="javascript:alert(1)">click</a>')
    expect(preview).not.toContain('{{symbol}}')
  })

  it('classifies unified diff lines by marker', () => {
    expect(diffLineTone('@@ rule 2 @@')).toBe('hunk')
    expect(diffLineTone('+ added')).toBe('added')
    expect(diffLineTone('- removed')).toBe('removed')
    expect(diffLineTone(' context')).toBe('context')
    expect(diffLineTone('--- a')).toBe('context')
    expect(diffLineTone('+++ b')).toBe('context')
  })
})

// -- list -------------------------------------------------------------------

async function renderList(
  options: {
    fetch?: (signal?: AbortSignal) => Promise<PromptSettingsEnvelope>
    isCompact?: boolean
  } = {},
) {
  const fetch = options.fetch ?? (async () => settingsEnvelope(settingsData()))
  render(<SettingsPromptsPage fetchPromptSettingsFn={fetch} isCompact={options.isCompact} />)
  await screen.findByTestId('prompt-row-analysis')
  return { fetch }
}

describe('SettingsPromptsPage', () => {
  it('lists the four roles with active version, author, time, and built-in default state', async () => {
    await renderList()

    expect(screen.getByTestId('prompts-builtin-banner')).toHaveTextContent('使用内置默认 v1')
    for (const role of ROLE_ORDER) {
      const row = screen.getByTestId(`prompt-row-${PROMPT_IDS[role]}`)
      expect(row).toHaveTextContent(ROLE_LABELS[role])
      expect(within(row).getByTestId(`prompt-active-${PROMPT_IDS[role]}`)).toHaveTextContent(
        'v1 active',
      )
      expect(within(row).getByTestId(`prompt-builtin-${PROMPT_IDS[role]}`)).toHaveTextContent(
        'built-in default v1',
      )
      expect(within(row).getByTestId(`prompt-author-${PROMPT_IDS[role]}`)).toHaveTextContent(
        'system',
      )
      expect(within(row).getByTestId(`prompt-updated-${PROMPT_IDS[role]}`)).toHaveTextContent(
        '2026-11-08T12:00:00.000Z',
      )
    }
    expect(screen.getByRole('link', { name: 'analysis' })).toHaveAttribute(
      'href',
      '/settings/prompts/analysis',
    )
    expect(screen.getAllByRole('link', { name: '历史' })[0]).toHaveAttribute(
      'href',
      '/settings/prompts/analysis/history',
    )
  })

  it('shows a custom active version instead of the built-in marker', async () => {
    const item = settingsRow('ANALYSIS', {
      active_version: 4,
      author: 'operator',
      version_count: 4,
      latest_version: 4,
      is_builtin_default: false,
    })
    await renderList({
      fetch: async () =>
        settingsEnvelope({ items: [item, ...settingsData().items.slice(1)], total: 4 }),
    })

    const row = screen.getByTestId('prompt-row-analysis')
    expect(within(row).getByTestId('prompt-active-analysis')).toHaveTextContent('v4 active')
    expect(within(row).getByTestId('prompt-author-analysis')).toHaveTextContent('operator')
    expect(within(row).getByTestId('prompt-custom-analysis')).toHaveTextContent(
      'custom · 4 versions',
    )
    expect(screen.queryByTestId('prompt-builtin-analysis')).toBeNull()
    expect(screen.queryByTestId('prompts-builtin-banner')).toBeNull()
  })

  it('shows the empty state when no template rows exist', async () => {
    render(
      <SettingsPromptsPage
        fetchPromptSettingsFn={async () => settingsEnvelope({ items: [], total: 0 })}
      />,
    )

    expect(await screen.findByText('尚未创建自定义模板')).toBeInTheDocument()
  })

  it('shows a load error and recovers on retry', async () => {
    const fetch = vi
      .fn<(signal?: AbortSignal) => Promise<PromptSettingsEnvelope>>()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(settingsEnvelope(settingsData()))
    render(<SettingsPromptsPage fetchPromptSettingsFn={fetch} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Prompt 模板不可用')
    expect(alert).toHaveTextContent('network down')

    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByTestId('prompt-row-analysis')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('is read-only at the compact override while keeping reads available', async () => {
    await renderList({ isCompact: true })

    expect(screen.getByRole('note')).toHaveTextContent(/桌面端或 CLI/)
    expect(screen.getByTestId('prompt-row-analysis')).toBeInTheDocument()
  })
})

// -- editor -----------------------------------------------------------------

async function renderEditor(
  options: {
    detail?: PromptDetailData
    fetch?: (promptId: string, signal?: AbortSignal) => Promise<PromptDetailEnvelope>
    create?: (
      promptId: string,
      input: { body: string; author?: string; reason?: string; activate?: boolean },
      key: string,
    ) => Promise<PromptVersionEnvelope>
    isCompact?: boolean
    clear?: (promptId: string) => void
    rotate?: (promptId: string) => string
  } = {},
) {
  const detail = options.detail ?? detailData()
  const fetch = options.fetch ?? (async () => detailEnvelope(detail))
  const create =
    options.create ??
    vi.fn(async () =>
      versionEnvelope(
        promptVersion({
          version: 2,
          status: 'DRAFT',
          is_active: false,
          is_default: false,
          author: 'web',
        }),
      ),
    )
  const clear = options.clear ?? vi.fn()
  const rotate = options.rotate ?? vi.fn(() => 'rotated-prompt-key')
  render(
    <SettingsPromptDetailPage
      promptId="analysis"
      fetchPromptDetailFn={fetch}
      createPromptVersionFn={create}
      promptVersionKeyFn={() => 'prompt-key'}
      rotatePromptVersionKeyFn={rotate}
      clearPromptVersionKeyFn={clear}
      isCompact={options.isCompact}
    />,
  )
  await screen.findByRole('textbox', { name: /模板正文/ })
  return { fetch, create, clear, rotate }
}

function editorTextarea(): HTMLTextAreaElement {
  return screen.getByRole('textbox', { name: /模板正文/ }) as HTMLTextAreaElement
}

describe('SettingsPromptDetailPage', () => {
  it('is read-only until 编辑 and lists the required variables', async () => {
    const { create } = await renderEditor()

    expect(editorTextarea()).toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: '保存为新版本' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument()
    for (const name of REQUIRED.ANALYSIS) {
      const item = screen.getByTestId(`prompt-variable-${name}`)
      expect(item).toHaveTextContent(`{{${name}}}`)
      expect(item).toHaveTextContent('已存在')
      expect(within(item).queryByRole('checkbox')).toBeNull()
    }
    expect(screen.getByRole('region', { name: '必需变量检查' })).toHaveAttribute('tabindex', '0')
    expect(create).not.toHaveBeenCalled()
  })

  it('does not flag a server-accepted read-only body as invalid', async () => {
    const accepted = 'A server-accepted body with no placeholders at all.'
    await renderEditor({
      detail: detailData({ versions: [promptVersion({ body: accepted })] }),
    })

    const textarea = screen.getByRole('textbox', { name: /模板正文/ })
    expect(textarea).not.toHaveAttribute('aria-invalid')
    expect(screen.getByTestId('prompt-editor-issues')).toBeEmptyDOMElement()
  })

  it('blocks a missing required variable inline and writes nothing', async () => {
    const { create } = await renderEditor()

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    const textarea = editorTextarea()
    expect(textarea).not.toHaveAttribute('readonly')
    fireEvent.change(textarea, {
      target: { value: VALID_ANALYSIS_BODY.replace('{{metrics}}', 'metrics') },
    })

    expect(screen.getByText('缺少必需变量 {{metrics}}')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法保存')
    expect(alert).toHaveTextContent('缺少必需变量 {{metrics}}')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(create).not.toHaveBeenCalled()
  })

  it('blocks malformed and unknown placeholders with no write', async () => {
    const { create } = await renderEditor()

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.change(editorTextarea(), { target: { value: `${VALID_ANALYSIS_BODY}{{oops` } })
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))
    const malformed = await screen.findByRole('alert')
    expect(malformed).toHaveTextContent('占位符语法错误')
    expect(create).not.toHaveBeenCalled()

    fireEvent.change(editorTextarea(), {
      target: { value: `${VALID_ANALYSIS_BODY}{{hidden}}` },
    })
    expect(screen.getByText('未知变量 {{hidden}}')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))
    const unknown = await screen.findByRole('alert')
    expect(unknown).toHaveTextContent('未知变量 {{hidden}}')
    expect(create).not.toHaveBeenCalled()
  })

  it('saves a NEW draft version and announces it without activating', async () => {
    const created = promptVersion({
      version: 2,
      status: 'DRAFT',
      is_active: false,
      is_default: false,
      author: 'web',
    })
    const create = vi.fn(async () => versionEnvelope(created))
    const clear = vi.fn()
    await renderEditor({ create, clear })

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.change(editorTextarea(), { target: { value: `${VALID_ANALYSIS_BODY}NEW LINE\n` } })
    fireEvent.change(screen.getByRole('textbox', { name: '作者' }), {
      target: { value: 'operator' },
    })
    fireEvent.change(screen.getByRole('textbox', { name: '变更原因' }), {
      target: { value: 'wording' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))

    await waitFor(() =>
      expect(screen.getByTestId('prompt-editor-visible')).toHaveTextContent('已创建 v2'),
    )
    expect(create).toHaveBeenCalledTimes(1)
    expect(create).toHaveBeenCalledWith(
      'analysis',
      {
        body: `${VALID_ANALYSIS_BODY}NEW LINE\n`,
        author: 'operator',
        reason: 'wording',
        activate: false,
      },
      'prompt-key',
    )
    expect(clear).toHaveBeenCalledWith('analysis')
    const visible = screen.getByTestId('prompt-editor-visible')
    expect(visible).toHaveTextContent('DRAFT')
    expect(visible).toHaveTextContent('激活前下一次分析继续使用 active v1')
    expect(visible).toHaveTextContent('既有决策仍绑定其原始版本')
    expect(screen.getByTestId('prompt-editor-status')).toHaveTextContent('已创建 v2')
  })

  it('moves focus to the 编辑 control after save and after discard', async () => {
    await renderEditor()

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.click(screen.getByRole('button', { name: '丢弃' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '编辑' })).toHaveFocus())

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))
    await waitFor(() =>
      expect(screen.getByTestId('prompt-editor-visible')).toHaveTextContent('已创建 v2'),
    )
    await waitFor(() => expect(screen.getByRole('button', { name: '编辑' })).toHaveFocus())
  })

  it('keeps focus on the 编辑 control across a delayed save refetch', async () => {
    const pending = deferred<PromptDetailEnvelope>()
    let calls = 0
    const fetch = vi.fn(() => {
      calls += 1
      return calls === 1 ? Promise.resolve(detailEnvelope(detailData())) : pending.promise
    })
    await renderEditor({ fetch })
    const formBefore = screen.getByRole('form', { name: '编辑 Prompt 模板' })

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))

    // The refetch stays in flight, so the loading state commits while the
    // editor (and the re-focused 编辑 control) must remain mounted.
    await waitFor(() => expect(screen.getByTestId('prompt-editor-refresh')).toBeVisible())
    // The same editor DOM subtree survives the loading commit; a remount here
    // would remove the focused control and drop focus to <body>.
    expect(screen.getByRole('form', { name: '编辑 Prompt 模板' })).toBe(formBefore)
    expect(document.activeElement).not.toBe(document.body)
    await waitFor(() => expect(screen.getByRole('button', { name: '编辑' })).toHaveFocus())

    pending.resolve(detailEnvelope(detailData()))
    await waitFor(() => expect(screen.getByTestId('prompt-editor-refresh')).not.toBeVisible())
    expect(screen.getByRole('button', { name: '编辑' })).toHaveFocus()
  })

  it('rotates the pending save key on IDEMPOTENCY_CONFLICT and does not clear it', async () => {
    const create = vi.fn(async () => {
      throw new PromptApiError('key already used', 'IDEMPOTENCY_CONFLICT', { httpStatus: 409 })
    })
    const { clear, rotate } = await renderEditor({ create })

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))

    const alert = await screen.findByTestId('prompt-editor-error')
    expect(alert).toHaveTextContent('请求冲突')
    expect(rotate).toHaveBeenCalledWith('analysis')
    expect(clear).not.toHaveBeenCalled()
  })

  it('saves and activates through one explicit confirmed choice', async () => {
    const created = promptVersion({
      version: 2,
      status: 'ACTIVE',
      is_active: true,
      is_default: false,
      author: 'web',
    })
    const create = vi.fn(async () => versionEnvelope(created))
    await renderEditor({ create })

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.change(editorTextarea(), { target: { value: `${VALID_ANALYSIS_BODY}ACTIVE\n` } })
    fireEvent.click(screen.getByRole('button', { name: '保存并激活' }))

    await waitFor(() =>
      expect(screen.getByTestId('prompt-editor-visible')).toHaveTextContent('已创建并激活 v2'),
    )
    expect(create).toHaveBeenCalledWith(
      'analysis',
      expect.objectContaining({ activate: true }),
      'prompt-key',
    )
    expect(screen.getByTestId('prompt-editor-visible')).toHaveTextContent('audit 已写入')
  })

  it('renders a hostile active body literally in the redacted preview', async () => {
    await renderEditor({
      detail: detailData({ versions: [promptVersion({ body: HOSTILE_ANALYSIS_BODY })] }),
    })

    const preview = screen.getByTestId('prompt-preview')
    expect(preview.textContent).toContain('<script>alert(1)</script>')
    expect(preview.textContent).toContain('<a href="javascript:alert(1)">click</a>')
    expect(preview.textContent).toContain('[redacted: symbol]')
    expect(preview.querySelector('script')).toBeNull()
    expect(preview.querySelector('a')).toBeNull()
  })

  it('surfaces a definitive save refusal, retires the key, and focuses the summary', async () => {
    const create = vi.fn(async () => {
      throw new PromptApiError(
        'missing required variable {{metrics}}',
        'PROMPT_VALIDATION_FAILED',
        {
          httpStatus: 422,
          details: [{ field: 'metrics', code: 'PROMPT_VALIDATION_FAILED' }],
        },
      )
    })
    const clear = vi.fn()
    await renderEditor({ create, clear })

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('missing required variable {{metrics}}')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(clear).toHaveBeenCalledWith('analysis')
  })

  it('keeps the key and reports an unknown outcome for a transport failure', async () => {
    const create = vi.fn(async () => {
      throw new Error('socket closed')
    })
    const clear = vi.fn()
    await renderEditor({ create, clear })

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    fireEvent.click(screen.getByRole('button', { name: '保存为新版本' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).toHaveTextContent('同一 Idempotency-Key')
    expect(clear).not.toHaveBeenCalled()
  })

  it('is read-only at 375 with a desktop/CLI instruction', async () => {
    await renderEditor({ isCompact: true })

    expect(screen.getByRole('note')).toHaveTextContent(/桌面端或 CLI/)
    expect(editorTextarea()).toBeDisabled()
    expect(screen.getByRole('button', { name: '编辑' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存为新版本' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存并激活' })).toBeDisabled()
  })

  it('shows a load error and recovers on retry', async () => {
    const fetch = vi
      .fn<(promptId: string, signal?: AbortSignal) => Promise<PromptDetailEnvelope>>()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(detailEnvelope(detailData()))
    render(<SettingsPromptDetailPage promptId="analysis" fetchPromptDetailFn={fetch} />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Prompt 模板不可用')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByRole('textbox', { name: /模板正文/ })
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})

// -- history ----------------------------------------------------------------

const DIFF_TEXT = `--- analysis v1
+++ analysis v2
@@ -1,3 +1,3 @@
- old wording
+ new wording
`

function historyDetail(overrides: Partial<PromptDetailData> = {}): PromptDetailData {
  return detailData({
    active_version: 2,
    updated_at: '2026-11-09T12:00:00.000Z',
    versions: [
      promptVersion({ version: 1, is_active: false, status: 'SUPERSEDED' }),
      promptVersion({
        version: 2,
        is_active: true,
        status: 'ACTIVE',
        author: 'operator',
        reason: 'tighten wording',
        created_at: '2026-11-09T12:00:00.000Z',
        is_default: false,
      }),
    ],
    ...overrides,
  })
}

async function renderHistory(
  options: {
    detail?: PromptDetailData
    fetchDetail?: (promptId: string, signal?: AbortSignal) => Promise<PromptDetailEnvelope>
    fetchDiff?: (
      promptId: string,
      from: number,
      to: number,
      signal?: AbortSignal,
    ) => Promise<PromptDiffEnvelope>
    activate?: (
      promptId: string,
      version: number,
      key: string,
      opts?: { actor?: string; reason?: string; signal?: AbortSignal },
    ) => Promise<PromptVersionEnvelope>
    restore?: (
      promptId: string,
      key: string,
      opts?: { author?: string; reason?: string; signal?: AbortSignal },
    ) => Promise<PromptVersionEnvelope>
    isCompact?: boolean
    clearActivate?: (promptId: string) => void
    rotateActivate?: (promptId: string) => string
    clearRestore?: (promptId: string) => void
    rotateRestore?: (promptId: string) => string
  } = {},
) {
  const detail = options.detail ?? historyDetail()
  const fetchDetail = options.fetchDetail ?? vi.fn(async () => detailEnvelope(detail))
  const fetchDiff = options.fetchDiff ?? vi.fn(async () => diffEnvelope(diffData(1, 2, DIFF_TEXT)))
  const activate =
    options.activate ??
    vi.fn(async () =>
      versionEnvelope(promptVersion({ version: 1, status: 'ACTIVE', is_active: true })),
    )
  const restore =
    options.restore ??
    vi.fn(async () =>
      versionEnvelope(
        promptVersion({
          version: 3,
          status: 'DRAFT',
          is_active: false,
          is_default: true,
          author: 'web',
        }),
      ),
    )
  const clearActivate = options.clearActivate ?? vi.fn()
  const clearRestore = options.clearRestore ?? vi.fn()
  const rotateActivate = options.rotateActivate ?? vi.fn(() => 'rotated-activate-key')
  const rotateRestore = options.rotateRestore ?? vi.fn(() => 'rotated-restore-key')
  render(
    <SettingsPromptHistoryPage
      promptId="analysis"
      fetchPromptDetailFn={fetchDetail}
      fetchPromptDiffFn={fetchDiff}
      activatePromptVersionFn={activate}
      restorePromptDefaultFn={restore}
      promptActivationKeyFn={() => 'prompt-activate-key'}
      rotatePromptActivationKeyFn={rotateActivate}
      clearPromptActivationKeyFn={clearActivate}
      promptRestoreKeyFn={() => 'prompt-restore-key'}
      rotatePromptRestoreKeyFn={rotateRestore}
      clearPromptRestoreKeyFn={clearRestore}
      isCompact={options.isCompact}
    />,
  )
  await screen.findByRole('region', { name: 'Prompt 版本历史' })
  return {
    fetchDetail,
    fetchDiff,
    activate,
    restore,
    clearActivate,
    clearRestore,
    rotateActivate,
    rotateRestore,
  }
}

describe('SettingsPromptHistoryPage', () => {
  it('shows version, author, created, reason, status, and the active row', async () => {
    await renderHistory()

    const activeRow = screen.getByTestId('prompt-version-2')
    expect(activeRow).toHaveTextContent('v2')
    expect(activeRow).toHaveTextContent('operator')
    expect(activeRow).toHaveTextContent('2026-11-09T12:00:00.000Z')
    expect(activeRow).toHaveTextContent('tighten wording')
    expect(within(activeRow).getByText('ACTIVE')).toBeInTheDocument()
    expect(within(activeRow).getByText('active')).toBeInTheDocument()
    expect(within(activeRow).queryByRole('button', { name: /激活 v2/ })).toBeNull()

    const archived = screen.getByTestId('prompt-version-1')
    expect(within(archived).getByText('SUPERSEDED')).toBeInTheDocument()
    // The state chip and the seeded reason both read "built-in default".
    expect(within(archived).getAllByText('built-in default').length).toBeGreaterThan(0)
    expect(within(archived).getByRole('button', { name: '激活 v1' })).toBeInTheDocument()
  })

  it('renders the unified diff as literal text from the selected version to active', async () => {
    const { fetchDiff } = await renderHistory()

    const body = await screen.findByTestId('prompt-diff-body')
    expect(fetchDiff).toHaveBeenCalledWith('analysis', 1, 2, expect.anything())
    expect(body.textContent).toContain('- old wording')
    expect(body.textContent).toContain('+ new wording')
    expect(body.querySelector('a')).toBeNull()
  })

  it('renders a hostile diff literally without executing links or markup', async () => {
    const hostile = `@@ -1 +1 @@
-<script>alert(1)</script>
+<a href="javascript:alert(2)">x</a>
`
    await renderHistory({
      fetchDiff: async () => diffEnvelope(diffData(1, 2, hostile)),
    })

    const body = await screen.findByTestId('prompt-diff-body')
    expect(body.textContent).toContain('<script>alert(1)</script>')
    expect(body.textContent).toContain('<a href="javascript:alert(2)">x</a>')
    expect(body.querySelector('script')).toBeNull()
    expect(body.querySelector('a')).toBeNull()
  })

  it('activates an old version only after explicit confirmation', async () => {
    const activate = vi.fn(async () =>
      versionEnvelope(promptVersion({ version: 1, status: 'ACTIVE', is_active: true })),
    )
    const clearActivate = vi.fn()
    await renderHistory({ activate, clearActivate })

    fireEvent.click(screen.getByRole('button', { name: '激活 v1' }))
    const dialog = await screen.findByRole('dialog', { name: '激活 Prompt 版本' })
    expect(within(dialog).getByRole('table', { name: '版本变更前后对比' })).toBeInTheDocument()
    expect(within(dialog).getByRole('columnheader', { name: '版本' })).toBeInTheDocument()
    expect(within(dialog).getByRole('columnheader', { name: '变更前' })).toBeInTheDocument()
    expect(within(dialog).getByRole('columnheader', { name: '变更后' })).toBeInTheDocument()
    expect(dialog).toHaveTextContent('v1')
    expect(activate).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: '激活' }))

    await waitFor(() =>
      expect(screen.getByTestId('prompt-history-visible')).toHaveTextContent('已激活 v1'),
    )
    expect(activate).toHaveBeenCalledWith('analysis', 1, 'prompt-activate-key', {
      actor: 'web',
    })
    expect(clearActivate).toHaveBeenCalledWith('analysis')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('prompt-history-visible')).toHaveTextContent('audit 已写入')
    // The invoker button is replaced by the "当前" span; focus lands on the
    // stable history heading instead of dropping to <body>.
    await waitFor(() =>
      expect(screen.getByRole('heading', { level: 2, name: /版本历史/ })).toHaveFocus(),
    )
  })

  it('restore shows the replaced active version and creates a new version on confirm', async () => {
    const restore = vi.fn(async () =>
      versionEnvelope(
        promptVersion({ version: 3, status: 'DRAFT', is_active: false, is_default: true }),
      ),
    )
    await renderHistory({ restore })

    fireEvent.click(screen.getByRole('button', { name: '恢复内置默认' }))
    const dialog = await screen.findByRole('dialog', { name: '恢复 Prompt 默认版本' })
    expect(dialog).toHaveTextContent('当前 active 版本（被替换）')
    expect(dialog).toHaveTextContent('v2')
    expect(dialog).toHaveTextContent('新的 built-in default 版本（新建，DRAFT）')
    expect(restore).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: '恢复默认' }))

    await waitFor(() =>
      expect(screen.getByTestId('prompt-history-visible')).toHaveTextContent('已创建 v3'),
    )
    expect(restore).toHaveBeenCalledWith('analysis', 'prompt-restore-key', {
      author: 'web',
      reason: 'restore default',
    })
    const visible = screen.getByTestId('prompt-history-visible')
    expect(visible).toHaveTextContent('将替换 active v2')
    expect(visible).toHaveTextContent('激活 v3 前 active 仍为 v2')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('clears the restore key after a successful restore', async () => {
    const restore = vi.fn(async () =>
      versionEnvelope(
        promptVersion({ version: 3, status: 'DRAFT', is_active: false, is_default: true }),
      ),
    )
    const clearRestore = vi.fn()
    await renderHistory({ restore, clearRestore })

    fireEvent.click(screen.getByRole('button', { name: '恢复内置默认' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '恢复 Prompt 默认版本' })).getByRole(
        'button',
        { name: '恢复默认' },
      ),
    )

    await waitFor(() =>
      expect(screen.getByTestId('prompt-history-visible')).toHaveTextContent('已创建 v3'),
    )
    expect(clearRestore).toHaveBeenCalledWith('analysis')
  })

  it('clears the restore key on a definitive refusal', async () => {
    const restore = vi.fn(async () => {
      throw new PromptApiError('restore refused', 'CONFLICT', { httpStatus: 409 })
    })
    const clearRestore = vi.fn()
    const rotateRestore = vi.fn(() => 'rotated-restore-key')
    await renderHistory({ restore, clearRestore, rotateRestore })

    fireEvent.click(screen.getByRole('button', { name: '恢复内置默认' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '恢复 Prompt 默认版本' })).getByRole(
        'button',
        { name: '恢复默认' },
      ),
    )

    const alert = await screen.findByTestId('prompt-history-error')
    expect(alert).toHaveTextContent('restore refused')
    expect(clearRestore).toHaveBeenCalledWith('analysis')
    expect(rotateRestore).not.toHaveBeenCalled()
  })

  it('keeps focus on the history heading across a delayed activation refetch', async () => {
    const pending = deferred<PromptDetailEnvelope>()
    let calls = 0
    const fetchDetail = vi.fn(() => {
      calls += 1
      return calls === 1 ? Promise.resolve(detailEnvelope(historyDetail())) : pending.promise
    })
    const activate = vi.fn(async () =>
      versionEnvelope(promptVersion({ version: 1, status: 'ACTIVE', is_active: true })),
    )
    await renderHistory({ fetchDetail, activate })
    const headingBefore = screen.getByRole('heading', { level: 2, name: /版本历史/ })

    fireEvent.click(screen.getByRole('button', { name: '激活 v1' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '激活 Prompt 版本' })).getByRole('button', {
        name: '激活',
      }),
    )

    // The refetch stays in flight: the loading state commits, but the panel and
    // its focused heading must survive it instead of dropping to <body>.
    await waitFor(() => expect(screen.getByTestId('prompt-history-refresh')).toBeVisible())
    // The heading DOM node is preserved across the loading commit (a remount
    // would have removed the focused node).
    expect(screen.getByRole('heading', { level: 2, name: /版本历史/ })).toBe(headingBefore)
    expect(document.activeElement).not.toBe(document.body)
    await waitFor(() => expect(headingBefore).toHaveFocus())

    pending.resolve(detailEnvelope(historyDetail()))
    await waitFor(() => expect(screen.getByTestId('prompt-history-refresh')).not.toBeVisible())
    expect(screen.getByRole('heading', { level: 2, name: /版本历史/ })).toHaveFocus()
  })

  it('cancels restore with Escape and writes nothing', async () => {
    const restore = vi.fn(async () => versionEnvelope(promptVersion({ version: 3 })))
    await renderHistory({ restore })

    const invoker = screen.getByRole('button', { name: '恢复内置默认' })
    invoker.focus()
    fireEvent.click(invoker)
    const dialog = await screen.findByRole('dialog', { name: '恢复 Prompt 默认版本' })
    expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Cancel keeps the invoker alive, so focus returns to it.
    await waitFor(() => expect(invoker).toHaveFocus())
    expect(restore).not.toHaveBeenCalled()
    expect(screen.getByTestId('prompt-history-visible')).toHaveTextContent('未写入任何变更')
  })

  it('disables replay for a missing active version without substituting another', async () => {
    await renderHistory({
      detail: historyDetail({
        active_version: 5,
        versions: [promptVersion({ version: 1, is_active: false })],
      }),
      fetchDiff: async () => diffEnvelope(diffData(1, 5, '')),
    })

    const missing = screen.getByTestId('prompt-version-missing')
    expect(missing).toHaveTextContent('missing')
    expect(within(missing).getByRole('button', { name: '不可回放' })).toBeDisabled()
    expect(missing).toHaveTextContent('回放会伪造输入')
  })

  it('is read-only at 375 and keeps history and diff readable', async () => {
    await renderHistory({ isCompact: true })

    expect(screen.getByRole('note')).toHaveTextContent(/桌面端或 CLI/)
    expect(screen.getByRole('button', { name: '恢复内置默认' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '激活 v1' })).toBeDisabled()
    expect(screen.getByTestId('prompt-version-2')).toBeInTheDocument()
  })

  it('reports an activate refusal, retires the key, and focuses the error summary', async () => {
    const activate = vi.fn(async () => {
      throw new PromptApiError('version missing', 'PROMPT_VERSION_MISSING', { httpStatus: 409 })
    })
    const { clearActivate, rotateActivate } = await renderHistory({ activate })

    fireEvent.click(screen.getByRole('button', { name: '激活 v1' }))
    const dialog = await screen.findByRole('dialog', { name: '激活 Prompt 版本' })
    fireEvent.click(within(dialog).getByRole('button', { name: '激活' }))

    const alert = await screen.findByTestId('prompt-history-error')
    expect(alert).toHaveTextContent('错误：')
    expect(alert).toHaveTextContent('version missing')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(screen.queryByRole('dialog')).toBeNull()
    // A definitive refusal wrote nothing: retire the pending key.
    expect(clearActivate).toHaveBeenCalledWith('analysis')
    expect(rotateActivate).not.toHaveBeenCalled()
  })

  it('keeps the pending key on an unknown activate outcome and replays it', async () => {
    const activate = vi
      .fn<
        (
          promptId: string,
          version: number,
          key: string,
          opts?: { actor?: string; reason?: string; signal?: AbortSignal },
        ) => Promise<PromptVersionEnvelope>
      >()
      .mockRejectedValueOnce(new Error('socket closed'))
      .mockResolvedValueOnce(
        versionEnvelope(promptVersion({ version: 1, status: 'ACTIVE', is_active: true })),
      )
    const { clearActivate, rotateActivate } = await renderHistory({ activate })

    fireEvent.click(screen.getByRole('button', { name: '激活 v1' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '激活 Prompt 版本' })).getByRole('button', {
        name: '激活',
      }),
    )

    const alert = await screen.findByTestId('prompt-history-error')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).toHaveTextContent('同一 Idempotency-Key')
    expect(clearActivate).not.toHaveBeenCalled()
    expect(rotateActivate).not.toHaveBeenCalled()
    expect(activate).toHaveBeenNthCalledWith(1, 'analysis', 1, 'prompt-activate-key', {
      actor: 'web',
    })

    // A retry after the ambiguous outcome replays the SAME pending key.
    fireEvent.click(screen.getByRole('button', { name: '激活 v1' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '激活 Prompt 版本' })).getByRole('button', {
        name: '激活',
      }),
    )

    await waitFor(() =>
      expect(screen.getByTestId('prompt-history-visible')).toHaveTextContent('已激活 v1'),
    )
    expect(activate).toHaveBeenNthCalledWith(2, 'analysis', 1, 'prompt-activate-key', {
      actor: 'web',
    })
    expect(clearActivate).toHaveBeenCalledWith('analysis')
  })

  it('rotates the pending key only on IDEMPOTENCY_CONFLICT', async () => {
    const activate = vi.fn(async () => {
      throw new PromptApiError('key already used', 'IDEMPOTENCY_CONFLICT', {
        httpStatus: 409,
      })
    })
    const { clearActivate, rotateActivate } = await renderHistory({ activate })

    fireEvent.click(screen.getByRole('button', { name: '激活 v1' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '激活 Prompt 版本' })).getByRole('button', {
        name: '激活',
      }),
    )

    const alert = await screen.findByTestId('prompt-history-error')
    expect(alert).toHaveTextContent('请求冲突')
    expect(rotateActivate).toHaveBeenCalledWith('analysis')
    expect(clearActivate).not.toHaveBeenCalled()
  })

  it('keeps the pending restore key on an unknown outcome', async () => {
    const restore = vi.fn(async () => {
      throw new Error('socket closed')
    })
    const { clearRestore, rotateRestore } = await renderHistory({ restore })

    fireEvent.click(screen.getByRole('button', { name: '恢复内置默认' }))
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: '恢复 Prompt 默认版本' })).getByRole(
        'button',
        { name: '恢复默认' },
      ),
    )

    const alert = await screen.findByTestId('prompt-history-error')
    expect(alert).toHaveTextContent('结果未知')
    expect(alert).toHaveTextContent('同一 Idempotency-Key')
    expect(restore).toHaveBeenCalledWith('analysis', 'prompt-restore-key', {
      author: 'web',
      reason: 'restore default',
    })
    expect(clearRestore).not.toHaveBeenCalled()
    expect(rotateRestore).not.toHaveBeenCalled()
  })

  it('compares older to newer when the selected version is newer than active', async () => {
    const detail = detailData({
      active_version: 1,
      versions: [
        promptVersion({ version: 1, is_active: true, status: 'ACTIVE', is_default: true }),
        promptVersion({
          version: 3,
          is_active: false,
          status: 'DRAFT',
          is_default: true,
          author: 'web',
          reason: 'restore default',
        }),
      ],
    })
    const { fetchDiff } = await renderHistory({
      detail,
      fetchDiff: vi.fn(async () => diffEnvelope(diffData(1, 3, DIFF_TEXT))),
    })

    await waitFor(() => expect(fetchDiff).toHaveBeenCalledWith('analysis', 1, 3, expect.anything()))
    const label = screen.getByTestId('prompt-diff-label')
    expect(label).toHaveTextContent('v1 active → v3')
    // The English marker is wrapped so screen readers switch language.
    expect(within(label).getByText('active')).toHaveAttribute('lang', 'en')
    expect(screen.getByTestId('prompt-diff-body')).toHaveTextContent('- old wording')
  })
})

// -- routes -----------------------------------------------------------------

describe('prompt routes', () => {
  it('resolves the list, editor, and history routes', () => {
    const list = resolveRoute('/settings/prompts')
    expect(list.isSettingsPrompts).toBe(true)
    expect(list.isSettings).toBe(false)
    expect(list.promptId).toBeNull()

    const detail = resolveRoute('/settings/prompts/analysis')
    expect(detail.isSettingsPromptDetail).toBe(true)
    expect(detail.isSettingsPrompts).toBe(false)
    expect(detail.promptId).toBe('analysis')

    const history = resolveRoute('/settings/prompts/analysis/history')
    expect(history.isSettingsPromptHistory).toBe(true)
    expect(history.isSettingsPromptDetail).toBe(false)
    expect(history.promptId).toBe('analysis')

    // A deeper or unrelated path is not a prompt route.
    expect(resolveRoute('/settings/prompts/a/b').isSettingsPromptDetail).toBe(false)
    expect(resolveRoute('/settings/prompts/a/b').isSettingsPromptHistory).toBe(false)
    expect(resolveRoute('/settings/prompts/a/b').isSettingsPrompts).toBe(false)
  })
})
