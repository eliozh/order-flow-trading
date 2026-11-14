import { useCallback, useEffect, useRef, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import { downloadJournalExport } from '../api/journal'

export type JournalExportFormat = 'csv' | 'json'

export interface JournalExportScope {
  period?: string
  symbol?: string
}

export interface JournalExportState {
  /** The format currently downloading, or null. */
  exporting: JournalExportFormat | null
  /** The last download failure, cleared when a new download starts. */
  exportError: string | null
  /** Fetch one export and trigger the browser download. */
  runExport: (format: JournalExportFormat, scope: JournalExportScope) => Promise<void>
}

/**
 * The shared export-download state for the journal list and detail routes.
 *
 * The API helper checks the response before saving anything, so a 401/422/500
 * envelope surfaces here as `exportError` instead of a bogus file. Duplicate
 * clicks are ignored and the in-flight request is aborted on unmount.
 */
export function useJournalExport(): JournalExportState {
  const [exporting, setExporting] = useState<JournalExportFormat | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const controller = useRef<AbortController | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      controller.current?.abort()
    }
  }, [])

  const runExport = useCallback(
    async (format: JournalExportFormat, scope: JournalExportScope): Promise<void> => {
      if (inFlight.current) {
        return
      }
      inFlight.current = true
      setExporting(format)
      setExportError(null)
      const next = new AbortController()
      controller.current = next
      try {
        await downloadJournalExport(
          { format, period: scope.period, symbol: scope.symbol },
          next.signal,
        )
      } catch (error: unknown) {
        if (next.signal.aborted || isAbortError(error)) {
          return
        }
        if (mounted.current) {
          setExportError(errorMessage(error))
        }
      } finally {
        inFlight.current = false
        if (mounted.current) {
          setExporting(null)
        }
      }
    },
    [],
  )

  return { exporting, exportError, runExport }
}
