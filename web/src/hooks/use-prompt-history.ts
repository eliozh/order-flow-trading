/**
 * F10b.2 history state: version list, on-demand unified diff, explicit
 * activation, and restore-as-a-new-version.
 *
 * Activation and restore are separate deliberate actions. Restore never
 * rewrites history: it asks the server for a new DRAFT version and the active
 * version stays unchanged until an explicit activation.
 */

import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import {
  activatePromptVersion,
  clearPromptActivationKey,
  clearPromptRestoreKey,
  fetchPromptDetail,
  fetchPromptDiff,
  PromptApiError,
  type PromptDetailData,
  type PromptDetailEnvelope,
  type PromptDiffData,
  type PromptDiffEnvelope,
  type PromptVersionEnvelope,
  promptActivationKey,
  promptRestoreKey,
  restorePromptDefault,
  rotatePromptActivationKey,
  rotatePromptRestoreKey,
} from '../api/prompts'
import { useSettingsReadonly } from '../components/settings-field'

export type PromptHistoryDialog =
  { kind: 'activate'; version: number } | { kind: 'restore'; activeVersion: number }

export interface PromptDiffLoad {
  state: 'idle' | 'loading' | 'ready' | 'error'
  data: PromptDiffData | null
  message?: string
}

export interface PromptHistoryLoad {
  state: 'loading' | 'ready' | 'error'
  detail: PromptDetailData | null
  message?: string
}

export interface UsePromptHistoryOptions {
  promptId: string
  fetchPromptDetailFn?: (promptId: string, signal?: AbortSignal) => Promise<PromptDetailEnvelope>
  fetchPromptDiffFn?: (
    promptId: string,
    fromVersion: number,
    toVersion: number,
    signal?: AbortSignal,
  ) => Promise<PromptDiffEnvelope>
  activatePromptVersionFn?: (
    promptId: string,
    version: number,
    idempotencyKey: string,
    options?: { actor?: string; reason?: string; signal?: AbortSignal },
  ) => Promise<PromptVersionEnvelope>
  restorePromptDefaultFn?: (
    promptId: string,
    idempotencyKey: string,
    options?: { author?: string; reason?: string; signal?: AbortSignal },
  ) => Promise<PromptVersionEnvelope>
  promptActivationKeyFn?: (promptId: string) => string
  rotatePromptActivationKeyFn?: (promptId: string) => string
  clearPromptActivationKeyFn?: (promptId: string) => void
  promptRestoreKeyFn?: (promptId: string) => string
  rotatePromptRestoreKeyFn?: (promptId: string) => string
  clearPromptRestoreKeyFn?: (promptId: string) => void
  isCompact?: boolean
}

export interface PromptHistoryState {
  readonly: boolean
  load: PromptHistoryLoad
  activeVersion: number | null
  selectedVersion: number | null
  diff: PromptDiffLoad
  dialog: PromptHistoryDialog | null
  saving: boolean
  statusMessage: string
  liveMessage: string
  errorText: string
  errorSummaryRef: RefObject<HTMLDivElement | null>
  headingRef: RefObject<HTMLHeadingElement | null>
  retry: () => void
  selectVersion: (version: number) => void
  requestActivate: (version: number) => void
  requestRestore: () => void
  cancelDialog: () => void
  confirmDialog: () => Promise<void>
}

export function usePromptHistory({
  promptId,
  fetchPromptDetailFn = fetchPromptDetail,
  fetchPromptDiffFn = fetchPromptDiff,
  activatePromptVersionFn = activatePromptVersion,
  restorePromptDefaultFn = restorePromptDefault,
  promptActivationKeyFn = promptActivationKey,
  rotatePromptActivationKeyFn = rotatePromptActivationKey,
  clearPromptActivationKeyFn = clearPromptActivationKey,
  promptRestoreKeyFn = promptRestoreKey,
  rotatePromptRestoreKeyFn = rotatePromptRestoreKey,
  clearPromptRestoreKeyFn = clearPromptRestoreKey,
  isCompact,
}: UsePromptHistoryOptions): PromptHistoryState {
  const readonly = useSettingsReadonly(isCompact)
  const [load, setLoad] = useState<PromptHistoryLoad>({ state: 'loading', detail: null })
  const [attempt, setAttempt] = useState(0)
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null)
  const [diff, setDiff] = useState<PromptDiffLoad>({ state: 'idle', data: null })
  const [dialog, setDialog] = useState<PromptHistoryDialog | null>(null)
  const [saving, setSaving] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [errorText, setErrorText] = useState('')
  const [focusTick, setFocusTick] = useState(0)
  const mounted = useRef(true)
  const inFlight = useRef(false)
  const errorSummaryRef = useRef<HTMLDivElement>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const settleFocusRef = useRef<'summary' | 'heading' | null>(null)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoad((previous) => ({ ...previous, state: 'loading' }))
    setDiff({ state: 'idle', data: null })
    fetchPromptDetailFn(promptId, controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        const data = envelope.data
        if (!data) {
          setLoad({ state: 'error', detail: null, message: 'Prompt 响应为空。' })
          return
        }
        setLoad({ state: 'ready', detail: data })
        const alternative = data.versions.find((version) => version.version !== data.active_version)
        setSelectedVersion(alternative?.version ?? null)
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setLoad({ state: 'error', detail: null, message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchPromptDetailFn, promptId, attempt])

  useEffect(() => {
    const detail = load.detail
    if (selectedVersion === null || detail === null) {
      setDiff({ state: 'idle', data: null })
      return
    }
    let active = true
    const controller = new AbortController()
    setDiff({ state: 'loading', data: null })
    // Compare older -> newer: a restored DRAFT can be newer than the active
    // version, and a reversed diff would mislabel the change direction.
    const fromVersion = Math.min(selectedVersion, detail.active_version)
    const toVersion = Math.max(selectedVersion, detail.active_version)
    fetchPromptDiffFn(promptId, fromVersion, toVersion, controller.signal)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (!envelope.data) {
          setDiff({ state: 'error', data: null, message: 'Diff 响应为空。' })
          return
        }
        setDiff({ state: 'ready', data: envelope.data })
      })
      .catch((error: unknown) => {
        if (!active || isAbortError(error)) {
          return
        }
        setDiff({ state: 'error', data: null, message: errorMessage(error) })
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [fetchPromptDiffFn, promptId, selectedVersion, load.detail])

  useEffect(() => {
    if (saving) {
      return
    }
    const target = settleFocusRef.current
    if (target === null) {
      return
    }
    settleFocusRef.current = null
    if (target === 'heading') {
      headingRef.current?.focus()
    } else {
      errorSummaryRef.current?.focus()
    }
  }, [saving, focusTick])

  const activeVersion = load.detail?.active_version ?? null

  function requestFocus(target: 'summary' | 'heading'): void {
    settleFocusRef.current = target
    setFocusTick((value) => value + 1)
  }

  function retry(): void {
    setAttempt((value) => value + 1)
  }

  function selectVersion(version: number): void {
    setSelectedVersion(version)
  }

  function requestActivate(version: number): void {
    if (readonly || inFlight.current) {
      return
    }
    setErrorText('')
    setDialog({ kind: 'activate', version })
    setLiveMessage(`激活 v${version} 需要确认；尚未写入任何变更。`)
  }

  function requestRestore(): void {
    if (readonly || inFlight.current || activeVersion === null) {
      return
    }
    setErrorText('')
    setDialog({ kind: 'restore', activeVersion })
    setLiveMessage(`恢复默认会创建新版本并替换 active v${activeVersion}；需要确认。`)
  }

  function cancelDialog(): void {
    setDialog(null)
    const message = '已取消；未写入任何变更。'
    setStatusMessage(message)
    setLiveMessage(message)
  }

  function handleMutationError(error: unknown, kind: 'activate' | 'restore'): void {
    if (!mounted.current || isAbortError(error)) {
      return
    }
    const message = errorMessage(error)
    setLiveMessage('')
    const clearKey = kind === 'activate' ? clearPromptActivationKeyFn : clearPromptRestoreKeyFn
    const rotateKey = kind === 'activate' ? rotatePromptActivationKeyFn : rotatePromptRestoreKeyFn
    // The action confirmed a write. A definitive refusal wrote nothing and its
    // key must not bind a later different request; an ambiguous outcome keeps
    // the pending key so a retry replays the original write.
    if (error instanceof PromptApiError && error.kind === 'refusal') {
      if (error.code === 'IDEMPOTENCY_CONFLICT') {
        rotateKey(promptId)
        setErrorText(
          `请求冲突；已生成新的 Idempotency-Key，可直接重试。${message ? ` ${message}` : ''}`,
        )
      } else {
        clearKey(promptId)
        setErrorText(message)
      }
    } else {
      setErrorText(`结果未知，重试会使用同一 Idempotency-Key 重放。${message ? ` ${message}` : ''}`)
    }
    setDialog(null)
    requestFocus('summary')
  }

  async function confirmDialog(): Promise<void> {
    if (dialog === null || inFlight.current || readonly) {
      return
    }
    inFlight.current = true
    setSaving(true)
    setErrorText('')
    setLiveMessage(dialog.kind === 'activate' ? '激活中…' : '恢复默认中…')
    try {
      if (dialog.kind === 'activate') {
        const envelope = await activatePromptVersionFn(
          promptId,
          dialog.version,
          promptActivationKeyFn(promptId),
          { actor: 'web' },
        )
        if (!envelope.data) {
          throw new Error('Prompt 响应为空。')
        }
        clearPromptActivationKeyFn(promptId)
        if (!mounted.current) {
          return
        }
        const message = `已激活 v${envelope.data.version}；下一次分析使用该版本；v${
          activeVersion ?? 0
        } 保留在历史中；audit 已写入。`
        setStatusMessage(message)
        setLiveMessage(message)
      } else {
        const replaced = dialog.activeVersion
        const envelope = await restorePromptDefaultFn(promptId, promptRestoreKeyFn(promptId), {
          author: 'web',
          reason: 'restore default',
        })
        if (!envelope.data) {
          throw new Error('Prompt 响应为空。')
        }
        clearPromptRestoreKeyFn(promptId)
        if (!mounted.current) {
          return
        }
        const created = envelope.data
        const message = `已创建 v${created.version}（${created.status}），将替换 active v${replaced}；历史保留；激活 v${created.version} 前 active 仍为 v${replaced}。`
        setStatusMessage(message)
        setLiveMessage(message)
      }
      setDialog(null)
      setSelectedVersion(null)
      setAttempt((value) => value + 1)
      // The refetch swaps the invoker button for the "当前" span; a heading
      // with tabIndex={-1} is the stable DESIGN A2.4 focus target.
      requestFocus('heading')
    } catch (error: unknown) {
      handleMutationError(error, dialog.kind)
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setSaving(false)
      }
    }
  }

  return {
    readonly,
    load,
    activeVersion,
    selectedVersion,
    diff,
    dialog,
    saving,
    statusMessage,
    liveMessage,
    errorText,
    errorSummaryRef,
    headingRef,
    retry,
    selectVersion,
    requestActivate,
    requestRestore,
    cancelDialog,
    confirmDialog,
  }
}
